"""Aggregate the raw JSON records into a :class:`BuildSummary`.

This module does all the maths and none of the printing. ``table.py``
and ``html.py`` both render the same :class:`BuildSummary`, so the two
outputs can never disagree about the numbers.
"""

from __future__ import annotations

import json
import math
from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from itertools import pairwise


class BenchmarkFileError(Exception):
    """Raised when the benchmarks JSON cannot be read or parsed."""


def load_records(path: str) -> dict:
    """Read the benchmarks JSON and return it as a dictionary."""
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        raise BenchmarkFileError(
            f"{path}: not found : run the build with the extension enabled "
            "or try changing the pwd to the docs directory"
        ) from None
    except json.JSONDecodeError as e:
        raise BenchmarkFileError(f"{path}: not valid JSON ({e})") from None


@dataclass(frozen=True)
class HandlerRow:
    """Aggregated timings of one handler on one event."""

    handler: str
    kind: str
    extension: str
    calls: int
    total: float
    avg: float


@dataclass(frozen=True)
class EventRow:
    """Aggregated timings of one event name over all its emissions."""

    name: str
    own_time: float
    duration: float
    emissions: int
    depth_min: int
    depth_max: int
    has_nested: bool
    handlers: tuple[HandlerRow, ...]
    handler_sum: float  # Sum of the handlers' ``total`` values (in seconds).
    overhead: float  # ``duration - handler_sum``: time inside emit() but outside any wrapped handler.


@dataclass(frozen=True)
class GapRow:
    """Cumulative gap between two consecutive top-level emissions.

    Parameters
    ----------
    source, target : str
        Names of the two events the gap falls between.
    total : float
        Summed gap time (in seconds) over all occurrences of this pair.
    count : int
        How many times this pair occurred consecutively.
    """

    source: str
    target: str
    total: float
    count: int

    @property
    def label(self) -> str:
        return f"{self.source} -> {self.target}"


@dataclass(frozen=True)
class OverviewRow:
    """One line of the overview: an event's own time or a gap.

    Parameters
    ----------
    label : str
        Event name, or a gap description such as ``"a -> b"``.
    kind : str
        ``"event"`` or ``"gap"``.
    seconds : float
        Own time of the event, or total gap time, in seconds.
    count : int or None
        Emissions of the event / occurrences of the gap; ``None`` for
        the startup and finish pseudo-gaps.
    """

    label: str
    kind: str
    seconds: float
    count: int | None


@dataclass(frozen=True)
class EmissionDetail:
    """One recorded emission of a single event, verbatim from the JSON.

    ``parent_name`` resolves ``parent_id`` to the parent emission's
    event name (``None`` for top-level emissions). ``duration`` and
    ``own_time`` are ``None`` for an emission still in progress when the
    JSON was written (e.g. ``build-finished``).
    """

    event_id: int
    call: int
    start: float
    depth: int
    duration: float | None
    own_time: float | None
    parent_name: str | None


@dataclass(frozen=True)
class CallDetail:
    """One recorded call of a single handler, verbatim from the JSON."""

    event: str
    handler: str
    module: str
    kind: str
    extension: str
    call: int
    start: float
    duration: float


@dataclass(frozen=True)
class GapOccurrence:
    """One individual gap between two consecutive top-level emissions.

    Parameters
    ----------
    source, target : str
        Event names on either side of the gap.
    source_call, target_call : int
        The ``call`` numbers of the two emissions involved.
    start : float
        Seconds since build start at which the gap began (source end).
    end : float
        Seconds since build start at which the gap ended (target start).
    """

    source: str
    target: str
    source_call: int
    target_call: int
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass(frozen=True)
class ProfileRow:
    """Sampled time of one function inside a gap (or all gaps).

    Parameters
    ----------
    function : str
        Qualified name of the function (``Class.method`` for methods).
    module, kind, extension : str
        Where the function is defined and where it comes from, classified
        like handlers (``extension``, ``sphinx-internal``, ``theme``,
        ``stdlib``, or ``unknown`` with the top-level module as ``extension``).
    file, line : str, int
        File and line where the function is defined.
    self_seconds : float
        Time spent in the function's own code. Time in the Python standard
        library is charged to the innermost frame that is not the standard
        library (so a ``Path.stat()`` call counts as self time of the Sphinx
        or docutils function that made it).
    total_seconds : float
        Time spent in the function and in everything it called (a function
        calling itself is counted once).
    """

    function: str
    module: str
    kind: str
    extension: str
    file: str
    line: int
    self_seconds: float
    total_seconds: float


@dataclass(frozen=True)
class ProfileNode:
    """One node of a sampled call tree: a function as called from its
    parent node, with the time of everything sampled under it.

    ``self_seconds`` is the time in which this node was the innermost
    sampled frame, ``total_seconds`` the time of the node and all its
    descendants, and ``children`` the functions it called, by
    ``total_seconds`` descending. ``where_seconds`` splits
    ``total_seconds`` by where the build was when the sample was taken
    (see :class:`Frames`); it is only filled in for the whole-build
    profile.
    """

    function: str
    module: str
    kind: str
    file: str
    line: int
    self_seconds: float
    total_seconds: float
    children: tuple[ProfileNode, ...]
    where_seconds: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Profile:
    """Where the sampled time of one part of the build goes.

    Parameters
    ----------
    label : str
        What the profile covers: ``"a -> b"`` for a gap (``"(startup) -> b"``
        and ``"a -> (finish)"`` for the time before the first and after the
        last top-level emission, ``"(all gaps, including startup)"`` for
        all of them), an event or handler name, or ``"(whole build)"``.
    scope : str
        What the percentages are a share of: ``"gap"``, ``"event"``,
        ``"handler"`` or ``"build"``.
    samples : int
        Number of stack samples the profile is built from.
    seconds : float
        Measured duration of what the profile covers, used to turn
        samples into seconds.
    rows : tuple of ProfileRow
        One row per function, sorted by ``self_seconds`` descending.
    tree : tuple of ProfileNode
        The sampled call tree (for the combined gaps profile: the gaps'
        trees merged, so reading, resolving and writing hang off the same
        root).
    """

    label: str
    scope: str
    samples: int
    seconds: float
    rows: tuple[ProfileRow, ...]
    tree: tuple[ProfileNode, ...]

    def pct(self, seconds: float) -> float:
        """Return ``seconds`` as a percentage of the profile's time."""
        return 100 * seconds / self.seconds if self.seconds else 0.0


@dataclass(frozen=True)
class Frames:
    """The stack snapshots of a build (the ``"frames"`` of the JSON),
    each located in time against the recorded emissions and handler calls.

    ``functions`` and ``stacks`` are the JSON's tables (stacks as tuples,
    innermost frame first). The other fields have one entry per snapshot,
    in time order:

    - ``times``, ``stack_ids``: seconds since the build started, index into
      ``stacks``.
    - ``where``: the innermost thing the build was in: ``"handler"`` (a
      handler call), ``"event"`` (an emission, but outside its handlers),
      ``"gap"`` (between two top-level emissions, or before the first or
      after the last), or ``"other"`` (none of those, which only happens
      when the timings overlap).
    - ``key``: what that was: ``(event, handler)`` for a handler, the
      event name for an event, ``(source, target)`` for a gap (``None`` on
      either side for the startup and the finish), ``None`` otherwise.
    - ``handlers``: every handler call still open at that time, innermost
      first, as ``(handler, module)`` pairs. A handler whose emission of a
      nested event is being sampled is still open.

    ``slot_seconds`` maps ``("handler", (event, handler))`` and
    ``("event", event)`` to the measured time in which those samples were
    the innermost thing running: the calls' or emissions' durations minus
    everything nested inside them. ``inf`` for an emission still in
    progress when the JSON was written.
    """

    functions: list[dict]
    stacks: list[tuple[int, ...]]
    times: list[float]
    stack_ids: list[int]
    where: list[str]
    key: list
    handlers: list[tuple[tuple[str, str], ...]]
    slot_seconds: dict[tuple[str, object], float]


@dataclass(frozen=True)
class BuildSummary:
    """Everything the table and HTML outputs need, in one place.

    Parameters
    ----------
    time_in_events : float
        Sum of all events' own times, in seconds.
    events : tuple of EventRow
        Events sorted by ``own_time`` descending.
    gaps : tuple of GapRow
        Gaps between top-level emissions, sorted by ``total`` descending.
    startup : float
        Time (in seconds) before the first top-level emission.
    finish : float
        Time (in seconds) after the last top-level emission ends.
    overlaps : int
        Number of negative gaps found. Top-level emissions can't
        overlap, so any value > 0 flags a bug in the recording.
    project_info : dict
        The ``project_info`` dict from the JSON (name, version,
        copyright, HEAD); empty if missing.
    build_info : dict
        The ``build_info`` dict from the JSON (builder, start_time,
        total_wall_time); empty if missing.
    """

    time_in_events: float
    events: tuple[EventRow, ...]
    gaps: tuple[GapRow, ...]
    startup: float
    finish: float
    overlaps: int
    project_info: dict = field(default_factory=dict)
    build_info: dict = field(default_factory=dict)

    @property
    def total_build_time(self) -> float:
        """Wall-clock build time in seconds: ``build_info.total_wall_time``,
        falling back to the sum of event own-times, or 1.0."""
        return self.build_info.get("total_wall_time") or self.time_in_events or 1.0

    @property
    def gaps_total(self) -> float:
        """Summed gap time between top-level emissions, in seconds."""
        return sum(g.total for g in self.gaps)

    @property
    def outside_events(self) -> float:
        """``startup + gaps + finish``: time outside any emission."""
        return self.startup + self.gaps_total + self.finish

    def pct(self, seconds: float) -> float:
        """Return ``seconds`` as a percentage of the build time."""
        return 100 * seconds / self.total_build_time


def compute_summary(data: dict) -> BuildSummary:
    """Aggregate the raw JSON ``data`` into a :class:`BuildSummary`."""
    calls = data.get("calls", [])
    events = data.get("events", [])

    # ---- per-(event, handler) aggregation -------------------------------
    handler_totals: dict[tuple[str, str], float] = defaultdict(float)
    call_counts: Counter = Counter()
    meta: dict[tuple[str, str], tuple[str, str, str | None]] = {}
    for c in calls:
        key = (c["event"], c["handler"])
        handler_totals[key] += c["duration"]
        call_counts[key] += 1
        meta[key] = (c["module"], c["kind"], c["extension"])

    # ---- per-event aggregation ------------------------------------------
    event_totals: dict[str, float] = defaultdict(float)
    own_totals: dict[str, float] = defaultdict(float)
    depths_by_event: dict[str, set[int]] = defaultdict(set)
    emissions_by_event: Counter = Counter()
    has_nested: set[str] = set()
    for e in events:
        if e["duration"] is None:
            continue
        event_totals[e["event_name"]] += e["duration"]
        depths_by_event[e["event_name"]].add(e["depth"])
        emissions_by_event[e["event_name"]] += 1
        if e["own_time"] is not None:
            own_totals[e["event_name"]] += e["own_time"]
            if e["own_time"] < e["duration"]:
                has_nested.add(e["event_name"])

    time_in_events = sum(own_totals.values())
    project_info = data.get("project_info") or {}
    build_info = data.get("build_info") or {}
    total_build_time = build_info.get("total_wall_time") or time_in_events or 1.0

    by_event: dict[str, list[HandlerRow]] = defaultdict(list)
    for (event, handler), total in handler_totals.items():
        n_calls = call_counts[(event, handler)]
        _module, kind, ext = meta[(event, handler)]
        avg = total / n_calls if n_calls else 0.0
        by_event[event].append(
            HandlerRow(handler, kind, ext or "-", n_calls, total, avg)
        )

    event_rows = []
    for name in sorted(own_totals, key=lambda e: own_totals[e], reverse=True):
        handlers = tuple(
            sorted(by_event.get(name, []), key=lambda r: r.total, reverse=True)
        )
        handler_sum = sum(r.total for r in handlers)
        depths = sorted(depths_by_event[name])
        event_rows.append(
            EventRow(
                name=name,
                own_time=own_totals[name],
                duration=event_totals[name],
                emissions=emissions_by_event[name],
                depth_min=depths[0],
                depth_max=depths[-1],
                has_nested=name in has_nested,
                handlers=handlers,
                handler_sum=handler_sum,
                overhead=event_totals[name] - handler_sum,
            )
        )

    top = sorted(
        (e["start"], e["duration"], e["event_name"])
        for e in events
        if e["depth"] == 0 and e["duration"] is not None
    )
    gap_acc: dict[tuple[str, str], tuple[float, int]] = {}
    overlaps = 0
    for (prev_start, prev_duration, prev_name), (start, _, name) in pairwise(top):
        gap = start - (prev_start + prev_duration)
        if gap < 0:
            overlaps += 1  # top-level emissions can't overlap; flags a bug
            continue
        total, count = gap_acc.get((prev_name, name), (0.0, 0))
        gap_acc[(prev_name, name)] = (total + gap, count + 1)

    gap_rows = tuple(
        sorted(
            (
                GapRow(src, dst, total, count)
                for (src, dst), (total, count) in gap_acc.items()
            ),
            key=lambda g: g.total,
            reverse=True,
        )
    )
    startup = top[0][0] if top else 0.0
    finish = total_build_time - (top[-1][0] + top[-1][1]) if top else 0.0

    return BuildSummary(
        time_in_events=time_in_events,
        events=tuple(event_rows),
        gaps=gap_rows,
        startup=startup,
        finish=finish,
        overlaps=overlaps,
        project_info=project_info,
        build_info=build_info,
    )


# --------------------------------------------------------------- details --


def event_names(data: dict) -> set[str]:
    """Return every event name that appears in the recorded emissions."""
    return {e["event_name"] for e in data.get("events", [])}


def handler_names(data: dict) -> set[str]:
    """Return every handler name that appears in the recorded calls."""
    return {c["handler"] for c in data.get("calls", [])}


def overview_rows(s: BuildSummary) -> tuple[OverviewRow, ...]:
    """Events (own time) and gaps interleaved, sorted by time descending."""
    rows = [OverviewRow(ev.name, "event", ev.own_time, ev.emissions) for ev in s.events]
    if s.startup > 0:
        rows.append(
            OverviewRow("(startup, before first emission)", "gap", s.startup, None)
        )
    if s.finish > 0:
        rows.append(OverviewRow("(finish, after last emission)", "gap", s.finish, None))
    rows += [OverviewRow(g.label, "gap", g.total, g.count) for g in s.gaps]
    rows.sort(key=lambda r: r.seconds, reverse=True)
    return tuple(rows)


def all_emission_details(data: dict) -> dict[str, tuple[EmissionDetail, ...]]:
    """Every recorded emission, grouped by event name, in emission order."""
    events = data.get("events", [])
    id_to_name = {e["event_id"]: e["event_name"] for e in events}
    grouped: dict[str, list[EmissionDetail]] = defaultdict(list)
    for e in events:
        grouped[e["event_name"]].append(
            EmissionDetail(
                event_id=e["event_id"],
                call=e["call"],
                start=e["start"],
                depth=e["depth"],
                duration=e["duration"],
                own_time=e["own_time"],
                parent_name=id_to_name.get(e["parent_id"]),
            )
        )
    return {name: tuple(rows) for name, rows in grouped.items()}


def emission_details(data: dict, event_name: str) -> tuple[EmissionDetail, ...]:
    """Every recorded emission of ``event_name``, in emission order."""
    return all_emission_details(data).get(event_name, ())


def all_handler_call_details(data: dict) -> dict[str, tuple[CallDetail, ...]]:
    """Every recorded call, grouped by handler name, in chronological order."""
    grouped: dict[str, list[CallDetail]] = defaultdict(list)
    for c in data.get("calls", []):
        grouped[c["handler"]].append(
            CallDetail(
                event=c["event"],
                handler=c["handler"],
                module=c["module"],
                kind=c["kind"],
                extension=c["extension"] or "-",
                call=c["call"],
                start=c["start"],
                duration=c["duration"],
            )
        )
    for rows in grouped.values():
        rows.sort(key=lambda r: r.start)
    return {name: tuple(rows) for name, rows in grouped.items()}


def handler_call_details(
    data: dict, handler_name: str, event_name: str | None = None
) -> tuple[CallDetail, ...]:
    """Every recorded call of ``handler_name``, in chronological order.

    If ``event_name`` is given, only calls made during that event's
    emissions are returned.
    """
    rows = all_handler_call_details(data).get(handler_name, ())
    if event_name is not None:
        rows = tuple(r for r in rows if r.event == event_name)
    return rows


def all_gap_occurrence_details(
    data: dict,
) -> dict[tuple[str, str], tuple[GapOccurrence, ...]]:
    """Every individual gap, grouped by (source, target) event pair, in
    chronological order.

    Negative gaps (overlapping emissions, which flag a recording bug)
    are skipped, matching :func:`compute_summary`.
    """
    top = sorted(
        (e["start"], e["duration"], e["event_name"], e["call"])
        for e in data.get("events", [])
        if e["depth"] == 0 and e["duration"] is not None
    )
    grouped: dict[tuple[str, str], list[GapOccurrence]] = defaultdict(list)
    for (p_start, p_dur, p_name, p_call), (start, _, name, call) in pairwise(top):
        gap_start = p_start + p_dur
        if start < gap_start:
            continue
        grouped[(p_name, name)].append(
            GapOccurrence(p_name, name, p_call, call, gap_start, start)
        )
    return {pair: tuple(rows) for pair, rows in grouped.items()}


def gap_occurrence_details(
    data: dict, source: str, target: str
) -> tuple[GapOccurrence, ...]:
    """Every individual gap between consecutive top-level emissions of
    ``source`` and ``target``, in chronological order.
    """
    return all_gap_occurrence_details(data).get((source, target), ())


# --------------------------------------------------------------- profile --

#: The wrappers the extension puts around every handler and around
#: ``EventManager.emit`` (function qualname, module), used to cut a sampled
#: stack down to the part inside a handler call or an emission.
_LISTENER_WRAPPER = ("wrap_listener.<locals>.wrapped", "sphinx_benchmark.extension")
_EMIT_WRAPPER = ("wrap_emit.<locals>.wrapped", "sphinx_benchmark.extension")


def load_frames(data: dict) -> Frames | None:
    """Read the ``"frames"`` of the JSON and locate every snapshot in time
    (see :class:`Frames`); ``None`` if the build was not sampled.
    """
    frames = data.get("frames") or {}
    snapshots = frames.get("snapshots") or []
    if not snapshots:
        return None
    functions = frames.get("functions", [])
    stacks = [tuple(s) for s in frames.get("stacks", [])]

    # every emission and handler call as (start, end, kind, key); an
    # emission still in progress when the JSON was written never ends
    intervals: list[tuple[float, float, str, object]] = []
    for e in data.get("events", []):
        end = e["start"] + e["duration"] if e["duration"] is not None else math.inf
        intervals.append((e["start"], end, "event", e["event_name"]))
    for c in data.get("calls", []):
        intervals.append(
            (
                c["start"],
                c["start"] + c["duration"],
                "handler",
                (c["event"], c["handler"], c["module"]),
            )
        )
    intervals.sort(key=lambda iv: (iv[0], -iv[1]))  # outer before inner
    # each interval's own time: its duration minus the intervals directly inside it
    own = [end - start for start, end, _, _ in intervals]
    nesting: list[int] = []
    for i, (start, end, _, _) in enumerate(intervals):
        while nesting and intervals[nesting[-1]][1] <= start:
            nesting.pop()
        if nesting and math.isfinite(end):
            own[nesting[-1]] -= end - start
        nesting.append(i)
    slot_seconds: dict[tuple[str, object], float] = defaultdict(float)
    for (_, _, kind, k), secs in zip(intervals, own):
        slot = (kind, (k[0], k[1]) if kind == "handler" else k)
        slot_seconds[slot] += max(secs, 0.0)
    top = sorted(
        (e["start"], e["start"] + e["duration"], e["event_name"])
        for e in data.get("events", [])
        if e["depth"] == 0 and e["duration"] is not None
    )
    top_starts = [t[0] for t in top]

    times, stack_ids = [], []
    where: list[str] = []
    key: list = []
    handlers: list[tuple[tuple[str, str], ...]] = []
    open_: list[tuple[float, float, str, object]] = []  # outermost first
    i = 0
    for t, stack_id in sorted(snapshots):
        times.append(t)
        stack_ids.append(stack_id)
        while i < len(intervals) and intervals[i][0] <= t:
            open_.append(intervals[i])
            i += 1
        open_ = [iv for iv in open_ if iv[1] > t]
        if open_:
            kind, k = open_[-1][2], open_[-1][3]
            if kind == "handler":
                where.append("handler")
                key.append((k[0], k[1]))
            else:
                where.append("event")
                key.append(k)
            handlers.append(
                tuple(
                    (iv[3][1], iv[3][2]) for iv in reversed(open_) if iv[2] == "handler"
                )
            )
            continue
        handlers.append(())
        g = bisect_right(top_starts, t) - 1
        if g < 0:
            where.append("gap")
            key.append((None, top[0][2] if top else None))
        elif t < top[g][1]:
            where.append("other")  # inside a top-level emission, yet nothing open
            key.append(None)
        else:
            where.append("gap")
            key.append((top[g][2], top[g + 1][2] if g + 1 < len(top) else None))
    return Frames(
        functions, stacks, times, stack_ids, where, key, handlers, dict(slot_seconds)
    )


def _is_wrapper(functions: list[dict], idx: int, wrapper: tuple[str, str]) -> bool:
    f = functions[idx]
    return f["function"] == wrapper[0] and f["module"] == wrapper[1]


def _under_handler(
    stack: tuple[int, ...], functions: list[dict], handler: str, module: str
) -> tuple[int, ...]:
    """The part of ``stack`` inside the call of ``handler``: everything
    called from the wrapper the extension put around it. The whole stack
    if that wrapper is not on it.
    """
    for i in range(1, len(stack)):
        if _is_wrapper(functions, stack[i], _LISTENER_WRAPPER):
            f = functions[stack[i - 1]]
            if f["function"] == handler and f["module"] == module:
                return stack[:i]
    return stack


def _under_emit(stack: tuple[int, ...], functions: list[dict]) -> tuple[int, ...]:
    """The part of ``stack`` inside the innermost event emission (from
    ``EventManager.emit`` down). The whole stack if no emission is on it.
    """
    for i in range(1, len(stack)):
        if _is_wrapper(functions, stack[i], _EMIT_WRAPPER):
            return stack[:i]
    return stack


def _profile(
    label: str,
    scope: str,
    counts: dict[tuple[int, ...], float],
    functions: list[dict],
    seconds: float,
    where_counts: dict[tuple[int, ...], Counter] | None = None,
    samples: int | None = None,
) -> Profile:
    """Aggregate sampled stacks into a :class:`Profile`.

    ``counts`` maps a stack (innermost frame first) to how often it was
    sampled; ``where_counts`` (optional) maps a stack to a ``Counter`` of
    where the build was for those samples, for :attr:`ProfileNode.where_seconds`.
    The stacks are first turned into a tree of ``[frame, parent, self]``
    nodes (``parent`` is ``-1`` for a root; a parent always precedes its
    children), then sample counts become seconds with ``seconds / samples``.
    If ``samples`` is given, ``counts`` (and ``where_counts``) are already
    in seconds and are used as they are.
    """
    nodes: list[list[int]] = []
    index: dict[tuple[int, int], int] = {}  # (parent, frame) -> node
    node_where: list[Counter] = []
    for stack, count in counts.items():
        parent = -1
        for frame in reversed(stack):  # outermost first
            node = index.get((parent, frame))
            if node is None:
                node = index[(parent, frame)] = len(nodes)
                nodes.append([frame, parent, 0])
                node_where.append(Counter())
            parent = node
        nodes[parent][2] += count
        if where_counts is not None:
            node_where[parent].update(where_counts[stack])
    if samples is None:
        samples = sum(counts.values())
        scale = seconds / samples if samples else 0.0
    else:
        scale = 1.0

    totals = [n[2] for n in nodes]
    children: dict[int, list[int]] = defaultdict(list)
    self_by_frame: Counter = Counter()
    for i in range(len(nodes) - 1, -1, -1):
        frame, parent, own = nodes[i]
        if parent >= 0:
            totals[parent] += totals[i]
            node_where[parent].update(node_where[i])
            children[parent].append(i)
        if own:
            # charge standard-library time to the nearest non-stdlib caller
            charged = i
            while (
                functions[nodes[charged][0]]["kind"] == "stdlib"
                and nodes[charged][1] >= 0
            ):
                charged = nodes[charged][1]
            self_by_frame[nodes[charged][0]] += own

    # total per frame, counting a frame once per stack even if it recurses
    total_by_frame: Counter = Counter()
    on_path: Counter = Counter()
    stack = [(i, False) for i, n in enumerate(nodes) if n[1] < 0]
    while stack:
        i, leaving = stack.pop()
        frame = nodes[i][0]
        if leaving:
            on_path[frame] -= 1
            continue
        if on_path[frame] == 0:
            total_by_frame[frame] += totals[i]
        on_path[frame] += 1
        stack.append((i, True))
        stack.extend((c, False) for c in children[i])

    rows = tuple(
        sorted(
            (
                _row(
                    functions[idx],
                    self_by_frame[idx] * scale,
                    total_by_frame[idx] * scale,
                )
                for idx in total_by_frame
            ),
            key=lambda r: (r.self_seconds, r.total_seconds),
            reverse=True,
        )
    )

    def node(i: int) -> ProfileNode:
        f = functions[nodes[i][0]]
        kids = sorted(
            (node(c) for c in children[i]), key=lambda n: n.total_seconds, reverse=True
        )
        return ProfileNode(
            f["function"],
            f["module"],
            f["kind"],
            f["file"],
            f["line"],
            nodes[i][2] * scale,
            totals[i] * scale,
            tuple(kids),
            {w: c * scale for w, c in node_where[i].items()},
        )

    tree = tuple(
        sorted(
            (node(i) for i, n in enumerate(nodes) if n[1] < 0),
            key=lambda n: n.total_seconds,
            reverse=True,
        )
    )
    return Profile(label, scope, samples, seconds, rows, tree)


def _row(f: dict, self_seconds: float, total_seconds: float) -> ProfileRow:
    return ProfileRow(
        f["function"],
        f["module"],
        f["kind"],
        f["extension"] or "-",
        f["file"],
        f["line"],
        self_seconds,
        total_seconds,
    )


def gap_label(source: str | None, target: str | None) -> str:
    """The label of a gap's profile (see :class:`Profile`)."""
    if source is None and target is None:
        return "(all gaps, including startup)"
    if source is None:
        return f"(startup) -> {target}"
    if target is None:
        return f"{source} -> (finish)"
    return f"{source} -> {target}"


def gap_profiles(
    fr: Frames | None, s: BuildSummary, data: dict
) -> dict[tuple[str | None, str | None], Profile]:
    """Sampled profile of every gap, keyed by ``(source, target)`` event
    names (``source`` is ``None`` for the startup, ``target`` for the
    finish); empty if the build was not sampled. Gaps that got no samples
    are left out.
    """
    if fr is None:
        return {}
    counts: dict[tuple, Counter] = defaultdict(Counter)
    for j, w in enumerate(fr.where):
        if w == "gap":
            counts[fr.key[j]][fr.stacks[fr.stack_ids[j]]] += 1
    seconds_of = _gap_seconds(s, data)
    return {
        key: _profile(gap_label(*key), "gap", c, fr.functions, seconds_of.get(key, 0.0))
        for key, c in counts.items()
    }


def _gap_seconds(s: BuildSummary, data: dict) -> dict[tuple, float]:
    """Measured seconds of every gap, keyed like :func:`gap_profiles`."""
    seconds_of: dict[tuple, float] = {(g.source, g.target): g.total for g in s.gaps}
    top = sorted(
        (e["start"], e["start"] + e["duration"], e["event_name"])
        for e in data.get("events", [])
        if e["depth"] == 0 and e["duration"] is not None
    )
    if top:
        seconds_of[(None, top[0][2])] = s.startup
        # the finish gap ends where the emission in progress at the end
        # (build-finished) began, or at the end of the build
        finish_end = min(
            (
                e["start"]
                for e in data.get("events", [])
                if e["depth"] == 0 and e["duration"] is None
            ),
            default=s.total_build_time,
        )
        seconds_of[(top[-1][2], None)] = max(finish_end - top[-1][1], 0.0)
    return seconds_of


def all_gap_profiles(
    data: dict, s: BuildSummary
) -> dict[tuple[str | None, str | None], Profile]:
    """:func:`gap_profiles` straight from the JSON ``data``."""
    return gap_profiles(load_frames(data), s, data)


def gap_profile(
    data: dict, s: BuildSummary, source: str, target: str
) -> Profile | None:
    """Sampled profile of the gap between ``source`` and ``target``."""
    return all_gap_profiles(data, s).get((source, target))


def _merge_trees(trees: list[tuple[ProfileNode, ...]]) -> tuple[ProfileNode, ...]:
    """Merge call trees: nodes with the same function under the same parent
    become one node, with their times added up."""
    groups: dict = defaultdict(list)
    for tree in trees:
        for n in tree:
            groups[(n.module, n.function, n.file, n.line)].append(n)
    merged = []
    for same in groups.values():
        where: Counter = Counter()
        for n in same:
            where.update(n.where_seconds)
        merged.append(
            ProfileNode(
                same[0].function,
                same[0].module,
                same[0].kind,
                same[0].file,
                same[0].line,
                sum(n.self_seconds for n in same),
                sum(n.total_seconds for n in same),
                _merge_trees([n.children for n in same]),
                dict(where),
            )
        )
    return tuple(sorted(merged, key=lambda n: n.total_seconds, reverse=True))


def combined_gap_profile(
    profiles: dict[tuple[str | None, str | None], Profile],
) -> Profile | None:
    """One profile over every gap in ``profiles`` (from
    :func:`all_gap_profiles`): per function, the seconds of each gap added
    up, and the gaps' call trees merged. ``None`` if there are no profiles.
    """
    if not profiles:
        return None
    self_s: dict = defaultdict(float)
    total_s: dict = defaultdict(float)
    by_key: dict = {}
    for p in profiles.values():
        for r in p.rows:
            key = (r.module, r.function, r.file, r.line)
            by_key[key] = r
            self_s[key] += r.self_seconds
            total_s[key] += r.total_seconds
    rows = tuple(
        sorted(
            (
                ProfileRow(
                    r.function,
                    r.module,
                    r.kind,
                    r.extension,
                    r.file,
                    r.line,
                    self_s[key],
                    total_s[key],
                )
                for key, r in by_key.items()
            ),
            key=lambda r: (r.self_seconds, r.total_seconds),
            reverse=True,
        )
    )
    return Profile(
        gap_label(None, None),
        "gap",
        sum(p.samples for p in profiles.values()),
        sum(p.seconds for p in profiles.values()),
        rows,
        _merge_trees([p.tree for p in profiles.values()]),
    )


def event_profiles(fr: Frames | None, s: BuildSummary) -> dict[str, Profile]:
    """Sampled profile of every event, keyed by event name: the samples
    taken during its emissions but outside any nested emission (so they
    match the event's own time), with the stacks cut down to the part
    inside ``EventManager.emit``. Events with no samples are left out.
    """
    if fr is None:
        return {}
    counts: dict[str, Counter] = defaultdict(Counter)
    cut: dict[int, tuple[int, ...]] = {}
    for j, w in enumerate(fr.where):
        if w == "handler":
            name = fr.key[j][0]
        elif w == "event":
            name = fr.key[j]
        else:
            continue
        stack_id = fr.stack_ids[j]
        stack = cut.get(stack_id)
        if stack is None:
            stack = cut[stack_id] = _under_emit(fr.stacks[stack_id], fr.functions)
        counts[name][stack] += 1
    # an emission still in progress at the end (build-finished) has no measured
    # time, so it is not in ``s.events`` and gets no profile, like in the tables
    own_time = {ev.name: ev.own_time for ev in s.events}
    return {
        name: _profile(name, "event", c, fr.functions, own_time[name])
        for name, c in counts.items()
        if name in own_time
    }


def handler_profiles(fr: Frames | None, s: BuildSummary) -> dict[str, Profile]:
    """Sampled profile of every handler, keyed by handler name (over all
    the events it is connected to): every sample taken while one of its
    calls was open, nested emissions included (so they match the handler's
    measured duration), with the stacks cut down to the part inside the
    handler. Handlers with no samples are left out.
    """
    if fr is None:
        return {}
    counts: dict[str, Counter] = defaultdict(Counter)
    cut: dict[tuple[int, str, str], tuple[int, ...]] = {}
    for j, open_handlers in enumerate(fr.handlers):
        stack_id = fr.stack_ids[j]
        for handler, module in open_handlers:
            stack = cut.get((stack_id, handler, module))
            if stack is None:
                stack = cut[(stack_id, handler, module)] = _under_handler(
                    fr.stacks[stack_id], fr.functions, handler, module
                )
            counts[handler][stack] += 1
    seconds: dict[str, float] = defaultdict(float)
    for ev in s.events:
        for r in ev.handlers:
            seconds[r.handler] += r.total
    return {
        name: _profile(name, "handler", c, fr.functions, seconds[name])
        for name, c in counts.items()
        if name in seconds  # not measured when only called by build-finished
    }


def build_profile(fr: Frames | None, s: BuildSummary, data: dict) -> Profile | None:
    """Sampled profile of the whole build: every snapshot, full stacks, with
    each node's time split by where the build was
    (:attr:`ProfileNode.where_seconds`). ``None`` if the build was not
    sampled.

    The sampler only gets to run when the build thread lets go of the GIL,
    so phases that do I/O or call into C get more samples per second than
    pure-Python phases. To keep the tree consistent with the measured
    timings, each sample is worth the measured time of the handler, event
    or gap it fell in divided by that slot's number of samples, rather than
    one global rate. Samples in a slot without a measurement (an emission
    still in progress, overlapping timings) get the global rate.
    """
    if fr is None:
        return None
    slots = list(zip(fr.where, fr.key))
    slot_samples = Counter(slots)
    gap_seconds = _gap_seconds(s, data)
    global_rate = s.total_build_time / len(slots)
    rate: dict[tuple, float] = {}
    for slot, n in slot_samples.items():
        where, key = slot
        secs = gap_seconds.get(key) if where == "gap" else fr.slot_seconds.get(slot)
        rate[slot] = (
            secs / n if secs is not None and math.isfinite(secs) else global_rate
        )
    counts: dict[tuple[int, ...], float] = defaultdict(float)
    where_counts: dict[tuple[int, ...], Counter] = defaultdict(Counter)
    for j, stack_id in enumerate(fr.stack_ids):
        stack = fr.stacks[stack_id]
        counts[stack] += rate[slots[j]]
        where_counts[stack][fr.where[j]] += rate[slots[j]]
    return _profile(
        "(whole build)",
        "build",
        counts,
        fr.functions,
        s.total_build_time,
        where_counts,
        samples=len(slots),
    )
