"""scan() -- explicit-stack, single-threaded scandir DFS. See docs/implementation-plan.md
§6 for the full design rationale (metadata-operation budget, cancellation granularity,
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
from storage_report.model import Node, NodeType, RootNode, ScanStats, SkippedPath, aggregate, sort_tree
from storage_report.utils import build_exclusion_matcher, display_path, is_junction, normalize_root_for_scan

logger = logging.getLogger(__name__)

_MAX_SKIPPED = 10_000
_ROOT_FILES_NAME = "Root Files"


@dataclass(frozen=True, slots=True)
class Progress:
    levels: dict[str, str]
    current_folder: str
    files: int
    directories: int
    elapsed: float
    completed_units: int
    total_units: int


def scan(
    root: str | os.PathLike[str],
    config: Config = Config(),
    progress_callback: Callable[[Progress], None] | None = None,
    cancel_event: "threading.Event | None" = None,
) -> RootNode:
    """Depth-first scan of `root`. Returns the aggregated, sorted tree with `.stats` attached.

    Never follows symlinks, never opens files, never stats a directory.
    Returns a partial tree with `stats.cancelled = True` if `cancel_event` is set mid-scan.
    """
    scan_root = normalize_root_for_scan(os.fspath(root))
    display_root = display_path(scan_root)
    is_excluded = build_exclusion_matcher(config.excludes)
    unit_depth = min(2, len(config.levels))

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
        cancelled=False,
        levels=config.levels,
    )
    root_node = RootNode(name=display_root, type=NodeType.ROOT, depth=0, stats=stats)

    logger.info("scan started: root=%s", display_root)

    state = _ScanState()
    _callback = _CallbackGuard(progress_callback)

    # Stack frames, one of:
    #   (node, path,   depth, "real")      -- scandir `path`; entries become `node`'s children
    #   (node, path,   depth, "phantom")   -- past max_folder_depth: scandir but fold into `node`
    #   (node, "",     0,     "unit_done") -- sentinel: `node`'s whole subtree finished (progress only)
    stack: list[tuple[Node, str, int, str]] = [(root_node, scan_root, 0, "real")]

    started_monotonic = time.monotonic()
    last_progress_monotonic = started_monotonic

    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        while stack:
            if cancel_event is not None and cancel_event.is_set():
                stats.cancelled = True
                logger.info("scan cancelled: root=%s", display_root)
                break

            node, path, depth, mode = stack.pop()

            if mode == "unit_done":
                state.completed_units += 1
                continue

            real = mode == "real"
            child_dirs: list[tuple[Node, str]] = []

            try:
                with os.scandir(path) as it:
                    for entry in it:
                        if is_excluded(entry.name):
                            continue
                        if entry.is_dir(follow_symlinks=False) and not is_junction(entry):
                            state.running_dirs += 1
                            if real:
                                _handle_real_subdir(entry, node, depth, config, child_dirs, stack)
                            else:
                                node.dir_count += 1
                                stack.append((node, entry.path, depth + 1, "phantom"))
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
                if real:
                    node.children = [child for child, _ in child_dirs] or None
                    _push_children(child_dirs, unit_depth, state, stack)

            now = time.monotonic()
            if _callback and now - last_progress_monotonic >= config.progress_interval:
                last_progress_monotonic = now
                _callback(
                    Progress(
                        levels=_levels_snapshot(node, config),
                        current_folder=display_path(path),
                        files=state.running_files,
                        directories=state.running_dirs,
                        elapsed=now - started_monotonic,
                        completed_units=state.completed_units,
                        total_units=state.total_units,
                    )
                )
    finally:
        if gc_was_enabled:
            gc.enable()

    stats.end_time = datetime.now()
    stats.total_files = state.running_files
    stats.total_dirs = state.running_dirs
    stats.total_size = state.running_size

    _materialize_root_files(root_node)
    aggregate(root_node)
    sort_tree(root_node, config.sort)

    if _callback:
        _callback(
            Progress(
                levels=_levels_snapshot(root_node, config),
                current_folder=display_root,
                files=state.running_files,
                directories=state.running_dirs,
                elapsed=time.monotonic() - started_monotonic,
                completed_units=state.completed_units,
                total_units=state.total_units,
            )
        )

    logger.info(
        "scan finished: root=%s files=%d dirs=%d size=%d cancelled=%s skipped=%d",
        display_root,
        stats.total_files,
        stats.total_dirs,
        stats.total_size,
        stats.cancelled,
        stats.skipped_total,
    )
    return root_node


@dataclass(slots=True)
class _ScanState:
    running_files: int = 0
    running_dirs: int = 0
    running_size: int = 0
    total_units: int = 0
    completed_units: int = 0


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


def _handle_real_subdir(
    entry: "os.DirEntry[str]",
    node: Node,
    depth: int,
    config: Config,
    child_dirs: list[tuple[Node, str]],
    stack: list[tuple[Node, str, int, str]],
) -> None:
    child_type = _type_for(depth + 1, config)
    if child_type == NodeType.FOLDER and config.max_folder_depth is not None:
        folder_depth = depth + 1 - len(config.levels)
        if folder_depth > config.max_folder_depth:
            # Cap reached: fold this subdirectory's contents into `node` instead of
            # creating a Node for it. `node` still receives credit for the directory.
            node.dir_count += 1
            stack.append((node, entry.path, depth + 1, "phantom"))
            return
    child = Node(name=entry.name, type=child_type, parent=node, depth=depth + 1)
    child_dirs.append((child, entry.path))


def _push_children(
    child_dirs: list[tuple[Node, str]],
    unit_depth: int,
    state: _ScanState,
    stack: list[tuple[Node, str, int, str]],
) -> None:
    to_push: list[tuple[Node, str, int, str]] = []
    for child, child_path in reversed(child_dirs):
        if child.depth == unit_depth:
            state.total_units += 1
            to_push.append((child, "", 0, "unit_done"))
        to_push.append((child, child_path, child.depth, "real"))
    stack.extend(to_push)


def _levels_snapshot(node: Node, config: Config) -> dict[str, str]:
    values: dict[str, str] = {}
    cur: Node | None = node
    while cur is not None:
        if cur.type in config.levels:
            values.setdefault(cur.type, cur.name)
        cur = cur.parent
    return {level: values.get(level, "") for level in config.levels}


def _record_skip(stats: ScanStats, path: str, reason: str, detail: str = "") -> None:
    stats.skipped_total += 1
    if len(stats.skipped) < _MAX_SKIPPED:
        stats.skipped.append(SkippedPath(path=display_path(path), reason=reason, detail=detail))
    logger.debug("skipped %s (%s): %s", path, reason, detail)


def _materialize_root_files(root: RootNode) -> None:
    """Insert a synthetic `root_files` child wherever a directory has direct files,
    at any structural level (root/type/asset/variant) that has them. `folder`
    nodes never get one -- their direct files stay folded into their own size.

    Must run after the whole tree is built (not per-directory during the DFS):
    with `max_folder_depth` capping, a node's own_size/file_count keep growing
    from folded-in descendants until its entire subtree has been walked.
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
