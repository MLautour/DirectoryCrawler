"""scan() -- explicit-stack, single-threaded scandir DFS. See docs/implementation-plan.md
§5 for the full design rationale (metadata-operation budget, cancellation granularity,
why child lists are materialised instead of keeping scandir iterators live, etc).
"""

from __future__ import annotations

import gc
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    import threading

from storage_report.config import Config
from storage_report.model import Node, NodeType, RootNode, ScanStats, SkippedPath, aggregate, levels_of, sort_tree
from storage_report.utils import (
    build_exclusion_matcher,
    console_progress_reporter,
    display_path,
    is_junction,
    normalize_root_for_scan,
)

logger = logging.getLogger(__name__)

_MAX_SKIPPED = 10_000
_ROOT_FILES_NAME = "Root Files"


@dataclass(frozen=True, slots=True)
class Progress:
    levels: dict[str, str]
    current_folder: str
    files: int
    directories: int
    locations: int
    max_locations: int | None
    elapsed: float


def scan(
    root: str | os.PathLike[str],
    config: Config = Config(),
    progress_callback: Callable[[Progress], None] | None = None,
    cancel_event: "threading.Event | None" = None,
) -> RootNode:
    """Depth-first scan of `root`. Returns the aggregated, sorted tree with `.stats` attached.

    Never follows symlinks, never opens files, never stats a directory.
    Returns a partial tree with `stats.stopped_early = True` if `config.max_locations`
    is reached or `cancel_event` fires mid-scan.

    `config.throttle_ms` / `config.throttle_ratio` pause between directories to
    keep sustained load off a shared filer; see `Config` for the trade-off.
    """
    scan_root = normalize_root_for_scan(os.fspath(root))
    display_root = display_path(scan_root)
    is_excluded = build_exclusion_matcher(config.excludes)

    start_time = datetime.now()
    stats = ScanStats(
        root=display_root,
        start_time=start_time,
        end_time=None,
        total_files=0,
        total_dirs=0,
        total_size=0,
        skipped=[],
        skipped_total=0,
        stopped_early=False,
        levels=config.levels,
    )
    root_node = RootNode(name=display_root, type=NodeType.ROOT, depth=0, stats=stats)

    logger.info("scan started: root=%s", display_root)

    state = _ScanState()
    _callback = _CallbackGuard(progress_callback or console_progress_reporter)

    stack: list[tuple[Node, str, int]] = [(root_node, scan_root, 0)]

    started_monotonic = time.monotonic()
    last_progress_monotonic = started_monotonic

    throttle_floor = config.throttle_ms / 1000.0
    throttle_ratio = config.throttle_ratio
    throttling = throttle_floor > 0.0 or throttle_ratio > 0.0
    if throttling:
        logger.info(
            "throttling enabled: root=%s floor=%.0fms ratio=%.2f",
            display_root,
            config.throttle_ms,
            throttle_ratio,
        )

    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        while stack:
            if config.max_locations is not None and state.locations >= config.max_locations:
                stats.stopped_early = True
                logger.info("scan stopped early: root=%s reason=max_locations", display_root)
                break
            if cancel_event is not None and cancel_event.is_set():
                stats.stopped_early = True
                logger.info("scan stopped early: root=%s reason=cancelled", display_root)
                break

            node, path, depth = stack.pop()
            state.locations += 1
            directory_started = time.monotonic() if throttling else 0.0
            child_dirs: list[tuple[Node, str]] = []

            try:
                with os.scandir(path) as it:
                    for entry in it:
                        if is_excluded(entry.name):
                            continue
                        if entry.is_dir(follow_symlinks=False) and not is_junction(entry):
                            state.running_dirs += 1
                            child = Node(name=entry.name, type=_type_for(depth + 1, config), parent=node, depth=depth + 1)
                            child_dirs.append((child, entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            try:
                                size = entry.stat(follow_symlinks=False).st_size
                            except OSError as exc:
                                _record_skip(stats, entry.path, "os-error", str(exc))
                                continue
                            node.own_size += size
                            node.file_count += 1
                            state.running_files += 1
                            state.running_size += size
                        else:
                            _record_skip(stats, entry.path, "symlink")
            except PermissionError:
                _record_skip(stats, path, "permission")
            except FileNotFoundError:
                _record_skip(stats, path, "not-found")
            except OSError as exc:
                _record_skip(stats, path, "os-error", str(exc))
            else:
                node.children = [child for child, _ in child_dirs] or None
                stack.extend((child, child_path, depth + 1) for child, child_path in reversed(child_dirs))

            now = time.monotonic()
            if _callback and now - last_progress_monotonic >= config.progress_interval:
                last_progress_monotonic = now
                _callback(
                    Progress(
                        levels=levels_of(node, config.levels),
                        current_folder=display_path(path),
                        files=state.running_files,
                        directories=state.running_dirs,
                        locations=state.locations,
                        max_locations=config.max_locations,
                        elapsed=now - started_monotonic,
                    )
                )

            # Yield the filer between directories. Placed after progress so the
            # console still updates promptly, and last in the body so the pause
            # sits between two network operations rather than mid-directory.
            if throttling:
                pause = throttle_floor + (now - directory_started) * throttle_ratio
                if pause > 0.0:
                    state.throttled_seconds += pause
                    _pause(pause, cancel_event)
    finally:
        if gc_was_enabled:
            gc.enable()

    stats.end_time = datetime.now()
    stats.total_files = state.running_files
    stats.total_dirs = state.running_dirs
    stats.total_size = state.running_size
    stats.throttled_seconds = state.throttled_seconds

    _materialize_root_files(root_node)
    aggregate(root_node)
    sort_tree(root_node, config.sort)

    if _callback:
        _callback(
            Progress(
                levels=levels_of(root_node, config.levels),
                current_folder=display_root,
                files=state.running_files,
                directories=state.running_dirs,
                locations=state.locations,
                max_locations=config.max_locations,
                elapsed=time.monotonic() - started_monotonic,
            )
        )

    logger.info(
        "scan finished: root=%s files=%d dirs=%d size=%d stopped_early=%s skipped=%d",
        display_root,
        stats.total_files,
        stats.total_dirs,
        stats.total_size,
        stats.stopped_early,
        stats.skipped_total,
    )
    return root_node


@dataclass(slots=True)
class _ScanState:
    running_files: int = 0
    running_dirs: int = 0
    running_size: int = 0
    locations: int = 0
    throttled_seconds: float = 0.0


def _pause(seconds: float, cancel_event: "threading.Event | None") -> None:
    """Sleep between directories, but wake instantly if the scan is cancelled.

    `Event.wait(timeout)` returns as soon as the flag is set, so throttling
    costs nothing in cancellation latency. A plain `time.sleep` here would make
    Cancel feel broken: at a 250 ms pause per directory the worst case is a
    quarter-second of dead time on every single stop request.
    """
    if cancel_event is not None:
        cancel_event.wait(seconds)
    else:
        time.sleep(seconds)


class _CallbackGuard:
    """Wraps `progress_callback` so a slow or throwing callback can't break a scan.

    An exception is logged once and the callback is permanently disabled for
    the rest of the run.
    """

    __slots__ = ("_callback", "_disabled")

    def __init__(self, callback: Callable[[Progress], None] | None) -> None:
        self._callback = callback
        self._disabled = callback is None

    def __bool__(self) -> bool:
        return not self._disabled

    def __call__(self, progress: Progress) -> None:
        if self._disabled:
            return
        try:
            self._callback(progress)  # type: ignore[misc]
        except Exception:
            logger.exception("progress_callback raised; disabling it for the rest of this scan")
            self._disabled = True


def _type_for(depth: int, config: Config) -> str:
    if depth <= 0:
        return NodeType.ROOT
    if depth <= len(config.levels):
        return config.levels[depth - 1]
    return NodeType.FOLDER


def _record_skip(stats: ScanStats, path: str, reason: str, detail: str = "") -> None:
    stats.skipped_total += 1
    if len(stats.skipped) < _MAX_SKIPPED:
        stats.skipped.append(SkippedPath(path=display_path(path), reason=reason, detail=detail))
    logger.debug("skipped %s (%s): %s", path, reason, detail)


def _materialize_root_files(root: RootNode) -> None:
    """Insert a synthetic `root_files` child wherever a directory has direct files,
    at any structural level (root/type/asset/variant) that has them. `folder`
    nodes never get one -- their direct files stay folded into their own size.
    """
    stack: list[Node] = [root]
    while stack:
        node = stack.pop()
        if node.type not in (NodeType.FOLDER, NodeType.ROOT_FILES) and (
            node.own_size > 0 or node.file_count > 0
        ):
            root_files = Node(
                name=_ROOT_FILES_NAME,
                type=NodeType.ROOT_FILES,
                parent=node,
                depth=node.depth + 1,
                size=node.own_size,
                own_size=node.own_size,
                file_count=node.file_count,
            )
            if node.children is None:
                node.children = []
            node.children.append(root_files)
            node.own_size = 0
            node.file_count = 0
        if node.children:
            stack.extend(node.children)
