from __future__ import annotations

import json
import os
import platform
import signal
import subprocess
import sys
import threading
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from functools import wraps
from importlib.metadata import entry_points
from time import perf_counter

from sphinx.application import Sphinx
from sphinx.util.logging import getLogger

logger = getLogger(__name__)

THEME_PACKAGES = {
    ep.module.split(".")[0] for ep in entry_points(group="sphinx.html_themes")
}

# Seconds the sampler sleeps between two stack samples (see :class:`StackSampler`);
# the default of the ``sphinx_benchmark_sampling_interval`` config value
DEFAULT_SAMPLING_INTERVAL = 0.001


@dataclass
class HandlerCall:
    """A single timed call of one event handler.

    Parameters
    ----------
    event : str
        Name of the Sphinx event this handler function was connected to
        (e.g. ``"doctree-resolved"``).
    handler : str
        Qualified name of the handler function (``__qualname__``), used
        to identify it in benchmark summary.
    module : str
        Full module path (separated by '.') of where the handler function is defined.
    kind : str
        Classification of where the handler is coming from. Could be:
        ``"extension"``, ``"sphinx-internal"``, ``"theme"``, ``"stdlib"`` or
        ``"unknown"``.
    extension : str or None
        Origin package of the handler: extension name for
        ``kind="extension"``, theme package for ``kind="theme"``,
        top-level module for ``kind="unknown"``, ``None`` otherwise.
    call : int
        Number of call for the specific ``(event, handler)`` pair
        (1 for the first time this handler ran for this event, 2 for the
        second call, and so on).
    start : float
        Time (in seconds) since the start of the build at which this handler gets called.
    duration : float
        Time (in seconds) this handler call took to execute.
    """

    event: str
    handler: str
    module: str
    kind: str
    extension: str | None
    call: int
    start: float
    duration: float


@dataclass
class Event:
    """Records the total time spent in a single event emission (including all its listeners and any gaps between them).

    Parameters
    ----------
    event_id : int
        Unique identifier of this emission, assigned in emission order
        from 0.
    event_name : str
        Name of the emitted event.
    call : int
        Number of the emission for this ``event_name``.
        (1 for the first time this event is emitted, 2 for the
        second emmission, and so on).
    start : float
        Time (in seconds) since the start of the build at which this emit began.
    depth : int
        Nesting level: 0 for a top-level emission(i.e. no nesting), 1 for
        emitted from inside another event's emission, and so on.
    duration : float or None
        Time (in seconds) this event's emit() call took, including nested emissions.
    parent_id : int or None
        :attr:`event_id` of the emission this one is nested inside, or
        ``None`` if not a nested event.
    own_time : float or None
        Time (in seconds) this event's emit() call took, excluding the
        `duration`s of nested event emissions.
    """

    event_id: int
    event_name: str
    call: int
    start: float
    depth: int
    duration: float | None = None
    parent_id: int | None = None
    own_time: float | None = None


class Recorder:
    """Records per-handler timing data for a single build.

    An :class:`Recorder` instance (i.e. :data:`recorder`) accumulates
    one :class:`HandlerCall` per wrapped-handler call and keeps all the
    records in one place.

    Attributes
    ----------
    calls : list of HandlerCall
        Every recorded handler call for the current build, in the order
        they were recorded. These records are later stored in a json.
    events : list of Event
        Every recorded event emission for the current build, in the order
        they began, so a parent precedes the emissions nested inside
        it. These records are later stored in a json.
    start_time : float or None
        The :func:`time.perf_counter` value captured when :meth:`start`
        was called, used as the zero point for relative timings.
        ``None`` before :meth:`start` has been called.
    start_ts : datetime or None
        The :class:`datetime` timestamp captured when :meth:`start`
        was called, later stored in json. ``None`` before :meth:`start`
        has been called.
    call_counts : collections.Counter
        Number of calls per ``(event, handler)`` pair; used to
        assign the ``call`` attribute of each :class:`HandlerCall`.
    event_call_counts : collections.Counter
        Number of calls per ``event_name``; used to assign the ``call``
        attribute of each :class:`Event`.
    total_wall_time : float
        Total wall-clock time of the build, as measured by
        ``perf_counter() - start_time``. This is not the same as the
        sum of all recorded handler durations, because there may be
        gaps between handler calls.
    """

    def __init__(self) -> None:
        self.calls: list[HandlerCall] = []
        self.events: list[Event] = []
        self.start_time: float | None = None
        self.start_ts: datetime | None = None
        self.call_counts: Counter = Counter()
        self.event_call_counts: Counter = Counter()
        self.total_wall_time: float = 0.0
        self._stack: list[
            tuple[Event, float]
        ] = []  # (Event record, perf_counter at emit start)
        self._next_event_id: int = 0

    def start(self) -> None:
        """Reset all recorded state and mark the start of a new build."""
        self.calls = []
        self.events = []
        self.call_counts = Counter()
        self.event_call_counts = Counter()
        self.start_time = perf_counter()
        self.start_ts = datetime.now(UTC)
        self._stack = []
        self._next_event_id = 0

    def record(
        self,
        event: str,
        handler_name: str,
        module: str,
        start_offset: float,
        duration: float,
    ) -> None:
        """Record one completed handler call.

        This also increments the internal per-``(event, handler_name)``
        counter in :attr:`call_counts` and appends a new
        :class:`HandlerCall` to :attr:`calls`.

        Parameters
        ----------
        event : str
            Name of the event the handler was connected to.
        handler_name : str
            Qualified name of the handler function.
        module : str
            Module path (separated by '.') where the handler is defined in.
        start_offset : float
            Seconds since :attr:`start_time` at which this call began.
        duration : float
            Seconds this handler call took to execute.
        """
        key = (event, handler_name)
        self.call_counts[key] += 1
        self.calls.append(
            HandlerCall(
                event=event,
                handler=handler_name,
                module=module,
                kind="unknown",  # will be filled in later by classify_all_handlers
                extension=None,  # will be filled in later by classify_all_handlers
                call=self.call_counts[key],
                start=start_offset,
                duration=duration,
            )
        )

    def enter_event(self, event_name):
        """Start timing an emission of ``event_name``, nested inside the
        innermost emission still in progress (if any)."""
        t0 = perf_counter()
        self.event_call_counts[event_name] += 1
        parent = self._stack[-1][0] if self._stack else None
        record = Event(
            event_id=self._next_event_id,
            event_name=event_name,
            call=self.event_call_counts[event_name],
            parent_id=parent.event_id if parent is not None else None,
            depth=len(self._stack),
            start=t0 - (self.start_time or t0),
        )
        self._next_event_id += 1
        self.events.append(record)
        self._stack.append((record, t0))

    def exit_event(self):
        """Finish timing the inner-most emission."""
        t1 = perf_counter()
        record, t0 = self._stack.pop()
        record.duration = t1 - t0

    def compute_own_times(self):
        """Computes and records the ``own_time`` for every event emission."""
        children_time = Counter()
        for e in self.events:
            if e.parent_id is not None and e.duration is not None:
                children_time[e.parent_id] += e.duration
        for e in self.events:
            if e.duration is not None:
                e.own_time = e.duration - children_time[e.event_id]


def classify_all_handlers(calls: list[HandlerCall], app: Sphinx) -> None:
    """Classify every recorded handler call in ``calls`` and set its ``kind`` and
    ``extension``.

    Sphinx adds an extension to `app.extensions` after its `setup()`
    returns, so classifying at the time of wrapping reports "extension"
    as `"unknown"`.
    """
    all_hc = {}
    for hc in calls:
        if hc.module not in all_hc:
            all_hc[hc.module] = classify_module(hc.module, app)
        hc.kind, hc.extension = all_hc[hc.module]


def write_json(
    calls: list[HandlerCall],
    events: list[Event],
    project_info,
    build_info,
    frames,
    filename: str = "sphinx_benchmarks.json",
) -> None:
    """Write all recorded handler calls to a JSON file. The default filename
    format is ``sphinx_benchmarks_YYYYMMDD-HHMMSS_[HEAD's last 7 char].json``.

    The json has a five top-level keys:

    ``"project_info"`` is a dict containing project name, version, copyright,
    and git HEAD commit hash (if found).

    ``"build_info"`` is a dict containing builder name, start time, and total wall time.

    ``"calls"`` is a list of the recorded :class:`HandlerCall` entries (as
    plain dicts, via :func:`dataclasses.asdict`), one per handler
    function call.

    ``"events"`` is a list of the recorded :class:`Event` entries (as
    plain dicts, via :func:`dataclasses.asdict`), one per event emission.

    ``"frames"`` is the stack of snapshots/samples of the whole docs build, see
    :meth:`StackSampler.records`.
    """
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(
            {
                "project_info": project_info,
                "build_info": build_info,
                "calls": [asdict(c) for c in calls],
                "events": [asdict(e) for e in events],
                "frames": frames,
            },
            f,
            indent=2,
        )


def classify_module(module: str, app: Sphinx) -> tuple[str, str | None]:
    """Return ``(kind, extension)`` for the code in ``module``.

    ``kind`` is ``"extension"``, ``"sphinx-internal"``, ``"theme"``, ``"stdlib"``,
    or ``"unknown"``; ``extension`` names the origin package (see
    :class:`HandlerCall`).
    """
    top = module.split(".")[0]
    if module.startswith("sphinx.ext."):
        # note, ``sphinx.ext.autodoc.typehints`` is reduced to ``sphinx.ext.autodoc``
        return "extension", ".".join(module.split(".")[:3])
    if module == "sphinx" or module.startswith("sphinx."):
        return "sphinx-internal", None
    # Checked before extensions: most themes also register a setup(), so they
    # appear in app.extensions and would otherwise be classified as extensions.
    if top in THEME_PACKAGES:
        return "theme", top
    for ext_name, ext in app.extensions.items():
        ext_top = ext.module.__name__.split(".")[0]
        if ext_top == top:
            return "extension", ext_name
    if top in sys.stdlib_module_names:
        return "stdlib", top
    return "unknown", top or None


class StackSampler(threading.Thread):
    """A background daemon thread that records the docs build thread's
    function call stack (via :func:`sys._current_frames`) every ``interval``
    seconds.

    Parameters
    ----------
    recorder : Recorder
        The `Recorder` instance of the docs build.
    interval : float
        Seconds to sleep between collecting two function stack samples.
    build_thread_id : int
        ``threading.get_ident()`` of the thread running the docs build.

    Attributes
    ----------
    functions : list of dict
        One entry per distinct function seen on a sampled stack, with its
        ``function`` name, ``module``, ``file`` and ``line`` number
    stacks : list of tuple
        Every distinct function call stack sampled, as a tuple of function indexes,
        innermost function first.
    snapshots : list of tuple
        One ``(start_time, stack index)`` entry per sample/snapshot.

    Notes
    -----

    Nothing in frames explicitly tells us if it was in an event or handler or gap.
    That gets figured out later by comparing the snapshot times to the start times
    and durations of the events and handler calls records.

    Overheads:
    - Each sample itself takes about a few µs, during which the build thread is paused.
    - GIL's switch interval (`sys.getswitchinterval()` --> 5 ms): when the sampler's
      sleep (1 ms by default) expires it has to re-acquire the GIL. If the build thread is
      running Python code, the sampler thread waits one switch interval (5 ms)
      and then requests the GIL, which the build thread hands over at its next
      bytecode boundary. So samples are never closer than about 1 ms, or could
      typically be sleep + one switch interval apart (~6 ms). But, if C code
      is running then that can hold the GIL for longer, which can add an overhead.
    """

    def __init__(self, recorder: Recorder, interval: float, build_thread_id: int):
        super().__init__(name="sphinx-benchmark-sampler", daemon=True)
        self.recorder = recorder
        self.interval = interval
        self.build_thread_id = build_thread_id
        self.functions: list[dict] = []
        self.stacks: list[tuple[int, ...]] = []
        self.snapshots: list[tuple[float, int]] = []
        self._function_index: dict = {}  # key: function's code object, value: index of the function
        self._stack_index: dict[
            tuple[int, ...], int
        ] = {}  # key: tuple of function indexes in a function call stack, value: index of the stack
        self._stop_event = threading.Event()  # shared flag (set to False) that this daemon thread checks every `self.interval` ms to decide whether to take another sample or exit (if the docs build thread is finished)

    def run(self):
        try:
            while not self._stop_event.wait(
                self.interval
            ):  # this loop takes 1 sample/snapshot
                frame = sys._current_frames().get(self.build_thread_id)
                if frame is None:
                    continue
                t = perf_counter() - (self.recorder.start_time or 0.0)
                stack = []
                while frame is not None:
                    code_obj = frame.f_code
                    idx = self._function_index.get(code_obj)
                    if idx is None:
                        idx = self._function_index[code_obj] = len(self.functions)
                        self.functions.append(
                            {
                                "function": code_obj.co_qualname,
                                # compiled Jinja templates have no __name__: store the file instead
                                "module": frame.f_globals.get("__name__")
                                or os.path.basename(code_obj.co_filename),
                                "file": code_obj.co_filename,
                                "line": code_obj.co_firstlineno,
                            }
                        )
                    stack.append(idx)
                    frame = frame.f_back  # collecting the full function call stack
                key = tuple(stack)
                stack_id = self._stack_index.get(key)
                if stack_id is None:
                    stack_id = self._stack_index[key] = len(self.stacks)
                    self.stacks.append(key)
                self.snapshots.append((t, stack_id))
        except Exception as e:
            logger.warning("Benchmarking stack sampler stopped: %s", e, exc_info=True)

    def stop(self):
        """Stop sampling and wait for the thread to finish."""
        self._stop_event.set()  # tells the daemon thread to stop sampling
        if self.is_alive():
            self.join()

    def records(self, app: Sphinx) -> dict:
        """Turn the snapshots into the ``"frames"`` value of the JSON."""
        functions = []
        for f in self.functions:
            kind, extension = classify_module(f["module"], app)
            functions.append({**f, "kind": kind, "extension": extension})
        return {
            "sampling_method": "default",
            "sampling_interval": self.interval,
            "samples": len(self.snapshots),
            "functions": functions,
            "stacks": [list(s) for s in self.stacks],
            "snapshots": [[round(t, 6), s] for t, s in self.snapshots],
        }


class TachyonSampler:
    """Samples the docs build with Python 3.15's ``profiling.sampling``
    (Tachyon), run in a subprocess attached to this process.

    Parameters
    ----------
    args : list of str
        Extra arguments for ``profiling.sampling attach`` (the
        ``tachyon_args`` config value), passed as they are.
    output : str
        Path of the binary profile Tachyon writes.
    """

    def __init__(self, args: list[str], output: str) -> None:
        self.args = list(args)
        self.output = os.path.abspath(output)
        self.process: subprocess.Popen | None = None

    def start(self) -> bool:
        """Start Tachyon; return False if it can't read this process's
        memory (e.g. without sudo on macOS)."""
        tachyon = [sys.executable, "-m", "profiling.sampling"]
        pid = str(os.getpid())
        # dump reads this process's stack once, so it fails the same way attach would
        dump = [*tachyon, "dump", pid]
        if subprocess.run(dump, stdout=subprocess.DEVNULL, check=False).returncode:
            return False
        self.process = subprocess.Popen(
            [*tachyon, "attach", "--binary", "-o", self.output, *self.args, pid]
        )
        return True

    def stop(self) -> None:
        """Stop sampling; Tachyon writes the binary profile on SIGINT."""
        self.process.send_signal(signal.SIGINT)
        self.process.wait()

    def records(self, app: Sphinx) -> dict:
        """The ``"frames"`` value of the JSON."""
        return {
            "sampling_method": "tachyon",
            "tachyon_args": self.args,
            "tachyon_binary": self.output,
        }


_README_TACHYON = (
    "https://github.com/Schefflera-Arboricola/sphinx-benchmark/blob/main/"
    "README.md#sampling-with-python-315-tachyon"
)

recorder = Recorder()
sampler: StackSampler | TachyonSampler | None = None

# set as an attribute on every wrapped handler to avoid wrapping an already wrapped handler
_WRAP_FLAG = "_event_profiler_wrapped"


def wrap_listener(event_name, listener):
    """Wrap a single event listener's handler by adding a
    ``perf_counter()`` at the start and the end of the handler
    function call.

    Parameters
    ----------
    event_name : str
        Name of the event this listener is registered for.
    listener : sphinx.events.EventListener
        The listener to wrap, as found in ``app.events.listeners``.

    Returns
    -------
    sphinx.events.EventListener
        A new listener with the same ``id`` and ``priority`` as the
        input, but with its ``handler`` replaced by a wrapped handler.
        If handler is already wrapped (see
        :data:`_WRAP_FLAG`), the original listener is returned
        as it is. Refer
        https://www.sphinx-doc.org/en/master/_modules/sphinx/events.html#EventManager

    Notes
    -----
    The returned wrapper calls the original handler, records its
    duration via :meth:`Recorder.record`, and re-raises any exception
    the original handler raised (timing is recorded in a ``finally`` block,
    so exceptions propagate normally and Sphinx's own error handling is unaffected).
    The wrapper's ``__name__``, ``__qualname__``, and ``__module__`` are
    copied from the original handler so that Sphinx's and extension's
    own error messages still point to the real handler rather than to the wrapper.
    """
    orig_handler = listener.handler
    if getattr(orig_handler, _WRAP_FLAG, False):
        return listener  # already wrapped, don't double-wrap

    handler_name = getattr(
        orig_handler,
        "__qualname__",
        getattr(orig_handler, "__name__", repr(orig_handler)),
    )
    module = getattr(orig_handler, "__module__", None)
    if module is None:
        try:
            file = getattr(orig_handler, "__globals__", {}).get("__file__", "")
            module = os.path.basename(file) or "unknown"  # file name, e.g. conf.py
        except Exception as e:
            logger.warning(
                "Could not determine module for handler %s: %s \nSetting `module='unknown'`.",
                handler_name,
                e,
                exc_info=True,
            )
            module = "unknown"

    @wraps(orig_handler)
    def wrapped(app_arg, *args, **kwargs):
        t0 = perf_counter()
        start_offset = t0 - (recorder.start_time or t0)
        try:
            return orig_handler(app_arg, *args, **kwargs)
        finally:
            duration = perf_counter() - t0
            recorder.record(
                event_name,
                handler_name,
                module,
                start_offset,
                duration,
            )

    setattr(wrapped, _WRAP_FLAG, True)
    if not hasattr(orig_handler, "__qualname__"):
        # for handlers that aren't simple functions (partials, callable objects, etc.)
        # have no qualname/name for @wraps to copy, so setting those explicitly
        wrapped.__name__ = handler_name
        wrapped.__qualname__ = handler_name

    return listener._replace(handler=wrapped)


def wrap_all_listeners(app: Sphinx, *_args) -> None:
    """Wrap every currently-registered event listener for timing.

    Parameters
    ----------
    app : sphinx.application.Sphinx
        The running Sphinx application whose ``app.events.listeners``
        registry should be wrapped in place.
    *_args
        Extra positional arguments Sphinx passes when this is connected
        directly as an event handler.

    Notes
    -----
    Iterates over every event name currently in
    ``app.events.listeners`` (including custom events added via
    ``app.add_event``). Safe to call more than once -- already-wrapped
    listeners are left unchanged.
    """
    for event_name in list(app.events.listeners.keys()):
        app.events.listeners[event_name] = [
            wrap_listener(event_name, listener)
            for listener in app.events.listeners[event_name]
        ]


def wrap_connect(app: Sphinx) -> None:
    """Wraps the ``app.events.connect`` so listeners get wrapped when registered."""
    original_connect = app.events.connect

    def wrapped(name, callback, *args, **kwargs):
        listener_id = original_connect(name, callback, *args, **kwargs)
        listeners = app.events.listeners[name]
        for i, listener in enumerate(listeners):
            if listener.id == listener_id:
                listeners[i] = wrap_listener(name, listener)
                break
        return listener_id

    app.events.connect = wrapped


def wrap_emit(app: Sphinx, *_args) -> None:
    """Wrap ``app.events.emit`` so the full cost of each event emission
    (all listeners plus any Sphinx-internal overhead between them) is
    recorded, not just the sum of the wrapped handlers' own durations.

    Parameters
    ----------
    app : sphinx.application.Sphinx
        The running Sphinx application whose ``app.events.emit`` should
        be wrapped in place.
    *_args
        Extra positional arguments Sphinx passes when this is connected
        directly as an event handler.
    """
    original_emit = app.events.emit

    def wrapped(event_name, *args, **kwargs):
        recorder.enter_event(event_name)
        try:
            return original_emit(event_name, *args, **kwargs)
        finally:
            recorder.exit_event()

    app.events.emit = wrapped


# (function, module) of the wrapper frames; summary.py uses them to find
# where a handler's or an event emission's call tree starts in a sampled stack
LISTENER_WRAPPER = (f"{wrap_listener.__qualname__}.<locals>.wrapped", __name__)
EMIT_WRAPPER = (f"{wrap_emit.__qualname__}.<locals>.wrapped", __name__)


def build_finished(app: Sphinx, exception) -> None:
    """Write the collected benchmarks and print the summary at the end of the build.

    This runs inside the ``build-finished`` event emission, so that emission is
    still in progress while this handler runs and is recorded with ``duration=None``.

    Parameters
    ----------
    app : sphinx.application.Sphinx
        The running Sphinx application.
    exception : Exception or None
        The exception that terminated the build, if any, or ``None``
        for a successful build. Unused, but received because
        Sphinx always passes it to ``build-finished`` handlers.
    """
    try:
        if sampler is not None:  # None when the GIL is disabled, see setup()
            sampler.stop()
        recorder.total_wall_time = perf_counter() - (
            recorder.start_time or perf_counter()
        )
        recorder.compute_own_times()
        classify_all_handlers(recorder.calls, app)

        cfg = app.config
        project_info = {
            "name": cfg.project,
            "version": cfg.version,
            "copyright": cfg.copyright,
        }
        build_info = {
            "builder": app.builder.name,
            "start_time": recorder.start_ts.strftime("%Y-%m-%d %H:%M:%S %Z"),
            "total_wall_time": recorder.total_wall_time,
            "python_version": platform.python_version(),
        }
        try:
            out = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=app.confdir,
                capture_output=True,
                text=True,
                check=True,
            )
            project_info["HEAD"] = out.stdout.strip()
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            # CalledProcessError : it's not a git repo; FileNotFoundError : git is not installed
            logger.info("no git HEAD found: %s", e)
            project_info["HEAD"] = None

        filename = "sphinx_benchmarks_" + recorder.start_ts.strftime("%Y%m%d-%H%M%S")
        if project_info["HEAD"]:
            filename += "_" + project_info["HEAD"][:7]
        filename += ".json"
        frames = sampler.records(app) if sampler is not None else None
        write_json(
            recorder.calls,
            recorder.events,
            project_info=project_info,
            build_info=build_info,
            filename=filename,
            frames=frames,
        )
        print(f"{filename} written to {os.path.abspath(filename)}")
    except Exception as e:
        logger.warning("Benchmarking extension failed: %s", e, exc_info=True)


def setup(app: Sphinx):
    """Sphinx extension entry point."""
    global sampler
    try:
        app.add_config_value(
            "sphinx_benchmark_sampling_interval",
            DEFAULT_SAMPLING_INTERVAL,
            "",
            types=frozenset({float, int}),
        )
        app.add_config_value("disable_sampling", False, "", types=frozenset({bool}))
        app.add_config_value("tachyon_args", [], "", types=frozenset({list}))
        recorder.start()
        wrap_emit(app)
        wrap_all_listeners(app)
        wrap_connect(app)

        sampler = None
        if not app.config.disable_sampling:
            # Tachyon is stopped with SIGINT, which Windows can't send to a process
            if sys.version_info >= (3, 15) and sys.platform != "win32":
                stamp = recorder.start_ts.strftime("%Y%m%d-%H%M%S")
                sampler = TachyonSampler(
                    app.config.tachyon_args, f"tachyon_{stamp}.bin"
                )
                if not sampler.start():
                    sampler = None
                    logger.warning(
                        "\n%s\nTACHYON COULD NOT SAMPLE THE BUILD (see its error above),"
                        "\nfalling back to the default sampling: no flamegraph or"
                        "\nheatmap. See the README for the permissions Tachyon needs:"
                        "\n%s\n%s",
                        "=" * 70,
                        _README_TACHYON,
                        "=" * 70,
                    )
            if sampler is None and getattr(sys, "_is_gil_enabled", lambda: True)():
                sampler = StackSampler(
                    recorder,
                    app.config.sphinx_benchmark_sampling_interval,
                    threading.get_ident(),
                )
                sampler.start()
            elif sampler is None:
                logger.warning(
                    "the GIL is disabled, so the build is not sampled (no call trees); "
                    "run with PYTHON_GIL=1 to enable sampling"
                )

        method = "tachyon" if isinstance(sampler, TachyonSampler) else "default"
        logger.info(
            "sphinx-benchmark sampling: %s", method if sampler else "off", color="green"
        )

        # the priority is set to 999  so that if any other handlers are connected
        # with the build-finished event, then those get executed first and stored in the json.
        app.connect("build-finished", build_finished, priority=999)

        # builder.cleanup() happens after build-finished
        # maybe should wrap `app.build`?
    except Exception as e:
        logger.warning("Benchmarking extension failed: %s", e, exc_info=True)

    return {
        "version": "0.1",
        "parallel_read_safe": False,
        "parallel_write_safe": False,
    }
