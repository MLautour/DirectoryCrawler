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

from storage_report import archive, crawler, html_report
from storage_report.config import DEFAULT_EXCLUDES, LEVELS, Config
from storage_report.crawler import Progress
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
    "RootNode",
    "ScanStats",
    "SkippedPath",
    "aggregate",
    "run",
    "sort_tree",
]

__version__ = "0.1.0"


def run(
    root: str | os.PathLike[str],
    output: str | os.PathLike[str],
    config: Config | None = None,
    *,
    max_locations: int | None = None,
    progress_callback: Callable[[Progress], None] | None = None,
    cancel_event: "threading.Event | None" = None,
    title: str | None = None,
) -> RootNode:
    """Scan `root`, analyze archives, and write the HTML report to `output` in
    one call. Returns the scanned (aggregated) tree.

    `config` takes precedence when given; `max_locations` is a shortcut for
    the common case of just wanting a safety valve without building a full
    `Config`.
    """
    if config is None:
        config = Config(max_locations=max_locations)
    tree = crawler.scan(root, config, progress_callback=progress_callback, cancel_event=cancel_event)
    archives = archive.analyze(tree, config)
    html_report.write(tree, output, archives=archives, title=title)
    return tree
