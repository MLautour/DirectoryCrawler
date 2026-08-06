# VFX Storage Hierarchy Size Reporter — Implementation Plan

**Status:** proposed (revision 3 — supersedes the Houdini-UI plan)
**Target:** Python 3.11+, standard library only. No UI, no Qt, no threads, no executable.
**Repository:** `c:\dev\DirectoryCrawler` (currently empty)

---

## 1. What This Is

A small importable package. You add the repo to `sys.path`, call one function, and get an HTML
report. It runs identically in the Houdini Python Shell, in `hython`, and in plain CPython, because
it imports nothing but the standard library.

```python
import sys
sys.path.append(r"C:\dev\DirectoryCrawler")

from storage_report import run

result = run(
    root=r"\\nas\projects\assets",
    output=r"C:\temp\report.html",
    max_locations=50_000,        # safety valve: stop after N directories
)
```

The three steps are also available separately when you want the data rather than the report:

```python
from storage_report import crawler, archive, html_report
from storage_report.config import Config

tree     = crawler.scan(root, Config(max_locations=50_000))
archives = archive.analyze(tree)          # the ARCHIVE / RSTEXBIN answers, as plain dataclasses
html_report.write(tree, output_html, archives=archives)
```

`archive.analyze()` returns ordinary dataclasses, so the two questions you need answered are
available in the shell without opening the report at all:

```python
for a in archives:
    print(a.asset, a.variant, a.archive_count, a.first_rstexbin)
```

**Constraints that drive the design:**

| Constraint | Consequence |
|---|---|
| Never read file contents | Only `DirEntry`/`stat()` metadata. No `open()` on scanned paths. |
| Minimal NAS load | One `scandir()` per directory; **zero** `stat()` on directories; at most one `stat()` per file. |
| Millions of files | No node per *file*. One node per *directory*; file bytes fold into the parent. |
| Low memory | Node count ∝ directory count. Paths derived, not stored. Skip log capped. |
| Never follow symlinks | `follow_symlinks=False` everywhere; symlinks/junctions skipped and recorded. |
| Runs inside a DCC | Pure stdlib, single-threaded, synchronous. Nothing to conflict with Houdini's event loop. |

---

## 2. Project Layout

```
DirectoryCrawler/
    docs/implementation-plan.md
    storage_report/
        __init__.py         # run() convenience wrapper + public re-exports
        config.py           # LEVELS, excludes, archive rules, Config dataclass
        model.py            # Node, RootNode, ScanStats, SkippedPath, aggregate, sort_tree
        crawler.py          # scan() — scandir DFS
        archive.py          # analyze() — ARCHIVE / dated-folder / RSTEXBIN post-pass
        html_report.py      # write() — tree -> single HTML file
        utils.py            # size/duration formatting, exclusion matcher, console progress
    tests/
        test_utils.py  test_model.py  test_crawler.py  test_archive.py  test_html_report.py
        test_purity.py  # static guard: no open()/os.walk/rglob/hou/Qt/threads in the package
        make_tree.py    # synthetic hierarchy generator for perf runs
    README.md
```

Seven modules, each one responsibility. The Houdini layer, the worker thread, the Qt bridge and the
package/shelf wiring from the previous revision are all gone.

---

## 3. Configuration (`config.py`)

```python
LEVELS = ["type", "asset", "variant"]

DEFAULT_EXCLUDES = ["*.tmp", "*.bak", "Thumbs.db", "__pycache__", ".git"]

@dataclass(frozen=True, slots=True)
class Config:
    levels: tuple[str, ...] = tuple(LEVELS)
    excludes: tuple[str, ...] = tuple(DEFAULT_EXCLUDES)
    sort: Literal["size", "name"] = "size"      # largest-first by default
    max_locations: int | None = None            # stop after N directories visited
    progress_interval: float = 2.0              # seconds between progress lines

    # --- archive analysis (§7) ---
    archive_dir: str = "ARCHIVE"                # matched case-insensitively
    archive_marker: str = "RSTEXBIN"            # ditto
    archive_marker_recursive: bool = False      # marker must be a direct child of the dated folder
    archive_date_patterns: tuple[str, ...] = (...)   # see §7.2
```

**Depth → node type** — the one rule that makes the hierarchy data-driven:

| Depth below root | Node type |
|---|---|
| 0 | `root` |
| 1 … `len(levels)` | `levels[depth - 1]` |
| > `len(levels)` | `folder` (recursive, unlimited) |

Node types, colours, toolbar buttons, progress fields and the "Largest X" summary rows are all
generated from `levels`. Changing it to `["show", "sequence", "shot"]` reconfigures everything.

`root_files` is emitted at **any** structural level with direct files, not just `variant` — files do
sit directly under assets sometimes, and the asset total must include them. Files inside a `folder`
just count toward that folder's own size, as in your example report.

---

## 4. Data Model (`model.py`)

```python
@dataclass(slots=True)
class Node:
    name: str
    type: str
    parent: "Node | None" = None
    size: int = 0          # aggregated: own_size + every descendant
    own_size: int = 0      # bytes of files directly inside this directory
    file_count: int = 0    # aggregated
    dir_count: int = 0     # aggregated
    depth: int = 0
    children: list["Node"] | None = None    # allocated only when a child exists

    @property
    def path(self) -> str: ...   # joined from the parent chain, cached on first access

@dataclass(slots=True)
class RootNode(Node):
    stats: ScanStats | None = None
```

`path` is derived from a parent reference rather than stored: a literal path string on every node
costs ~150 bytes (~75 MB at 500k directories) and is pure redundancy. `node.path` still works
everywhere.

`ScanStats` (attached to the root only, so `scan()` can return a plain tree) carries root, start/end
time, duration, total files, total directories, total size, the capped skip list with its true count,
and `stopped_early: bool` when `max_locations` was hit.

`aggregate(root)` and `sort_tree(root, key)` are separate iterative post-order functions, not crawler
side effects — so they are testable against hand-built trees with no filesystem involved.

---

## 5. Crawler (`crawler.py`)

```python
def scan(root, config=Config(), progress_callback=None) -> RootNode:
    """Depth-first scan. Returns the aggregated, sorted tree with .stats attached.

    Never follows symlinks, never opens files, never stats a directory.
    Stops cleanly and returns a partial tree when config.max_locations is reached
    or Ctrl+C is pressed.
    """
```

### 5.1 Algorithm

Explicit-stack DFS, one `scandir()` per directory, handle closed *before* descending:

```python
stack = [(root_node, root_path, 0)]
locations = 0

while stack:
    if config.max_locations is not None and locations >= config.max_locations:
        stats.stopped_early = True
        break                                  # partial tree is still valid and aggregatable
    node, path, depth = stack.pop()
    locations += 1
    child_dirs = []
    try:
        with os.scandir(path) as it:           # handle released at the end of the with-block
            for entry in it:
                if excluded(entry.name):
                    continue
                # is_dir/is_file come from the cached dirent type (POSIX) or the cached
                # WIN32_FIND_DATA (Windows) -> no syscall, no network round trip.
                if entry.is_dir(follow_symlinks=False) and not is_reparse(entry):
                    child_dirs.append((Node(entry.name, type_for(depth + 1, config),
                                            parent=node, depth=depth + 1), entry.path))
                elif entry.is_file(follow_symlinks=False):
                    # The only stat() in the program. Free on Windows (already cached by the
                    # directory enumeration); one unavoidable call on POSIX/NFS.
                    node.own_size += entry.stat(follow_symlinks=False).st_size
                    node.file_count += 1
                else:
                    record_skip(entry.path, "symlink")   # symlinks, junctions, sockets, FIFOs
    except PermissionError:   record_skip(path, "permission"); continue
    except FileNotFoundError: record_skip(path, "not-found"); continue
    except OSError as exc:    record_skip(path, "os-error", exc); continue

    attach_children(node, child_dirs, config)  # incl. the synthetic root_files node
    stack.extend(reversed(child_dirs))         # reversed -> natural left-to-right DFS order
    report_progress(...)                       # throttled by wall clock, see §6
```

The whole loop is wrapped in `try/except KeyboardInterrupt` so Ctrl+C in `hython` or a terminal
returns the partial tree instead of a traceback.

**Why the child list is materialised instead of keeping the iterator on the stack:** a live `scandir`
iterator per stack frame holds one open directory handle per depth level for the entire duration of
that subtree. On SMB/NFS those are server-side state the server can invalidate mid-scan. Draining
each directory in one pass and closing immediately is handle-light and network-friendly.

### 5.2 `max_locations` semantics

One "location" = one directory enumerated. The counter increments per `scandir()`, and the check runs
once per directory, so it costs nothing.

**Be aware of what a DFS cap gives you:** it returns a complete *prefix in depth-first order* — the
first few branches fully explored, the rest untouched. That makes it an excellent **safety valve and
smoke test** ("does this run at all against the share, and what does the report look like?"), but not
a representative sample of the whole tree. Sizes in a stopped-early report are lower bounds, and the
report says so in a banner. If you want representative sampling instead, that is a breadth-first cap
— a different feature, noted in §12 rather than built now.

An optional `cancel_event: threading.Event | None` parameter shares the same break path (three lines)
so a future caller can stop a scan from another thread without redesigning anything.

### 5.3 Metadata-operation budget (the core performance contract)

| Operation | Count | Notes |
|---|---|---|
| `scandir()` | exactly once per directory | The unavoidable minimum. |
| `stat()` on a directory | **zero** | `is_dir(follow_symlinks=False)` is served from the cached dirent/FIND_DATA. |
| `stat()` on a file | at most once | Free on Windows; one call on POSIX. Skipped entirely for excluded files. |
| `open()` / read / hash | **zero** | Enforced by `test_purity.py`. |
| extra `lstat` for symlinks | zero | `is_dir`/`is_file` both returning False already identifies the branch. |

### 5.4 Platform notes worth planning for now

- **Windows long paths.** VFX repos routinely exceed `MAX_PATH`. The root is normalised once to an
  extended-length path (`\\?\D:\...`, `\\?\UNC\server\share\...`) and the prefix stripped for
  display. Without this the scan dies deep in the tree with `FileNotFoundError` and silently
  under-reports.
- **Junctions.** `is_dir(follow_symlinks=False)` is `False` for a symlink but historically `True` for
  a junction. `DirEntry.is_junction()` exists from 3.12; written as
  `getattr(entry, "is_junction", _false)()` so 3.11 still runs.
- **Apparent size.** `st_size`, not on-disk size — the latter needs a per-file round trip on Windows.
  Hard links are counted once per link. Both stated in the report footer.

### 5.5 Exclusions

Glob patterns matched against the **entry name**, compiled once into a single alternation regex via
`fnmatch.translate` — this predicate runs on every entry in the tree, so per-pattern `fnmatch()`
calls would dominate the loop. Case-insensitive on Windows. Excluded directories are not descended
into and excluded files are skipped before their `stat()`, so exclusions actively reduce NAS load.

---

## 6. Progress

`progress_callback` is optional. When omitted, the crawler uses a built-in console reporter that
prints one line every `progress_interval` seconds (default 2.0):

```
[00:04:28]  Characters / Dragon / High      files 4,253,221   dirs 18,244   locations 18,244/50,000
```

**Deliberately a new line each time, not an in-place `\r` redraw.** Houdini's Python Shell appends
output rather than honouring carriage returns, so a redraw-style progress bar turns into thousands of
concatenated fragments — this is exactly the kind of thing that made the previous approach unpleasant
in Houdini. One line every two seconds stays readable in the Shell, in `hython`, and in a terminal.
Output is flushed on every write.

Throttling is one `time.monotonic()` comparison per *directory*, never per file. The callback
receives a `Progress` dataclass:

```python
@dataclass(frozen=True, slots=True)
class Progress:
    levels: dict[str, str]     # {"type": "Characters", "asset": "Dragon", "variant": "High"}
    current_folder: str
    files: int
    directories: int
    locations: int
    max_locations: int | None
    elapsed: float
```

`levels` is generated from `config.levels`, so a caller never hardcodes level names.

Because the scan is now synchronous and on the main thread, a custom callback **can safely touch
`hou`** — no thread marshalling to get wrong:

```python
import hou
crawler.scan(root, config, progress_callback=lambda p: hou.ui.setStatusMessage(
    f"{p.directories:,} dirs — {p.current_folder}"))
```

**Honest trade-off:** a synchronous scan means Houdini's UI is unresponsive until it finishes. That is
the cost of dropping the worker thread, and it is the right trade for a tool run deliberately rather
than continuously. `max_locations` bounds it, and long full scans are better run in `hython` outside
the GUI.

---

## 7. Archive Analysis (`archive.py`)

### 7.1 What it does

For every `VARIANT` containing an `ARCHIVE` folder, answer:

1. **How many dated archives are there?** → `archive_count`
2. **Which is the first archive containing an `RSTEXBIN` folder?** → `first_rstexbin`

```python
@dataclass(frozen=True, slots=True)
class ArchiveInfo:
    levels: dict[str, str]          # {"type": "Characters", "asset": "Dragon", "variant": "High"}
    variant_path: str
    archive_size: int               # total bytes under ARCHIVE — free, already in the tree
    archive_count: int              # dated folders inside ARCHIVE
    dated: tuple[DatedArchive, ...] # chronologically sorted
    unparsed: tuple[str, ...]       # names in ARCHIVE that did not parse as a date
    first_rstexbin: str | None      # earliest dated folder containing the marker
    first_rstexbin_date: date | None
    first_rstexbin_index: int | None  # its 1-based position in chronological order
    rstexbin_count: int             # how many dated archives contain the marker

@dataclass(frozen=True, slots=True)
class DatedArchive:
    name: str; date: date | None; size: int; has_marker: bool

def analyze(tree: RootNode, config: Config = Config()) -> list[ArchiveInfo]: ...
```

**This costs zero additional filesystem operations.** The crawler already enumerated every directory,
so `analyze()` is a pure walk over the in-memory tree. That is the whole reason it is a post-pass and
not a hook inside the crawler: the crawler stays hierarchy-agnostic, and this studio-specific rule
lives in one 120-line module that can be edited without touching traversal code.

### 7.2 Matching rules

- **Finding ARCHIVE:** a child node whose name matches `config.archive_dir` case-insensitively and
  whose parent is at the last configured level (`variant`). Case-insensitive because share contents
  drift between `ARCHIVE`, `Archive`, and `archive`.
- **Dated folders:** direct children of ARCHIVE. Each name is tried against
  `config.archive_date_patterns` in order; the first match wins. Defaults cover the common studio
  formats:

  | Pattern | Matches |
  |---|---|
  | `YYYY-MM-DD` / `YYYY_MM_DD` / `YYYYMMDD` | `2024-01-15`, `2024_01_15`, `20240115` |
  | the above with a trailing suffix | `2024-01-15_v002`, `20240115-lighting` |
  | `YYYYMMDD_HHMM` | `20240115_1430` |

  A name that matches nothing goes to `unparsed` and sorts *after* all parsed dates, by name. It is
  still counted in `archive_count` (it is a folder in ARCHIVE) but can never be `first_rstexbin`,
  because "first" is meaningless without a date. The report shows unparsed names explicitly so a
  format the patterns miss is visible rather than silently mis-ordered.
- **"First":** earliest parsed date, ties broken by name. Not filesystem mtime — mtime is unreliable
  after a share migration or a restore, and reading it would also mean stat-ing directories, which
  §5.3 forbids.
- **RSTEXBIN:** by default a **direct child** of the dated folder, matched case-insensitively. Set
  `archive_marker_recursive=True` to match anywhere beneath it — also free, since the subtree is
  already in memory.

### 7.3 Reporting

Two outputs, because you asked to *capture* the information, not only to look at it:

- **Programmatic** — `analyze()` returns the list above; iterate it in the shell, write it to CSV or
  JSON, feed it to a pipeline tool.
- **In the HTML report** — a dedicated **Archives** section: one row per variant with Type, Asset,
  Variant, archive count, first RSTEXBIN archive (with its position, e.g. `2024-03-02 (3 of 11)`),
  RSTEXBIN count, and archive size. Sortable by any column, with a filter for "variants with no
  RSTEXBIN at all", since that is the actionable set. Above the table, a one-line roll-up:
  *312 variants with an ARCHIVE · 1,847 dated archives · 209 variants contain RSTEXBIN · 103 do not.*
  Variants whose ARCHIVE folder is empty appear with a count of 0 rather than being dropped.

If the scan stopped early on `max_locations`, the section is labelled partial — some variants were
never visited.

---

## 8. HTML Report (`html_report.py`)

```python
def write(tree, output_html, *, archives=None, title=None, sort=None) -> None:
```

`archives` defaults to `None`, in which case `write()` calls `archive.analyze()` itself — so the
two-argument call in §1 produces the full report with no extra ceremony.

**Rendering strategy: embedded JSON + lazy DOM.** The tree is serialised to a compact payload and a
small vanilla-JS runtime builds rows on demand. A server-rendered DOM of 100k `<details>` elements is
tens of MB of HTML and unresponsive on every keystroke; with lazy rendering the initial DOM is a few
hundred rows regardless of tree size and search filters an array instead of the DOM.

```js
// [name, typeIndex, size, fileCount, [childIndex, ...]]   -- positional arrays, not objects
const NODES = [["Characters",0,137438953472,0,[1,7]], ...];
```

- **Safe embedding:** `json.dumps(ensure_ascii=False, separators=(",",":"))`, then `</` → `<\/` so no
  filename can terminate the `<script>` early. Names stay raw in the payload (search must match real
  text) and are escaped by a JS helper at render time. UTF-8, written to a temp file then
  `os.replace` so an interrupted write never leaves a half-report.
- **Page:** sticky toolbar (search + Expand/Collapse All + Expand/Collapse per level, generated from
  `config.levels`) · summary · tree · **archives** · skipped paths · footer with the apparent-size and
  hard-link caveats.
- **Summary:** scan root, start, finish, duration, total files, total directories, total storage, and
  a "Largest &lt;Level&gt;" row per configured level — so Largest Asset and Largest Variant appear
  automatically.
- **CSS:** sticky toolbar; grid rows with `padding-left: calc(var(--depth) * 18px)`; alternating
  stripes; hover that wins over stripe and heatmap; monospace size column with `tabular-nums`;
  asset blue / variant green / folder grey / root_files orange via `data-type`; responsive below
  720px; light and dark via `prefers-color-scheme`.
- **Heatmap:** logarithmic, `t = log1p(size) / log1p(maxSize)`, alpha capped at 55% for text
  contrast. Linear normalisation would make everything except the single largest folder invisible.
- **JS:** `expanded: Set<number>` plus `savedExpanded` captured when filtering starts; one string
  build per render; debounced case-insensitive search showing matches ∪ their ancestors with matching
  branches auto-expanded; clearing the box restores the previous expansion state exactly; a confirm
  guard if Expand All would exceed ~25,000 rows.

---

## 9. Errors and Logging

Caught per directory and per entry, always continue: `PermissionError`, `FileNotFoundError`,
`NotADirectoryError`, and a general `OSError` catch-all (network hiccups, stale handles,
path-too-long). Each becomes a `SkippedPath` with a category and the OS message. The list is **capped
at 10,000** while `skipped_total` keeps counting, so a badly-permissioned share cannot exhaust memory
— the report says "showing 10,000 of 43,112". A file vanishing between `scandir()` and `stat()` is
normal on a live share: recorded, skipped, not raised.

`logging` for diagnostics — each module takes `logging.getLogger(__name__)` and `__init__.py`
attaches a `NullHandler`; the package never calls `basicConfig()`, which would corrupt Houdini's own
logging. Skipped paths log at DEBUG only (40k permission errors must not produce 40k log lines; the
counts go in the report). The one exception to "no print" is the default console progress reporter in
§6, which writes to stdout by design — that is its whole job.

---

## 10. Build Phases

| Phase | Deliverable | Acceptance |
|---|---|---|
| 1 | `config.py`, `utils.py`, `model.py`, `test_purity.py` | `format_size` covers B→PB incl. boundaries; `aggregate`/`sort_tree` verified on hand-built trees; purity test passes. |
| 2 | `crawler.scan()` | Correct totals on the fixture; `root_files` only when files exist; symlinks skipped; `max_locations` returns a valid partial tree; metadata budget (§5.3) verified with a `scandir`/`stat` call counter. |
| 3 | Progress + error capture | One line per interval, never per file; readable in the Houdini Shell; unreadable directory recorded, not fatal. |
| 4 | `archive.analyze()` | Counts and "first RSTEXBIN" correct on a fixture with mixed date formats, an unparsed name, an empty ARCHIVE, and a variant with no ARCHIVE; zero filesystem calls (asserted by patching `os.scandir`). |
| 5 | `html_report.write()` + JS | Opens offline; no `http`/`<link`/`<script src` anywhere; archives section correct; toolbar and search behave; expansion state survives filtering. |
| 6 | `run()`, README, perf pass | End-to-end from a Houdini Python Shell against a real share; perf numbers recorded (§11). |

---

## 11. Verification

`unittest` only (stdlib — the suite runs inside `hython` with no pip installs).

Fixtures via `tests/make_tree.py`: the example hierarchy (`Characters/Dragon/{High,Low}`,
`Props/Sword/Default`) with known byte counts, plus an unreadable directory, a symlink loop, a file
deleted mid-scan, and an ARCHIVE tree exercising every date format, an unparsed name, and variants
with/without RSTEXBIN.

Notable cases: excluded files are never `stat()`ed; a node named `</script><img onerror=1>` and one
named `Ünïcode 名前` survive embedding as literal text; `max_locations=N` visits exactly N
directories; `analyze()` performs zero filesystem calls.

Performance: ~2M files across ~50k directories on local disk — targets peak RSS < 250 MB, HTML < 15 MB,
first paint < 1 s, search keystroke < 100 ms. Final gate is a real share, since local disk cannot
reproduce per-`stat` network latency.

---

## 12. Decisions, Risks, and What Was Deferred

| Decision | Rationale |
|---|---|
| Synchronous, single-threaded, no UI | Nothing to conflict with Houdini's event loop; callbacks may safely touch `hou`. Cost: Houdini blocks during a scan (§6). |
| Nodes per directory, not per file | The only way to stay memory-bounded at millions of files. |
| Archive analysis as a tree post-pass | Zero extra filesystem calls; keeps the studio-specific rule out of the traversal code. |
| "First" archive by parsed folder date, not mtime | mtime is destroyed by migrations and restores, and reading it would mean stat-ing directories. |
| Unparsed names counted but never "first" | Ordering without a date is a guess; surfacing them in the report exposes a missing pattern instead of hiding it. |
| `path` as a cached property over a parent ref | Same API for ~8 bytes instead of ~150 per node. |
| Embedded JSON + lazy DOM | Keeps a 100k-node report responsive. |
| Logarithmic heatmap | Sizes span orders of magnitude; linear would flatten everything. |
| Apparent size (`st_size`) | On-disk size needs a per-file round trip on Windows. Disclosed in the footer. |

**Deferred, deliberately:** the Houdini dialog and worker thread (removed); breadth-first sampling as
an alternative to the DFS cap; `max_folder_depth` node pruning (`max_locations` covers the practical
need, and pruning would break the archive analysis); hard-link de-duplication (needs an unbounded
inode set, which breaks the memory guarantee — counted per link and disclosed instead); multiple
roots in one report (the model supports it; a synthetic parent node is the whole change).

## 13. Open Questions

1. **Dated-folder naming.** The default patterns in §7.2 assume zero-padded year-month-day. If your
   archives use something else (`15-01-2024`, `v012_20240115`, week numbers, a job code prefix),
   send me two or three real folder names and I will set `archive_date_patterns` correctly — this is
   the one input that decides whether "first" is right.
2. **RSTEXBIN depth.** Default assumes `ARCHIVE/<date>/RSTEXBIN`. If it can be nested deeper
   (`ARCHIVE/<date>/textures/RSTEXBIN`), flip `archive_marker_recursive=True` — it is free either way,
   but the default should match reality.
3. **A sensible `max_locations` default.** Currently `None` (unlimited). If most runs are exploratory,
   a default like 100,000 would be friendlier, with `None` as the explicit opt-in for a full scan.
