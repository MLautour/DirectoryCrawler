"""Per-variant ARCHIVE/TEX/RSTEXBIN summary, derived from already-generated reports.

Reads nothing from the filesystem except the report files themselves. See
docs/variant-summary-plan.md.

Level identification is by DEPTH from each report's root, not by the type
strings stored in the report: those reports were scanned with the root pointing
at a type directory, so their recorded types are shifted by one and cannot be
trusted (plan §0).
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from storage_report.archive import compile_date_patterns, parse_archive_date
from storage_report.config import Config
from storage_report.model import Node, NodeType
from storage_report.report_reader import ReadReport
from storage_report.utils import format_size

logger = logging.getLogger(__name__)

#: Depth of a variant node below each report's root. Report root = TYPE,
#: depth 1 = ASSET, depth 2 = VARIANT.
DEFAULT_VARIANT_DEPTH = 2

#: A node at the variant depth should have at least one of these children.
#: Used to validate the depth mapping rather than trust it.
_VARIANT_MARKERS = ("archive", "tex", "rstexbin")

_ROOT_FILES_LABEL = "Root Files"


class ReportStructureError(ValueError):
    """A report's hierarchy does not sit at the expected depth."""


@dataclass(frozen=True, slots=True)
class VariantSummary:
    """One line of the output report."""

    type_name: str
    asset_name: str
    variant_name: str
    first_archive_with_marker: str | None   # dated folder name, ARCHIVE only
    first_archive_date: date | None
    first_archive_index: int | None         # 1-based position among dated archives
    first_archive_undated: str | None       # marker present but name unorderable
    dated_archive_count: int
    tex_size: int
    root_files_size: int
    rstexbin_size: int                      # VARIANT/RSTEXBIN, size only
    total_size: int

    @property
    def first_label(self) -> str:
        if self.first_archive_with_marker is not None:
            return self.first_archive_with_marker
        if self.first_archive_undated is not None:
            return f"{self.first_archive_undated} (undated)"
        return "—"


@dataclass(frozen=True, slots=True)
class ReportDiagnostics:
    """What was actually found in one report, so an empty result is explicable."""

    source: str
    type_name: str
    variants: int
    with_archive: int
    dated_total: int
    dated_parsed: int
    dated_with_marker: int
    variants_with_rstexbin: int
    variants_with_tex: int
    unparsed_samples: tuple[str, ...]


def summarize(
    reports: list[ReadReport],
    *,
    config: Config = Config(),
    variant_depth: int = DEFAULT_VARIANT_DEPTH,
) -> tuple[list[VariantSummary], list[ReportDiagnostics]]:
    """Compute one `VariantSummary` per variant across every report."""
    patterns = compile_date_patterns(config)
    summaries: list[VariantSummary] = []
    diagnostics: list[ReportDiagnostics] = []

    for report in reports:
        variants = list(_nodes_at_depth(report.tree, variant_depth))
        _validate_depth(report, variants, variant_depth)

        unparsed: list[str] = []
        with_archive = dated_total = dated_parsed = dated_marked = 0
        with_rstexbin = with_tex = 0

        for asset_name, variant in variants:
            summary, stats = _summarize_variant(
                report.type_name, asset_name, variant, config, patterns
            )
            summaries.append(summary)
            with_archive += stats["has_archive"]
            dated_total += summary.dated_archive_count
            dated_parsed += stats["parsed"]
            dated_marked += stats["marked"]
            with_rstexbin += 1 if summary.rstexbin_size else 0
            with_tex += 1 if summary.tex_size else 0
            unparsed.extend(stats["unparsed"])

        diagnostics.append(
            ReportDiagnostics(
                source=report.source,
                type_name=report.type_name,
                variants=len(variants),
                with_archive=with_archive,
                dated_total=dated_total,
                dated_parsed=dated_parsed,
                dated_with_marker=dated_marked,
                variants_with_rstexbin=with_rstexbin,
                variants_with_tex=with_tex,
                unparsed_samples=tuple(sorted(set(unparsed))[:10]),
            )
        )

    summaries.sort(key=lambda s: (s.type_name.lower(), s.asset_name.lower(), s.variant_name.lower()))
    return summaries, diagnostics


def _nodes_at_depth(root: Node, depth: int):
    """Yield `(parent_name, node)` for every real directory at `depth`.

    Synthetic `root_files` nodes are skipped: they are display artefacts, not
    directories, and would otherwise be mistaken for assets or variants.
    """
    stack: list[tuple[Node, int]] = [(root, 0)]
    while stack:
        node, current = stack.pop()
        if current == depth:
            yield (node.parent.name if node.parent is not None else ""), node
            continue
        for child in node.children or ():
            if child.type != NodeType.ROOT_FILES:
                stack.append((child, current + 1))


def _validate_depth(report: ReadReport, variants: list, depth: int) -> None:
    """Refuse to emit a plausible-but-wrong report when the depth mapping is off.

    A variant should own at least one of ARCHIVE/TEX/RSTEXBIN. If not one node
    at this depth does, the report was almost certainly scanned from a different
    level than the others and its rows would be silently shifted.
    """
    if not variants:
        return
    if any(_looks_like_variant(node) for _, node in variants):
        return

    samples = []
    for d in range(0, depth + 3):
        names = [n.name for _, n in _nodes_at_depth(report.tree, d)][:6]
        if names:
            samples.append(f"    depth {d}: {', '.join(names)}")
    raise ReportStructureError(
        f"{report.source}: none of the {len(variants)} nodes at depth {depth} contains an "
        f"ARCHIVE, TEX or RSTEXBIN folder, so depth {depth} is probably not the variant level "
        f"in this report.\n  Scan root: {report.scan_root}\n  Sample names per depth:\n"
        + "\n".join(samples)
        + "\n  Pass variant_depth=<n> to override."
    )


def _looks_like_variant(node: Node) -> bool:
    return any((child.name.lower() in _VARIANT_MARKERS) for child in node.children or ())


def _child_named(node: Node, name: str) -> Node | None:
    """Case-insensitive child lookup. Folder case drifts on a share, and a
    case-sensitive miss would fail silently.
    """
    target = name.lower()
    for child in node.children or ():
        if child.type != NodeType.ROOT_FILES and child.name.lower() == target:
            return child
    return None


def _root_files_child(node: Node) -> Node | None:
    for child in node.children or ():
        if child.type == NodeType.ROOT_FILES:
            return child
    return None


def _summarize_variant(
    type_name: str, asset_name: str, variant: Node, config: Config, patterns: list
) -> tuple[VariantSummary, dict]:
    tex = _child_named(variant, "TEX")
    rstexbin = _child_named(variant, config.archive_marker)   # VARIANT/RSTEXBIN
    root_files = _root_files_child(variant)
    archive = _child_named(variant, config.archive_dir)

    dated: list[tuple[str, date | None, bool]] = []
    if archive is not None:
        for child in archive.children or ():
            if child.type == NodeType.ROOT_FILES:
                continue
            # Only markers inside ARCHIVE/<dated>/ count. The variant's own
            # RSTEXBIN is reported for its size and never as a "first" candidate.
            has_marker = _child_named(child, config.archive_marker) is not None
            dated.append((child.name, parse_archive_date(child.name, patterns), has_marker))

    # Parsed dates first (chronological), unparsed last (alphabetical); this
    # ordering is what `first_archive_index` counts within.
    dated.sort(key=lambda d: (d[1] is None, d[1] or date.min, d[0].lower()))

    first_name = first_date = first_index = None
    for position, (name, parsed, has_marker) in enumerate(dated, start=1):
        if parsed is not None and has_marker:
            first_name, first_date, first_index = name, parsed, position
            break

    undated = next((n for n, parsed, marked in dated if parsed is None and marked), None)

    summary = VariantSummary(
        type_name=type_name,
        asset_name=asset_name,
        variant_name=variant.name,
        first_archive_with_marker=first_name,
        first_archive_date=first_date,
        first_archive_index=first_index,
        first_archive_undated=undated if first_name is None else None,
        dated_archive_count=len(dated),
        tex_size=tex.size if tex else 0,
        root_files_size=root_files.size if root_files else 0,
        rstexbin_size=rstexbin.size if rstexbin else 0,
        total_size=variant.size,
    )
    stats = {
        "has_archive": 1 if archive is not None else 0,
        "parsed": sum(1 for _, parsed, _ in dated if parsed is not None),
        "marked": sum(1 for _, _, marked in dated if marked),
        "unparsed": [n for n, parsed, _ in dated if parsed is None],
    }
    return summary, stats


# --------------------------------------------------------------------------
# CSV
# --------------------------------------------------------------------------

CSV_COLUMNS = (
    "type", "asset", "variant",
    "first_rstexbin_archive", "first_rstexbin_date", "first_rstexbin_position",
    # An archive holding a marker whose name did not parse as a date. Carried
    # separately so the CSV never implies "no RSTEXBIN" when one exists.
    "rstexbin_in_undated_archive",
    "dated_archive_count",
    "tex_bytes", "root_files_bytes", "rstexbin_bytes", "total_bytes",
)


def write_csv(summaries: list[VariantSummary], output: Path) -> None:
    """Sibling CSV, so the numbers land in a spreadsheet without re-scraping."""
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_COLUMNS)
        for s in summaries:
            writer.writerow([
                s.type_name, s.asset_name, s.variant_name,
                s.first_archive_with_marker or "",
                s.first_archive_date.isoformat() if s.first_archive_date else "",
                s.first_archive_index or "",
                s.first_archive_undated or "",
                s.dated_archive_count,
                s.tex_size, s.root_files_size, s.rstexbin_size, s.total_size,
            ])


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------

def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;").replace("'", "&#39;")
    )


def _group(summaries: list[VariantSummary]) -> dict[str, dict[str, list[VariantSummary]]]:
    grouped: dict[str, dict[str, list[VariantSummary]]] = {}
    for s in summaries:
        grouped.setdefault(s.type_name, {}).setdefault(s.asset_name, []).append(s)
    return grouped


def _rollup(rows: list[VariantSummary]) -> tuple[str, int, int, int, int, int]:
    """Aggregate a set of variants for an asset or type row.

    The "first" shown on a parent row is the earliest across its children --
    which is the asset-level answer the original badge was meant to give.
    """
    dated = [(r.first_archive_date, r.first_archive_with_marker or "") for r in rows
             if r.first_archive_date is not None]
    if dated:
        label = min(dated)[1]
    else:
        undated = sorted(r.first_archive_undated for r in rows if r.first_archive_undated)
        label = f"{undated[0]} (undated)" if undated else "—"
    return (
        label,
        sum(r.dated_archive_count for r in rows),
        sum(r.tex_size for r in rows),
        sum(r.root_files_size for r in rows),
        sum(r.rstexbin_size for r in rows),
        sum(r.total_size for r in rows),
    )


def _row(kind: str, key: str, parent: str, label: str, cells: tuple, indent: int, expandable: bool) -> str:
    first, count, tex, root_files, rstexbin, total = cells
    arrow = "▾" if expandable else ""
    return (
        f'<div class="row row-{kind}" data-kind="{kind}" data-key="{_esc(key)}" '
        f'data-parent="{_esc(parent)}" style="--indent:{indent}">'
        f'<span class="arrow">{arrow}</span>'
        f'<span class="name">{_esc(label)}</span>'
        f'<span class="first">{_esc(first)}</span>'
        f'<span class="num">{count:,}</span>'
        f'<span class="num">{_esc(format_size(tex))}</span>'
        f'<span class="num">{_esc(format_size(root_files))}</span>'
        f'<span class="num">{_esc(format_size(rstexbin))}</span>'
        f'<span class="num total">{_esc(format_size(total))}</span>'
        f"</div>"
    )


def render_html(
    summaries: list[VariantSummary],
    diagnostics: list[ReportDiagnostics],
    sources: list[str],
    title: str,
) -> str:
    grouped = _group(summaries)
    body: list[str] = []

    for type_name in sorted(grouped, key=str.lower):
        assets = grouped[type_name]
        all_rows = [r for rows in assets.values() for r in rows]
        body.append(_row("type", type_name, "", type_name, _rollup(all_rows), 0, True))
        for asset_name in sorted(assets, key=str.lower):
            rows = assets[asset_name]
            asset_key = f"{type_name}/{asset_name}"
            body.append(_row("asset", asset_key, type_name, asset_name, _rollup(rows), 1, True))
            for s in sorted(rows, key=lambda r: r.variant_name.lower()):
                position = (
                    f"{s.first_label} ({s.first_archive_index} of {s.dated_archive_count})"
                    if s.first_archive_index
                    else s.first_label
                )
                body.append(
                    _row(
                        "variant", "", asset_key, s.variant_name,
                        (position, s.dated_archive_count, s.tex_size,
                         s.root_files_size, s.rstexbin_size, s.total_size),
                        2, False,
                    )
                )

    diag = "\n".join(
        f"<tr><td>{_esc(Path(d.source).name)}</td><td>{_esc(d.type_name)}</td>"
        f"<td class='num'>{d.variants:,}</td><td class='num'>{d.with_archive:,}</td>"
        f"<td class='num'>{d.dated_total:,}</td><td class='num'>{d.dated_parsed:,}</td>"
        f"<td class='num'>{d.dated_with_marker:,}</td>"
        f"<td class='num'>{d.variants_with_tex:,}</td>"
        f"<td class='num'>{d.variants_with_rstexbin:,}</td>"
        f"<td>{_esc(', '.join(d.unparsed_samples)) or '—'}</td></tr>"
        for d in diagnostics
    )

    source_list = "\n".join(f"<li>{_esc(s)}</li>" for s in sources)
    totals = _rollup(summaries) if summaries else ("—", 0, 0, 0, 0, 0)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)}</title>
<style>
{_CSS}
</style>
</head>
<body>
<header class="toolbar">
  <input id="search" type="search" placeholder="Search type / asset / variant…" autocomplete="off">
  <button type="button" class="btn" data-action="expand-all">Expand All</button>
  <button type="button" class="btn" data-action="collapse-all">Collapse All</button>
  <button type="button" class="btn" data-action="expand-assets">Expand Assets</button>
  <span id="row-count" class="row-count"></span>
</header>

<section class="summary">
  <div><strong>{len(summaries):,}</strong> variants across <strong>{len(grouped):,}</strong> types,
  from <strong>{len(sources):,}</strong> report(s). Total {_esc(format_size(totals[5]))}.</div>
  <details><summary>Source reports</summary><ul>{source_list}</ul></details>
</section>

<div class="head">
  <span class="arrow"></span>
  <span class="name">Type / Asset / Variant</span>
  <span class="first">First RSTEXBIN archive</span>
  <span class="num">Archives</span>
  <span class="num">TEX</span>
  <span class="num">Root Files</span>
  <span class="num">RSTEXBIN</span>
  <span class="num total">Total</span>
</div>
<div id="rows">
{chr(10).join(body)}
</div>
<div id="no-results" class="no-results" hidden>No rows match your search.</div>

<details class="diagnostics">
  <summary>Diagnostics — what was found in each source report</summary>
  <table>
    <thead><tr><th>Report</th><th>Type</th><th>Variants</th><th>with ARCHIVE</th>
    <th>dated folders</th><th>parsed as date</th><th>with RSTEXBIN</th>
    <th>variants with TEX</th><th>variants with RSTEXBIN</th><th>unparsed name samples</th></tr></thead>
    <tbody>
{diag}
    </tbody>
  </table>
</details>

<footer class="footer">
  Derived from existing storage_report HTML reports — no re-scan, no storage access.
  Sizes are apparent size as recorded by the original scan.
  Generated {datetime.now().isoformat(sep=" ", timespec="seconds")}.
</footer>
<script>
{_JS}
</script>
</body>
</html>
"""


_CSS = """
:root {
  color-scheme: light dark;
  --bg:#fff; --fg:#1a1a1a; --muted:#6b7280; --border:#e2e2e2;
  --row-alt:rgba(0,0,0,.03); --row-hover:rgba(37,99,235,.10);
  --toolbar-bg:rgba(255,255,255,.85);
  --type:#2563eb; --asset:#059669; --variant:#9ca3af;
  --mono:ui-monospace,"Cascadia Code",Consolas,"Liberation Mono",monospace;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg:#14161a; --fg:#e5e7eb; --muted:#9ca3af; --border:#2a2d33;
    --row-alt:rgba(255,255,255,.03); --row-hover:rgba(96,165,250,.16);
    --toolbar-bg:rgba(20,22,26,.85);
    --type:#60a5fa; --asset:#34d399; --variant:#6b7280;
  }
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
 font:14px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.toolbar{position:sticky;top:0;z-index:10;display:flex;flex-wrap:wrap;align-items:center;gap:8px;
 padding:10px 16px;background:var(--toolbar-bg);backdrop-filter:blur(8px);border-bottom:1px solid var(--border)}
#search{flex:1 1 240px;min-width:140px;padding:6px 10px;border:1px solid var(--border);
 border-radius:6px;background:var(--bg);color:var(--fg);font:inherit}
.btn{padding:6px 10px;border:1px solid var(--border);border-radius:6px;background:var(--bg);
 color:var(--fg);font:inherit;cursor:pointer}
.btn:hover{background:var(--row-hover)}
.row-count{margin-left:auto;color:var(--muted);font-variant-numeric:tabular-nums;white-space:nowrap}
.summary{padding:12px 16px;border-bottom:1px solid var(--border);color:var(--muted)}
.summary ul{margin:6px 0 0;padding-left:20px;font-family:var(--mono);font-size:12px}
.summary summary{cursor:pointer;margin-top:6px}
.head,.row{display:grid;grid-template-columns:20px minmax(180px,1.4fr) minmax(200px,1.6fr)
 90px 110px 110px 110px 120px;align-items:center;gap:8px;padding:4px 16px}
.head{position:sticky;top:53px;z-index:9;background:var(--bg);border-bottom:1px solid var(--border);
 color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.04em}
.row{border-left:3px solid transparent;cursor:default}
/* The UA rule for [hidden] is display:none, but an author-level display:grid
   above outranks it -- without this the JS can set .hidden all it likes and
   every row still renders. */
.row[hidden]{display:none}
/* Striping is applied by the script over the *visible* rows; :nth-child would
   count hidden ones and stripe collapsed sections at random. */
.row.alt{background:var(--row-alt)}
.row:hover{background:var(--row-hover)}
.row-type{border-left-color:var(--type);font-weight:600}
.row-asset{border-left-color:var(--asset);font-weight:600}
.row-variant{border-left-color:var(--variant)}
.row-type,.row-asset{cursor:pointer;user-select:none}
.name{padding-left:calc(var(--indent,0)*18px);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.arrow{color:var(--muted);text-align:center;font-size:11px}
.row.closed .arrow{transform:rotate(-90deg)}
.first{font-family:var(--mono);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.num{font-family:var(--mono);font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap}
.head .num{text-align:right}
.total{font-weight:600}
.no-results{padding:24px;text-align:center;color:var(--muted)}
.diagnostics{margin:16px;border-top:1px solid var(--border);padding-top:12px}
.diagnostics summary{cursor:pointer;color:var(--muted)}
.diagnostics table{width:100%;border-collapse:collapse;font-size:12px;margin-top:10px}
.diagnostics th,.diagnostics td{text-align:left;padding:4px 8px;border-bottom:1px solid var(--border)}
.diagnostics td.num{text-align:right;font-family:var(--mono)}
.footer{padding:16px;color:var(--muted);font-size:12px;border-top:1px solid var(--border)}
@media (max-width:900px){
  .head{display:none}
  .row{grid-template-columns:20px 1fr auto;row-gap:2px}
  .row .first,.row .num:not(.total){grid-column:2}
}
"""


_JS = """
(function(){
  'use strict';
  var rows = Array.prototype.slice.call(document.querySelectorAll('#rows .row'));
  var search = document.getElementById('search');
  var counter = document.getElementById('row-count');
  var noResults = document.getElementById('no-results');
  var closed = Object.create(null);   // key -> true when collapsed

  function key(r){ return r.getAttribute('data-key'); }
  function parent(r){ return r.getAttribute('data-parent'); }
  function kind(r){ return r.getAttribute('data-kind'); }

  // key -> row, so walking ancestors is O(depth) rather than a linear scan per
  // row (which made filtering O(n^2) over a few thousand variants).
  var byKey = Object.create(null);
  rows.forEach(function(r, i){
    r.__id = '#' + i;
    var k = key(r);
    if (k) byKey[k] = r;
  });

  function ancestorsOpen(r){
    var p = parent(r);
    while (p) {
      if (closed[p]) return false;
      var pr = byKey[p];
      p = pr ? parent(pr) : '';
    }
    return true;
  }

  function apply(){
    var q = search.value.trim().toLowerCase();
    var shown = 0;
    var matched = Object.create(null);
    if (q) {
      rows.forEach(function(r){
        if (r.textContent.toLowerCase().indexOf(q) !== -1) {
          matched[key(r) || r.__id] = true;
          var p = parent(r);
          while (p) {
            matched[p] = true;
            var pr = byKey[p];
            p = pr ? parent(pr) : '';
          }
        }
      });
    }
    rows.forEach(function(r){
      var visible = q ? !!matched[key(r) || r.__id] : ancestorsOpen(r);
      r.hidden = !visible;
      if (visible) {
        r.classList.toggle('alt', shown % 2 === 1);
        shown++;
      }
      var k = key(r);
      if (k) r.classList.toggle('closed', !!closed[k]);
    });
    noResults.hidden = shown > 0;
    counter.textContent = shown.toLocaleString() + (q ? ' matching rows' : ' rows');
  }

  document.getElementById('rows').addEventListener('click', function(e){
    var row = e.target.closest ? e.target.closest('.row') : null;
    if (!row) return;
    var k = key(row);
    if (!k) return;                     // variant rows do not toggle
    closed[k] = !closed[k];
    apply();
  });

  document.querySelectorAll('[data-action]').forEach(function(btn){
    btn.addEventListener('click', function(){
      var a = btn.getAttribute('data-action');
      closed = Object.create(null);
      if (a === 'collapse-all') {
        rows.forEach(function(r){ if (key(r)) closed[key(r)] = true; });
      } else if (a === 'expand-assets') {
        rows.forEach(function(r){ if (kind(r) === 'asset') closed[key(r)] = true; });
      }
      apply();
    });
  });

  var timer = null;
  search.addEventListener('input', function(){
    clearTimeout(timer);
    timer = setTimeout(apply, 120);
  });

  apply();
})();
"""
