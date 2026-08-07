"""The tree data model. Stdlib only, no filesystem access -- `crawler` builds
this, `html_report` renders it, and nothing here knows how either happens.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal


class NodeType(StrEnum):
    """Fixed node-type constants. Structural level names (e.g. "asset",
    "variant") come from `Config.levels` and are plain strings, not members
    of this enum -- but they compare equal to themselves either way since
    this is a `StrEnum`.
    """

    ROOT = "root"
    FOLDER = "folder"
    ROOT_FILES = "root_files"


@dataclass(slots=True)
class Node:
    """One directory in the scanned hierarchy (or a synthetic `root_files` leaf).

    `size`/`file_count`/`dir_count` are aggregated totals (own + every
    descendant), valid only after `aggregate()` has run. Before that they
    hold the raw, directly-observed values the crawler recorded.
    """

    name: str
    type: str
    parent: "Node | None" = None
    size: int = 0
    own_size: int = 0
    file_count: int = 0
    dir_count: int = 0
    depth: int = 0
    children: list["Node"] | None = None
    _path: str | None = field(default=None, repr=False, init=False)

    @property
    def path(self) -> str:
        """Full path, derived from the ancestor chain and cached on first access."""
        if self._path is not None:
            return self._path

        names: list[str] = []
        node: Node | None = self
        while node is not None and node.parent is not None:
            names.append(node.name)
            node = node.parent
        root_name = node.name if node is not None else ""

        if names:
            names.reverse()
            result = os.path.join(root_name, *names)
        else:
            result = root_name

        self._path = result
        return result


@dataclass(slots=True)
class SkippedPath:
    path: str
    reason: Literal["permission", "not-found", "symlink", "os-error"]
    detail: str = ""


@dataclass(slots=True)
class ScanStats:
    root: str
    start_time: datetime
    end_time: datetime | None
    total_files: int
    total_dirs: int
    total_size: int
    skipped: list[SkippedPath] = field(default_factory=list)
    skipped_total: int = 0
    stopped_early: bool = False
    # Wall-clock seconds spent deliberately paused by the throttle, so a report
    # reader can tell a slow filer apart from a politely-paced scan.
    throttled_seconds: float = 0.0
    # The structural level names this scan was configured with (Config.levels),
    # e.g. ("type", "asset", "variant"). html_report reads this instead of
    # importing Config, keeping the model/render layers Config-independent and
    # avoiding fragile inference of level names by walking the tree.
    levels: tuple[str, ...] = ()

    @property
    def duration(self) -> timedelta:
        end = self.end_time if self.end_time is not None else self.start_time
        return end - self.start_time


@dataclass(slots=True)
class RootNode(Node):
    stats: ScanStats | None = None


def aggregate(root: RootNode) -> RootNode:
    """Roll `own_size`/`file_count`/(seeded) `dir_count` up into aggregated totals.

    Single iterative post-order pass (no recursion, so depth is bounded only
    by available memory, not the interpreter's call stack). `root_files`
    children contribute to `size`/`file_count` but are not directories, so
    they are excluded from `dir_count`.
    """
    # (node, children_processed)
    stack: list[tuple[Node, bool]] = [(root, False)]
    while stack:
        node, children_processed = stack.pop()
        if not children_processed:
            stack.append((node, True))
            if node.children:
                for child in node.children:
                    stack.append((child, False))
            continue

        size = node.own_size
        file_count = node.file_count
        dir_count = node.dir_count
        if node.children:
            for child in node.children:
                size += child.size
                file_count += child.file_count
                if child.type != NodeType.ROOT_FILES:
                    dir_count += 1 + child.dir_count
        node.size = size
        node.file_count = file_count
        node.dir_count = dir_count
    return root


def sort_tree(root: RootNode, key: Literal["size", "name"] = "size") -> RootNode:
    """Sort every node's children in place, largest-first or A-Z. `root_files`
    always sorts last within its siblings regardless of `key`, since it is a
    catch-all rather than a peer asset/variant.
    """
    stack: list[Node] = [root]
    while stack:
        node = stack.pop()
        if not node.children:
            continue
        if key == "size":
            node.children.sort(
                key=lambda n: (n.type == NodeType.ROOT_FILES, -n.size, n.name.lower())
            )
        else:
            node.children.sort(
                key=lambda n: (n.type == NodeType.ROOT_FILES, n.name.lower())
            )
        stack.extend(node.children)
    return root


def levels_of(node: Node, levels: tuple[str, ...]) -> dict[str, str]:
    """Walk `node`'s ancestor chain and collect `{level: name}` for each
    configured structural level found along the way. Shared by `crawler`
    (progress reporting) and `archive` (`ArchiveInfo.levels`) so both agree
    on exactly one way to answer "which type/asset/variant is this node under".
    """
    values: dict[str, str] = {}
    cur: Node | None = node
    while cur is not None:
        if cur.type in levels:
            values.setdefault(cur.type, cur.name)
        cur = cur.parent
    return {level: values.get(level, "") for level in levels}
