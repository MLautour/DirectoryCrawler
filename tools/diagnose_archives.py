"""Explain why an ARCHIVE / RSTEXBIN badge is or is not appearing for a real root.

Run against the same root you scanned. Reports, stage by stage, where the chain
breaks -- hierarchy depth, ARCHIVE discovery, date parsing, marker detection --
so a missing badge points at a cause instead of a guess.

    python tools/diagnose_archives.py "\\\\nas\\projects\\assets"

Optional: --levels type,asset,variant  --max-locations 20000  --marker-recursive
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage_report import archive, crawler  # noqa: E402
from storage_report.config import Config  # noqa: E402
from storage_report.model import Node, NodeType  # noqa: E402


def _walk(node: Node):
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        if n.children:
            stack.extend(n.children)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("root")
    p.add_argument("--levels", default="type,asset,variant")
    p.add_argument("--max-locations", type=int, default=None)
    p.add_argument("--marker-recursive", action="store_true")
    p.add_argument("--archive-dir", default="ARCHIVE")
    p.add_argument("--marker", default="RSTEXBIN")
    args = p.parse_args()

    config = Config(
        levels=tuple(args.levels.split(",")),
        max_locations=args.max_locations,
        archive_dir=args.archive_dir,
        archive_marker=args.marker,
        archive_marker_recursive=args.marker_recursive,
        progress_interval=5.0,
    )

    print(f"root   : {args.root}")
    print(f"levels : {config.levels}")
    print(f"looking for '{config.archive_dir}' under level '{config.levels[-1]}', "
          f"marker '{config.archive_marker}' "
          f"({'anywhere beneath' if config.archive_marker_recursive else 'as a direct child of'} the dated folder)")
    print()

    tree = crawler.scan(args.root, config)
    print()

    # --- 1. did the hierarchy map onto the configured levels? ---------------
    by_type: dict[str, int] = {}
    for n in _walk(tree):
        by_type[str(n.type)] = by_type.get(str(n.type), 0) + 1
    print("1. nodes per level")
    for level in config.levels:
        count = by_type.get(level, 0)
        flag = "" if count else "   <-- NOTHING AT THIS LEVEL"
        print(f"     {level:<12} {count:>7,}{flag}")
    print(f"     {'folder':<12} {by_type.get('folder', 0):>7,}")
    if not by_type.get(config.levels[-1]):
        print("\n   STOP: no nodes at the last configured level, so no variant can own an")
        print("   ARCHIVE folder. Your --root is probably pointing at the wrong depth.")
        print("   Check that root/<level1>/<level2>/<level3> matches the real tree.")
        return 1

    # --- 2. where do folders named ARCHIVE actually live? -------------------
    target = config.archive_dir.lower()
    found: dict[str, int] = {}
    for n in _walk(tree):
        if n.type != NodeType.ROOT_FILES and n.name.lower() == target:
            parent_type = str(n.parent.type) if n.parent else "(none)"
            found[parent_type] = found.get(parent_type, 0) + 1
    print(f"\n2. folders named '{config.archive_dir}', by the level of their PARENT")
    if not found:
        print(f"     none found anywhere in the tree")
        print(f"\n   STOP: no folder named '{config.archive_dir}' was seen. Check the name,")
        print(f"   and check it is not being removed by Config.excludes.")
        return 1
    for parent_type, count in sorted(found.items(), key=lambda kv: -kv[1]):
        ok = " <-- counted" if parent_type == config.levels[-1] else " <-- IGNORED, wrong level"
        print(f"     under {parent_type:<12} {count:>7,}{ok}")

    # --- 3. the analysis itself --------------------------------------------
    archives = archive.analyze(tree, config)
    print(f"\n3. ArchiveInfo records built : {len(archives):,}")
    if not archives:
        print("   STOP: ARCHIVE folders exist but none sit directly under a "
              f"'{config.levels[-1]}' node (see step 2).")
        return 1

    total_dated = sum(a.archive_count for a in archives)
    parsed = sum(1 for a in archives for d in a.dated if d.date is not None)
    marked = sum(1 for a in archives for d in a.dated if d.has_marker)
    print(f"     dated folders inside them  : {total_dated:,}")
    print(f"       name parsed as a date    : {parsed:,}")
    print(f"       containing '{config.archive_marker}'    : {marked:,}")

    if total_dated and not parsed:
        print("\n   PROBLEM: no folder name parsed as a date, so nothing can be ordered")
        print("   and no 'first' exists. Sample names that failed:")
        for a in archives[:3]:
            for name in a.unparsed[:4]:
                print(f"       {name}")
        print("   Fix: add a matching regex to Config.archive_date_patterns.")

    if total_dated and not marked:
        print(f"\n   PROBLEM: no dated folder contains a '{config.archive_marker}' folder.")
        if not config.archive_marker_recursive:
            print("   If it is nested deeper than a direct child, re-run with --marker-recursive.")

    # --- 4. asset roll-up, i.e. what the badge shows ------------------------
    summaries = archive.summarize_by_asset(archives, config.levels)
    with_first = sum(1 for s in summaries.values() if s.first_rstexbin)
    undated = sum(1 for s in summaries.values() if not s.first_rstexbin and s.first_rstexbin_undated)
    none = len(summaries) - with_first - undated
    print(f"\n4. asset badges that will render : {len(summaries):,}")
    print(f"     'FIRST RSTEXBIN FROM ...'    : {with_first:,}")
    print(f"     'RSTEXBIN IN ... (UNDATED)'  : {undated:,}")
    print(f"     'NO RSTEXBIN'                : {none:,}")

    print("\n   examples:")
    for s in list(summaries.values())[:5]:
        label = (
            f"FIRST RSTEXBIN FROM {s.first_rstexbin}" if s.first_rstexbin
            else f"RSTEXBIN IN {s.first_rstexbin_undated} (UNDATED)" if s.first_rstexbin_undated
            else "NO RSTEXBIN"
        )
        print(f"     {s.asset_name:<24} ({label})")

    if tree.stats and tree.stats.stopped_early:
        print("\n   NOTE: the scan stopped early (max_locations), so assets never visited")
        print("   have no badge simply because they were not scanned.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
