"""Configuration for a storage_report scan.

`LEVELS` is the single source of truth for the structural hierarchy below the
scan root. Everything else -- node types, CSS colour classes, toolbar
buttons, progress fields, and the "Largest X" summary rows -- is derived from
this tuple at scan/render time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

LEVELS: list[str] = ["type", "asset", "variant"]

DEFAULT_EXCLUDES: list[str] = ["*.tmp", "*.bak", "Thumbs.db", "__pycache__", ".git"]


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
    max_folder_depth: int | None = None
    progress_interval: float = 0.5

    def __post_init__(self) -> None:
        if not self.levels:
            raise ValueError("Config.levels must contain at least one level name")
        if len(set(self.levels)) != len(self.levels):
            raise ValueError(f"Config.levels must be unique, got {self.levels!r}")
        if self.sort not in ("size", "name"):
            raise ValueError(f"Config.sort must be 'size' or 'name', got {self.sort!r}")
        if self.max_folder_depth is not None and self.max_folder_depth < 0:
            raise ValueError("Config.max_folder_depth must be >= 0 or None")
        if self.progress_interval < 0:
            raise ValueError("Config.progress_interval must be >= 0")
