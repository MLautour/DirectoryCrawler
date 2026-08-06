"""Configuration for a storage_report scan.

`LEVELS` is the single source of truth for the structural hierarchy below the
scan root. Everything else -- node types, CSS colour classes, toolbar
buttons, progress fields, and the "Largest X" summary rows -- is derived from
this tuple at scan/render time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

LEVELS: list[str] = ["type", "asset", "variant"]

DEFAULT_EXCLUDES: list[str] = ["*.tmp", "*.bak", "Thumbs.db", "__pycache__", ".git"]

# Tried in order, first match wins. Each pattern captures y/m/d named groups;
# an optional trailing suffix (a version tag, a descriptive slug, a time
# stamp) is accepted but not parsed into the date. See plan §7.2.
DEFAULT_ARCHIVE_DATE_PATTERNS: tuple[str, ...] = (
    # 2026-08-07_10-30-00, 2026-08-07_10-30-00-noProcess. Must precede the
    # generic YYYY-MM-DD entry below, which matches these names too but
    # discards the time as an anonymous suffix.
    r"^(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})_(?P<H>\d{2})-(?P<M>\d{2})-(?P<S>\d{2})(?:-noProcess)?$",
    r"^(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})(?:[_-].+)?$",   # 2024-01-15, 2024-01-15_v002
    r"^(?P<y>\d{4})_(?P<m>\d{2})_(?P<d>\d{2})(?:[_-].+)?$",   # 2024_01_15
    r"^(?P<y>\d{4})(?P<m>\d{2})(?P<d>\d{2})_\d{4}$",          # 20240115_1430
    r"^(?P<y>\d{4})(?P<m>\d{2})(?P<d>\d{2})(?:[_-].+)?$",     # 20240115, 20240115-lighting
)


@dataclass(frozen=True, slots=True)
class Config:
    """Immutable scan configuration.

    `levels` maps filesystem depth below the root to a node type: depth 1 is
    `levels[0]`, depth 2 is `levels[1]`, and so on. Anything deeper than
    `len(levels)` is a generic, recursively-nested `folder` node.
    """

    levels: tuple[str, ...] = tuple(LEVELS)
    excludes: tuple[str, ...] = tuple(DEFAULT_EXCLUDES)
    sort: Literal["size", "name"] = "size"
    max_locations: int | None = None
    progress_interval: float = 2.0

    # --- archive analysis (plan §7) ---
    archive_dir: str = "ARCHIVE"
    archive_marker: str = "RSTEXBIN"
    archive_marker_recursive: bool = False
    archive_date_patterns: tuple[str, ...] = DEFAULT_ARCHIVE_DATE_PATTERNS

    def __post_init__(self) -> None:
        if not self.levels:
            raise ValueError("Config.levels must contain at least one level name")
        if len(set(self.levels)) != len(self.levels):
            raise ValueError(f"Config.levels must be unique, got {self.levels!r}")
        if self.sort not in ("size", "name"):
            raise ValueError(f"Config.sort must be 'size' or 'name', got {self.sort!r}")
        if self.max_locations is not None and self.max_locations < 0:
            raise ValueError("Config.max_locations must be >= 0 or None")
        if self.progress_interval < 0:
            raise ValueError("Config.progress_interval must be >= 0")
        for pattern in self.archive_date_patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"Config.archive_date_patterns: invalid regex {pattern!r}: {exc}") from exc
