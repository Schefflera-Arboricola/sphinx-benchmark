from __future__ import annotations

import argparse
import difflib
import glob
import os
import sys

from .html import write_report
from .summary import (
    BenchmarkFileError,
    all_gap_profiles,
    combined_gap_profile,
    compute_summary,
    emission_details,
    event_names,
    gap_occurrence_details,
    gap_profile,
    handler_call_details,
    handler_names,
    load_records,
)
from .table import (
    print_build_info,
    print_emissions,
    print_events,
    print_gap_occurrences,
    print_gap_profile,
    print_gaps,
    print_handler_calls,
    print_overview,
    print_summary,
)

_TABLE_USAGE = """\
with no format:
  run                                top 10 events and gaps by % of build
  run --top N                        same, but show the N largest rows

table selectors:
  table                              both summary tables (events + gaps)
  table events                       only the per-event handler tables
  table events <event>               every emission of that event
  table events <handler>             every call of that handler (any event)
  table events <event> <handler>     that handler's calls during that event
  table gaps                         the gaps summary table, then the functions
                                     the gap time was sampled in (all gaps)
  table gaps <start-event> <end-event>   every gap between those two events, then
                                     the functions that gap's time was sampled in\
"""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sphinx-benchmark",
        description=(
            "Summarise the sphinx_benchmarks_<date>-<time>_<commit>.json "
            "written by the sphinx-benchmark extension during a Sphinx build."
        ),
    )
    parser.add_argument("--version", action="version", version="%(prog)s 0.1.0")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser(
        "run",
        help="render the recorded benchmarks",
        description=(
            "Render the recorded benchmarks as terminal tables or an HTML "
            "report. With no format, print the top events and gaps by % of "
            "build (see --top)."
        ),
        epilog=_TABLE_USAGE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    run.add_argument(
        "format",
        nargs="?",
        choices=("table", "html"),
        help=(
            "'table' prints summary/detail tables; 'html' writes a static "
            "report; omit to print the top events and gaps by %% of build"
        ),
    )
    run.add_argument(
        "selector",
        nargs="*",
        metavar="SELECTOR",
        help=(
            "what to show with 'table': events [EVENT|HANDLER "
            "[HANDLER]] | gaps [START END]; see the selector list below"
        ),
    )
    run.add_argument(
        "--top",
        type=int,
        default=None,
        metavar="N",
        help=(
            "with no format: how many of the largest events/gaps to show (default: 10)"
        ),
    )
    run.add_argument(
        "-i",
        "--input",
        default=None,
        metavar="JSON",
        help=(
            "path to the benchmarks JSON (default: the latest "
            "sphinx_benchmarks*.json in the current directory)"
        ),
    )
    run.add_argument(
        "-o",
        "--output-dir",
        default="sphinx_benchmark_report",
        metavar="DIR",
        help="directory for the HTML report; only used with 'html' (default: %(default)s)",
    )
    return parser


def _unknown_name(what: str, name: str, known: set[str]) -> str:
    """Build an error message for an unrecognised event/handler name."""
    msg = f"unknown {what}: {name!r}"
    close = difflib.get_close_matches(name, known, n=3)
    if close:
        msg += "; did you mean: " + ", ".join(close)
    return msg


def _run_table(
    parser: argparse.ArgumentParser, data: dict, selector: list[str]
) -> None:
    """Dispatch ``run table [selector...]`` to the right printer."""
    s = compute_summary(data)
    print_build_info(s)

    if not selector:
        print_summary(s)
        return

    view, rest = selector[0], selector[1:]

    if view == "gaps":
        if not rest:
            print()
            print("Build time: ", s.total_build_time)
            print_gaps(s)
            profile = combined_gap_profile(all_gap_profiles(data, s))
            if profile is not None:
                print_gap_profile(profile)
            return
        if len(rest) != 2:
            parser.error(
                "'table gaps' takes no arguments (summary) or exactly two: "
                "<start-event> <end-event>"
            )
        start, end = rest
        events = event_names(data)
        for name in (start, end):
            if name not in events:
                parser.error(_unknown_name("event", name, events))
        rows = gap_occurrence_details(data, start, end)
        if not rows:
            # a data error, not a usage error: exit 1 without the usage banner
            sys.exit(
                f"no top-level gaps recorded between {start!r} and {end!r}; "
                "run 'sphinx-benchmark run table gaps' to see the pairs that occurred"
            )
        print_gap_occurrences(start, end, rows, s)
        profile = gap_profile(data, s, start, end)
        if profile is not None:
            print_gap_profile(profile)
        return

    if view == "events":
        if not rest:
            print()
            print("Build time: ", s.total_build_time)
            print_events(s)
            return
        if len(rest) > 2:
            parser.error("'table events' takes at most two arguments")
        events, handlers = event_names(data), handler_names(data)
        if len(rest) == 2:
            event, handler = rest
            if event not in events:
                parser.error(_unknown_name("event", event, events))
            if handler not in handlers:
                parser.error(_unknown_name("handler", handler, handlers))
            rows = handler_call_details(data, handler, event)
            if not rows:
                # a data error, not a usage error: exit 1 without the usage banner
                sys.exit(f"handler {handler!r} was never called during {event!r}")
            print_handler_calls(handler, rows, s, event)
            return
        (name,) = rest
        if name in events:
            if name in handlers:
                print(
                    f"note: {name!r} is both an event and a handler; showing "
                    "the event's emissions",
                    file=sys.stderr,
                )
            print_emissions(name, emission_details(data, name), s)
            return
        if name in handlers:
            print_handler_calls(name, handler_call_details(data, name), s)
            return
        parser.error(_unknown_name("event or handler", name, events | handlers))

    parser.error(f"unknown table selector {view!r}: expected 'events' or 'gaps'")


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``sphinx-benchmark`` console script.
    Returns exit code 0 on success.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Validate argument combinations before touching the filesystem, so a
    # usage mistake is reported even when the JSON is missing.
    if args.top is not None and args.format is not None:
        parser.error(
            "--top is only valid without a format: 'sphinx-benchmark run --top N'"
        )
    top = args.top if args.top is not None else 10
    if args.format is None and top < 1:
        parser.error("--top must be at least 1")
    if args.format == "html" and args.selector:
        parser.error("'run html' takes no selector arguments")

    path = args.input
    if path is None:
        # picking the most recent json in the pwd
        candidates = glob.glob("sphinx_benchmarks*.json")
        if not candidates:
            sys.exit(
                "no sphinx_benchmarks*.json found in the current directory: run "
                "the build with the extension enabled, try changing the pwd, or "
                "pass -i"
            )
        path = max(candidates, key=os.path.getmtime)

    try:
        data = load_records(path)
    except BenchmarkFileError as e:
        sys.exit(str(e))

    if args.format is None:
        s = compute_summary(data)
        print_build_info(s)
        print_overview(s, top)
    elif args.format == "table":
        _run_table(parser, data, args.selector)
    else:
        out_dir = write_report(compute_summary(data), args.output_dir, data, path)
        print(f"Report written: open {os.path.join(out_dir, 'index.html')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
