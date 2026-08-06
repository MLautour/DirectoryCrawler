"""analyze() -- the ARCHIVE / RSTEXBIN post-pass. See docs/implementation-plan.md §7.

A pure walk over the tree `crawler.scan` already built -- zero additional
filesystem operations, so this studio-specific rule lives in one small module
that can change without touching traversal code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from storage_report.config import Config
from storage_report.model import Node, NodeType, RootNode, levels_of


@dataclass(frozen=True, slots=True)
class DatedArchive:
    name: str
    date: date | None
    size: int
    has_marker: bool


@dataclass(frozen=True, slots=True)
class ArchiveInfo:
    levels: dict[str, str]
    variant_path: str
    archive_size: int
    archive_count: int
    dated: tuple[DatedArchive, ...]
    unparsed: tuple[str, ...]
    first_rstexbin: str | None
    first_rstexbin_date: date | None
    first_rstexbin_index: int | None
    rstexbin_count: int


def analyze(tree: RootNode, config: Config = Config()) -> list[ArchiveInfo]:
    """For every variant containing an `ARCHIVE` folder, answer how many dated
    archives it has and which is the first one containing the marker folder.
    """
    variant_level = config.levels[-1]
    date_patterns = [re.compile(p) for p in config.archive_date_patterns]

    results: list[ArchiveInfo] = []
    stack: list[Node] = [tree]
    while stack:
        node = stack.pop()
        if node.children:
            stack.extend(node.children)
        if node.type != variant_level:
            continue
        archive_node = _find_child_ci(node, config.archive_dir)
        if archive_node is None:
            continue
        results.append(_build_archive_info(node, archive_node, config, date_patterns))

    results.sort(key=lambda info: info.variant_path)
    return results


def _find_child_ci(node: Node, name: str) -> Node | None:
    target = name.lower()
    for child in node.children or ():
        if child.type != NodeType.ROOT_FILES and child.name.lower() == target:
            return child
    return None


def _parse_date(name: str, patterns: list[re.Pattern[str]]) -> date | None:
    for pattern in patterns:
        match = pattern.match(name)
        if not match:
            continue
        groups = match.groupdict()
        try:
            return date(int(groups["y"]), int(groups["m"]), int(groups["d"]))
        except (KeyError, ValueError):
            continue
    return None


def _has_marker(dated_node: Node, config: Config) -> bool:
    target = config.archive_marker.lower()
    if not config.archive_marker_recursive:
        return any(
            child.type != NodeType.ROOT_FILES and child.name.lower() == target
            for child in dated_node.children or ()
        )
    stack: list[Node] = list(dated_node.children or ())
    while stack:
        node = stack.pop()
        if node.type != NodeType.ROOT_FILES and node.name.lower() == target:
            return True
        if node.children:
            stack.extend(node.children)
    return False


def _build_archive_info(
    variant_node: Node,
    archive_node: Node,
    config: Config,
    date_patterns: list[re.Pattern[str]],
) -> ArchiveInfo:
    dated: list[DatedArchive] = []
    for child in archive_node.children or ():
        if child.type == NodeType.ROOT_FILES:
            continue
        dated.append(
            DatedArchive(
                name=child.name,
                date=_parse_date(child.name, date_patterns),
                size=child.size,
                has_marker=_has_marker(child, config),
            )
        )

    # Parsed dates first (chronological), unparsed names last (alphabetical).
    # This ordering *is* `first_rstexbin_index`'s 1-based position.
    dated.sort(key=lambda a: (a.date is None, a.date or date.min, a.name.lower()))

    unparsed = tuple(a.name for a in dated if a.date is None)

    first: DatedArchive | None = None
    first_index: int | None = None
    for i, a in enumerate(dated, start=1):
        if a.date is not None and a.has_marker:
            first, first_index = a, i
            break

    return ArchiveInfo(
        levels=levels_of(variant_node, config.levels),
        variant_path=variant_node.path,
        archive_size=archive_node.size,
        archive_count=len(dated),
        dated=tuple(dated),
        unparsed=unparsed,
        first_rstexbin=first.name if first else None,
        first_rstexbin_date=first.date if first else None,
        first_rstexbin_index=first_index,
        rstexbin_count=sum(1 for a in dated if a.has_marker),
    )
