import json
import re
import sys
import threading
import time
import types

import pytest
from sphinx.application import Sphinx
from sphinx.events import EventListener
from sphinx.extension import Extension

import sphinx_benchmark.extension as bs
from sphinx_benchmark.extension import (
    _WRAP_FLAG,
    EventLogger,
    recorder,
    wrap_all_listeners,
    wrap_connect,
    wrap_emit,
    wrap_listener,
)

SLEEP = 0.5


# dummy stuff and fixtures


class DummyEventManager:
    def __init__(self, app):
        self.app = app
        self.listeners: dict[str, list[EventListener]] = {}
        self._next_id = 0

    def connect(self, name, callback, priority=500):
        listener = EventListener(self._next_id, callback, priority)
        self._next_id += 1
        self.listeners.setdefault(name, []).append(listener)
        return listener.id

    def emit(self, name, *args, **kwargs):
        return [
            listener.handler(self.app, *args, **kwargs)
            for listener in self.listeners.get(name, [])
        ]


class DummyApp:
    def __init__(self):
        self.events = DummyEventManager(self)
        self.extensions: dict[str, Extension] = {}

    def add_extension(self, name: str) -> None:
        self.extensions[name] = Extension(name, types.ModuleType(name))

    def connect(self, name, callback, priority=500):
        return self.events.connect(name, callback, priority)


@pytest.fixture
def app():
    return DummyApp()


@pytest.fixture(autouse=True)
def fresh_recorder():
    """The wrappers write to the module-level ``recorder``, so reset it before every test."""
    recorder.start()


@pytest.fixture
def log():
    """A standalone EventLogger, for tests that don't go through the wrappers."""
    logger = EventLogger()
    logger.start()
    return logger


def dummy_handler(app, *args, **kwargs):
    """A handler that takes a measurable amount of time."""
    time.sleep(SLEEP)


# wrapping a listener should be invisible to Sphinx and to the handler


def test_wrapped_listener():
    original = EventListener(7, dummy_handler, 42)
    wrapped = wrap_listener("build-finished", original)

    assert wrapped.id == original.id
    assert wrapped.priority == original.priority
    assert wrapped.handler is not original.handler
    assert wrapped.handler.__qualname__ == dummy_handler.__qualname__
    assert wrapped.handler.__module__ == dummy_handler.__module__


def test_wrapped_handler(app):
    seen = {}

    def handler(app_arg, docname, source):
        seen.update(app=app_arg, docname=docname, source=source)
        return {"ok": True}

    wrapped = wrap_listener("source-read", EventListener(0, handler, 500))

    result = wrapped.handler(app, "index", source=["blah blah"])

    assert result == {"ok": True}
    assert seen == {"app": app, "docname": "index", "source": ["blah blah"]}


def test_listeners_wrapped_once(app):
    app.events.connect("builder-inited", dummy_handler)

    wrap_all_listeners(app)
    first_wrapper = app.events.listeners["builder-inited"][0].handler
    assert getattr(first_wrapper, _WRAP_FLAG, False) is True

    wrap_all_listeners(app)
    assert app.events.listeners["builder-inited"][0].handler is first_wrapper

    app.events.emit("builder-inited")
    assert len(recorder.calls) == 1


def test_handler_connected_after_setup_gets_wrapped(app):
    wrap_connect(app)

    app.events.connect("source-read", dummy_handler)
    app.events.emit("source-read")

    assert [c.handler for c in recorder.calls] == ["dummy_handler"]


# testing durations and timings


def test_handler_duration_and_start(app):
    app.events.connect("builder-inited", dummy_handler)
    wrap_all_listeners(app)

    app.events.emit("builder-inited")

    (call,) = recorder.calls
    build_time = time.perf_counter() - recorder.start_time
    assert call.duration >= SLEEP
    assert call.start >= 0
    assert call.start + call.duration <= build_time


def test_repeated_calls_are_counted_per_event_and_handler(app):
    app.events.connect("source-read", dummy_handler)
    app.events.connect("doctree-read", dummy_handler)
    wrap_all_listeners(app)

    app.events.emit("source-read")
    app.events.emit("source-read")
    app.events.emit("doctree-read")

    assert [(c.event, c.call) for c in recorder.calls] == [
        ("source-read", 1),
        ("source-read", 2),
        ("doctree-read", 1),
    ]


def test_nested_emissions_record_parent_depth_and_own_time(app):
    def outer(app_arg):
        app_arg.events.emit("inner")

    def inner(app_arg):
        time.sleep(SLEEP)

    app.events.connect("outer", outer)
    app.events.connect("inner", inner)
    wrap_emit(app)

    app.events.emit("outer")

    outer_rec, inner_rec = recorder.events
    assert (outer_rec.event_name, outer_rec.depth, outer_rec.parent_id) == (
        "outer",
        0,
        None,
    )
    assert (inner_rec.event_name, inner_rec.depth, inner_rec.parent_id) == (
        "inner",
        1,
        outer_rec.event_id,
    )
    assert outer_rec.duration >= inner_rec.duration >= SLEEP

    recorder.compute_own_times()
    assert inner_rec.own_time == pytest.approx(inner_rec.duration)
    assert outer_rec.own_time == pytest.approx(outer_rec.duration - inner_rec.duration)


# testing classification and output


def test_classify_all_handlers(app, log, monkeypatch):
    monkeypatch.setattr(bs, "THEME_PACKAGES", {"pydata_sphinx_theme"})
    app.add_extension("sphinx_gallery.gen_gallery")

    expected = {
        "sphinx.builders.html": ("sphinx-internal", None),
        "sphinx.ext.intersphinx": ("extension", "sphinx.ext.intersphinx"),
        "sphinx.ext.autodoc.typehints": ("extension", "sphinx.ext.autodoc"),
        "sphinx_gallery.gen_gallery": ("extension", "sphinx_gallery.gen_gallery"),
        "sphinx_gallery.interactive_example": (
            "extension",
            "sphinx_gallery.gen_gallery",
        ),
        "pydata_sphinx_theme.toctree": ("theme", "pydata_sphinx_theme"),
        "conf.py": ("unknown", "conf"),
    }
    for module in expected:
        log.record("builder-inited", f"handler_in_{module}", module, 0.0, 0.1)

    log.classify_all_handlers(app)

    assert {c.module: (c.kind, c.extension) for c in log.calls} == expected


def test_write_json(log, tmp_path):
    log.record("source-read", "handler", "some_ext", 0.5, 0.25)
    log.enter_event("source-read")
    log.exit_event()
    out = tmp_path / "bench.json"
    project_info = {"name": "proj", "version": "1.0", "copyright": "me", "HEAD": None}
    build_info = {
        "builder": "html",
        "start_time": "2026-01-01 00:00:00 UTC",
        "total_wall_time": 12.5,
    }

    frames = {"sampling_interval": 0.001, "samples": 0, "snapshots": []}

    log.write_json(project_info, build_info, frames, str(out))
    data = json.loads(out.read_text())

    assert set(data) == {"project_info", "build_info", "calls", "events", "frames"}
    assert data["frames"] == frames
    assert data["project_info"] == project_info
    assert data["build_info"] == build_info
    assert data["calls"][0]["handler"] == "handler"
    assert data["calls"][0]["duration"] == 0.25
    assert data["events"][0]["event_name"] == "source-read"


def test_starts_fresh_build(log):
    log.record("source-read", "handler", "some_ext", 0.0, 0.1)
    log.enter_event("source-read")
    log.exit_event()

    log.start()

    assert log.calls == []
    assert log.events == []
    assert log.call_counts == {}
    assert log.event_call_counts == {}


# end-to-end smoke test on a dummy Sphinx build


def test_real_build_benchmarks(tmp_path, monkeypatch):
    srcdir = tmp_path / "src"
    srcdir.mkdir()
    (srcdir / "conf.py").write_text(
        "project = 'proj'\nextensions = ['sphinx_benchmark']\n"
    )
    (srcdir / "index.rst").write_text("Title\n=====\n\nblah blah blah blah\n")
    outdir = tmp_path / "out"

    monkeypatch.chdir(tmp_path)
    app = Sphinx(
        str(srcdir),
        str(srcdir),
        str(outdir),
        str(outdir / ".doctrees"),
        "html",
        status=None,
        freshenv=True,
    )
    app.build()

    # sphinx_benchmarks_<date>-<time>[_<short HEAD>].json
    (json_path,) = tmp_path.glob("sphinx_benchmarks_*.json")
    assert re.fullmatch(
        r"sphinx_benchmarks_\d{8}-\d{6}(_[0-9a-f]{7})?\.json", json_path.name
    )
    data = json.loads(json_path.read_text())

    assert data["build_info"]["total_wall_time"] > 0
    assert data["build_info"]["builder"] == "html"
    assert data["build_info"]["start_time"]
    assert data["project_info"]["name"] == "proj"
    assert "HEAD" in data["project_info"]
    assert data["calls"] and data["events"]
    assert {"builder-inited", "build-finished"} <= {
        e["event_name"] for e in data["events"]
    }
    assert all(c["kind"] != "unknown" or c["extension"] for c in data["calls"])
    assert any(c["kind"] == "sphinx-internal" for c in data["calls"])
    frames = data["frames"]
    assert frames["sampling_interval"] == bs.DEFAULT_SAMPLING_INTERVAL
    assert frames["samples"] == len(frames["snapshots"]) > 0
    assert {f["kind"] for f in frames["functions"]} >= {"sphinx-internal", "stdlib"}
    n_functions, n_stacks = len(frames["functions"]), len(frames["stacks"])
    assert all(0 <= i < n_functions for stack in frames["stacks"] for i in stack)
    times = [t for t, _ in frames["snapshots"]]
    assert times == sorted(times)
    assert 0 <= times[0] and times[-1] <= data["build_info"]["total_wall_time"]
    assert all(0 <= stack < n_stacks for _, stack in frames["snapshots"])


# stack snapshots of the build


def busy_wait(seconds):
    """Burn CPU (so the sampler sees this frame) for ``seconds``."""
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        pass


def test_sampler_records_snapshots_with_time_and_stack():
    sampler = bs.StackSampler(recorder, 0.001, threading.get_ident())
    sampler.start()
    try:
        t0 = time.perf_counter() - recorder.start_time
        busy_wait(SLEEP)
        t1 = time.perf_counter() - recorder.start_time
    finally:
        sampler.stop()

    times = [t for t, _ in sampler.snapshots]
    assert times == sorted(times)
    inside = [(t, s) for t, s in sampler.snapshots if t0 <= t <= t1]
    # the sampler only gets the GIL every 5ms (16ms on Windows), more on a loaded
    # machine, so only ask for a few samples
    assert len(inside) >= 3
    functions = {
        sampler.functions[i]["function"] for _, s in inside for i in sampler.stacks[s]
    }
    assert {
        "busy_wait",
        "test_sampler_records_snapshots_with_time_and_stack",
    } <= functions
    # a sampled stack lists the innermost frame first
    innermost = {sampler.functions[sampler.stacks[s][0]]["function"] for _, s in inside}
    assert innermost == {"busy_wait"}


def test_sampler_keeps_sampling_inside_emissions(app):
    app.events.connect("builder-inited", lambda app_arg: busy_wait(SLEEP / 2))
    wrap_emit(app)
    wrap_all_listeners(app)
    sampler = bs.StackSampler(recorder, 0.001, threading.get_ident())
    sampler.start()
    try:
        app.events.emit("builder-inited")
    finally:
        sampler.stop()

    (call,) = recorder.calls
    during = [
        sampler.stacks[s]
        for t, s in sampler.snapshots
        if call.start <= t <= call.start + call.duration
    ]
    assert len(during) >= 3  # see test_sampler_records_snapshots_with_time_and_stack
    assert {sampler.functions[stack[0]]["function"] for stack in during} == {
        "busy_wait"
    }
    # the wrapper the extension put around the handler is on those stacks, so
    # the summary can cut them down to the part inside the handler
    wrappers = {
        sampler.functions[i]["function"]
        for stack in during
        for i in stack
        if sampler.functions[i]["module"] == "sphinx_benchmark.extension"
    }
    assert wrappers >= {"wrap_listener.<locals>.wrapped", "wrap_emit.<locals>.wrapped"}


def test_records_dumps_functions_stacks_and_snapshots(app, log, monkeypatch):
    monkeypatch.setattr(bs, "THEME_PACKAGES", set())
    sampler = bs.StackSampler(log, 0.001, threading.get_ident())
    sampler.functions = [
        {"function": "main", "module": "sphinx.cmd.build", "file": "b.py", "line": 1},
        {
            "function": "read_doc",
            "module": "sphinx.builders",
            "file": "b.py",
            "line": 2,
        },
        {"function": "Path.stat", "module": "pathlib", "file": "p.py", "line": 3},
        {
            "function": "render",
            "module": "layout.html",
            "file": "layout.html",
            "line": 1,
        },
    ]
    # stacks are innermost-first: main > read_doc > Path.stat, main > read_doc,
    # main > render
    sampler.stacks = [(2, 1, 0), (1, 0), (3, 0)]
    sampler.snapshots = [(0.0011234567, 0), (0.002, 0), (0.003, 1), (0.004, 2)]

    frames = sampler.records(app)

    assert frames["sampling_interval"] == 0.001
    assert frames["samples"] == 4
    assert [f["kind"] for f in frames["functions"]] == [
        "sphinx-internal",
        "sphinx-internal",
        "stdlib",
        "unknown",
    ]
    assert frames["functions"][3]["extension"] == "layout"
    assert frames["stacks"] == [[2, 1, 0], [1, 0], [3, 0]]
    # times are rounded to the microsecond
    assert frames["snapshots"] == [[0.001123, 0], [0.002, 0], [0.003, 1], [0.004, 2]]


def test_setup_starts_the_sampler_only_with_the_gil(app, monkeypatch):
    monkeypatch.setattr(bs, "sampler", None)
    bs.setup(app)
    try:
        assert bs.sampler is not None and bs.sampler.is_alive()
    finally:
        bs.sampler.stop()

    monkeypatch.setattr(sys, "_is_gil_enabled", lambda: False, raising=False)
    bs.setup(app)
    assert bs.sampler is None


def test_build_finished_without_a_sampler_still_writes_the_json(monkeypatch, tmp_path):
    """With the GIL disabled there is no sampler; the timings must still be written."""
    monkeypatch.setattr(bs, "sampler", None)
    monkeypatch.chdir(tmp_path)

    class Builder:
        name = "html"

    class Config:
        project, version, copyright = "proj", "1.0", "me"

    app = types.SimpleNamespace(
        config=Config(), builder=Builder(), confdir=str(tmp_path), extensions={}
    )
    recorder.record("source-read", "handler", "some_ext", 0.5, 0.25)
    bs.build_finished(app, None)

    (json_path,) = tmp_path.glob("sphinx_benchmarks_*.json")
    data = json.loads(json_path.read_text())
    assert data["frames"] is None
    assert data["calls"][0]["handler"] == "handler"
