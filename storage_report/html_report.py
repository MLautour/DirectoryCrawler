"""write() -- render a scanned tree to one self-contained HTML file.

Stdlib only, no filesystem access beyond writing the output file. See
docs/implementation-plan.md §10 for the rendering strategy (embedded JSON +
lazy DOM) and why it matters at VFX-repository scale.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Literal

from storage_report.archive import ArchiveInfo, analyze as analyze_archives, summarize_by_asset
from storage_report.model import Node, RootNode, sort_tree
from storage_report.utils import format_duration, format_size

_GENERATOR_VERSION = "0.1.0"

_LEVEL_COLOR_CYCLE = ("#2563eb", "#059669", "#7c3aed", "#db2777", "#0891b2", "#ea580c")


def write(
    tree: RootNode,
    output_html: str | os.PathLike[str],
    *,
    archives: list[ArchiveInfo] | None = None,
    title: str | None = None,
    sort: Literal["size", "name"] | None = None,
) -> None:
    """Render `tree` to `output_html` as one self-contained HTML file.

    `sort` re-orders the tree in place before rendering (see `model.sort_tree`);
    omit it to keep whatever order the tree already has (typically whatever
    `crawler.scan` produced).

    `archives` defaults to `None`, in which case `archive.analyze(tree)` is
    called internally (with a default `Config`) so `write(tree, output_html)`
    alone still produces the full report. Callers using a non-default
    `Config` should run `archive.analyze(tree, config)` themselves and pass
    the result here, so archive matching (levels, ARCHIVE/RSTEXBIN names,
    date patterns) stays consistent with the scan.
    """
    if sort is not None:
        sort_tree(tree, sort)
    if archives is None:
        archives = analyze_archives(tree)

    levels = tree.stats.levels if tree.stats is not None else ()
    badge_text = _asset_badges(archives, levels)
    asset_level = levels[-2] if len(levels) >= 2 else None

    nodes, parents, type_names, largest, badges = _flatten(tree, badge_text, asset_level)
    meta = {"types": type_names}

    page = _render_page(
        nodes=nodes,
        parents=parents,
        meta=meta,
        badges=badges,
        tree=tree,
        levels=levels,
        largest=largest,
        archives=archives,
        title=title or f"Storage Report \u2014 {tree.path}",
    )

    output_path = Path(output_html)
    tmp_path = output_path.with_name(f".{output_path.name}.tmp-{os.getpid()}")
    tmp_path.write_text(page, encoding="utf-8")
    os.replace(tmp_path, output_path)


def _flatten(
    root: RootNode,
    badge_text: dict[str, list] | None = None,
    badge_level: str | None = None,
) -> tuple[list[list], list[int], list[str], dict[str, tuple[int, int]], dict[int, list]]:
    """Flatten the tree into parent-major, depth-first positional arrays.

    Returns `(nodes, parents, type_names, largest, badges)`:
      - `nodes[i]`   = [name, typeIndex, size, fileCount, [childIndex, ...]]
      - `parents[i]` = index of node i's parent, or -1 for the root
      - `type_names` = distinct `node.type` strings in first-seen order (typeIndex maps into this)
      - `largest`    = {type: (size, index)} the single biggest node seen per type
      - `badges`     = {nodeIndex: [text, tooltip, hasMarker]} for nodes at `badge_level`

    `badges` is a sparse map rather than a sixth element on every node: only a
    few thousand asset rows carry one, and an empty slot per node would add
    megabytes to the payload on a large repository.
    """
    nodes: list[list] = []
    parents: list[int] = []
    type_names: list[str] = []
    type_index: dict[str, int] = {}
    largest: dict[str, tuple[int, int]] = {}
    badges: dict[int, list] = {}

    def type_idx(t: str) -> int:
        idx = type_index.get(t)
        if idx is None:
            idx = len(type_names)
            type_index[t] = idx
            type_names.append(t)
        return idx

    # (node, parent_index); LIFO with a reversed child push keeps natural
    # left-to-right visiting order, same technique used in crawler/model.
    stack: list[tuple[Node, int]] = [(root, -1)]
    while stack:
        node, parent_idx = stack.pop()
        idx = len(nodes)
        nodes.append([node.name, type_idx(str(node.type)), node.size, node.file_count, []])
        parents.append(parent_idx)
        if parent_idx != -1:
            nodes[parent_idx][4].append(idx)

        current = largest.get(str(node.type))
        if current is None or node.size > current[0]:
            largest[str(node.type)] = (node.size, idx)

        # `node.path` is only touched for the handful of nodes at the badge
        # level, so the per-node path cache stays cold for the rest of the tree.
        if badge_text and badge_level is not None and str(node.type) == badge_level:
            entry = badge_text.get(node.path)
            if entry is not None:
                badges[idx] = entry

        if node.children:
            for child in reversed(node.children):
                stack.append((child, idx))

    return nodes, parents, type_names, largest, badges


def _asset_badges(archives: list[ArchiveInfo], levels: tuple[str, ...]) -> dict[str, list]:
    """Build `{asset_path: [text, tooltip, hasMarker]}` for the asset rows.

    Promotes the first-RSTEXBIN answer to the asset row so it is readable
    without expanding down to the variant's ARCHIVE folder. Assets with no
    ARCHIVE anywhere beneath them get no entry at all -- an absent badge means
    "nothing to say", which is distinct from the "NO RSTEXBIN" badge meaning
    "archives exist, none of them contain the marker".
    """
    summaries = summarize_by_asset(archives, levels)
    variant_label = levels[-1] if levels else "variant"

    badges: dict[str, list] = {}
    for asset_path, s in summaries.items():
        if s.first_rstexbin is not None:
            text = f"FIRST RSTEXBIN FROM {s.first_rstexbin}"
            tooltip = (
                f"{variant_label}: {s.first_rstexbin_variant} · "
                f"{s.rstexbin_count} of {s.archive_count} archives contain RSTEXBIN "
                f"across {s.variant_count} {variant_label}(s)"
            )
            badges[asset_path] = [text, tooltip, 1]
        else:
            text = "NO RSTEXBIN"
            tooltip = (
                f"{s.archive_count} dated archive(s) across {s.variant_count} "
                f"{variant_label}(s), none containing RSTEXBIN"
            )
            badges[asset_path] = [text, tooltip, 0]
    return badges


def _safe_json(obj: object) -> str:
    """`json.dumps` guarded against premature `</script>` closure, HTML comment
    injection, and the U+2028/U+2029 line separators that are illegal inside a
    JS string literal in some engines.
    """
    text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    text = text.replace("<!--", "<\\!--")
    text = text.replace("</", "<\\/")
    text = text.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return text


def _html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def _plural_label(level: str) -> str:
    label = level[:1].upper() + level[1:]
    return label if label.endswith("s") else label + "s"


def _iso(dt: datetime | None) -> str:
    return dt.isoformat(sep=" ", timespec="seconds") if dt is not None else "\u2014"


def _render_page(
    *,
    nodes: list[list],
    parents: list[int],
    meta: dict,
    badges: dict[int, list],
    tree: RootNode,
    levels: tuple[str, ...],
    largest: dict[str, tuple[int, int]],
    archives: list[ArchiveInfo],
    title: str,
) -> str:
    stats = tree.stats
    level_css = "\n".join(
        f'[data-type="{_html_escape(level)}"] {{ --type-color: '
        f"{_LEVEL_COLOR_CYCLE[i % len(_LEVEL_COLOR_CYCLE)]}; }}"
        for i, level in enumerate(levels)
    )

    toolbar_level_buttons = "\n".join(
        f'<button type="button" class="btn" data-action="expand-level" data-level="{i}">'
        f"Expand {_html_escape(_plural_label(level))}</button>\n"
        f'<button type="button" class="btn" data-action="collapse-level" data-level="{i}">'
        f"Collapse {_html_escape(_plural_label(level))}</button>"
        for i, level in enumerate(levels)
    )

    summary_rows = [
        ("Scan Root", _html_escape(tree.path)),
        ("Start Time", _iso(stats.start_time) if stats else "\u2014"),
        ("Finish Time", _iso(stats.end_time) if stats else "\u2014"),
        ("Duration", format_duration(stats.duration.total_seconds()) if stats else "\u2014"),
        ("Total Files", f"{stats.total_files:,}" if stats else "0"),
        ("Total Directories", f"{stats.total_dirs:,}" if stats else "0"),
        ("Total Storage", format_size(stats.total_size) if stats else format_size(tree.size)),
    ]
    if stats and stats.throttled_seconds > 0:
        # Only shown when throttling was on, so an unthrottled report stays uncluttered.
        share = stats.throttled_seconds / max(stats.duration.total_seconds(), 1e-9) * 100
        summary_rows.append(
            ("Paused by Throttle", f"{format_duration(stats.throttled_seconds)} ({share:.0f}% of run)")
        )
    for i, level in enumerate(levels):
        entry = largest.get(level)
        if entry is None:
            continue
        size, idx = entry
        name = nodes[idx][0]
        summary_rows.append(
            (f"Largest {level[:1].upper() + level[1:]}", f'{_html_escape(name)} \u2014 {format_size(size)}')
        )

    summary_html = "\n".join(
        f'<div class="summary-row"><span class="summary-label">{label}</span>'
        f'<span class="summary-value">{value}</span></div>'
        for label, value in summary_rows
    )

    stopped_early = bool(stats and stats.stopped_early)
    incomplete_banner = ""
    if stopped_early:
        incomplete_banner = (
            '<div class="banner banner-incomplete">INCOMPLETE SCAN \u2014 stopped early; '
            "totals below reflect only what was covered.</div>"
        )

    skipped_html = _render_skipped_section(stats.skipped, stats.skipped_total) if stats else ""
    archives_html = _render_archives_section(archives, levels, stopped_early)

    data_script = (
        f"const NODES={_safe_json(nodes)};\n"
        f"const PARENT={_safe_json(parents)};\n"
        f"const META={_safe_json(meta)};\n"
        f"const BADGES={_safe_json({str(k): v for k, v in badges.items()})};\n"
        f"const ARCHIVES={_safe_json(_archives_payload(archives, levels))};\n"
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_html_escape(title)}</title>
<style>
{_CSS}
{level_css}
</style>
</head>
<body>
{incomplete_banner}
<header class="toolbar">
  <input id="search" type="search" placeholder="Search names\u2026" autocomplete="off" aria-label="Search names">
  <button type="button" class="btn" data-action="expand-all">Expand All</button>
  <button type="button" class="btn" data-action="collapse-all">Collapse All</button>
  {toolbar_level_buttons}
  <span id="row-count" class="row-count" aria-live="polite"></span>
</header>
<section class="summary">
{summary_html}
</section>
<section class="tree-section">
  <div id="tree" class="tree" role="tree" aria-label="Directory hierarchy"></div>
  <div id="no-results" class="no-results" hidden>No names match your search.</div>
</section>
{archives_html}
{skipped_html}
<footer class="footer">
  <p>Generated by storage_report {_GENERATOR_VERSION}. Sizes are apparent size (<code>st_size</code>), not
  on-disk/allocated size. Hard-linked files are counted once per link, not deduplicated by inode.</p>
</footer>
<script>
{data_script}
{_JS}
</script>
</body>
</html>
"""


def _render_skipped_section(skipped: list, skipped_total: int) -> str:
    if skipped_total == 0:
        return ""
    caption = (
        f"Showing {len(skipped):,} of {skipped_total:,} skipped paths"
        if skipped_total > len(skipped)
        else f"{skipped_total:,} skipped paths"
    )
    rows = "\n".join(
        f"<tr><td>{_html_escape(s.reason)}</td><td>{_html_escape(s.path)}</td>"
        f"<td>{_html_escape(s.detail)}</td></tr>"
        for s in skipped
    )
    return f"""<details class="skipped">
  <summary>{caption}</summary>
  <table class="skipped-table">
    <thead><tr><th>Reason</th><th>Path</th><th>Detail</th></tr></thead>
    <tbody>
{rows}
    </tbody>
  </table>
</details>
"""


def _archives_payload(archives: list[ArchiveInfo], levels: tuple[str, ...]) -> list[dict]:
    payload = []
    for info in archives:
        if info.first_rstexbin_date is not None:
            first_label = f"{info.first_rstexbin_date.isoformat()} ({info.first_rstexbin_index} of {info.archive_count})"
        else:
            first_label = "none"
        payload.append(
            {
                "levels": [info.levels.get(level, "") for level in levels],
                "path": info.variant_path,
                "archiveCount": info.archive_count,
                "firstLabel": first_label,
                "rstexbinCount": info.rstexbin_count,
                "size": info.archive_size,
            }
        )
    return payload


def _render_archives_section(archives: list[ArchiveInfo], levels: tuple[str, ...], stopped_early: bool) -> str:
    if not archives:
        return ""

    n_variants = len(archives)
    total_dated = sum(a.archive_count for a in archives)
    n_with = sum(1 for a in archives if a.rstexbin_count > 0)
    n_without = n_variants - n_with

    partial = " (partial — some variants were never visited)" if stopped_early else ""

    level_headers = "\n".join(
        f'<th data-key="level:{i}">{_html_escape(level[:1].upper() + level[1:])}</th>'
        for i, level in enumerate(levels)
    )

    return f"""<section class="archives" id="archives-section">
  <h2>Archives{partial}</h2>
  <p class="archives-rollup">{n_variants:,} variants with an ARCHIVE · {total_dated:,} dated archives ·
  {n_with:,} variants contain RSTEXBIN · {n_without:,} do not.</p>
  <label class="archives-filter"><input type="checkbox" id="archives-no-rstexbin"> Show only variants with no RSTEXBIN</label>
  <table class="archives-table">
    <thead><tr>
{level_headers}
      <th data-key="archiveCount">Archives</th>
      <th data-key="firstLabel">First RSTEXBIN</th>
      <th data-key="rstexbinCount">RSTEXBIN Count</th>
      <th data-key="size">Archive Size</th>
    </tr></thead>
    <tbody id="archives-tbody"></tbody>
  </table>
  <p id="archives-row-count" class="row-count"></p>
</section>
"""


_CSS = """
:root {
  color-scheme: light dark;
  --bg: #ffffff;
  --fg: #1a1a1a;
  --muted: #6b7280;
  --border: #e2e2e2;
  --row-alt: rgba(0,0,0,0.03);
  --row-hover: rgba(37,99,235,0.10);
  --toolbar-bg: rgba(255,255,255,0.85);
  --accent: #2563eb;
  --heat: #ef4444;
  --mono: ui-monospace, "Cascadia Code", "SFMono-Regular", Consolas, "Liberation Mono", monospace;
  --type-root: #4b5563;
  --type-folder: #9ca3af;
  --type-root_files: #f59e0b;
  --banner-bg: #fef2f2;
  --banner-fg: #991b1b;
  --badge-ok-fg: #065f46;
  --badge-ok-bg: rgba(5,150,105,0.14);
  --badge-none-fg: #92400e;
  --badge-none-bg: rgba(245,158,11,0.16);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #14161a;
    --fg: #e5e7eb;
    --muted: #9ca3af;
    --border: #2a2d33;
    --row-alt: rgba(255,255,255,0.03);
    --row-hover: rgba(96,165,250,0.16);
    --toolbar-bg: rgba(20,22,26,0.85);
    --accent: #60a5fa;
    --heat: #f87171;
    --type-root: #9ca3af;
    --type-folder: #6b7280;
    --type-root_files: #fbbf24;
    --banner-bg: #3f1414;
    --banner-fg: #fca5a5;
    --badge-ok-fg: #6ee7b7;
    --badge-ok-bg: rgba(5,150,105,0.24);
    --badge-none-fg: #fcd34d;
    --badge-none-bg: rgba(245,158,11,0.20);
  }
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--fg);
  font: 14px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
}
.banner {
  padding: 10px 16px;
  font-weight: 600;
  text-align: center;
}
.banner-incomplete {
  background: var(--banner-bg);
  color: var(--banner-fg);
}
.toolbar {
  position: sticky;
  top: 0;
  z-index: 10;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  padding: 10px 16px;
  background: var(--toolbar-bg);
  backdrop-filter: blur(8px);
  border-bottom: 1px solid var(--border);
}
#search {
  flex: 1 1 220px;
  min-width: 140px;
  padding: 6px 10px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--bg);
  color: var(--fg);
  font: inherit;
}
.btn {
  padding: 6px 10px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--bg);
  color: var(--fg);
  font: inherit;
  cursor: pointer;
}
.btn:hover { background: var(--row-hover); }
.row-count {
  margin-left: auto;
  color: var(--muted);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.summary {
  padding: 14px 16px;
  border-bottom: 1px solid var(--border);
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 4px 20px;
}
.summary-row {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  padding: 2px 0;
}
.summary-label { color: var(--muted); }
.summary-value { font-family: var(--mono); font-variant-numeric: tabular-nums; text-align: right; }
.tree-section { padding: 8px 0 24px; }
.tree { padding: 4px 8px; }
[data-type="root"] { --type-color: var(--type-root); }
[data-type="folder"] { --type-color: var(--type-folder); }
[data-type="root_files"] { --type-color: var(--type-root_files); }
.row {
  display: grid;
  grid-template-columns: 20px 1fr auto;
  align-items: center;
  gap: 8px;
  padding: 3px 8px;
  padding-left: calc(8px + var(--depth) * 18px);
  border-left: 3px solid var(--type-color, var(--type-folder));
  border-radius: 4px;
  cursor: pointer;
  user-select: none;
}
.row:nth-child(even) { background: var(--row-alt); }
.row:hover, .row:focus { background: var(--row-hover); outline: none; }
.arrow {
  color: var(--muted);
  text-align: center;
  font-size: 11px;
}
.arrow.leaf { visibility: hidden; }
.name { display: flex; align-items: baseline; flex-wrap: wrap; gap: 8px; min-width: 0; }
.name-text { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.badge {
  flex: 0 0 auto;
  font-family: var(--mono);
  font-size: 11px;
  line-height: 1.6;
  letter-spacing: 0.02em;
  padding: 0 7px;
  border-radius: 999px;
  white-space: nowrap;
  cursor: help;
}
.badge-ok { color: var(--badge-ok-fg); background: var(--badge-ok-bg); }
.badge-none { color: var(--badge-none-fg); background: var(--badge-none-bg); }
.size {
  font-family: var(--mono);
  font-variant-numeric: tabular-nums;
  color: var(--muted);
  white-space: nowrap;
}
.no-results { padding: 24px; text-align: center; color: var(--muted); }
.archives { margin: 8px 16px 20px; border-top: 1px solid var(--border); padding-top: 16px; }
.archives h2 { font-size: 15px; margin: 0 0 6px; }
.archives-rollup { color: var(--muted); margin: 0 0 10px; }
.archives-filter { display: inline-block; margin-bottom: 10px; color: var(--muted); cursor: pointer; }
.archives-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.archives-table th, .archives-table td {
  text-align: left;
  padding: 5px 10px;
  border-bottom: 1px solid var(--border);
}
.archives-table th { cursor: pointer; color: var(--muted); user-select: none; white-space: nowrap; }
.archives-table th:hover { color: var(--fg); }
.archives-table td.num { font-family: var(--mono); font-variant-numeric: tabular-nums; text-align: right; }
.archives-table tr.no-rstexbin { background: var(--row-alt); }
.skipped { margin: 0 16px 16px; }
.skipped summary { cursor: pointer; color: var(--muted); padding: 8px 0; }
.skipped-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.skipped-table th, .skipped-table td {
  text-align: left;
  padding: 4px 8px;
  border-bottom: 1px solid var(--border);
  word-break: break-all;
}
.footer {
  padding: 16px;
  color: var(--muted);
  font-size: 12px;
  border-top: 1px solid var(--border);
}
@media (max-width: 720px) {
  .row { grid-template-columns: 20px 1fr; }
  .size { grid-column: 2; padding-left: calc(var(--depth) * 18px); }
}
"""


_JS = """
(function () {
  'use strict';

  var MAX_VISIBLE_ROWS_WITHOUT_CONFIRM = 25000;

  var treeEl = document.getElementById('tree');
  var searchInput = document.getElementById('search');
  var rowCountEl = document.getElementById('row-count');
  var noResultsEl = document.getElementById('no-results');

  var TYPE_NAMES = META.types;
  var N = NODES.length;

  var DEPTH = new Array(N);
  for (var i = 0; i < N; i++) {
    DEPTH[i] = PARENT[i] === -1 ? 0 : DEPTH[PARENT[i]] + 1;
  }

  var NAMES_LOWER = new Array(N);
  for (var j = 0; j < N; j++) {
    NAMES_LOWER[j] = NODES[j][0].toLowerCase();
  }

  var MAX_SIZE = N > 0 ? (NODES[0][2] || 1) : 1;

  var expanded = new Set(hasChildren(0) ? [0] : []);
  var savedExpanded = null;
  var filterQuery = '';
  var filterVisible = null; // Set of indices, or null when not filtering

  function hasChildren(idx) { return NODES[idx][4].length > 0; }
  function childrenOf(idx) { return NODES[idx][4]; }

  function esc(s) {
    return s.replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  var SIZE_UNITS = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
  function formatSize(numBytes) {
    if (numBytes < 1024) return numBytes + ' B';
    var size = numBytes;
    for (var u = 1; u < SIZE_UNITS.length; u++) {
      size /= 1024;
      var isLast = u === SIZE_UNITS.length - 1;
      var rounded = Math.round(size * 100) / 100;
      if (isLast || rounded < 1024) {
        return rounded.toFixed(2) + ' ' + SIZE_UNITS[u];
      }
    }
    return size.toFixed(2) + ' ' + SIZE_UNITS[SIZE_UNITS.length - 1];
  }

  function heatStyle(size) {
    var t = Math.log1p(size) / Math.log1p(MAX_SIZE || 1);
    var pct = (t * 55).toFixed(1);
    return 'background:color-mix(in oklab, var(--heat) ' + pct + '%, transparent)';
  }

  function expandedEffective(idx) {
    if (filterVisible) {
      var kids = childrenOf(idx);
      for (var k = 0; k < kids.length; k++) {
        if (filterVisible.has(kids[k])) return true;
      }
    }
    return expanded.has(idx);
  }

  function visibleList() {
    var out = [];
    (function walk(idx, depth) {
      if (filterVisible && !filterVisible.has(idx)) return;
      out.push({ index: idx, depth: depth });
      if (expandedEffective(idx)) {
        var kids = childrenOf(idx);
        for (var k = 0; k < kids.length; k++) walk(kids[k], depth + 1);
      }
    })(0, 0);
    return out;
  }

  function render() {
    if (N === 0) {
      treeEl.innerHTML = '';
      rowCountEl.textContent = '0 rows';
      return;
    }
    var rows = visibleList();
    noResultsEl.hidden = !(filterVisible && rows.length === 0);

    var html = '';
    for (var r = 0; r < rows.length; r++) {
      var idx = rows[r].index;
      var depth = rows[r].depth;
      var n = NODES[idx];
      var name = n[0], typeIdx = n[1], size = n[2];
      var expandable = hasChildren(idx);
      var open = expandedEffective(idx);
      var typeName = TYPE_NAMES[typeIdx];
      var arrow = expandable ? (open ? '\\u25be' : '\\u25b8') : '';
      // Asset rows carry the first-RSTEXBIN answer inline so it is readable
      // without expanding down to the variant's ARCHIVE folder.
      var badge = BADGES[idx];
      var badgeHtml = badge
        ? '<span class="badge badge-' + (badge[2] ? 'ok' : 'none') + '" title="' + esc(badge[1]) +
          '">(' + esc(badge[0]) + ')</span>'
        : '';
      html += '<div class="row" role="treeitem" data-idx="' + idx + '" data-type="' + esc(typeName) + '"' +
        ' style="--depth:' + depth + ';' + heatStyle(size) + '"' +
        ' aria-expanded="' + (expandable ? String(open) : 'false') + '"' +
        ' tabindex="-1">' +
        '<span class="arrow' + (expandable ? '' : ' leaf') + '">' + arrow + '</span>' +
        '<span class="name"><span class="name-text">' + esc(name) + '</span>' + badgeHtml + '</span>' +
        '<span class="size">' + formatSize(size) + '</span>' +
        '</div>';
    }
    treeEl.innerHTML = html;

    var rowEls = treeEl.querySelectorAll('.row');
    for (var e = 0; e < rowEls.length; e++) {
      rowEls[e].addEventListener('click', onRowClick);
    }
    if (rowEls.length > 0) rowEls[0].tabIndex = 0;

    rowCountEl.textContent = rows.length.toLocaleString() + (filterVisible ? ' matching rows' : ' rows');
  }

  function toggle(idx) {
    if (!hasChildren(idx)) return;
    if (expanded.has(idx)) expanded.delete(idx); else expanded.add(idx);
    render();
  }

  function onRowClick(e) {
    toggle(parseInt(e.currentTarget.getAttribute('data-idx'), 10));
  }

  function confirmLargeExpand(targetCount) {
    if (targetCount <= MAX_VISIBLE_ROWS_WITHOUT_CONFIRM) return true;
    return window.confirm(
      'This will show ' + targetCount.toLocaleString() + ' rows, which may be slow to render. Continue?'
    );
  }

  function expandAll() {
    if (!confirmLargeExpand(N)) return;
    var next = new Set();
    for (var i = 0; i < N; i++) if (hasChildren(i)) next.add(i);
    expanded = next;
    render();
  }

  function collapseAll() {
    expanded = new Set(hasChildren(0) ? [0] : []);
    render();
  }

  function expandToLevel(levelIdx) {
    var d = levelIdx + 1;
    var next = new Set();
    for (var i = 0; i < N; i++) {
      if (hasChildren(i) && DEPTH[i] < d) next.add(i);
    }
    expanded = next;
    render();
  }

  function collapseToLevel(levelIdx) {
    var d = levelIdx; // one shallower than expandToLevel
    var next = new Set();
    for (var i = 0; i < N; i++) {
      if (hasChildren(i) && DEPTH[i] < d) next.add(i);
    }
    expanded = next;
    render();
  }

  document.querySelectorAll('[data-action]').forEach(function (btn) {
    var action = btn.getAttribute('data-action');
    btn.addEventListener('click', function () {
      if (action === 'expand-all') expandAll();
      else if (action === 'collapse-all') collapseAll();
      else if (action === 'expand-level') expandToLevel(parseInt(btn.getAttribute('data-level'), 10));
      else if (action === 'collapse-level') collapseToLevel(parseInt(btn.getAttribute('data-level'), 10));
    });
  });

  var searchTimer = null;
  searchInput.addEventListener('input', function () {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(applySearch, 120);
  });

  function applySearch() {
    var q = searchInput.value.trim().toLowerCase();
    if (q === filterQuery) return;
    var entering = q !== '' && filterQuery === '';
    var leaving = q === '' && filterQuery !== '';
    filterQuery = q;

    if (entering) savedExpanded = new Set(expanded);

    if (q === '') {
      filterVisible = null;
      if (leaving && savedExpanded) {
        expanded = savedExpanded;
        savedExpanded = null;
      }
      render();
      return;
    }

    var matches = new Set();
    for (var i = 0; i < N; i++) {
      if (NAMES_LOWER[i].indexOf(q) !== -1) {
        matches.add(i);
        var p = PARENT[i];
        while (p !== -1 && !matches.has(p)) {
          matches.add(p);
          p = PARENT[p];
        }
      }
    }
    filterVisible = matches;
    render();
  }

  treeEl.addEventListener('keydown', function (e) {
    var rowEls = Array.prototype.slice.call(treeEl.querySelectorAll('.row'));
    var current = e.target.closest ? e.target.closest('.row') : null;
    var pos = current ? rowEls.indexOf(current) : -1;

    if (e.key === 'ArrowDown') {
      e.preventDefault();
      var down = Math.min(rowEls.length - 1, pos + 1);
      if (rowEls[down]) rowEls[down].focus();
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      var up = Math.max(0, pos - 1);
      if (rowEls[up]) rowEls[up].focus();
    } else if (e.key === 'ArrowRight' && current) {
      var idxR = parseInt(current.getAttribute('data-idx'), 10);
      if (hasChildren(idxR) && !expandedEffective(idxR)) toggle(idxR);
    } else if (e.key === 'ArrowLeft' && current) {
      var idxL = parseInt(current.getAttribute('data-idx'), 10);
      if (expandedEffective(idxL) && hasChildren(idxL)) {
        toggle(idxL);
      } else if (PARENT[idxL] !== -1) {
        var parentEl = treeEl.querySelector('[data-idx="' + PARENT[idxL] + '"]');
        if (parentEl) parentEl.focus();
      }
    } else if ((e.key === 'Enter' || e.key === ' ') && current) {
      e.preventDefault();
      toggle(parseInt(current.getAttribute('data-idx'), 10));
    }
  });

  render();
})();

(function () {
  'use strict';

  var tbody = document.getElementById('archives-tbody');
  if (!tbody) return; // section omitted: no variant had an ARCHIVE folder

  var filterCheckbox = document.getElementById('archives-no-rstexbin');
  var rowCountEl = document.getElementById('archives-row-count');
  var headers = document.querySelectorAll('.archives-table th[data-key]');

  var sortKey = 'path';
  var sortDir = 1;

  function esc(s) {
    return s.replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  var SIZE_UNITS = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
  function formatSize(numBytes) {
    if (numBytes < 1024) return numBytes + ' B';
    var size = numBytes;
    for (var u = 1; u < SIZE_UNITS.length; u++) {
      size /= 1024;
      var isLast = u === SIZE_UNITS.length - 1;
      var rounded = Math.round(size * 100) / 100;
      if (isLast || rounded < 1024) return rounded.toFixed(2) + ' ' + SIZE_UNITS[u];
    }
    return size.toFixed(2) + ' ' + SIZE_UNITS[SIZE_UNITS.length - 1];
  }

  function valueFor(row, key) {
    if (key.indexOf('level:') === 0) return row.levels[parseInt(key.slice(6), 10)] || '';
    return row[key];
  }

  function renderArchives() {
    var rows = ARCHIVES.slice();
    if (filterCheckbox.checked) rows = rows.filter(function (r) { return r.rstexbinCount === 0; });

    rows.sort(function (a, b) {
      var av = valueFor(a, sortKey), bv = valueFor(b, sortKey);
      if (av < bv) return -1 * sortDir;
      if (av > bv) return 1 * sortDir;
      return 0;
    });

    var html = '';
    for (var i = 0; i < rows.length; i++) {
      var r = rows[i];
      var cells = '';
      for (var l = 0; l < r.levels.length; l++) cells += '<td>' + esc(r.levels[l]) + '</td>';
      html += '<tr class="' + (r.rstexbinCount === 0 ? 'no-rstexbin' : '') + '">' + cells +
        '<td class="num">' + r.archiveCount.toLocaleString() + '</td>' +
        '<td>' + esc(r.firstLabel) + '</td>' +
        '<td class="num">' + r.rstexbinCount.toLocaleString() + '</td>' +
        '<td class="num">' + formatSize(r.size) + '</td>' +
        '</tr>';
    }
    tbody.innerHTML = html;
    rowCountEl.textContent = rows.length.toLocaleString() + ' of ' + ARCHIVES.length.toLocaleString() + ' variants';
  }

  headers.forEach(function (th) {
    th.addEventListener('click', function () {
      var key = th.getAttribute('data-key');
      if (sortKey === key) sortDir *= -1; else { sortKey = key; sortDir = 1; }
      renderArchives();
    });
  });
  filterCheckbox.addEventListener('change', renderArchives);

  renderArchives();
})();
"""
