# Variant Summary Report — Implementation Plan

**Status:** proposed (revision 2 — open questions answered) — no code written yet
**Goal:** derive a new, simpler report by merging several **already-generated** HTML reports, with
no re-scan and therefore zero additional load on the NAS.

---

## 0. Root Cause of the Empty RSTEXBIN Badge

Each report was scanned with a root whose **last path token is the asset type**
(`\\nas\...\assets\Characters`). With the default `levels=("type","asset","variant")` the crawler
maps depth to level names starting *below* the root, so those scans produced:

| Real thing | Depth | Type it was labelled |
|---|---|---|
| `Characters` (the scan root) | 0 | `root` |
| `Dragon` — an **asset** | 1 | `type` |
| `High` — a **variant** | 2 | `asset` |
| `ARCHIVE`, `TEX`, `RSTEXBIN` | 3 | `variant` |
| dated folders | 4 | `folder` |

`archive.analyze()` looks for a folder named `ARCHIVE` that is a **child of a node typed
`variant`** — which, under this shift, means looking for `ARCHIVE/ARCHIVE`. It matches nothing, so
zero `ArchiveInfo` records are produced, no badge is emitted and the Archives section is omitted
entirely. This reproduces exactly as case 4 of `tools/diagnose_archives.py` ("root pointed one level
too deep").

**Consequences for this utility:** the type strings stored in the reports are shifted and must
**not** be trusted. Level identification is by **depth from the report root** (§2.1). The scan data
itself — names, sizes, structure — is completely correct, which is why extraction works.

**For future scans** the fix is one argument: point `--root` at the parent of the type directories,
or keep the per-type roots and pass `Config(levels=("asset", "variant"))`.

---

## 1. Objective & Entry Point

```python
from storage_report import summarize_report

summarize_report(r"C:\reports")                       # every *.html in a folder, merged
summarize_report(r"C:\reports\*.html")                # or a glob
summarize_report([r"C:\reports\characters.html",      # or an explicit list
                  r"C:\reports\props.html"])
summarize_report(r"C:\reports\characters.html")       # or just one
# -> writes C:\reports\variant_summary.html (+ .csv)
```

One argument, accepting a directory, a glob, a list, or a single file. The output path is derived
(`variant_summary.html` in the common directory) with an optional `output=` override. Nothing else
is required — every input comes from the reports themselves.

**Each report contributes one TYPE**, named from the last token of its scan root path (§0). The
merged tree is therefore:

```
(merged root)
  Characters          <- from report 1's root path
    Dragon            <- depth 1 in that report
      High            <- depth 2
  Props               <- from report 2's root path
    Sword
      Default
```

**Why this is possible at all:** every report already embeds the complete directory tree as
`const NODES=[...]` — one entry per directory, with name, type, aggregated size and child indices.
I verified this by generating a report over a variant shaped like yours and reading the whole
subtree back out of the HTML:

```
High                          type=variant    size=21200
  TEX                         type=folder     size=9000
  RSTEXBIN                    type=folder     size=5000
  ARCHIVE                     type=folder     size=200
    2026-08-07_10-30-00       type=folder     size=100
      RSTEXBIN                type=folder     size=100
    2026-08-08_09-00-00-noProcess type=folder size=100
      RSTEXBIN                type=folder     size=100
  Root Files                  type=root_files size=7000
```

All four requested metrics are computable from that. No filesystem access at any point.

---

## 2. Reading the Report Back (`report_reader.py`)

Single responsibility: **HTML in, `RootNode` tree out.** Producing the same `model.Node` structure
the crawler produces means everything downstream works identically on a live scan or a recovered
report, and the summary code never knows which it got.

```python
def read_report(path: str | os.PathLike[str]) -> RootNode:
    """Reconstruct the scanned tree from a generated report. No filesystem access."""
```

Steps:

1. Read the file as UTF-8, locate `const NODES=`, `const PARENT=`, `const META=` by regex.
2. **Un-escape before `json.loads`.** The writer escapes `<!--` as `<\!--` to stop HTML comment
   injection. `\!` is a legal escape in *JavaScript* (so browsers are fine and existing reports are
   not broken) but is **invalid JSON**, and Python's `json.loads` rejects it outright — I confirmed
   this with `JSONDecodeError: Invalid \escape`. The reader must therefore turn `\!` back into `!`
   before parsing. `<\/` needs no handling: `\/` is valid JSON and decodes to `/` natively.
3. Rebuild `Node` objects from `NODES[i] = [name, typeIndex, size, fileCount, [childIdx…]]`, linking
   parents from `PARENT[i]` and resolving the type string via `META["types"][typeIndex]`.
4. Recover what stats are available (scan root, times, totals are in the summary section HTML) on a
   best-effort basis; the summary report needs almost none of it.

**Version tolerance.** `NODES` / `PARENT` / `META` have been present in every version of the writer
since the first commit (`bbd6627`), so all your existing reports are readable. `BADGES` only exists
from `55c6c8a` — the reader must not require it, and the summary does not use it. The reader will
fail loudly with a clear message if a const is missing rather than half-parsing.

### 2.1 Levels come from depth, not from the stored type strings

The type strings in these reports are shifted by one (§0), so `META["types"]` is actively
misleading and must be ignored for structural purposes. The mapping is fixed and positional:

| Depth from report root | Meaning |
|---|---|
| 0 | the TYPE (name taken from the last token of the scan root path) |
| 1 | ASSET |
| 2 | VARIANT |
| 3+ | folders — `ARCHIVE`, `TEX`, `RSTEXBIN`, `Root Files`, dated archives |

`root_files` is the one stored type worth keeping, since the synthetic "Root Files" node is
identified by type rather than by depth and its name is a display string.

The scan root path is recovered from the report's summary section ("Scan Root"), falling back to
the root node's name, which the writer sets to the display path.

**Guard rail:** the depth mapping is asserted, not assumed. If depth-2 nodes do not look like
variants — e.g. none of them has any of `ARCHIVE`/`TEX`/`RSTEXBIN` as a child — the tool reports
the mismatch with sample names per depth instead of emitting a plausible-looking but wrong report.
An `assume_depth=` override allows correcting it without a code change.

**Separate small improvement:** add `"levels"` to `META` in `html_report.py` so future reports carry
their level names explicitly.

---

## 3. The Four Metrics (`variant_summary.py`)

Computed per **variant** node, in one pass over its subtree.

```python
@dataclass(frozen=True, slots=True)
class VariantSummary:
    type_name: str
    asset_name: str
    variant_name: str
    first_archive_with_marker: str | None   # (1) dated folder name
    first_archive_date: date | None
    first_archive_index: int | None         # its position, e.g. 3 of 11
    dated_archive_count: int                # (2)
    tex_size: int                           # (3) VARIANT/TEX
    root_files_size: int                    # (3) VARIANT/Root Files
    rstexbin_size: int                      # (4) VARIANT/RSTEXBIN
    total_size: int                         # displayed last
```

| # | Metric | Derivation |
|---|---|---|
| 1 | First ARCHIVE date folder containing RSTEXBIN | Children of `VARIANT/ARCHIVE`; for each, does it contain an `RSTEXBIN` child. Earliest by **parsed date**, ties on name. **Only markers inside `ARCHIVE/<dated>/` count** — the variant's own `RSTEXBIN` is deliberately ignored here. |
| 2 | Number of dated archives | Count of children of `VARIANT/ARCHIVE`. |
| 3 | `TEX` and `Root Files` sizes | Reported as **two separate columns**, each 0 when the folder is absent. |
| 4 | `RSTEXBIN` size | Size of the `RSTEXBIN` child **directly under the variant** — reported for its size only, never as a "first" candidate. |

`TEX`, `RSTEXBIN` and `ARCHIVE` are the exact folder names. Matching is still **case-insensitive**,
since folder case drifts on a share and a case-sensitive miss fails silently — but no name variants
or aliases are assumed.

The two RSTEXBIN locations are the crux of requirement 2: `VARIANT/RSTEXBIN` supplies column 4,
`VARIANT/ARCHIVE/<dated>/RSTEXBIN` supplies column 1, and the two must never be conflated.

Date parsing reuses `config.archive_date_patterns`, which already covers
`2026-08-07_10-30-00` and `2026-08-07_10-30-00-noProcess`.

**Names that do not parse as a date** are still counted in metric 2 (they are folders in ARCHIVE)
but cannot be "first". If a marker sits only in unparseable folders, it is reported as
`<name> (undated)` rather than "none" — the mistake I already had to fix in the main report.

---

## 4. Output Report

Three levels, variant as the leaf and a child of the asset, exactly as specified:

```
                 First RSTEXBIN archive   Archives    TEX   Root Files  RSTEXBIN     Total
▼ Characters                                                                       126.8 GB
  ▼ Dragon                                                                         126.8 GB
      High       2026-08-07_10-30-00       11        30.5 GB   1.2 GB    5.0 GB     92.1 GB
      Low        —                          0        33.9 GB   0.8 GB      0 B      34.7 GB
  ▶ Knight                                                                          58.2 GB
▼ Props                                                                             12.4 GB
```

Column order follows the requirement: everything requested first, **total size last on every
variant line**.

- Total size stays the **last** column on every variant line.
- Asset and type rows show their own totals and stay collapsible.
- Reuses the existing report's CSS conventions (sticky toolbar, monospace tabular sizes,
  alternating rows, light/dark) so it looks like a sibling of the main report, but drops the tree
  machinery: at variant granularity this is a few thousand rows, so it can be rendered server-side
  as plain HTML with no lazy-DOM JS. Much simpler than `html_report.py`.
- Search box and expand/collapse only. No heatmap, no skipped-paths section.
- A **CSV alongside the HTML** (`_variants.csv`) so the numbers can go into a spreadsheet without
  scraping a second time. Cheap to add and likely wanted.
- Header states the source report path, its scan date, and the inferred level names.

### 4.1 Diagnostics in the output

Because the RSTEXBIN badge is not working on your real data and the cause is still unknown, the
summary prints a short diagnostic block: how many variants were found, how many had an `ARCHIVE`
child, how many dated folders parsed as dates, how many contained a marker, and up to ten sample
folder names that failed to parse. That turns this utility into the answer *and* the explanation for
why the original badge came out empty.

---

## 5. Module Layout

```
storage_report/
    report_reader.py     # HTML -> RootNode. No filesystem, no rendering.
    variant_summary.py   # tree -> VariantSummary[] -> simple HTML + CSV
    __init__.py          # exposes summarize_report()
tests/
    test_report_reader.py     # round-trip: scan -> write -> read -> identical tree
    test_variant_summary.py   # the four metrics, incl. every edge case in §6
```

The round-trip test is the important one: build a tree, render it, read it back, and assert the
recovered tree matches the original node-for-node in name, type, size and structure.

---

## 6. Edge Cases To Handle Explicitly

| Case | Behaviour |
|---|---|
| Variant has no `ARCHIVE` | count 0, first `—`, still listed |
| `ARCHIVE` exists but is empty | count 0, first `—` |
| No `TEX` folder | contributes 0, and the split is shown so 0 is distinguishable from missing |
| No `RSTEXBIN` under the variant | 0 B |
| Marker only in unparseable date folders | `<name> (undated)` |
| Report from an older writer version | works; `BADGES` absent is fine |
| Node name containing `<!--` | handled by the `\!` un-escape (§2) |
| Report file truncated / not a storage_report | clear error naming the missing const |

---

## 7. Effort

| Phase | Work | Est. |
|---|---|---|
| 0 | Inspector run against one of **your real reports** to confirm actual folder names and shape | 30 min |
| 1 | `report_reader.py` + round-trip test | half day |
| 2 | `variant_summary.py` metrics + tests | half day |
| 3 | HTML/CSV rendering | half day |
| 4 | Diagnostics, README, edge cases | half day |

Roughly two days. Phase 0 comes first deliberately — see below.

---

## 8. Resolved Decisions

| Question | Answer |
|---|---|
| Folder names | `TEX`, `RSTEXBIN`, `ARCHIVE` exactly; matched case-insensitively, no aliases |
| `TEX` / `Root Files` / `RSTEXBIN` sizes | **Three separate columns**, not summed |
| Which RSTEXBIN determines "first" | Only those inside `ARCHIVE/<dated>/`; the variant's own `RSTEXBIN` is size-only |
| TYPE as a top grouping | Yes — one TYPE per input report, named from the last token of its scan root |
| One report or several | Several, merged into one report |

## 9. Remaining Risk

One assumption is load-bearing: **depth 2 in each report is the variant**. It follows directly from
"the asset type is the last token in the root path", and it is what makes §0 explain the badge
failure. But if any report was scanned from a different depth than the others, its rows come out
shifted.

Mitigation is built in rather than assumed: the tool validates the mapping per report (do depth-2
nodes actually have `ARCHIVE`/`TEX`/`RSTEXBIN` children?) and refuses to emit a wrong-looking report,
naming the offending file and printing sample names per depth. Mixed-depth report sets are therefore
detected, not silently mis-merged.
