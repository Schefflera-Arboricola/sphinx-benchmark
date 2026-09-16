import json
import os

import pytest

from sphinx_benchmark.cli import main
from sphinx_benchmark.summary import (
    all_gap_profiles,
    build_profile,
    combined_gap_profile,
    compute_summary,
    event_profiles,
    gap_profile,
    handler_profiles,
    load_frames,
)

SAMPLE = {
    "project_info": {
        "name": "proj",
        "version": "1.0",
        "copyright": "2026, Someone",
        "HEAD": "abc123",
    },
    "build_info": {
        "builder": "html",
        "start_time": "2026-01-01 00:00:00 UTC",
        "total_wall_time": 10.0,
    },
    "calls": [
        {
            "event": "builder-inited",
            "handler": "gen_gallery",
            "module": "sphinx_gallery",
            "kind": "extension",
            "extension": "sphinx_gallery.gen_gallery",
            "call": 1,
            "start": 0.5,
            "duration": 4.0,
        },
        {
            "event": "doctree-read",
            "handler": "process_docs",
            "module": "sphinx.ext.autodoc",
            "kind": "sphinx-internal",
            "extension": None,
            "call": 1,
            "start": 5.2,
            "duration": 0.5,
        },
        {
            "event": "doctree-read",
            "handler": "process_docs",
            "module": "sphinx.ext.autodoc",
            "kind": "sphinx-internal",
            "extension": None,
            "call": 2,
            "start": 6.0,
            "duration": 0.7,
        },
    ],
    "events": [
        {
            "event_id": 0,
            "event_name": "builder-inited",
            "call": 1,
            "start": 0.5,
            "depth": 0,
            "duration": 4.5,
            "parent_id": None,
            "own_time": 4.2,
        },
        {
            "event_id": 1,
            "event_name": "env-updated",
            "call": 1,
            "start": 0.6,
            "depth": 1,
            "duration": 0.3,
            "parent_id": 0,
            "own_time": 0.3,
        },
        {
            "event_id": 2,
            "event_name": "doctree-read",
            "call": 1,
            "start": 5.2,
            "depth": 0,
            "duration": 1.5,
            "parent_id": None,
            "own_time": 1.5,
        },
        # still-in-progress emission (build-finished): must be skipped
        {
            "event_id": 3,
            "event_name": "build-finished",
            "call": 1,
            "start": 9.0,
            "depth": 0,
            "duration": None,
            "parent_id": None,
            "own_time": None,
        },
    ],
    # stack snapshots of the build
    "frames": {
        "sampling_interval": 0.001,
        "samples": 21,
        "functions": [
            {
                "function": "main",
                "module": "sphinx.cmd.build",
                "file": "build.py",
                "line": 1,
                "kind": "sphinx-internal",
                "extension": None,
            },
            {
                "function": "Builder.read_doc",
                "module": "sphinx.builders",
                "file": "b.py",
                "line": 2,
                "kind": "sphinx-internal",
                "extension": None,
            },
            {
                "function": "RSTParser.parse",
                "module": "docutils.parsers.rst",
                "file": "r.py",
                "line": 3,
                "kind": "unknown",
                "extension": "docutils",
            },
            {
                "function": "Path.stat",
                "module": "pathlib",
                "file": "p.py",
                "line": 4,
                "kind": "stdlib",
                "extension": "pathlib",
            },
            {
                "function": "import_ext",
                "module": "importlib",
                "file": "i.py",
                "line": 5,
                "kind": "stdlib",
                "extension": "importlib",
            },
            {
                "function": "gen_gallery",
                "module": "sphinx_gallery",
                "file": "g.py",
                "line": 6,
                "kind": "extension",
                "extension": "sphinx_gallery.gen_gallery",
            },
            {
                "function": "wrap_listener.<locals>.wrapped",
                "module": "sphinx_benchmark.extension",
                "file": "e.py",
                "line": 7,
                "kind": "extension",
                "extension": "sphinx_benchmark",
            },
            {
                "function": "EventManager.emit",
                "module": "sphinx.events",
                "file": "ev.py",
                "line": 8,
                "kind": "sphinx-internal",
                "extension": None,
            },
            {
                "function": "wrap_emit.<locals>.wrapped",
                "module": "sphinx_benchmark.extension",
                "file": "e.py",
                "line": 9,
                "kind": "extension",
                "extension": "sphinx_benchmark",
            },
            {
                "function": "process_docs",
                "module": "sphinx.ext.autodoc",
                "file": "a.py",
                "line": 10,
                "kind": "extension",
                "extension": "sphinx.ext.autodoc",
            },
        ],
        # innermost frame first
        "stacks": [
            [4, 0],  # main > import_ext
            [2, 2, 1, 0],  # main > read_doc > parse > parse (recursion)
            [2, 1, 0],  # main > read_doc > parse
            [3, 1, 0],  # main > read_doc > Path.stat
            [1, 0],  # main > read_doc
            [
                5,
                6,
                7,
                8,
                0,
            ],  # main > emit wrapper > emit > listener wrapper > gen_gallery
            [7, 8, 0],  # main > emit wrapper > emit (inside an event, no handler)
            [9, 6, 7, 8, 0],  # ... > process_docs
        ],
        # [time, stack]:
        # startup (0 - 0.5s): 4x main > import_ext
        # builder-inited (0.5 - 5.0): gen_gallery runs 0.5 - 4.5, with env-updated
        #   nested at 0.6 - 0.9; 4.5 - 5.0 is the emission outside any handler
        # gap builder-inited -> doctree-read (5.0 - 5.2): 10 samples: 2 self in
        #   the inner parse, 3 in the outer parse, 4 in Path.stat, 1 in read_doc
        # doctree-read (5.2 - 6.7): process_docs runs 5.2 - 5.7 and 6.0 - 6.7
        "snapshots": [
            [0.1, 0],
            [0.2, 0],
            [0.3, 0],
            [0.4, 0],
            [0.7, 6],  # inside the nested env-updated, gen_gallery still open
            [1.0, 5],
            [2.0, 5],
            [4.6, 6],
            [5.01, 1],
            [5.02, 1],
            [5.03, 2],
            [5.04, 2],
            [5.05, 2],
            [5.06, 3],
            [5.07, 3],
            [5.08, 3],
            [5.09, 3],
            [5.1, 4],
            [5.5, 7],
            [5.8, 6],
            [6.5, 7],
        ],
    },
}


def test_compute_summary_aggregates():
    s = compute_summary(SAMPLE)
    assert s.total_build_time == 10.0
    # own times: 4.2 + 0.3 + 1.5 (build-finished skipped)
    assert s.time_in_events == pytest.approx(6.0)
    assert [ev.name for ev in s.events] == [
        "builder-inited",
        "doctree-read",
        "env-updated",
    ]
    assert s.events[0].has_nested is True
    doctree = s.events[1]
    assert doctree.handlers[0].calls == 2
    assert doctree.handlers[0].total == pytest.approx(1.2)
    # one gap between the two top-level emissions: 5.2 - (0.5 + 4.5) = 0.2
    assert s.gaps[0].label == "builder-inited -> doctree-read"
    assert s.gaps[0].total == pytest.approx(0.2)
    assert s.startup == pytest.approx(0.5)
    assert s.overlaps == 0


def test_run_table_and_html(tmp_path, capsys):
    json_path = tmp_path / "sphinx_benchmarks.json"
    json_path.write_text(json.dumps(SAMPLE))

    assert main(["run", "table", "-i", str(json_path)]) == 0
    out = capsys.readouterr().out
    assert "builder-inited" in out and "Gaps Summary" in out
    assert "Project: proj 1.0  |  HEAD: abc123" in out
    assert "Builder: html  |  Started: 2026-01-01 00:00:00 UTC" in out
    assert "Someone" not in out  # copyright is not printed

    report = tmp_path / "report"
    assert main(["run", "html", "-i", str(json_path), "-o", str(report)]) == 0
    for name in ("index.html", "events.html", "gaps.html", "style.css", "report.js"):
        assert (report / name).exists()
    index = (report / "index.html").read_text(encoding="utf-8")
    assert "conic-gradient" in index
    assert '<span class="project">proj 1.0</span>' in index
    assert "abc123" in index and "2026-01-01 00:00:00 UTC" in index
    assert "Someone" not in index  # copyright is not printed
    assert '<span class="project">proj 1.0</span>' in (report / "gaps.html").read_text(
        encoding="utf-8"
    )


def test_run_default_overview(tmp_path, capsys):
    json_path = tmp_path / "sphinx_benchmarks.json"
    json_path.write_text(json.dumps(SAMPLE))

    # bare 'run' prints the overview (top 10 by default) without 'gap: ' prefixes
    assert main(["run", "-i", str(json_path)]) == 0
    out = capsys.readouterr().out
    assert "Overview" in out
    assert "builder-inited -> doctree-read" in out
    assert "gap: " not in out
    assert "Inside events: 6.000000s (60.00%)" in out
    assert "Outside events (gaps): 4.000000s (40.00%)" in out
    # footer shows full-build totals: events 4.2+1.5+0.3, gaps 3.3+0.5+0.2
    assert "events total" in out and "gaps total" in out
    # sorted descending: builder-inited (4.2s) before doctree-read (1.5s)
    assert out.index("builder-inited") < out.index("doctree-read")

    # --top limits the rows: top 2 are builder-inited (4.2s) and finish (3.3s)
    assert main(["run", "--top", "2", "-i", str(json_path)]) == 0
    out = capsys.readouterr().out
    assert "Top 2 of" in out
    assert "builder-inited" in out and "doctree-read" not in out
    assert "use --top N to show more" in out

    # --top only makes sense for the bare overview
    with pytest.raises(SystemExit):
        main(["run", "table", "--top", "2", "-i", str(json_path)])
    assert "--top is only valid" in capsys.readouterr().err


def test_table_selectors(tmp_path, capsys):
    json_path = tmp_path / "sphinx_benchmarks.json"
    json_path.write_text(json.dumps(SAMPLE))

    def run(*selector):
        assert main(["run", "table", *selector, "-i", str(json_path)]) == 0
        return capsys.readouterr().out

    out = run("gaps")
    assert "Gaps Summary" in out and "builder-inited" in out

    out = run("events")
    assert "builder-inited" in out and "Gaps Summary" not in out

    # event detail: all emissions
    out = run("events", "builder-inited")
    assert "Emissions of 'builder-inited'" in out and "(top-level)" in out

    # handler detail across events
    out = run("events", "process_docs")
    assert "Calls of 'process_docs'" in out
    assert "doctree-read" in out  # the event column
    assert out.count("doctree-read") >= 2  # both calls listed

    # handler detail scoped to one event
    out = run("events", "builder-inited", "gen_gallery")
    assert "Calls of 'gen_gallery' during 'builder-inited'" in out

    # gap occurrences between two events
    out = run("gaps", "builder-inited", "doctree-read")
    assert "Gaps between 'builder-inited' -> 'doctree-read'" in out
    assert "0.200000" in out


def test_table_unknown_name_suggests(tmp_path, capsys):
    json_path = tmp_path / "sphinx_benchmarks.json"
    json_path.write_text(json.dumps(SAMPLE))
    with pytest.raises(SystemExit):
        main(["run", "table", "events", "gen_galery", "-i", str(json_path)])
    err = capsys.readouterr().err
    assert "unknown event or handler" in err and "gen_gallery" in err


def test_html_detail_pages(tmp_path):
    json_path = tmp_path / "sphinx_benchmarks.json"
    json_path.write_text(json.dumps(SAMPLE))
    report = tmp_path / "report"
    assert main(["run", "html", "-i", str(json_path), "-o", str(report)]) == 0

    events_html = (report / "events.html").read_text(encoding="utf-8")
    assert 'href="event-builder-inited.html"' in events_html
    assert 'href="handler-gen_gallery.html?event=builder-inited"' in events_html

    gaps_html = (report / "gaps.html").read_text(encoding="utf-8")
    assert 'href="gap-builder-inited----doctree-read.html"' in gaps_html

    handler_page = (report / "handler-process_docs.html").read_text(encoding="utf-8")
    assert 'id="event-filter"' in handler_page  # per-event dropdown
    assert "All events" in handler_page
    assert 'class="sortable"' in handler_page

    event_page = (report / "event-builder-inited.html").read_text(encoding="utf-8")
    assert "1 recorded emissions" in event_page

    gap_page = (report / "gap-builder-inited----doctree-read.html").read_text(
        encoding="utf-8"
    )
    assert "1 occurrences" in gap_page and "0.200000" in gap_page


def test_html_long_handler_name(tmp_path):
    # e.g. a functools.partial repr used as a handler: its name can exceed
    # the filesystem's 255-byte filename limit unless the slug is truncated
    data = json.loads(json.dumps(SAMPLE))
    long_name = "functools.partial(<function setup at 0x10d4b7060>, " + " ".join(
        f"arg{i}=<class 'pkg.mod{i}.Thing{i}'>" for i in range(40)
    )
    data["calls"].append(
        {
            "event": "builder-inited",
            "handler": long_name,
            "module": "conf",
            "kind": "unknown",
            "extension": "conf",
            "call": 1,
            "start": 0.6,
            "duration": 0.1,
        }
    )
    json_path = tmp_path / "sphinx_benchmarks.json"
    json_path.write_text(json.dumps(data))
    report = tmp_path / "report"
    assert main(["run", "html", "-i", str(json_path), "-o", str(report)]) == 0
    handler_pages = [
        p.name
        for p in report.glob("handler-*.html")
        if not p.name.startswith("handler-tree-")
    ]
    assert len(handler_pages) == 3
    assert all(len(name) < 120 for name in handler_pages)
    # the events page links to the truncated filename
    long_page = next(p for p in handler_pages if "functools" in p)
    assert f'href="{long_page}' in (report / "events.html").read_text(encoding="utf-8")


def test_missing_file_exits_with_message(tmp_path):
    with pytest.raises(SystemExit, match="not found"):
        main(["run", "table", "-i", str(tmp_path / "nope.json")])


def test_default_input_is_latest_benchmarks_file(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit, match="no sphinx_benchmarks\\*.json found"):
        main(["run"])

    old = tmp_path / "sphinx_benchmarks_20260101-000000_aaaaaaa.json"
    new = tmp_path / "sphinx_benchmarks_20260102-000000_bbbbbbb.json"
    old.write_text(json.dumps(SAMPLE))
    newer = json.loads(json.dumps(SAMPLE))
    newer["project_info"]["name"] = "newer-project"
    new.write_text(json.dumps(newer))
    os.utime(old, (1, 1))

    assert main(["run"]) == 0
    assert "newer-project" in capsys.readouterr().out


# sampled gap profiles


def test_gap_profile_self_total_and_stdlib_folding():
    s = compute_summary(SAMPLE)
    p = gap_profile(SAMPLE, s, "builder-inited", "doctree-read")
    assert p.samples == 10 and p.seconds == pytest.approx(0.2)
    rows = {r.function: r for r in p.rows}
    # 0.02s per sample; Path.stat's 4 samples are charged to read_doc's self time
    assert rows["Builder.read_doc"].self_seconds == pytest.approx(0.1)
    assert rows["Builder.read_doc"].total_seconds == pytest.approx(0.2)
    # recursion: parse is on 5 samples' stacks, counted once each
    assert rows["RSTParser.parse"].self_seconds == pytest.approx(0.1)
    assert rows["RSTParser.parse"].total_seconds == pytest.approx(0.1)
    assert rows["Path.stat"].self_seconds == 0
    assert rows["Path.stat"].total_seconds == pytest.approx(0.08)
    assert rows["main"].total_seconds == pytest.approx(0.2)
    assert p.rows[0].function in ("Builder.read_doc", "RSTParser.parse")  # by self
    # the call tree: main > read_doc > (parse > parse, Path.stat)
    (root,) = p.tree
    assert root.function == "main" and root.total_seconds == pytest.approx(0.2)
    (read_doc,) = root.children
    assert [c.function for c in read_doc.children] == ["RSTParser.parse", "Path.stat"]
    assert read_doc.children[0].total_seconds == pytest.approx(0.1)
    assert read_doc.children[0].children[0].self_seconds == pytest.approx(0.04)
    assert read_doc.children[1].file == "p.py" and read_doc.children[1].line == 4


def test_gap_profile_startup_and_combined():
    s = compute_summary(SAMPLE)
    profiles = all_gap_profiles(SAMPLE, s)
    startup = profiles[(None, "builder-inited")]
    assert startup.seconds == pytest.approx(s.startup) == pytest.approx(0.5)
    assert startup.label == "(startup) -> builder-inited"
    # import_ext is stdlib: its time is charged to main, the caller
    rows = {r.function: r for r in startup.rows}
    assert rows["main"].self_seconds == pytest.approx(0.5)
    assert rows["import_ext"].self_seconds == 0
    assert rows["import_ext"].total_seconds == pytest.approx(0.5)

    combined = combined_gap_profile(profiles)
    assert combined.label == "(all gaps, including startup)"
    assert combined.samples == 14 and combined.seconds == pytest.approx(0.7)
    rows = {r.function: r for r in combined.rows}
    assert rows["main"].total_seconds == pytest.approx(0.7)
    assert rows["main"].self_seconds == pytest.approx(0.5)
    assert rows["Builder.read_doc"].self_seconds == pytest.approx(0.1)
    # the two gaps' trees are merged under the shared root
    (root,) = combined.tree
    assert root.function == "main" and root.total_seconds == pytest.approx(0.7)
    assert [c.function for c in root.children] == ["import_ext", "Builder.read_doc"]

    assert gap_profile(SAMPLE, s, "doctree-read", "env-updated") is None
    assert combined_gap_profile({}) is None


def test_table_and_html_show_gap_profiles(tmp_path, capsys):
    json_path = tmp_path / "sphinx_benchmarks.json"
    json_path.write_text(json.dumps(SAMPLE))

    assert main(["run", "table", "gaps", "-i", str(json_path)]) == 0
    out = capsys.readouterr().out
    assert "Inside the gap (all gaps, including startup)" in out
    assert "Top 5 of 5 functions by self time" in out

    assert (
        main(
            [
                "run",
                "table",
                "gaps",
                "builder-inited",
                "doctree-read",
                "-i",
                str(json_path),
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "Gaps between 'builder-inited' -> 'doctree-read'" in out
    assert "Inside the gap builder-inited -> doctree-read  -  10 stack samples" in out
    assert "Builder.read_doc" in out and "docutils.parsers.rst" in out

    report = tmp_path / "report"
    assert main(["run", "html", "-i", str(json_path), "-o", str(report)]) == 0
    gaps_html = (report / "gaps.html").read_text(encoding="utf-8")
    assert "Where the time of all gaps goes" in gaps_html
    gap_page = (report / "gap-builder-inited----doctree-read.html").read_text(
        encoding="utf-8"
    )
    assert "Where the time inside the gap goes" in gap_page
    assert 'href="gap-tree-builder-inited----doctree-read.html"' in gap_page
    assert 'title="b.py:2"' in gap_page  # file:line of Builder.read_doc
    tree_page = (report / "gap-tree-builder-inited----doctree-read.html").read_text(
        encoding="utf-8"
    )
    assert "<svg" in tree_page and 'marker-end="url(#arrow)"' in tree_page
    # one box per function on the tree, with where it is defined on hover
    assert tree_page.count('class="node"') == 5  # main, read_doc, parse, parse, stat
    assert "Builder.read_doc  [sphinx.builders]\nb.py:2" in tree_page
    assert 'marker-end="url(#arrow)"' in tree_page
    assert "<svg" in (report / "gaps-tree.html").read_text(encoding="utf-8")
    assert 'href="gaps-tree.html"' in gaps_html


def test_load_frames_locates_every_snapshot():
    fr = load_frames(SAMPLE)
    by_time = dict(zip(fr.times, zip(fr.where, fr.key, fr.handlers)))
    assert by_time[0.1] == ("gap", (None, "builder-inited"), ())
    gen = (("gen_gallery", "sphinx_gallery"),)
    assert by_time[1.0] == ("handler", ("builder-inited", "gen_gallery"), gen)
    # inside the nested env-updated emission: that event, with gen_gallery still open
    assert by_time[0.7] == ("event", "env-updated", gen)
    # after gen_gallery returned, still inside builder-inited's emission
    assert by_time[4.6] == ("event", "builder-inited", ())
    assert by_time[5.05] == ("gap", ("builder-inited", "doctree-read"), ())
    assert by_time[5.5] == (
        "handler",
        ("doctree-read", "process_docs"),
        (("process_docs", "sphinx.ext.autodoc"),),
    )
    assert by_time[5.8] == ("event", "doctree-read", ())
    assert load_frames({k: v for k, v in SAMPLE.items() if k != "frames"}) is None


def test_event_and_handler_profiles_cut_the_stacks():
    s = compute_summary(SAMPLE)
    fr = load_frames(SAMPLE)

    events = event_profiles(fr, s)
    assert set(events) == {"builder-inited", "env-updated", "doctree-read"}
    p = events["builder-inited"]
    # the two gen_gallery samples and the one outside any handler; the sample in
    # the nested env-updated is that event's, matching the own times
    assert p.samples == 3 and p.seconds == pytest.approx(4.2) and p.scope == "event"
    assert p.label == "builder-inited"
    (root,) = p.tree  # cut at the emit wrapper: EventManager.emit is the root
    assert root.function == "EventManager.emit"
    assert root.total_seconds == pytest.approx(4.2)
    (wrapper,) = root.children
    (gen,) = wrapper.children
    assert gen.function == "gen_gallery" and gen.total_seconds == pytest.approx(2.8)
    assert events["env-updated"].samples == 1
    assert events["env-updated"].seconds == pytest.approx(0.3)

    handlers = handler_profiles(fr, s)
    assert set(handlers) == {"gen_gallery", "process_docs"}
    p = handlers["gen_gallery"]
    # its two own samples plus the one taken inside the nested emission
    assert p.samples == 3 and p.seconds == pytest.approx(4.0) and p.scope == "handler"
    roots = {n.function: n for n in p.tree}
    # cut at the listener wrapper: the handler itself is the root ...
    assert roots["gen_gallery"].children == ()
    assert roots["gen_gallery"].total_seconds == pytest.approx(4.0 * 2 / 3)
    # ... unless the wrapper is not on the stack (the nested emission's sample)
    assert roots["main"].total_seconds == pytest.approx(4.0 / 3)
    p = handlers["process_docs"]
    assert p.samples == 2 and p.seconds == pytest.approx(1.2)
    assert [n.function for n in p.tree] == ["process_docs"]


def test_build_profile_weights_samples_by_measured_time():
    s = compute_summary(SAMPLE)
    fr = load_frames(SAMPLE)

    # the measured time each innermost slot covers: calls and emissions
    # minus what is nested inside them
    assert fr.slot_seconds[("handler", ("builder-inited", "gen_gallery"))] == (
        pytest.approx(4.0 - 0.3)  # minus the nested env-updated
    )
    assert fr.slot_seconds[("handler", ("doctree-read", "process_docs"))] == (
        pytest.approx(1.2)
    )
    assert fr.slot_seconds[("event", "builder-inited")] == pytest.approx(0.5)
    assert fr.slot_seconds[("event", "env-updated")] == pytest.approx(0.3)
    assert fr.slot_seconds[("event", "doctree-read")] == pytest.approx(0.3)

    p = build_profile(fr, s, SAMPLE)
    assert p.label == "(whole build)" and p.scope == "build"
    assert p.samples == 21 and p.seconds == pytest.approx(10.0)
    (root,) = p.tree
    assert root.function == "main"
    # every subtree adds up to the measured timings, whatever the sample density
    assert root.where_seconds == {
        "gap": pytest.approx(0.5 + 0.2),
        "handler": pytest.approx(3.7 + 1.2),
        "event": pytest.approx(0.5 + 0.3 + 0.3),
    }
    assert root.total_seconds == pytest.approx(6.7)  # the finish (3.3s) got no samples
    rows = {r.function: r for r in p.rows}
    assert rows["gen_gallery"].total_seconds == pytest.approx(3.7)
    assert rows["process_docs"].total_seconds == pytest.approx(1.2)
    assert build_profile(None, s, SAMPLE) is None


def test_html_event_handler_and_build_pages(tmp_path):
    json_path = tmp_path / "sphinx_benchmarks.json"
    json_path.write_text(json.dumps(SAMPLE))
    report = tmp_path / "report"
    assert main(["run", "html", "-i", str(json_path), "-o", str(report)]) == 0

    nav = '<a href="gaps.html">Gaps</a><a href="tree.html">Call tree</a>'
    assert nav in (report / "index.html").read_text(encoding="utf-8")

    event_page = (report / "event-builder-inited.html").read_text(encoding="utf-8")
    assert "Where the time inside the event goes" in event_page
    assert "<th>% event" in event_page and "<th>% gap" not in event_page
    assert 'href="event-tree-builder-inited.html"' in event_page
    tree = (report / "event-tree-builder-inited.html").read_text(encoding="utf-8")
    assert "Call tree: builder-inited" in tree and "<svg" in tree
    # an event that got no samples has no profile section and no tree page
    assert "Where the time" not in (report / "event-build-finished.html").read_text(
        encoding="utf-8"
    )
    assert not (report / "event-tree-build-finished.html").exists()

    handler_page = (report / "handler-gen_gallery.html").read_text(encoding="utf-8")
    assert "Where the time inside the handler goes" in handler_page
    assert "<th>% handler" in handler_page
    assert 'href="handler-tree-gen_gallery.html"' in handler_page
    assert "<svg" in (report / "handler-tree-gen_gallery.html").read_text(
        encoding="utf-8"
    )

    build = (report / "tree.html").read_text(encoding="utf-8")
    assert "Call tree: (whole build)" in build
    assert 'class="tree light"' in build  # coloured by where the build was
    assert "inside a handler" in build and "in a gap between emissions" in build
    assert 'fill="#c6dff2"' in build  # main is mostly inside handlers: lighter blue
    assert "4.900s inside a handler" in build and "0.700s in a gap" in build
    assert "Where the time inside the build goes" in build
    assert "<th>% build" in build


def test_without_frames_nothing_changes(tmp_path, capsys):
    data = {k: v for k, v in SAMPLE.items() if k != "frames"}
    json_path = tmp_path / "sphinx_benchmarks.json"
    json_path.write_text(json.dumps(data))
    assert main(["run", "table", "gaps", "-i", str(json_path)]) == 0
    assert "Inside the gap" not in capsys.readouterr().out
    report = tmp_path / "report"
    assert main(["run", "html", "-i", str(json_path), "-o", str(report)]) == 0
    assert "Where the time" not in (report / "gaps.html").read_text(encoding="utf-8")
    assert "Where the time" not in (report / "event-builder-inited.html").read_text(
        encoding="utf-8"
    )
    assert "was not sampled" in (report / "tree.html").read_text(encoding="utf-8")


def test_profiles_skip_the_unmeasured_build_finished_emission():
    # build-finished has duration None (the JSON is written inside it), so it has
    # no own time: it and a handler only called by it get no (zero-second) profile
    data = json.loads(json.dumps(SAMPLE))
    data["calls"].append(
        {
            "event": "build-finished",
            "handler": "cleanup",
            "module": "conf",
            "kind": "unknown",
            "extension": "conf",
            "call": 1,
            "start": 9.1,
            "duration": 0.5,
        }
    )
    data["frames"]["snapshots"].append([9.2, 0])
    data["frames"]["samples"] += 1
    s = compute_summary(data)
    fr = load_frames(data)

    assert "build-finished" not in event_profiles(fr, s)
    assert "cleanup" not in handler_profiles(fr, s)
    assert set(handler_profiles(fr, s)) == {"gen_gallery", "process_docs"}
    # the whole-build profile still counts that sample
    assert build_profile(fr, s, data).samples == SAMPLE["frames"]["samples"] + 1
