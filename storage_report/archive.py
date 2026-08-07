"""analyze() -- the ARCHIVE / RSTEXBIN post-pass. See docs/implementation-plan.md §7.

A pure walk over the tree `crawler.scan` already built -- zero additional
filesystem operations, so this studio-specific rule lives in one small module
that can change without touching traversal code.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
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
    # Path of the asset containing this variant, taken from the actual ancestor
    # node rather than string-manipulating `variant_path`, so it matches
    # `Node.path` exactly and can be used as a lookup key by the renderer.
    # Empty when the configured hierarchy has no level above the variant.
    asset_path: str = ""


@dataclass(frozen=True, slots=True)
class AssetArchiveSummary:
    """Every variant ARCHIVE beneath one asset, rolled up for display on the
    asset row. `first_rstexbin` is the earliest dated archive containing the
    marker across *all* of the asset's variants.
    """

    asset_path: str
    asset_name: str
    variant_count: int          # variants under this asset that have an ARCHIVE
    archive_count: int          # dated archives across those variants
    first_rstexbin: str | None
    first_rstexbin_date: date | None
    first_rstexbin_variant: str | None
    rstexbin_count: int
    # An archive that contains the marker but whose folder name did not parse
    # as a date. It cannot be "first" -- there is nothing to order it by -- but
    # reporting it as "no RSTEXBIN" would be a lie, so it is surfaced
    # separately. Alphabetically first when there are several.
    first_rstexbin_undated: str | None = None


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


def summarize_by_asset(
    archives: Sequence[ArchiveInfo], levels: tuple[str, ...]
) -> dict[str, AssetArchiveSummary]:
    """Roll variant-level `ArchiveInfo` up to the asset containing them.

    Keyed by asset path so a renderer can look up an asset node's summary by
    `Node.path` without walking the tree a second time. Takes `levels` rather
    than a `Config` so the render layer can stay Config-independent (it reads
    `ScanStats.levels`), matching how the rest of the report is built.

    An asset with several variants reports the earliest first-RSTEXBIN across
    all of them; ties break on variant name then archive name, so the result is
    deterministic regardless of scan or sort order.
    """
    if len(levels) < 2:
        return {}  # no level above the variant, so nothing to attach a summary to
    asset_level, variant_level = levels[-2], levels[-1]

    grouped: dict[str, list[ArchiveInfo]] = {}
    for info in archives:
        if info.asset_path:
            grouped.setdefault(info.asset_path, []).append(info)

    summaries: dict[str, AssetArchiveSummary] = {}
    for asset_path, infos in grouped.items():
        # (date, variant, archive name) -- all three comparable, so min() picks
        # the earliest with a stable tiebreak and never compares ArchiveInfo.
        candidates = [
            (i.first_rstexbin_date, i.levels.get(variant_level, ""), i.first_rstexbin or "")
            for i in infos
            if i.first_rstexbin_date is not None
        ]
        first = min(candidates) if candidates else None
        # Marker-bearing archives whose name did not parse as a date: real
        # RSTEXBIN folders that simply cannot be ordered.
        undated_marked = sorted(
            (d.name for i in infos for d in i.dated if d.date is None and d.has_marker),
            key=str.lower,
        )
        summaries[asset_path] = AssetArchiveSummary(
            asset_path=asset_path,
            asset_name=infos[0].levels.get(asset_level, ""),
            variant_count=len(infos),
            archive_count=sum(i.archive_count for i in infos),
            first_rstexbin=first[2] if first else None,
            first_rstexbin_date=first[0] if first else None,
            first_rstexbin_variant=first[1] if first else None,
            rstexbin_count=sum(i.rstexbin_count for i in infos),
            first_rstexbin_undated=undated_marked[0] if undated_marked else None,
        )
    return summaries


def _asset_path_of(variant_node: Node, levels: tuple[str, ...]) -> str:
    """Path of the nearest ancestor at the level directly above the variant."""
    if len(levels) < 2:
        return ""
    asset_level = levels[-2]
    cur: Node | None = variant_node.parent
    while cur is not None:
        if cur.type == asset_level:
            return cur.path
        cur = cur.parent
    return ""


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
        asset_path=_asset_path_of(variant_node, config.levels),
    )
