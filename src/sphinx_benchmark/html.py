"""HTML output: writes a small static report from a :class:`BuildSummary`.

The output directory gets four top-level pages plus one detail page per
event, per handler, and per gap pair, and the shared assets:

- ``index.html``    : overview with a pie chart of build % (events' own time and the gaps between them, labels in descending order)
- ``events.html``   : handler-breakdown table per event
- ``gaps.html``     : the gaps summary table
- ``tree.html``     : (when the build was sampled) the call tree of the whole
  build, each box coloured by where the build was (inside a handler, inside
  an event but outside its handlers, in a gap, or neither)
- ``event-*.html``  : every emission of one event
- ``handler-*.html``: every call of one handler, filterable by event
- ``gap-*.html``    : every individual gap between one pair of events
- when the build was sampled, the event, handler and gap pages also show
  where the time inside them goes, per function
- ``event-tree-*.html``, ``handler-tree-*.html``, ``gap-tree-*.html``,
  ``gaps-tree.html``: the sampled call tree of one event, handler or gap
  (of all gaps together) drawn as a graph; hover a node for where the
  function is defined
- ``style.css``, ``report.js``

Every event name, handler name and gap in the tables is a hyperlink to
its detail page, and all tables can be re-sorted by clicking a column
heading (the arrows show the current sort direction).
"""

from __future__ import annotations

import hashlib
import os
import re
from html import escape
from urllib.parse import quote

from .summary import (
    BuildSummary,
    Profile,
    all_emission_details,
    all_gap_occurrence_details,
    all_handler_call_details,
    build_profile,
    combined_gap_profile,
    event_names,
    event_profiles,
    gap_profiles,
    handler_names,
    handler_profiles,
    load_frames,
)

#: What each column means; shown in the "i" tooltip on column headings.
COLUMN_HELP = {
    "Handler": "Qualified name of the event handler function.",
    "Kind": "Where the handler comes from: extension, sphinx-internal, theme, or unknown.",
    "Ext/Module": "Origin package: extension or theme name, or the top-level module for unknown handlers.",
    "Module": "Full module path where the handler function is defined.",
    "Calls": "Number of times this handler ran for this event.",
    "Total(s)": "Summed execution time over all calls, in seconds.",
    "Avg(ms)": "Total divided by Calls, in milliseconds.",
    "Between": "The two consecutive top-level emissions this gap falls between.",
    "Gap Total(s)": "Summed duration of all gaps between the 2 events, in seconds.",
    "Count": "Number of times this pair of emissions occurred consecutively.",
    "Avg Gap(ms)": "Gap Total divided by Count, in milliseconds.",
    "% build": "Share of the total wall-clock build time.",
    "Event": "Name of the event this handler call was made from.",
    "Call#": "Sequence number of this emission/call (1 for the first, and so on).",
    "Start(s)": "Seconds since the start of the build at which this began.",
    "Depth": "Nesting level: 0 is a top-level emission, 1 was emitted from inside another emission, and so on.",
    "Duration(s)": "How long this took, in seconds.",
    "Own(s)": "Duration excluding nested event emissions, in seconds.",
    "Parent event": "The emission this one was nested inside, or (top-level).",
    "#": "The gap number between the two event emissions. (1st gap, 2nd gap, ...)",
    "First event emission#": "The emission number of the first event before the gap (e.g. 5 = the first event was emitted for the 5th time and after which this gap entered).",
    "Second event emission#": "The emission number of the second event before which the gap ends (e.g. 6 means after this gap ends the second event was emitted the 6th time during the build process)",
    "Gap start(s)": "Seconds since build start at which the source emission ended.",
    "Gap end(s)": "Seconds since build start at which the target emission began.",
    "Function": "Qualified name of the function (Class.method for methods); hover for the file and line where it is defined.",
    "Self time(s)": "Estimated time spent in the function's own code, with calls into the Python standard library charged to the caller. Estimated from stack samples.",
    "% gap": "Share of this gap's total time.",
    "% event": "Share of this event's own time (nested emissions excluded).",
    "% handler": "Share of this handler's total time over all its calls (nested emissions included).",
    "Total time(s)": "Estimated time spent in the function and in everything it called (a function calling itself is counted once). Estimated from stack samples.",
}

#: Slice colours for the pie chart, cycled if there are more slices.
PALETTE = [
    "#155e63",
    "#c26a2a",
    "#5a4fa2",
    "#2e7d4f",
    "#a83a5a",
    "#946200",
    "#3a6ea5",
    "#7a5230",
    "#4f7d7d",
    "#8a4fa2",
    "#6b7d2e",
    "#a25a4f",
]

_PAGES = [
    ("index.html", "Overview"),
    ("events.html", "Events"),
    ("gaps.html", "Gaps"),
    ("tree.html", "Call tree"),
]


# ------------------------------------------------------------------- links --


#: Longest slug used in a detail-page filename. Handler names can be huge
#: (e.g. the repr of a functools.partial), and most filesystems cap a file
#: name at 255 bytes, so anything longer is truncated and disambiguated
#: with a short hash of the full name.
_MAX_SLUG = 80


def _slug(text: str) -> str:
    """Make ``text`` safe for use in a filename."""
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", text).strip("-").lower() or "x"
    if len(slug) > _MAX_SLUG:
        digest = hashlib.sha1(text.encode()).hexdigest()[:8]
        slug = f"{slug[:_MAX_SLUG].rstrip('-.')}-{digest}"
    return slug


def _unique_files(prefix: str, names: set[str]) -> dict[str, str]:
    """Map each name to a unique ``<prefix>-<slug>.html`` filename."""
    files: dict[str, str] = {}
    used: set[str] = set()
    for name in sorted(names):
        base = f"{prefix}-{_slug(name)}"
        fname, i = base + ".html", 2
        while fname in used:
            fname, i = f"{base}-{i}.html", i + 1
        used.add(fname)
        files[name] = fname
    return files


class _Links:
    """Filenames of the detail pages, keyed by event/handler/gap-pair name."""

    def __init__(self, data: dict, s: BuildSummary) -> None:
        self.events = _unique_files("event", event_names(data))
        self.handlers = _unique_files("handler", handler_names(data))
        self.gaps = _unique_files("gap", {f"{g.source} --> {g.target}" for g in s.gaps})
        self.gap_pairs = {
            (g.source, g.target): self.gaps[f"{g.source} --> {g.target}"]
            for g in s.gaps
        }

    def event_a(self, name: str) -> str:
        """An ``<a>`` to the event's detail page (or plain text if unknown)."""
        fname = self.events.get(name)
        if not fname:
            return escape(name)
        return f'<a href="{fname}">{escape(name)}</a>'

    def handler_a(self, name: str, event: str | None = None) -> str:
        """An ``<a>`` to the handler's detail page, optionally pre-filtered."""
        fname = self.handlers.get(name)
        if not fname:
            return escape(name)
        if event:
            fname += "?event=" + quote(event)
        return f'<a href="{fname}">{escape(name)}</a>'

    def gap_a(self, source: str, target: str, label: str) -> str:
        """An ``<a>`` to the gap pair's detail page (or plain text)."""
        fname = self.gap_pairs.get((source, target))
        if not fname:
            return escape(label)
        return f'<a href="{fname}">{escape(label)}</a>'


# ------------------------------------------------------------------- cells --


def _th(name: str) -> str:
    """Return a ``<th>`` with an "i" info button that explains the column on hover."""
    tip = escape(COLUMN_HELP.get(name, ""))
    return (
        f'<th>{escape(name)} <span class="info" tabindex="0">i'
        f'<span class="tip">{tip}</span></span></th>'
    )


def _num_td(v: float | None, fmt: str = "{:.6f}", suffix: str = "") -> str:
    """A right-aligned numeric ``<td>`` with a machine-sortable value."""
    if v is None:
        return "<td class='num' data-sort='-1'>-</td>"
    return f"<td class='num' data-sort='{v!r}'>{fmt.format(v)}{suffix}</td>"


def _page(
    title: str, current: str, body: str, s: BuildSummary, json_path: str = ""
) -> str:
    """Wrap ``body`` in the shared page shell (head, nav bar, footer)."""
    nav = "".join(
        f'<a href="{fname}"{' class="current"' if fname == current else ""}>{label}</a>'
        for fname, label in _PAGES
    )
    p = s.project_info
    project = escape(f"{p.get('name', '')} {p.get('version', '')}".strip())
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)} — sphinx-benchmark</title>
<link rel="stylesheet" href="style.css">
<script src="report.js" defer></script>
</head>
<body>
<header>
  <span class="brand">sphinx-benchmark</span>
  <span class="project">{project}</span>
  <nav>{nav}</nav>
  <span class="wall">wall clock {s.total_build_time:.3f}s</span>
</header>
<main>
{body}
</main>
<footer>Generated by sphinx-benchmark from {escape(json_path)}</footer>
</body>
</html>
"""


# ---------------------------------------------------------------- overview --


def _pie_slices(s: BuildSummary, links: _Links) -> list[tuple[str, float, str | None]]:
    """Return ``(label, seconds, detail-page filename)`` slices, descending.

    Slices are the events' own times plus the time outside events
    (startup, each gap pair, finish). Slices below 1% of the build are
    grouped into one "everything else" slice to keep the chart readable.
    """
    items: list[tuple[str, float, str | None]] = [
        (ev.name, ev.own_time, links.events.get(ev.name)) for ev in s.events
    ]
    items += [("gap: (startup)", s.startup, None), ("gap: (finish)", s.finish, None)]
    items += [
        (f"gap: {g.label}", g.total, links.gap_pairs.get((g.source, g.target)))
        for g in s.gaps
    ]

    items = [it for it in items if it[1] > 0]
    items.sort(key=lambda it: it[1], reverse=True)

    big = [it for it in items if s.pct(it[1]) >= 1.0]
    rest = sum(sec for _, sec, _ in items) - sum(sec for _, sec, _ in big)
    if rest > 0:
        big.append(("everything else (< 1% each)", rest, None))
    return big


def _overview_body(s: BuildSummary, links: _Links) -> str:
    slices = _pie_slices(s, links)
    stops, legend, acc = [], [], 0.0
    for i, (label, sec, href) in enumerate(slices):
        colour = PALETTE[i % len(PALETTE)]
        start, acc = acc, acc + s.pct(sec)
        # clamp both ends: decreasing stop positions are invalid CSS
        stops.append(f"{colour} {min(start, 100):.3f}% {min(acc, 100):.3f}%")
        text = f'<a href="{href}">{escape(label)}</a>' if href else escape(label)
        legend.append(
            f'<li><span class="swatch" style="background:{colour}"></span>'
            f'{text}<span class="num">{sec:.3f}s · {s.pct(sec):.2f}%</span></li>'
        )
    if acc < 100:  # rounding / unattributed remainder
        stops.append(f"#d9d9d9 {acc:.3f}% 100%")

    outside_pct = s.pct(s.total_build_time - s.time_in_events)
    p, b = s.project_info, s.build_info
    info = ""
    if p or b:
        info = f"""
<p class="meta">Project: <b>{escape(str(p.get("name", "-")))} {escape(str(p.get("version", "")))}</b>
 · HEAD: <code>{escape(str(p.get("HEAD") or "-"))}</code>
 · Builder: <b>{escape(str(b.get("builder", "-")))}</b>
 · Started: {escape(str(b.get("start_time", "-")))}</p>"""
    body = f"""
<h1>Where the build time went</h1>{info}
<div class="stats">
  <div><b>{s.total_build_time:.3f}s</b> wall clock</div>
  <div><b>{s.time_in_events:.3f}s</b> inside events (own time) ({s.pct(s.time_in_events):.2f}%)</div>
  <div><b>{s.total_build_time - s.time_in_events:.3f}s</b> outside any event ({outside_pct:.2f}%)</div>
</div>
<div class="pie-wrap">
  <div class="pie" style="background: conic-gradient({", ".join(stops)})"
       role="img" aria-label="Pie chart of build time share"></div>
  <ol class="legend">{"".join(legend)}</ol>
</div>
<p class="note">Slices are each event's <i>own</i> time (nested emissions excluded)
plus the gaps between top-level emissions, where Sphinx-core work such as
parsing and writing happens. See <a href="events.html">Events</a> and
<a href="gaps.html">Gaps</a> for the full tables; every name is a link to
its detailed breakdown.</p>
"""
    if s.overlaps:
        body += (
            f'<p class="warn">WARNING: {s.overlaps} negative gaps -- top-level '
            "emissions overlap, so these timings are unreliable</p>"
        )
    return body


# ------------------------------------------------------------------ events --


def _events_body(s: BuildSummary, links: _Links) -> str:
    header = "".join(
        _th(n)
        for n in ("Handler", "Kind", "Ext/Module", "Calls", "Total(s)", "Avg(ms)")
    )
    parts = [
        "<h1>Events &amp; handlers</h1> <p class='meta'>Note that the `unaccounted overhead` is `event_duration - sum_of_handlers` i.e. the time inside an event's `emit()` but outside any handler.</p><p class='meta'>The `depth` represents the nesting of events. `depth 0` means a top-level event emission. `depth 1` means emission happened from inside another event emission. And `depth 0-1` means that the event was a top-level event as well as a child event during the build process.</p><p class='meta'>Lastly, the `duration incl. nested events` is only displayed for events that had internal event emissions, and the `own_time` is the `duration_of_the_event - durations_of_child_events`</p>"
    ]
    for ev in s.events:
        depth = (
            str(ev.depth_min)
            if ev.depth_min == ev.depth_max
            else f"{ev.depth_min}–{ev.depth_max}"
        )
        nested = (
            f" · {ev.duration:.6f}s duration incl. nested events"
            if ev.has_nested
            else ""
        )
        rows = "".join(
            f"<tr><td>{links.handler_a(r.handler, ev.name)}</td><td>{escape(r.kind)}</td>"
            f"<td>{escape(r.extension)}</td>{_num_td(r.calls, '{:d}')}"
            f"{_num_td(r.total)}{_num_td(r.avg * 1000, '{:.3f}')}</tr>"
            for r in ev.handlers
        )
        parts.append(f"""
<section>
<h2>{links.event_a(ev.name)}</h2>
<p class="meta">{ev.own_time:.6f}s own time ({s.pct(ev.own_time):.2f}% of build)
 · {ev.emissions} emissions{nested} · depth {depth}</p>
<table class="sortable">
<thead><tr>{header}</tr></thead>
<tbody>{rows}
<tr class="total"><td>(sum of handlers)</td><td></td><td></td><td></td>
<td class="num">{ev.handler_sum:.6f}</td><td></td></tr>
<tr class="total"><td>(unaccounted overhead)</td><td></td><td></td><td></td>
<td class="num">{ev.overhead:.6f}</td><td></td></tr>
</tbody>
</table>
</section>""")
    return "".join(parts)


# -------------------------------------------------------------------- gaps --


def _gaps_body(s: BuildSummary, links: _Links, profile: Profile | None) -> str:
    header = "".join(
        _th(n) for n in ("Between", "Gap Total(s)", "Count", "Avg Gap(ms)", "% build")
    )

    def row(label_html: str, total: float, count: int | None) -> str:
        count_td = _num_td(count, "{:d}") if count is not None else "<td></td>"
        avg_td = _num_td(total / count * 1000, "{:.3f}") if count else "<td></td>"
        return (
            f"<tr><td>{label_html}</td>{_num_td(total)}"
            f"{count_td}{avg_td}"
            f"{_num_td(s.pct(total), '{:.2f}', '%')}</tr>"
        )

    rows = [row("(startup, before first emission)", s.startup, None)]
    rows += [
        row(links.gap_a(g.source, g.target, g.label), g.total, g.count) for g in s.gaps
    ]
    rows.append(row("(finish, after last emission)", s.finish, None))
    body = f"""
<h1>Gaps between emissions</h1>
<p class="meta">Sphinx emits events at fixed points in the build; the work between
two emissions (parsing, writing output) is not inside emit() and so is not
recorded per handler. These rows account for that time.</p>
<table class="sortable">
<thead><tr>{header}</tr></thead>
<tbody>{"".join(rows)}
<tr class="total"><td>(total outside events)</td>
<td class="num">{s.outside_events:.6f}</td><td></td><td></td>
<td class="num">{s.pct(s.outside_events):.2f}%</td></tr>
</tbody>
</table>
"""
    if s.overlaps:
        body += (
            f'<p class="warn">WARNING: {s.overlaps} negative gaps -- top-level '
            "emissions overlap, so these timings are unreliable</p>"
        )
    if profile is not None:
        body += _profile_section(profile, "gaps-tree.html").replace(
            "<h2>Where the time inside the gap goes</h2>",
            "<h2>Where the time of all gaps goes (startup included)</h2>",
            1,
        )
    return body


# ----------------------------------------------------------- detail pages --


def _event_page_body(
    name: str,
    rows: tuple,
    s: BuildSummary,
    links: _Links,
    profile: Profile | None = None,
    tree_page: str = "",
) -> str:
    header = "".join(
        _th(n)
        for n in (
            "Call#",
            "Start(s)",
            "Depth",
            "Parent event",
            "Duration(s)",
            "Own(s)",
            "% build",
        )
    )
    body_rows = []
    for r in rows:
        parent = links.event_a(r.parent_name) if r.parent_name else "(top-level)"
        pct = s.pct(r.own_time) if r.own_time is not None else None
        body_rows.append(
            f"<tr>{_num_td(r.call, '{:d}')}{_num_td(r.start)}"
            f"{_num_td(r.depth, '{:d}')}<td>{parent}</td>"
            f"{_num_td(r.duration)}{_num_td(r.own_time)}"
            f"{_num_td(pct, '{:.2f}', '%')}</tr>"
        )
    total_own = sum(r.own_time for r in rows if r.own_time is not None)
    in_progress = (
        "<p class='meta'>'-' marks an emission still in progress when the JSON "
        "was written (e.g. build-finished).</p>"
        if any(r.duration is None for r in rows)
        else ""
    )
    return f"""
<p class="backlink"><a href="events.html">← all events</a></p>
<h1>Event: {escape(name)}</h1>
<p class="meta">{len(rows)} recorded emissions · {total_own:.6f}s own time
 ({s.pct(total_own):.2f}% of build). Click a column heading to sort.</p>
<table class="sortable">
<thead><tr>{header}</tr></thead>
<tbody>{"".join(body_rows)}
<tr class="total"><td>(total)</td><td></td><td></td><td></td><td></td>
<td class="num">{total_own:.6f}</td>
<td class="num">{s.pct(total_own):.2f}%</td></tr>
</tbody>
</table>
{in_progress}{_profile_section(profile, tree_page) if profile is not None else ""}"""


def _handler_page_body(
    name: str,
    rows: tuple,
    s: BuildSummary,
    links: _Links,
    profile: Profile | None = None,
    tree_page: str = "",
) -> str:
    # default order: biggest share of the build first
    rows = tuple(sorted(rows, key=lambda r: r.duration, reverse=True))
    from_events = sorted({r.event for r in rows})
    options = '<option value="">All events</option>' + "".join(
        f'<option value="{escape(ev, quote=True)}">{escape(ev)}</option>'
        for ev in from_events
    )
    header_names = (
        "Event",
        "Call#",
        "Start(s)",
        "Duration(s)",
        "% build",
        "Kind",
        "Ext/Module",
        "Module",
    )
    header = "".join(
        # pre-mark the default sort column so its arrow shows
        _th(n).replace("<th>", '<th data-dir="desc">', 1)
        if n == "Duration(s)"
        else _th(n)
        for n in header_names
    )
    body_rows = "".join(
        f'<tr data-event="{escape(r.event, quote=True)}">'
        f"<td>{links.event_a(r.event)}</td>{_num_td(r.call, '{:d}')}"
        f"{_num_td(r.start)}{_num_td(r.duration)}"
        f"{_num_td(s.pct(r.duration), '{:.2f}', '%')}"
        f"<td>{escape(r.kind)}</td><td>{escape(r.extension)}</td>"
        f"<td>{escape(r.module)}</td></tr>"
        for r in rows
    )
    total = sum(r.duration for r in rows)
    return f"""
<p class="backlink"><a href="events.html">← all events</a></p>
<h1>Handler: {escape(name)}</h1>
<p class="meta">{len(rows)} recorded calls across {len(from_events)} event(s)
 · {total:.6f}s total ({s.pct(total):.2f}% of build). Rows are ordered by
duration (share of build) descending; click a column heading to re-sort.</p>
<p class="filter"><label>Show calls from event:
<select id="event-filter">{options}</select></label></p>
<p class="meta" id="filter-summary" hidden></p>
<noscript><p class="meta">Filtering and sorting require JavaScript; all
calls are shown.</p></noscript>
<table class="sortable">
<thead><tr>{header}</tr></thead>
<tbody>{body_rows}
<tr class="total"><td>(total, all events)</td><td></td><td></td>
<td class="num">{total:.6f}</td>
<td class="num">{s.pct(total):.2f}%</td><td></td><td></td><td></td></tr>
</tbody>
</table>{_profile_section(profile, tree_page) if profile is not None else ""}"""


def _gap_page_body(
    source: str,
    target: str,
    rows: tuple,
    s: BuildSummary,
    links: _Links,
    profile: Profile | None = None,
    tree_page: str = "",
) -> str:
    header = "".join(
        _th(n)
        for n in (
            "#",
            "First event emission#",
            "Second event emission#",
            "Gap start(s)",
            "Gap end(s)",
            "Duration(s)",
            "% build",
        )
    )
    body_rows = "".join(
        f"<tr>{_num_td(i, '{:d}')}{_num_td(r.source_call, '{:d}')}"
        f"{_num_td(r.target_call, '{:d}')}{_num_td(r.start)}{_num_td(r.end)}"
        f"{_num_td(r.duration)}{_num_td(s.pct(r.duration), '{:.2f}', '%')}</tr>"
        for i, r in enumerate(rows, 1)
    )
    total = sum(r.duration for r in rows)
    return f"""
<p class="backlink"><a href="gaps.html">← all gaps</a></p>
<h1>Gap: {links.event_a(source)} → {links.event_a(target)}</h1>
<p class="meta">During the build there were multiple times when the {source} and {target} events had a time gap between their consecutive emissions. In the table below, each row is one such gap.</p>
<p class="meta">{len(rows)} occurrences · {total:.6f}s total
 ({s.pct(total):.2f}% of build). Click a column heading to sort.</p>
<table class="sortable">
<thead><tr>{header}</tr></thead>
<tbody>{body_rows}
<tr class="total"><td>(total)</td><td></td><td></td><td></td><td></td>
<td class="num">{total:.6f}</td>
<td class="num">{s.pct(total):.2f}%</td></tr>
</tbody>
</table>{_profile_section(profile, tree_page) if profile is not None else ""}"""


# ----------------------------------------------------------------- profile --


def _profile_section(
    p: Profile, tree_page: str = "", min_total_pct: float = 0.5
) -> str:
    """The sampled breakdown of one gap, event, handler or the whole build
    (``p.scope``): a link to the call tree graph in ``tree_page`` (if
    any), and the functions with a total of at least ``min_total_pct``
    percent of it.
    """
    pct = f"% {p.scope}"
    func_header = "".join(
        _th(n)
        for n in (
            "Function",
            "Module",
            "Kind",
            "Self time(s)",
            pct,
            "Total time(s)",
            pct,
        )
    )
    func_rows = "".join(
        f'<tr><td title="{escape(f"{r.file}:{r.line}", quote=True)}">'
        f"{escape(r.function)}</td><td>{escape(r.module)}</td>"
        f"<td>{escape(r.kind)}</td>{_num_td(r.self_seconds)}"
        f"{_num_td(p.pct(r.self_seconds), '{:.2f}', '%')}"
        f"{_num_td(r.total_seconds)}{_num_td(p.pct(r.total_seconds), '{:.2f}', '%')}</tr>"
        for r in p.rows
        if p.pct(r.total_seconds) >= min_total_pct
    )
    tree = (
        f'<h3>Call tree</h3><p class="meta"><a href="{tree_page}">Open the call tree '
        f"as a graph</a>: which function called which, and how much of the {p.scope} "
        "went under each.</p>"
        if tree_page
        else ""
    )
    covers = {
        "gap": "during this gap",
        "event": "during this event's emissions, outside nested emissions",
        "handler": "while this handler was running, nested emissions included",
        "build": "during the build",
    }[p.scope]
    return f"""
<h2>Where the time inside the {p.scope} goes</h2>
<p class="meta">{p.samples} stack samples of the build thread were taken {covers}
({p.seconds:.6f}s), so the times below are estimates.
<b>Self</b> is time in a function's own code (calls into the Python standard
library count towards the caller); <b>Total</b> is the function and everything
it called.</p>
{tree}
<h3>Functions</h3>
<p class="meta">Functions with a total of at least {min_total_pct}% of the {p.scope}, by
self time; click a column heading to re-sort. Hover a function for its file and line.</p>
<table class="sortable">
<thead><tr>{func_header}</tr></thead>
<tbody>{func_rows}</tbody>
</table>"""


# --------------------------------------------------------------- tree graph --

#: Node colour per origin of the code.
_KIND_COLOUR = {
    "sphinx-internal": "#155e63",
    "extension": "#c26a2a",
    "theme": "#8a4fa2",
    "stdlib": "#8c8c8c",
}
_OTHER_COLOUR = "#3a6ea5"  # docutils, jinja2, pygments, ...

#: Node colour per where the build was when the samples were taken (the
#: whole-build tree), and what to call each in the legend and tooltips.
_WHERE_COLOUR = {
    "event": "#7fbde6",  # light blue: inside an event, outside its handlers
    "handler": "#c6dff2",  # lighter blue: inside a handler
    "gap": "#f6b8c6",  # light pink: between two emissions
    "other": "#c8c8c8",  # grey: neither
}
_WHERE_LABEL = {
    "event": "inside an event, outside its handlers",
    "handler": "inside a handler",
    "gap": "in a gap between emissions",
    "other": "neither (overlapping timings)",
}

_BOX_W, _BOX_H, _COL_GAP, _ROW_GAP = 250, 56, 14, 44


def _tree_graph_svg(p: Profile, min_pct: float = 1.0, by_where: bool = False) -> str:
    """Draw the call tree as an SVG, one box per function: the outermost
    function at the top, an arrow from each function down to the functions
    it called. Branches under ``min_pct`` percent of the profile are left
    out. Each box shows the function, its module, and its time and share;
    hovering shows where it is defined and its self time.

    Boxes are coloured by the origin of the code, or, with ``by_where``,
    by where the build mostly was when the function was sampled (inside a
    handler, inside an event but outside its handlers, in a gap, or
    neither), with the full split in the tooltip.
    """
    cutoff = p.seconds * min_pct / 100
    boxes: list[str] = []
    edges: list[str] = []
    next_col = 0
    depth_max = 0

    def place(n, depth: int) -> float:
        """Assign columns bottom-up: a box is centred over its children."""
        nonlocal next_col, depth_max
        depth_max = max(depth_max, depth)
        kids = [c for c in n.children if c.total_seconds >= cutoff]
        xs = [place(c, depth + 1) for c in kids]
        if xs:
            x = (xs[0] + xs[-1]) / 2
        else:
            x = next_col * (_BOX_W + _COL_GAP)
            next_col += 1
        y = depth * (_BOX_H + _ROW_GAP)
        if by_where and n.where_seconds:
            mostly = max(n.where_seconds, key=n.where_seconds.get)
            colour = _WHERE_COLOUR.get(mostly, _WHERE_COLOUR["other"])
        else:
            colour = _KIND_COLOUR.get(n.kind, _OTHER_COLOUR)
        name = n.function if len(n.function) <= 28 else n.function[:27] + "…"
        module = n.module if len(n.module) <= 32 else n.module[:31] + "…"
        tip = (
            f"{n.function}  [{n.module}]\n{n.file}:{n.line}\n"
            f"total {n.total_seconds:.3f}s ({p.pct(n.total_seconds):.2f}% of {p.scope})\n"
            f"self  {n.self_seconds:.3f}s ({p.pct(n.self_seconds):.2f}% of {p.scope})"
        )
        if by_where:
            tip += "".join(
                f"\n{secs:.3f}s {_WHERE_LABEL.get(w, w)}"
                for w, secs in sorted(
                    n.where_seconds.items(), key=lambda it: it[1], reverse=True
                )
            )
        boxes.append(
            f'<g class="node" transform="translate({x:.0f},{y:.0f})" '
            f'data-tip="{escape(tip, quote=True)}"><title>{escape(tip)}</title>'
            f'<rect width="{_BOX_W}" height="{_BOX_H}" rx="5" fill="{colour}"/>'
            f'<text x="8" y="16" class="fn">{escape(name)}</text>'
            f'<text x="8" y="31" class="num">{escape(module)}</text>'
            f'<text x="8" y="47" class="num">{n.total_seconds:.3f}s · '
            f"{p.pct(n.total_seconds):.1f}% of {escape(p.scope)}</text></g>"
        )
        for cx in xs:
            x1, y1 = x + _BOX_W / 2, y + _BOX_H
            x2, y2 = cx + _BOX_W / 2, y + _BOX_H + _ROW_GAP
            mid = (y1 + y2) / 2
            edges.append(
                f'<path d="M{x1:.0f},{y1:.0f} C{x1:.0f},{mid:.0f} {x2:.0f},{mid:.0f} '
                f'{x2:.0f},{y2 - 6:.0f}" marker-end="url(#arrow)"/>'
            )
        return x

    for root in p.tree:
        if root.total_seconds >= cutoff:
            place(root, 0)
    width = max(next_col, 1) * (_BOX_W + _COL_GAP)
    height = (depth_max + 1) * (_BOX_H + _ROW_GAP)
    cls = "tree light" if by_where else "tree"  # dark text on the pale colours
    return (
        f'<svg class="{cls}" xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="-4 -4 {width + 8} {height + 8}">'
        '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" '
        'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        '<path d="M0,0 L10,5 L0,10 z" fill="#5c6a6a"/></marker></defs>'
        f'<g class="edges">{"".join(edges)}</g>{"".join(boxes)}</svg>'
    )


def _tree_page_body(
    p: Profile, s: BuildSummary, back: str, back_label: str, by_where: bool = False
) -> str:
    if by_where:
        legend_items = [(_WHERE_LABEL[w], c) for w, c in _WHERE_COLOUR.items()]
        coloured = (
            "Boxes are coloured by where the build mostly was when the function "
            "was sampled (hover for the full split)"
        )
    else:
        legend_items = [
            *_KIND_COLOUR.items(),
            ("other libraries (docutils, jinja2, ...)", _OTHER_COLOUR),
        ]
        coloured = "Boxes are coloured by where the code comes from"
    legend = "".join(
        f'<li><span class="swatch" style="background:{c}"></span>{escape(k)}</li>'
        for k, c in legend_items
    )
    backlink = f'<p class="backlink"><a href="{back}">← {escape(back_label)}</a></p>'
    return f"""
{backlink if back else ""}
<h1>Call tree: {escape(p.label)}</h1>
<p class="meta">{p.seconds:.6f}s ({s.pct(p.seconds):.2f}% of the build), estimated
from {p.samples} stack samples. Read top to bottom: the box at the top is the
outermost function, each arrow points to a function it called, and every box shows
how much of the {escape(p.scope)} was spent in it and everything it called. Branches
under 1% of the {escape(p.scope)} are left out. {coloured}. <b>Hover a box</b> for
where the function is defined (file:line) and its self time.</p>
<ul class="legend inline">{legend}</ul>
<div class="tree-wrap">{_tree_graph_svg(p, by_where=by_where)}</div>
<div id="tip" class="tooltip" hidden></div>"""


# ------------------------------------------------------------- build tree --


def _build_tree_body(p: Profile | None, s: BuildSummary) -> str:
    if p is None:
        return """
<h1>Call tree: (whole build)</h1>
<p class="meta">The build was not sampled,
so there is no call tree.</p>"""
    return _tree_page_body(p, s, "", "", by_where=True) + _profile_section(p)


# ------------------------------------------------------------------- write --


def write_report(s: BuildSummary, out_dir: str, data: dict, json_path: str = "") -> str:
    """Write the report into ``out_dir`` and return that path.

    Parameters
    ----------
    s : BuildSummary
        The aggregated build data to render.
    out_dir : str
        Directory to write the pages and assets into. Created if missing.
    data : dict
        The raw benchmarks JSON, used for the per-event, per-handler and
        per-gap detail pages.
    json_path : str
        Path of the JSON, shown in the page footer.
    """
    os.makedirs(out_dir, exist_ok=True)
    links = _Links(data, s)

    def write(fname: str, content: str) -> None:
        with open(os.path.join(out_dir, fname), "w", encoding="utf-8") as f:
            f.write(content)

    write(
        "index.html",
        _page("Overview", "index.html", _overview_body(s, links), s, json_path),
    )
    write(
        "events.html",
        _page("Events", "events.html", _events_body(s, links), s, json_path),
    )
    # locate the stack snapshots once; every profile below is built from them
    frames = load_frames(data)
    profiles = gap_profiles(frames, s, data)
    combined = combined_gap_profile(profiles)
    write(
        "gaps.html",
        _page("Gaps", "gaps.html", _gaps_body(s, links, combined), s, json_path),
    )
    if combined is not None:
        body = _tree_page_body(combined, s, "gaps.html", "all gaps")
        write(
            "gaps-tree.html",
            _page("Call tree, all gaps", "gaps.html", body, s, json_path),
        )
    body = _build_tree_body(build_profile(frames, s, data), s)
    write("tree.html", _page("Call tree", "tree.html", body, s, json_path))
    write("style.css", _STYLE)
    write("report.js", _REPORT_JS)
    # group the raw records once; each detail page then renders its own rows
    emissions = all_emission_details(data)
    ev_profiles = event_profiles(frames, s)
    for name, fname in links.events.items():
        profile = ev_profiles.get(name)
        tree_fname = "event-tree-" + fname[len("event-") :]
        body = _event_page_body(
            name, emissions.get(name, ()), s, links, profile, tree_fname
        )
        write(fname, _page(f"Event {name}", "events.html", body, s, json_path))
        if profile is not None:
            body = _tree_page_body(profile, s, fname, f"event {name}")
            write(
                tree_fname,
                _page(f"Call tree {name}", "events.html", body, s, json_path),
            )
    calls = all_handler_call_details(data)
    h_profiles = handler_profiles(frames, s)
    for name, fname in links.handlers.items():
        profile = h_profiles.get(name)
        tree_fname = "handler-tree-" + fname[len("handler-") :]
        body = _handler_page_body(
            name, calls.get(name, ()), s, links, profile, tree_fname
        )
        write(fname, _page(f"Handler {name}", "events.html", body, s, json_path))
        if profile is not None:
            body = _tree_page_body(profile, s, fname, f"handler {name}")
            write(
                tree_fname,
                _page(f"Call tree {name}", "events.html", body, s, json_path),
            )
    occurrences = all_gap_occurrence_details(data)
    for (source, target), fname in links.gap_pairs.items():
        profile = profiles.get((source, target))
        tree_fname = "gap-tree-" + fname[len("gap-") :]
        body = _gap_page_body(
            source,
            target,
            occurrences.get((source, target), ()),
            s,
            links,
            profile,
            tree_fname,
        )
        write(fname, _page(f"Gap {source} → {target}", "gaps.html", body, s, json_path))
        if profile is not None:
            body = _tree_page_body(profile, s, fname, f"gap {source} → {target}")
            write(
                tree_fname,
                _page(
                    f"Call tree {source} → {target}", "gaps.html", body, s, json_path
                ),
            )
    return out_dir


_STYLE = """\
:root {
  --ink: #1c2222; --muted: #5c6a6a; --line: #d8dede;
  --bg: #fbfbf9; --panel: #ffffff; --accent: #155e63;
  --mono: "SF Mono", ui-monospace, "Cascadia Code", Consolas, monospace;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink);
  font: 15px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif; }
header { display: flex; align-items: baseline; gap: 1.5rem; flex-wrap: wrap;
  padding: .8rem 1.5rem; border-bottom: 2px solid var(--accent); background: var(--panel); }
.brand { font-family: var(--mono); font-weight: 700; color: var(--accent); }
.project { font-weight: 600; }
nav a { margin-right: 1rem; color: var(--muted); text-decoration: none; }
nav a.current { color: var(--accent); font-weight: 600;
  border-bottom: 2px solid var(--accent); }
.wall { margin-left: auto; font-family: var(--mono); color: var(--muted); }
main { max-width: 70rem; margin: 0 auto; padding: 1.5rem; }
h1 { font-size: 1.5rem; } h2 { font-size: 1.1rem; margin: 2rem 0 .2rem;
  font-family: var(--mono); color: var(--accent); }
.meta, .note, .backlink { color: var(--muted); font-size: .9rem; }
.warn { color: #a83a3a; font-weight: 600; }
.stats { display: flex; gap: 2.5rem; flex-wrap: wrap; margin: 1rem 0;
  font-family: var(--mono); }
.stats b { font-size: 1.25rem; display: block; }
table { border-collapse: collapse; width: 100%; background: var(--panel);
  font-size: .85rem; }
th, td { padding: .35rem .6rem; border-bottom: 1px solid var(--line);
  text-align: left; }
th { border-bottom: 2px solid var(--ink); white-space: nowrap; }
td.num { text-align: right; font-family: var(--mono); }
td a, h2 a, .legend a { color: var(--accent); }
tr.total td { border-top: 2px solid var(--line); color: var(--muted); }
h3 { font-size: 1rem; margin: 1.5rem 0 .3rem; }
/* call tree graph */
.tree-wrap { overflow: auto; background: var(--panel); border: 1px solid var(--line);
  padding: 1rem; }
svg.tree .edges path { fill: none; stroke: #5c6a6a; stroke-width: 1.2; }
svg.tree .node text { fill: #fff; font: 12px var(--mono); pointer-events: none; }
svg.tree .node .num, svg.tree .node .run { fill: #e8eeee; font-size: 10.5px; }
svg.tree.light .node text, svg.tree.light .node .num { fill: var(--ink); }
svg.tree .node:hover rect { stroke: var(--ink); stroke-width: 2; }
.legend.inline { display: flex; gap: 1.2rem; flex-wrap: wrap; max-width: none; margin: .5rem 0 1rem; }
.tooltip { position: fixed; z-index: 3; max-width: 40rem; padding: .5rem .7rem;
  background: var(--ink); color: #fff; font: .78rem/1.45 var(--mono);
  white-space: pre; border-radius: 4px; pointer-events: none; overflow: hidden; }
tbody tr:hover { background: #f1f5f4; }
/* click-to-sort column headings */
table.sortable th { cursor: pointer; -webkit-user-select: none; user-select: none; }
table.sortable th::after { content: " \\2195"; color: var(--muted); opacity: .45; }
table.sortable th[data-dir="asc"]::after { content: " \\2191"; color: var(--accent); opacity: 1; }
table.sortable th[data-dir="desc"]::after { content: " \\2193"; color: var(--accent); opacity: 1; }
.filter select { font: inherit; padding: .25rem .4rem; }
/* "i" info button with a hover/focus tooltip */
.info { position: relative; display: inline-block; width: 1.05em; height: 1.05em;
  line-height: 1.05em; text-align: center; border-radius: 50%;
  border: 1px solid var(--muted); color: var(--muted);
  font: italic 700 .7rem/1.35 Georgia, serif; cursor: help; }
.info .tip { display: none; position: absolute; left: 50%; top: 130%; z-index: 2;
  transform: translateX(-50%); width: 16rem; padding: .5rem .6rem;
  background: var(--ink); color: #fff; font: .78rem/1.4 system-ui, sans-serif;
  font-style: normal; border-radius: 4px; white-space: normal; }
.info:hover .tip, .info:focus .tip { display: block; }
/* pie */
.pie-wrap { display: flex; gap: 2.5rem; align-items: center; flex-wrap: wrap;
  margin: 1.5rem 0; }
.pie { width: 260px; height: 260px; border-radius: 50%;
  border: 1px solid var(--line); flex-shrink: 0; }
.legend { list-style: none; margin: 0; padding: 0; font-size: .85rem;
  max-width: 32rem; flex: 1; min-width: 18rem; }
.legend li { display: flex; align-items: baseline; gap: .5rem; padding: .15rem 0; }
.legend .num { margin-left: auto; font-family: var(--mono); color: var(--muted);
  white-space: nowrap; padding-left: 1rem; }
.swatch { width: .8rem; height: .8rem; border-radius: 2px; flex-shrink: 0;
  align-self: center; }
footer { text-align: center; color: var(--muted); font-size: .8rem;
  padding: 2rem 0; }
@media (max-width: 640px) { .wall { margin-left: 0; } main { padding: 1rem; } }
"""

_REPORT_JS = """\
// Click-to-sort for table.sortable and the per-handler event filter.
(function () {
  function cellKey(row, i) {
    var td = row.cells[i];
    if (!td) return "";
    var v = td.dataset.sort !== undefined ? td.dataset.sort : td.textContent.trim();
    // Number (not parseFloat) so "2captcha" stays text, not the number 2
    var n = v === "" ? NaN : Number(v);
    return !isNaN(n) ? n : v.toLowerCase();
  }
  function cmp(a, b) {
    return a < b ? -1 : a > b ? 1 : 0;
  }
  document.querySelectorAll("table.sortable").forEach(function (table) {
    var ths = table.querySelectorAll("thead th");
    ths.forEach(function (th, i) {
      th.addEventListener("click", function (ev) {
        if (ev.target.closest(".info")) return; // the "i" tooltip, not a sort
        var dir = th.dataset.dir === "desc" ? "asc" : "desc";
        ths.forEach(function (h) { h.removeAttribute("data-dir"); });
        th.dataset.dir = dir;
        var tbody = table.tBodies[0];
        var rows = Array.prototype.slice.call(tbody.rows);
        var totals = rows.filter(function (r) { return r.classList.contains("total"); });
        rows = rows.filter(function (r) { return !r.classList.contains("total"); });
        rows.sort(function (a, b) {
          var ka = cellKey(a, i), kb = cellKey(b, i);
          var na = typeof ka === "number", nb = typeof kb === "number";
          // numbers group above text/empty cells in both directions
          if (na !== nb) return na ? -1 : 1;
          return cmp(ka, kb) * (dir === "asc" ? 1 : -1);
        });
        rows.concat(totals).forEach(function (r) { tbody.appendChild(r); });
      });
    });
  });
  // hover tooltip for the call tree graph nodes
  var tip = document.getElementById("tip");
  if (tip) {
    document.querySelectorAll("svg.tree [data-tip]").forEach(function (node) {
      node.addEventListener("mouseenter", function () {
        tip.textContent = node.dataset.tip;
        tip.hidden = false;
      });
      node.addEventListener("mousemove", function (ev) {
        var x = ev.clientX + 14, y = ev.clientY + 14;
        if (x + tip.offsetWidth > window.innerWidth) x = ev.clientX - tip.offsetWidth - 14;
        if (y + tip.offsetHeight > window.innerHeight) y = ev.clientY - tip.offsetHeight - 14;
        tip.style.left = x + "px";
        tip.style.top = y + "px";
      });
      node.addEventListener("mouseleave", function () { tip.hidden = true; });
    });
  }
  var sel = document.getElementById("event-filter");
  if (sel) {
    var note = document.getElementById("filter-summary");
    var apply = function () {
      var v = sel.value, count = 0, total = 0;
      document.querySelectorAll("tbody tr[data-event]").forEach(function (tr) {
        var show = v === "" || tr.dataset.event === v;
        tr.style.display = show ? "" : "none";
        if (show) {
          count += 1;
          total += Number(tr.cells[3].dataset.sort) || 0; // Duration(s) column
        }
      });
      if (note) {
        note.hidden = v === "";
        if (v !== "") {
          note.textContent = "Filtered: " + count + " call(s) during '" + v +
            "' \\u00b7 " + total.toFixed(6) + "s; the totals above cover all events.";
        }
      }
    };
    // links from the events page pre-select one event via ?event=...
    var pre = new URLSearchParams(location.search).get("event");
    if (pre) {
      for (var i = 0; i < sel.options.length; i++) {
        if (sel.options[i].value === pre) { sel.value = pre; break; }
      }
    }
    sel.addEventListener("change", apply);
    apply();
  }
})();
"""
