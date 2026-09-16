"""Terminal output: prints the summary and detail tables.

This module only formats and prints -- all numbers come pre-computed
from :mod:`sphinx_benchmark.summary`.
"""

from __future__ import annotations

from .summary import (
    BuildSummary,
    CallDetail,
    EmissionDetail,
    GapOccurrence,
    OverviewRow,
    Profile,
    overview_rows,
)

_COL_HEADER = (
    f"  {'Handler':50}{'Kind':20}{'Ext/Module':40}"
    f"{'Calls':>10}{'Total(s)':>15}{'Avg(ms)':>15}"
)
_WIDTH = len(_COL_HEADER)


def print_build_info(s: BuildSummary) -> None:
    """Print the project and build info recorded in the JSON (if any)."""
    p, b = s.project_info, s.build_info
    if not p and not b:
        return
    print()
    print(
        f"Project: {p.get('name', '-')} {p.get('version', '')}  |  "
        f"HEAD: {p.get('HEAD') or '-'}"
    )
    print(f"Builder: {b.get('builder', '-')}  |  Started: {b.get('start_time', '-')}")


def print_summary(s: BuildSummary) -> None:
    """Print the per-event handler tables and the gaps summary table."""
    print()
    print("Build time: ", s.total_build_time)
    print_events(s)
    print()
    print_gaps(s)


def print_events(s: BuildSummary) -> None:
    """Print one handler-breakdown table per event."""
    width = _WIDTH
    for ev in s.events:
        depth_str = (
            str(ev.depth_min)
            if ev.depth_min == ev.depth_max
            else f"{ev.depth_min}-{ev.depth_max}"
        )
        nested_str = (
            f"  |  {ev.duration:.6f}s duration (including nested event)"
            if ev.has_nested
            else ""
        )
        print("=" * width)
        print(
            f"{ev.name}  -  {ev.own_time:.6f}s own time "
            f"({s.pct(ev.own_time):.2f}% of build)  |  "
            f"{ev.emissions} emissions"
            f"{nested_str}  |  depth {depth_str}"
        )
        print("=" * width)
        print(_COL_HEADER)
        print("-" * width)
        for r in ev.handlers:
            print(
                f"  {r.handler[:49]:50}{r.kind:20}{r.extension[:39]:40}"
                f"{r.calls:10d}{r.total:15.6f}{r.avg * 1000:15.3f}"
            )
        print("-" * width)
        print(f"  {'(sum of handlers)':50}{'':20}{'':40}{'':10}{ev.handler_sum:15.6f}")
        print(
            f"  {'(unaccounted overhead)':50}{'':20}{'':40}{'':10}{ev.overhead:15.6f}"
        )
        print()

    print("=" * width)
    print(
        f"Sum of own durations of all events: {s.time_in_events:.6f}s   "
        f"Wall clock: {s.total_build_time:.6f}s   "
        f"Outside any event: {s.total_build_time - s.time_in_events:.6f}s "
        f"({s.pct(s.total_build_time - s.time_in_events):.2f}%)"
    )


def print_gaps(s: BuildSummary) -> None:
    """Print the gaps summary table."""
    width = _WIDTH
    # 2 for the indent, then the 15/10/12/10 numeric columns
    label_w = width - 49
    print("=" * width)
    print("Gaps Summary")
    print("=" * width)
    print(
        f"  {'Between':{label_w}}{'Gap Total(s)':>15}{'Count':>10}"
        f"{'Avg(ms)':>12}{'% build':>10}"
    )
    print("-" * width)
    have_top = bool(s.events) or s.startup or s.finish
    if have_top:
        print(
            f"  {'(startup, before first emission)':{label_w}}{s.startup:15.6f}"
            f"{'':10}{'':12}{s.pct(s.startup):9.2f}%"
        )
    for g in s.gaps:
        print(
            f"  {g.label[: label_w - 1]:{label_w}}{g.total:15.6f}"
            f"{g.count:10d}{g.total / g.count * 1000:12.3f}{s.pct(g.total):9.2f}%"
        )
    if have_top:
        print(
            f"  {'(finish, after last emission)':{label_w}}{s.finish:15.6f}"
            f"{'':10}{'':12}{s.pct(s.finish):9.2f}%"
        )
    print("-" * width)
    print(
        f"  {'(total outside events)':{label_w}}{s.outside_events:15.6f}"
        f"{'':10}{'':12}{s.pct(s.outside_events):9.2f}%"
    )
    if s.overlaps:
        print(
            f"  WARNING: {s.overlaps} negative gaps -- top-level emissions "
            "overlap, so these timings are unreliable"
        )


def print_overview(s: BuildSummary, top: int | None = None) -> None:
    """Print events (own time) and gaps in one table, by % build descending.

    ``top`` limits the table to the N largest rows (``None`` shows all).
    """
    all_rows: tuple[OverviewRow, ...] = overview_rows(s)
    rows = all_rows[:top] if top is not None else all_rows
    label_w = max([30] + [len(r.label) + 2 for r in rows])
    header = (
        f"  {'Name':{label_w}}{'Type':>8}{'Time(s)':>15}{'Count':>10}{'% build':>10}"
    )
    width = len(header)
    title = (
        f"Top {len(rows)} of {len(all_rows)} events and gaps, by % of build"
        if len(rows) < len(all_rows)
        else "Overview (events' own time and gaps, by % of build)"
    )
    print()
    print(
        f"Build time: {s.total_build_time:.6f}s   "
        f"Inside events: {s.time_in_events:.6f}s ({s.pct(s.time_in_events):.2f}%)   "
        f"Outside events (gaps): {s.outside_events:.6f}s "
        f"({s.pct(s.outside_events):.2f}%)"
    )
    print("=" * width)
    print(title)
    print("=" * width)
    print(header)
    print("-" * width)
    for r in rows:
        count = f"{r.count}" if r.count is not None else ""
        print(
            f"  {r.label:{label_w}}{r.kind:>8}{r.seconds:15.6f}"
            f"{count:>10}{s.pct(r.seconds):9.2f}%"
        )
    print("-" * width)
    # totals cover the only top N rows in the table
    events_total = sum(r.seconds for r in rows if r.kind == "event")
    gaps_total = sum(r.seconds for r in rows if r.kind == "gap")
    print(
        f"  {'events total':{label_w}}{'':>8}{events_total:15.6f}"
        f"{'':>10}{s.pct(events_total):9.2f}%"
    )
    print(
        f"  {'gaps total':{label_w}}{'':>8}{gaps_total:15.6f}"
        f"{'':>10}{s.pct(gaps_total):9.2f}%"
    )
    if len(rows) < len(all_rows):
        print(f"  ({len(all_rows) - len(rows)} more rows; use --top N to show more)")
    if s.overlaps:
        print(
            f"  WARNING: {s.overlaps} negative gaps -- top-level emissions "
            "overlap, so these timings are unreliable"
        )


def print_emissions(
    event_name: str, rows: tuple[EmissionDetail, ...], s: BuildSummary
) -> None:
    """Print every recorded emission of one event."""
    header = (
        f"  {'Call#':>7}{'Start(s)':>15}{'Depth':>8}{'Duration(s)':>15}"
        f"{'Own(s)':>15}{'% build':>10}   {'Parent event':30}"
    )
    width = len(header)
    total_own = sum(r.own_time for r in rows if r.own_time is not None)
    print()
    print("=" * width)
    print(f"Emissions of '{event_name}'  -  {len(rows)} recorded")
    print("=" * width)
    print(header)
    print("-" * width)
    for r in rows:
        duration = f"{r.duration:15.6f}" if r.duration is not None else f"{'-':>15}"
        own = f"{r.own_time:15.6f}" if r.own_time is not None else f"{'-':>15}"
        pct = f"{s.pct(r.own_time):9.2f}%" if r.own_time is not None else f"{'-':>10}"
        parent = r.parent_name if r.parent_name is not None else "(top-level)"
        print(
            f"  {r.call:7d}{r.start:15.6f}{r.depth:8d}{duration}{own}{pct}   {parent:30}"
        )
    print("-" * width)
    print(
        f"  {'(total)':>7}{'':15}{'':8}{'':15}{total_own:15.6f}{s.pct(total_own):9.2f}%"
    )
    if any(r.duration is None for r in rows):
        print(
            "  Note: '-' marks an emission still in progress when the JSON "
            "was written (e.g. build-finished)."
        )


def print_handler_calls(
    handler_name: str,
    rows: tuple[CallDetail, ...],
    s: BuildSummary,
    event_name: str | None = None,
) -> None:
    """Print every recorded call of one handler.

    The event column is always shown; when ``event_name`` is given the
    rows were already filtered to that event.
    """
    event_w = max([15] + [len(r.event) + 2 for r in rows])
    header = (
        f"  {'Event':{event_w}}{'Call#':>7}{'Start(s)':>15}{'Duration(s)':>15}"
        f"{'% build':>10}   {'Kind':18}{'Ext/Module':30}"
    )
    width = len(header)
    total = sum(r.duration for r in rows)
    scope = f" during '{event_name}'" if event_name else ""
    print()
    print("=" * width)
    print(f"Calls of '{handler_name}'{scope}  -  {len(rows)} recorded")
    print("=" * width)
    print(header)
    print("-" * width)
    for r in rows:
        print(
            f"  {r.event:{event_w}}{r.call:7d}{r.start:15.6f}{r.duration:15.6f}"
            f"{s.pct(r.duration):9.2f}%   {r.kind:18}{r.extension[:29]:30}"
        )
    print("-" * width)
    print(f"  {'(total)':{event_w}}{'':7}{'':15}{total:15.6f}{s.pct(total):9.2f}%")


def print_gap_occurrences(
    source: str, target: str, rows: tuple[GapOccurrence, ...], s: BuildSummary
) -> None:
    """Print every individual gap between two events' top-level emissions."""
    header = (
        f"  {'#':>4}{'First event emission#':>24}{'Second event emission#':>24}{'Gap start(s)':>15}"
        f"{'Gap end(s)':>15}{'Duration(s)':>15}{'% build':>10}"
    )
    width = len(header)
    total = sum(r.duration for r in rows)
    print()
    print("=" * width)
    print(f"Gaps between '{source}' -> '{target}'  -  {len(rows)} occurrences")
    print("=" * width)
    print(header)
    print("-" * width)
    for i, r in enumerate(rows, 1):
        print(
            f"  {i:4d}{r.source_call:24d}{r.target_call:24d}{r.start:15.6f}"
            f"{r.end:15.6f}{r.duration:15.6f}{s.pct(r.duration):9.2f}%"
        )
    print("-" * width)
    print(f"  {'(total)':32}{'':25}{'':25}{total:15.6f}{s.pct(total):9.2f}%")


def print_gap_profile(p: Profile, top: int = 15) -> None:
    """Print where the time of a gap goes: the ``top`` functions by self time."""
    header = (
        f"  {'Function':48}{'Module':38}{'Kind':16}"
        f"{'Self(s)':>12}{'% gap':>8}{'Total(s)':>12}{'% gap':>8}"
    )
    width = len(header)
    print()
    print("=" * width)
    print(
        f"Inside the gap {p.label}  -  {p.samples} stack samples over "
        f"{p.seconds:.6f}s (times below are estimated from the samples)"
    )
    print("=" * width)
    print(
        "  Self = time in the function's own code (calls into the Python standard "
        "library count towards the caller); Total = the function and everything it "
        "called."
    )
    rows = p.rows[:top]
    print("-" * width)
    print(f"  Top {len(rows)} of {len(p.rows)} functions by self time")
    print("-" * width)
    print(header)
    print("-" * width)
    for r in rows:
        print(
            f"  {r.function[:47]:48}{r.module[:37]:38}{r.kind:16}"
            f"{r.self_seconds:12.6f}{p.pct(r.self_seconds):7.2f}%"
            f"{r.total_seconds:12.6f}{p.pct(r.total_seconds):7.2f}%"
        )
    print("-" * width)
    if p.tree:
        print(
            "  The call tree (which function called which) is drawn as a graph in "
            "the HTML report: sphinx-benchmark run html"
        )
