"""storage_report -- scan a VFX asset hierarchy and render a size report.

Stdlib-only, DCC-agnostic. Never imports `hou` or any Qt binding; see
`tests/test_layering.py` for the enforced boundary.

Typical usage::

    from storage_report import crawler, html_report, Config

    tree = crawler.scan(root, Config())
    html_report.write(tree, "report.html")
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Callable

import glob as _glob
from pathlib import Path

from storage_report import archive, crawler, html_report, report_reader, variant_summary
from storage_report.config import DEFAULT_EXCLUDES, LEVELS, Config
from storage_report.crawler import Progress
from storage_report.report_reader import ReadReport, ReportFormatError, read_report
from storage_report.variant_summary import ReportStructureError, VariantSummary
from storage_report.model import (
    Node,
    NodeType,
    RootNode,
    ScanStats,
    SkippedPath,
    aggregate,
    sort_tree,
)

if TYPE_CHECKING:
    import threading
    from collections.abc import Sequence

# Library discipline: never configure handlers, levels, or formatters here.
# That is the host application's job -- a library that calls basicConfig()
# corrupts the logging setup of whatever process imported it (a DCC, a farm
# job, a test runner).
logging.getLogger(__name__).addHandler(logging.NullHandler())

__all__ = [
    "Config",
    "DEFAULT_EXCLUDES",
    "LEVELS",
    "Node",
    "NodeType",
    "Progress",
    "ReadReport",
    "ReportFormatError",
    "ReportStructureError",
    "RootNode",
    "ScanStats",
    "SkippedPath",
    "VariantSummary",
    "aggregate",
    "read_report",
    "run",
    "sort_tree",
    "summarize_report",
]

__version__ = "0.1.0"


def run(
    root: str | os.PathLike[str],
    output: str | os.PathLike[str],
    config: Config | None = None,
    *,
    max_locations: int | None = None,
    throttle_ms: float = 0.0,
    throttle_ratio: float = 0.0,
    progress_callback: Callable[[Progress], None] | None = None,
    cancel_event: "threading.Event | None" = None,
    title: str | None = None,
) -> RootNode:
    """Scan `root`, analyze archives, and write the HTML report to `output` in
    one call. Returns the scanned (aggregated) tree.

    `config` takes precedence when given; `max_locations`, `throttle_ms` and
    `throttle_ratio` are shortcuts for the common cases of wanting a safety
    valve or a gentle pace without building a full `Config`.
    """
    if config is None:
        config = Config(
            max_locations=max_locations,
            throttle_ms=throttle_ms,
            throttle_ratio=throttle_ratio,
        )
    tree = crawler.scan(root, config, progress_callback=progress_callback, cancel_event=cancel_event)
    archives = archive.analyze(tree, config)
    html_report.write(tree, output, archives=archives, title=title)
    return tree


_SUMMARY_STEM = "variant_summary"


def summarize_report(
    reports: "str | os.PathLike[str] | Sequence[str | os.PathLike[str]]",
    output: str | os.PathLike[str] | None = None,
    *,
    config: Config | None = None,
    variant_depth: int = variant_summary.DEFAULT_VARIANT_DEPTH,
) -> Path:
    """Merge already-generated HTML reports into one per-variant summary.

    `reports` accepts a directory (every ``*.html`` inside), a glob, a list of
    paths, or a single file. Each report contributes one TYPE, named from the
    last token of its scan root -- see docs/variant-summary-plan.md §0.

    Reads nothing but the report files: no re-scan, no storage access. Returns
    the path of the HTML written; a sibling ``.csv`` is written alongside.
    """
    config = config or Config()
    paths, discovered = _resolve_report_paths(reports)
    if not paths:
        raise ValueError(f"no report files found for {reports!r}")

    parsed: list[ReadReport] = []
    for path in paths:
        try:
            parsed.append(report_reader.read_report(path))
        except ReportFormatError:
            # Auto-discovered files may legitimately be other HTML; an
            # explicitly-listed file that cannot be parsed is an error.
            if not discovered:
                raise
            logging.getLogger(__name__).info("skipping non-report file %s", path)

    if not parsed:
        raise ValueError(f"none of the {len(paths)} file(s) found are storage_report reports")

    summaries, diagnostics = variant_summary.summarize(
        parsed, config=config, variant_depth=variant_depth
    )

    out_path = Path(output) if output is not None else paths[0].parent / f"{_SUMMARY_STEM}.html"
    html = variant_summary.render_html(
        summaries,
        diagnostics,
        sources=[p.source for p in parsed],
        title=f"Variant Summary — {len(summaries):,} variants",
    )
    tmp = out_path.with_name(f".{out_path.name}.tmp-{os.getpid()}")
    tmp.write_text(html, encoding="utf-8")
    os.replace(tmp, out_path)

    variant_summary.write_csv(summaries, out_path.with_suffix(".csv"))
    logging.getLogger(__name__).info(
        "variant summary written: %s (%d variants from %d report(s))",
        out_path, len(summaries), len(parsed),
    )
    return out_path


def _resolve_report_paths(reports) -> tuple[list[Path], bool]:
    """Return (paths, auto_discovered). Auto-discovered sets are filtered
    leniently; explicit lists are not.
    """
    if isinstance(reports, (list, tuple, set)):
        return [Path(r) for r in reports], False

    text = os.fspath(reports)
    if any(ch in text for ch in "*?["):
        return sorted(Path(p) for p in _glob.glob(text)), True

    path = Path(text)
    if path.is_dir():
        found = sorted(
            p for p in path.glob("*.html") if p.stem != _SUMMARY_STEM
        )
        return found, True
    return [path], False
