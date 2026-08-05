"""Shared, stdlib-only helpers: size/duration formatting, exclusion matching,
and path handling. Used by both `crawler` and `html_report`.
"""

from __future__ import annotations

import fnmatch
import os
import re
import sys
from typing import Callable

_SIZE_UNITS: tuple[str, ...] = ("B", "KB", "MB", "GB", "TB", "PB")


def format_size(num_bytes: int) -> str:
    """Render a byte count as a human-readable string, B through PB.

    Binary (1024) units. Sizes beyond 1 PB keep the "PB" suffix with a
    larger number rather than inventing an EB unit.
    """
    if num_bytes < 0:
        raise ValueError(f"format_size: num_bytes must be >= 0, got {num_bytes}")
    if num_bytes < 1024:
        return f"{num_bytes} B"
    size = float(num_bytes)
    for unit in _SIZE_UNITS[1:]:
        size /= 1024.0
        # Compare the *rounded* value against the next boundary: 1023.9990234375
        # KB naively looks like "< 1024" but rounds to "1024.00 KB", which reads
        # as if it belongs to the next unit. Roll over in that case instead.
        if unit == _SIZE_UNITS[-1] or round(size, 2) < 1024.0:
            return f"{size:.2f} {unit}"
    return f"{size:.2f} {_SIZE_UNITS[-1]}"  # unreachable: the loop always returns at the last unit


def format_duration(seconds: float) -> str:
    """Render a duration in seconds as e.g. "45s", "3m 07s", "1h 02m 03s"."""
    if seconds < 0:
        raise ValueError(f"format_duration: seconds must be >= 0, got {seconds}")
    total_seconds = int(seconds)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def build_exclusion_matcher(patterns: tuple[str, ...] | list[str]) -> Callable[[str], bool]:
    """Compile glob `patterns` (matched against a bare entry name) into one predicate.

    All patterns are combined into a single alternation regex via
    `fnmatch.translate` so the hot per-entry loop in the crawler pays for one
    regex match instead of N `fnmatch()` calls. Case-insensitive on Windows
    (matching filesystem semantics there), case-sensitive elsewhere.
    """
    patterns = tuple(patterns)
    if not patterns:
        return lambda name: False

    combined = "|".join(f"(?:{fnmatch.translate(p)})" for p in patterns)
    flags = re.IGNORECASE if sys.platform == "win32" else 0
    regex = re.compile(combined, flags)
    return lambda name: regex.match(name) is not None


_WINDOWS_EXTENDED_LOCAL_PREFIX = "\\\\?\\"
_WINDOWS_EXTENDED_UNC_PREFIX = "\\\\?\\UNC\\"


def normalize_root_for_scan(root: str) -> str:
    """Return an extended-length form of `root` on Windows so deep VFX trees don't
    silently truncate past MAX_PATH. A no-op on other platforms.
    """
    if sys.platform != "win32":
        return root

    path = os.path.abspath(root)
    if path.startswith(_WINDOWS_EXTENDED_LOCAL_PREFIX):
        return path
    if path.startswith("\\\\"):
        # UNC path: \\server\share\... -> \\?\UNC\server\share\...
        return _WINDOWS_EXTENDED_UNC_PREFIX + path[2:]
    return _WINDOWS_EXTENDED_LOCAL_PREFIX + path


def display_path(path: str) -> str:
    """Strip the Windows extended-length prefix added by `normalize_root_for_scan`
    so paths shown in the UI/report look normal. A no-op on other platforms
    and for paths that were never prefixed.
    """
    if path.startswith(_WINDOWS_EXTENDED_UNC_PREFIX):
        return "\\\\" + path[len(_WINDOWS_EXTENDED_UNC_PREFIX):]
    if path.startswith(_WINDOWS_EXTENDED_LOCAL_PREFIX):
        return path[len(_WINDOWS_EXTENDED_LOCAL_PREFIX):]
    return path


def _false(*_args: object, **_kwargs: object) -> bool:
    return False


def is_junction(entry: "os.DirEntry[str]") -> bool:
    """True for a Windows reparse point that `DirEntry.is_dir()` reports as a
    directory (junctions), so the crawler can skip it like a symlink.

    `DirEntry.is_junction()` exists from Python 3.12; on 3.11 this always
    returns False (junctions there fall through to a normal directory visit,
    same as historical `os.path.isdir()` behaviour).
    """
    return getattr(entry, "is_junction", _false)()
