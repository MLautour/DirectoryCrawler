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

from storage_report.config import DEFAULT_EXCLUDES, LEVELS, Config
from storage_report.model import (
    Node,
    NodeType,
    RootNode,
    ScanStats,
    SkippedPath,
    aggregate,
    sort_tree,
)

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
    "RootNode",
    "ScanStats",
    "SkippedPath",
    "aggregate",
    "sort_tree",
]

__version__ = "0.1.0"
