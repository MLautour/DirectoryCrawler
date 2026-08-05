# VFX Storage Hierarchy Size Reporter — Implementation Plan

**Status:** proposed (revision 2 — supersedes the standalone-CLI plan)
**Target:** Python 3.11+, standard library only for the core; PySide6 only in the Houdini layer
**Repository:** `c:\dev\DirectoryCrawler` (currently empty)

---

## 1. Objective & Scope

A reusable Python package that scans a configurable VFX asset hierarchy on shared NAS/SAN storage,
aggregates apparent file sizes into an in-memory tree, and renders that tree to one self-contained
HTML file. A thin PySide6 dialog makes it usable from Houdini today; the same package must drop into
Maya, Nuke, or a render-farm job tomorrow with no changes.

**Explicitly not** a standalone executable, and not a CLI application. The deliverable is an
importable library plus a front-end.

**Hard constraints that drive every design decision below:**

| Constraint | Consequence in the design |
|---|---|
| Never read file contents | Only `DirEntry`/`stat()` metadata is touched. No `open()` on scanned paths anywhere. |
| Minimal load on network storage | One `scandir()` per directory; **zero** `stat()` on directories; at most one `stat()` per file. |
| Millions of files | No node per *file*. Nodes exist per *directory*; file bytes fold into the parent. |
| Low, bounded memory | Node count ∝ directory count. Paths are derived, not stored. Skip log is capped. |
| Never follow symlinks | `follow_symlinks=False` everywhere; symlinks/junctions are skipped and recorded. |
| Single-threaded traversal | Explicit-stack DFS. Sequential I/O is also gentler on a NAS than parallel fan-out. |
| Crawler must never import `hou` | Enforced by an AST test in CI, not by convention (§13). |
| Cancellable from a UI | `threading.Event` checked once per directory; partial tree returned intact. |

---

## 2. Architecture — Three Layers

```
┌──────────────────────────────────────────────────────────────┐
│  houdini/            dialog.py · launcher.py                 │  may import hou, PySide6
│  collects input, runs the scan on a worker thread,           │
│  marshals progress to the UI thread, writes the report       │
└───────────────┬──────────────────────────────────────────────┘
                │ calls only the public API below
┌───────────────▼───────────────┐   ┌──────────────────────────┐
│  crawler.scan(...) -> Node    │──▶│  html_report.write(...)  │  stdlib only
│  config · crawler · model     │   │  html_report             │  no hou, no Qt
└───────────────────────────────┘   └──────────────────────────┘
                        utils (shared, stdlib only)
```

**The dependency rule is one-directional and absolute:** `houdini` → `storage_report`, never the
reverse. `crawler` and `html_report` do not import each other either — they communicate only through
the `model` types, so the tree can be rendered by a different backend (JSON, CSV, a farm dashboard)
without touching the crawler.

**Layer contracts:**

| Layer | Imports allowed | Knows about |
|---|---|---|
| `storage_report.model` | stdlib | nothing but itself |
| `storage_report.crawler` | stdlib, `model`, `config`, `utils` | the filesystem |
| `storage_report.html_report` | stdlib, `model`, `utils` | the tree, not the filesystem |
| `houdini.*` | everything, incl. `hou` / PySide6 | all of the above |

---

## 3. Project Layout

```
DirectoryCrawler/                       # repo root == the project root in the brief
    docs/
        implementation-plan.md          # this document
    storage_report/                     # the importable package
        __init__.py                     # public API re-exports + logging NullHandler
        config.py                       # LEVELS, DEFAULT_EXCLUDES, Config dataclass
        model.py                        # Node, RootNode, NodeType, ScanStats, SkippedPath, aggregate, sort_tree
        crawler.py                      # scan() — scandir DFS
        html_report.py                  # write() — tree -> single HTML file
        utils.py                        # size/duration formatting, exclusion matcher, path helpers
    houdini/
        __init__.py                     # see naming note below
        dialog.py                       # PySide6 QDialog, worker thread, progress bridge
        launcher.py                     # show() entry point for shelf tools / menus
        package/
            storage_report.json         # Houdini package definition (path wiring)
            shelf/storage_report.shelf  # shelf tool that calls launcher.show()
    tests/
        test_utils.py  test_model.py  test_crawler.py  test_html_report.py
        test_layering.py                # AST guard: no hou/Qt in the core
        make_tree.py                    # synthetic hierarchy generator for perf runs
    README.md
```

Two small additions to the brief's tree, both justified:

- **`houdini/__init__.py`** — without it, `dialog.py`/`launcher.py` are only importable by mutating
  `sys.path` to point *inside* the folder, which is fragile in a DCC. As a package it imports cleanly
  as `houdini.launcher`.
  *Naming risk:* a top-level module called `houdini` is generic enough to collide with something else
  in a studio `PYTHONPATH`. Recommend renaming to `storage_report_houdini/` before deployment; the
  plan keeps `houdini/` as written and flags this in §16.
- **`houdini/package/`** — the deployment wiring (§12). Without it, "run it from Houdini" is left to
  each artist's `PYTHONPATH`.

---

## 4. Configuration (`config.py`)

```python
LEVELS: list[str] = ["type", "asset", "variant"]

DEFAULT_EXCLUDES: list[str] = ["*.tmp", "*.bak", "Thumbs.db", "__pycache__", ".git"]

@dataclass(frozen=True, slots=True)
class Config:
    levels: tuple[str, ...] = tuple(LEVELS)
    excludes: tuple[str, ...] = tuple(DEFAULT_EXCLUDES)
    sort: Literal["size", "name"] = "size"      # largest-first by default
    max_folder_depth: int | None = None         # node-retention cap; see §6.5
    progress_interval: float = 0.5              # seconds between callbacks
```

**Depth → node-type mapping** — the single rule that makes the hierarchy data-driven:

| Filesystem depth below root | Node type |
|---|---|
| 0 | `root` |
| 1 … `len(levels)` | `levels[depth - 1]` |
| > `len(levels)` | `folder` (recursive, unlimited) |

Everything derived from `levels` — node types, CSS colour classes, toolbar buttons, progress fields,
and the "Largest X" summary rows — is generated from this tuple. Switching to
`["show", "sequence", "shot"]` reconfigures the whole tool with no other edit, in the crawler, the
renderer *and* the dialog.

**Generalisation decision:** the brief shows `Root Files` only under `VARIANT`, but files can sit
directly under an `ASSET` or `TYPE`, and the aggregation rules require the asset total to include
them. So `root_files` is emitted at **any** structural level that has direct files. Files inside a
`folder` are not given a synthetic node — they are simply part of that folder's own size, matching
the example report.

---

## 5. Data Model (`model.py`)

```python
class NodeType(StrEnum):
    ROOT = "root"; FOLDER = "folder"; ROOT_FILES = "root_files"
    # structural level names come from Config.levels and are validated against it

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
    children: list["Node"] | None = None   # None until a child exists
    _path: str | None = field(default=None, repr=False)

    @property
    def path(self) -> str:
        """Full path, derived from the ancestor chain and cached on first access."""
```

**The brief requires `full path` on every node; storing it literally costs ~150 bytes per node
(~75 MB at 500k directories), all of it redundant.** The compromise above satisfies the API exactly
while paying 8 bytes for a parent reference: `node.path` works everywhere, and the string only
materialises for the nodes someone actually asks about (skipped paths, the odd tooltip). The
renderer, which walks depth-first anyway, passes paths down its own stack and never triggers the
cache.

Parent references make the tree cyclic. Two consequences handled in the plan:

- **`gc.disable()` around the crawl**, restored in a `finally`. Python's generational collector
  re-scans the whole live object graph as it grows; while building hundreds of thousands of linked
  nodes that is pure overhead with nothing to collect. This is measured in Phase 6, not assumed.
- The tree is dropped as one unit after rendering, so the cycle collector reclaims it normally.

```python
@dataclass(slots=True)
class SkippedPath:
    path: str; reason: str; detail: str     # reason: permission | not-found | symlink | os-error

@dataclass(slots=True)
class ScanStats:
    root: str
    start_time: datetime; end_time: datetime | None
    total_files: int; total_dirs: int; total_size: int
    skipped: list[SkippedPath]      # capped at 10_000
    skipped_total: int              # true count even when the list is capped
    cancelled: bool                 # True if the cancel_event fired
    duration: timedelta             # convenience property

@dataclass(slots=True)
class RootNode(Node):
    stats: ScanStats | None = None
```

**Why `RootNode`:** the brief specifies `tree = crawler.scan(...)` (a tree, not a result object) and
`html_report.write(tree, output_html)` — yet the renderer needs start/end times, totals and the skip
list for the summary section. Hanging `stats` off a root-only subclass satisfies both signatures
without adding a field to every one of the 500k nodes.

`aggregate(root)` and `sort_tree(root, key)` are **separate functions**, not crawler side effects:
the crawler stays a pure "observe the filesystem" component, and aggregation is unit-testable against
hand-built trees with no I/O. Both are single iterative post-order passes.

---

## 6. Crawler (`crawler.py`)

### 6.1 Public API

```python
def scan(
    root: str | os.PathLike[str],
    config: Config = Config(),
    progress_callback: Callable[[Progress], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> RootNode:
    """Depth-first scan of `root`. Returns the aggregated, sorted tree with .stats attached.

    Never follows symlinks, never opens files, never stats a directory.
    Returns a partial tree with stats.cancelled=True if `cancel_event` is set mid-scan.
    """
```

Defaults make the simplest call `crawler.scan(root)`. The callback and event are optional so a
render-farm job can use the identical API with neither.

### 6.2 Algorithm

Depth-first with an **explicit stack**, one `scandir()` per directory, handle closed *before*
descending:

```python
stack: list[tuple[Node, str, int]] = [(root_node, root_path, 0)]

while stack:
    if cancel_event is not None and cancel_event.is_set():
        stats.cancelled = True
        break                                  # partial tree is still valid and aggregatable
    node, path, depth = stack.pop()
    child_dirs: list[tuple[Node, str]] = []
    try:
        with os.scandir(path) as it:           # handle released at the end of the with-block
            for entry in it:
                if excluded(entry.name):
                    continue
                # is_dir/is_file are answered from the cached dirent type (POSIX) or the cached
                # WIN32_FIND_DATA (Windows) -> no syscall, no network round trip.
                if entry.is_dir(follow_symlinks=False) and not is_reparse(entry):
                    child = Node(name=entry.name, type=type_for(depth + 1, config),
                                 parent=node, depth=depth + 1)
                    child_dirs.append((child, entry.path))
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
    report_progress(...)                       # throttled; see §7
```

**Why the child list is materialised instead of keeping the iterator on the stack:** holding a live
`scandir` iterator per stack frame keeps one open directory handle per depth level for the whole
duration of that subtree. On SMB/NFS those handles are server-side state that the server can
invalidate mid-scan. Draining each directory in one pass and closing immediately is both handle-light
and network-friendly; the transient cost is one directory's worth of child nodes.

**Cancellation granularity** is per directory, not per file — one `Event.is_set()` per `scandir()` is
free, while a per-file check would add millions of lock acquisitions to the hot loop. Worst-case
latency is the time to enumerate one directory.

### 6.3 Metadata-operation budget (the core performance contract)

| Operation | Count | Notes |
|---|---|---|
| `scandir()` | exactly once per directory | The unavoidable minimum. |
| `stat()` on a directory | **zero** | `is_dir(follow_symlinks=False)` is served from the cached dirent/FIND_DATA. |
| `stat()` on a file | at most once | Free on Windows; one call on POSIX. Skipped entirely for excluded files. |
| `open()` / read / hash | **zero** | Enforced by a static test over the package. |
| extra `lstat` for symlinks | zero | `is_dir`/`is_file` both returning False already identifies the "other" branch. |

### 6.4 Platform details worth planning for now

- **Windows long paths.** VFX repositories routinely exceed `MAX_PATH`. The root is normalised once
  to an extended-length path (`\\?\D:\Projects`, `\\?\UNC\server\share\...`) before traversal and the
  prefix is stripped for display. Without this the scan dies deep in the tree with `FileNotFoundError`
  and silently under-reports.
- **Junctions / reparse points.** `is_dir(follow_symlinks=False)` is `False` for a symlink but
  historically `True` for a junction. `DirEntry.is_junction()` exists from 3.12; the check is written
  as `getattr(entry, "is_junction", _false)()` so 3.11 still runs and 3.12+ gets the correct skip.
- **Apparent vs. on-disk size.** `st_size` (apparent) is used. On-disk size needs `st_blocks` (absent
  on Windows) or a per-file `GetCompressedFileSize` round trip, which we refuse to pay. Hard-linked
  files are counted once per link. Both facts are stated in the report footer so the numbers are
  never silently misread.

### 6.5 Exclusions and memory

`Config.excludes` holds glob patterns matched against the **entry name**. All patterns compile once
into a **single alternation regex** via `fnmatch.translate`, because this predicate runs on every
entry in the tree and per-pattern `fnmatch()` calls would dominate the loop. Matching is
case-insensitive on Windows, case-sensitive elsewhere. Excluded directories are not descended into,
and excluded files are skipped before their `stat()` — so exclusions actively *reduce* NAS load and
never contribute to reported sizes, as required.

Node count equals directory count: ~1M files across ~60k directories costs ~10 MB. Pathological trees
(millions of directories) are covered by `Config.max_folder_depth`: folders deeper than N levels
below the last structural level still have their **bytes counted** into the nearest retained
ancestor, but no `Node` is created. Default `None` (unlimited).

---

## 7. Progress Reporting (callback, not stdout)

```python
@dataclass(frozen=True, slots=True)
class Progress:
    levels: dict[str, str]      # {"type": "Characters", "asset": "Dragon", "variant": "High"}
    current_folder: str         # path currently being enumerated (display form)
    files: int
    directories: int
    elapsed: float              # seconds
    completed_units: int        # assets finished  -> determinate progress bar
    total_units: int            # assets discovered (0 until the asset level is enumerated)
```

- `levels` is **generated from `config.levels`**, so the dialog renders whatever hierarchy is
  configured without knowing the level names in advance.
- Throttled by wall clock (`Config.progress_interval`, default 0.5 s) using one `time.monotonic()`
  comparison per *directory* — never per file. The callback is invoked on the crawler's thread; the
  UI layer is responsible for marshalling (§11).
- A slow or throwing callback must not break a scan: invocations are wrapped so an exception is
  logged once and the callback is disabled for the rest of the run.

**Determinate progress without a pre-pass.** A percentage normally requires knowing the total file
count up front — a counting pass that would exactly double the metadata load and violate the primary
design goal. Instead the crawler emits `completed_units/total_units` at **asset granularity**: once
the asset level has been enumerated (which the scan does anyway, nearly free — it is a handful of
`scandir()` calls at the top of the tree), the total asset count is known and the bar advances as
each asset subtree completes. Honest, monotonic, and costs nothing extra.

---

## 8. Logging

`logging` throughout, no `print` anywhere. Library discipline:

- Each core module does `logger = logging.getLogger(__name__)`; `storage_report/__init__.py` attaches
  a `NullHandler`. **The library never configures handlers, levels or formatters** — that is the
  application's job, and a library that calls `basicConfig()` corrupts the host DCC's logging.
- The Houdini layer configures: a `StreamHandler` (visible in the Houdini console / terminal) plus an
  optional `FileHandler` when the user supplies a log path in the dialog.
- Log volume is deliberately low: per-scan lifecycle events at INFO, skipped paths at DEBUG only —
  a share with 40k permission errors must not produce 40k log lines. The counts and the capped list
  go to the report instead.

---

## 9. Error Handling

Caught per directory *and* per entry; always continue:
`PermissionError`, `FileNotFoundError`, `NotADirectoryError`, and a general `OSError` catch-all
(network hiccups, `ERROR_NETNAME_DELETED`, stale NFS handles, path-too-long).

- Each skip becomes a `SkippedPath` with a category and the OS message.
- The list is **capped at 10,000**; `skipped_total` keeps counting past the cap so the report can say
  "showing 10,000 of 43,112". This is what bounds memory on a badly-permissioned share.
- A file vanishing between `scandir()` and `stat()` is normal on a live share: recorded as
  `not-found`, skipped, not raised.
- Cancellation is **not** an error: `scan()` returns normally with a partial tree and
  `stats.cancelled = True`; the report renders with an "INCOMPLETE SCAN" banner.

---

## 10. HTML Report (`html_report.py`)

```python
def write(tree: RootNode, output_html: str | os.PathLike[str], *,
          title: str | None = None, sort: Literal["size", "name"] | None = None) -> None:
```

Keyword-only extras keep the brief's two-argument call working as the common case.

### 10.1 Rendering strategy — embedded JSON + lazy DOM

The renderer serialises the tree into a compact JSON payload and lets a small vanilla-JS runtime
build rows on demand, rather than emitting one `<details>` element per node.

**Why:** a server-rendered DOM of 100k nodes is tens of MB of HTML and makes the browser unresponsive
on load and on every filter keystroke. With lazy rendering the initial DOM is a few hundred rows
regardless of tree size, and search filters an array (fast) instead of the DOM (slow). This single
decision is what keeps a real VFX-repository report usable.

```js
// [name, typeIndex, size, fileCount, [childIndex, ...]]   -- positional arrays, not objects
const NODES  = [["Characters",0,137438953472,0,[1,7]], ["Dragon",1,137438953472,0,[2,5]], ...];
const PARENT = Int32Array-like flat array, for O(1) ancestor walks during search
const META   = {levels:["type","asset","variant"], root:"...", start:"...", skipped:[...], ...};
```

Flat arrays with index references (parent-major, depth-first order) mean no duplicated strings, no
pointer chasing, and O(1) lookups for search and ancestor expansion.

### 10.2 Safe embedding

- `json.dumps(..., ensure_ascii=False, separators=(",", ":"))`, then `</` → `<\/` so no filename can
  terminate the `<script>` early; `<!--` likewise neutralised.
- Names stay **raw** in the payload (search must match real text) and are escaped by a JS `esc()`
  helper at render time.
- UTF-8 with `<meta charset="utf-8">`; written to a temp file then `os.replace`, so an interrupted
  write never leaves a half-report at the destination.

### 10.3 Page structure

```
<header>   sticky toolbar: search | Expand All | Collapse All | Expand/Collapse <Level>, per level
<section>  summary
<section>  the tree
<section>  skipped paths (collapsed by default)
<footer>   generator version, apparent-size and hard-link caveats
```

Summary fields: Scan Root, Start Time, Finish Time, Duration, Total Files, Total Directories,
Total Storage, plus one **"Largest &lt;Level&gt;"** row per configured level — so `Largest Asset` and
`Largest Variant` appear automatically, and become `Largest Shot` if the config changes.

### 10.4 CSS (embedded, hand-written, no framework)

- **Sticky toolbar** — `position: sticky; top: 0`, backdrop blur, bottom border.
- **Rows** — CSS grid `[arrow][name][size]`; indent via `padding-left: calc(var(--depth) * 18px)` set
  as an inline custom property.
- **Alternating rows + hover** — `:nth-child(even)` stripe, with a `:hover` rule that wins over both
  the stripe and the heatmap.
- **Monospace size column** — monospace stack with `font-variant-numeric: tabular-nums` so sizes
  align on the decimal point.
- **Type colours** — asset blue, variant green, folder grey, root_files orange, applied via
  `data-type` attributes from a generated palette so new levels get colours automatically.
- **Responsive** — single column below 720px; the size column wraps to a second line.
- Light **and** dark via `prefers-color-scheme` over a CSS custom-property palette.

### 10.5 Size heatmap

Intensity relative to the largest node, on a **logarithmic** scale:

```
t = log1p(size) / log1p(maxSize)
background = color-mix(in oklab, var(--heat) calc(t * 55%), transparent)
```

Linear normalisation would render everything except the single largest folder invisible — asset sizes
span several orders of magnitude. The 55% alpha ceiling keeps text contrast readable in both themes.

### 10.6 JavaScript (vanilla, ~250 lines, embedded)

- **State:** `expanded: Set<number>`, plus `savedExpanded` captured on entering filter mode.
- **Render:** compute the visible index list, build one HTML string, assign once. A single string
  build beats per-row DOM insertion by a wide margin.
- **Toolbar:** Expand/Collapse All, plus per-level Expand/Collapse buttons generated from
  `META.levels` — "Expand Assets" expands everything down to and including the asset level.
- **Guard:** if Expand All would exceed ~25,000 visible rows, confirm first, so one stray click can't
  freeze a half-million-node report.
- **Search:** debounced ~120 ms, case-insensitive substring over a pre-lowercased name array; visible
  set = matches ∪ their ancestors, with matching branches auto-expanded. Clearing the box restores
  `savedExpanded` **exactly** — expansion state survives filtering, as required.
- Row click toggles; arrow-key navigation and `aria-expanded` for accessibility.

---

## 11. Houdini Integration (`houdini/`)

### 11.1 `dialog.py`

A `QDialog` parented to `hou.qt.mainWindow()` so it never falls behind the main window, holding:
root directory picker (`QFileDialog.getExistingDirectory`), output HTML field + picker, exclude
pattern field (comma-separated, pre-filled from `DEFAULT_EXCLUDES`), optional log-file field, sort
selector, Start, Cancel, a progress bar, and live labels for the current folder and per-level
counters.

**Threading — the one thing that must be right:**

```
[UI thread]  Start -> create threading.Event, spawn threading.Thread(target=_worker)
[worker]     tree = crawler.scan(root, config, progress_callback=bridge.emit_progress,
                                 cancel_event=event)
             html_report.write(tree, output)          # also off the UI thread: it is not free
             bridge.finished.emit(...)
[UI thread]  queued signal handlers update the widgets
```

- `crawler.scan` runs on a **plain `threading.Thread`**, not a `QThread`: the crawler must stay
  Qt-free, and the work is I/O-bound (the GIL is released across `scandir`/`stat`, so a thread is the
  right tool and multiprocessing would only add NAS load).
- The progress callback fires on the worker thread. It does **nothing but emit a Qt signal** from a
  small `QObject` bridge created on the UI thread; the connection is queued, so all widget writes
  happen on the UI thread. Touching a widget — or `hou` — from the worker is the classic way to crash
  Houdini, and the bridge exists solely to make that impossible.
- Cancel sets the `threading.Event` and disables the button; the worker returns a partial tree, the
  report is written anyway (marked incomplete), and the dialog reports what was covered.
- Closing the dialog mid-scan sets the event and waits with a bounded `join()` rather than leaving an
  orphan thread writing to dead widgets.
- The dialog is kept alive by a **module-level reference** in `launcher.py`. A locally-scoped PySide
  dialog is garbage-collected the moment the function returns and vanishes — the single most common
  Houdini/PySide bug.
- On success, optionally `webbrowser.open(output_html)` — driven by a checkbox, off the UI thread's
  critical path.

### 11.2 `launcher.py`

```python
def show(reload_modules: bool = False) -> None:
    """Create-or-raise the dialog. Safe to call repeatedly from a shelf tool."""
```

Keeps the singleton reference, optionally `importlib.reload`s the package for iterative development
inside a running Houdini session, and is the only symbol a shelf tool needs.

### 11.3 PySide6 / Houdini version note

The brief specifies PySide6, which means Houdini 20.5+ (Qt6, Python 3.11) — a clean match for the
3.11+ target. Older builds ship PySide2/Qt5. If the studio still runs those, a ~12-line import shim
(`try: from PySide6 import QtWidgets except ImportError: from PySide2 import QtWidgets`) confined to
the top of `dialog.py` covers both, since nothing else in the dialog uses Qt6-only API. Written as
PySide6-only unless you confirm older builds must be supported.

---

## 12. Deployment into Houdini (`houdini/package/`)

A Houdini **package JSON** is the correct delivery mechanism — no artist edits `PYTHONPATH`, and the
tool is versionable alongside the repo:

```json
{
  "env": [{ "PYTHONPATH": { "value": "$STORAGE_REPORT_ROOT" } }],
  "path": "$STORAGE_REPORT_ROOT/houdini"
}
```

Dropped into `$HOUDINI_USER_PREF_DIR/packages/` (or a studio-wide package directory), this puts the
repo root on `sys.path` so `import storage_report` and `import houdini.launcher` both resolve, and
registers the shelf. The shelf tool body is two lines:

```python
from houdini import launcher
launcher.show()
```

---

## 13. Enforcing the Layering

`tests/test_layering.py` is not a formality — it is the mechanism that keeps the reuse promise:

1. **AST scan:** parse every module under `storage_report/` and assert no `import hou`,
   `import PySide*`, `import PyQt*`, `import toolutils`, and no `os.walk` / `rglob` / `open(` /
   `hashlib` / `threading.Thread` / `asyncio` / `multiprocessing`.
2. **Import isolation:** import `storage_report` in a subprocess with `hou` and `PySide6` forced to
   `None` in `sys.modules`; scanning and rendering must both still work end to end.
3. **Direction check:** assert `storage_report` never imports from `houdini`.

A grep in a code review is forgettable; a failing test is not.

---

## 14. Build Phases

| Phase | Deliverable | Acceptance criteria |
|---|---|---|
| 0 | Package skeleton, `config.py`, logging discipline, `test_layering.py` | Layering tests pass against empty modules; `import storage_report` has no side effects. |
| 1 | `utils.py` + `model.py` + tests | `format_size` covers B→PB incl. boundaries; `aggregate`/`sort_tree` verified on hand-built trees; exclusion matcher handles all `DEFAULT_EXCLUDES`. |
| 2 | `crawler.scan()` end to end | Correct totals on the synthetic fixture; `root_files` only when files exist; symlinks skipped and recorded; metadata-budget test (§6.3) passes via a `scandir`/`stat` call counter. |
| 3 | Progress callback + cancellation + error capture | Callback throttled to ~1/interval and never per file; cancel returns a valid partial tree within one directory's latency; an unreadable directory is recorded, not fatal. |
| 4 | `html_report.write()`: payload, structure, CSS | Opens offline; hierarchy/sizes/summary/skipped all correct; "no external resource" test finds no `http`, `//cdn`, `<link`, `<script src`. |
| 5 | JavaScript: collapse, toolbar, search, heatmap | All six toolbar actions behave; search preserves and restores expansion state exactly; heatmap intensity is monotonic in size. |
| 6 | Houdini dialog + launcher + package | Scan runs off the UI thread; progress and cancel work live; dialog survives close-during-scan; report opens in the browser; perf numbers recorded (§15). |
| 7 | Hardening, docstrings, `README.md` | Full type hints; `python -m compileall` clean; README covers install, package deployment, and reuse from Maya/Nuke. |

---

## 15. Verification Plan

**Unit tests** — `unittest` (stdlib; no pytest dependency, so the suite runs inside `hython`):

- Size/duration formatting incl. boundaries (1023 B, 1024 B, 1 PB).
- Aggregation: asset total = its files + all variants + every descendant folder.
- `root_files` created only when direct files exist, with correct attribution.
- Exclusions: patterns, platform case sensitivity, directory pruning, and that an excluded file is
  never `stat()`ed.
- Cancellation: event set after N directories → partial tree, `cancelled=True`, aggregates consistent.
- HTML: payload round-trips through `json.loads`; nodes named `</script><img onerror=1>` and
  `Ünïcode 名前` both survive embedding and render as literal text.
- Layering, per §13.

**Integration** — `tests/make_tree.py` builds the brief's example hierarchy
(`Characters/Dragon/{High,Low}`, `Props/Sword/Default`) with known byte counts, plus an unreadable
directory, a symlink loop, and a file deleted mid-scan.

**Performance** — synthetic tree of ~2M files across ~50k directories on local disk, recording wall
time, peak RSS (`tracemalloc` + OS), `scandir`/`stat` counts, and HTML size.
Targets: peak RSS < 250 MB, HTML < 15 MB, first paint < 1 s, search keystroke < 100 ms.
The `gc.disable()` optimisation (§5) is validated here — kept only if it measurably wins.
Final gate is a run against a real share from inside Houdini, since local disk cannot reproduce
per-`stat` network latency.

---

## 16. Key Decisions & Risks

| Decision | Rationale | Risk / mitigation |
|---|---|---|
| Three hard layers, test-enforced | The reuse promise (Maya/Nuke/farm) is worthless if a stray `import hou` creeps in | Costs one extra test module. |
| Nodes per directory, not per file | The only way to stay memory-bounded at millions of files | No per-file detail in the report — matches the brief's node model. |
| `path` as a cached property over a parent ref | Satisfies the required API for ~8 bytes instead of ~150 per node | Introduces cycles; handled by `gc` control and measured in Phase 6. |
| `RootNode.stats` | Keeps `scan() -> tree` and `write(tree, out)` exactly as specified while the renderer still gets scan metadata | Root is slightly special-cased; documented in the model. |
| Close each directory handle before descending | SMB/NFS handles are fragile server-side state over long scans | Transient per-directory child list, bounded by fan-out. |
| Cancel checked per directory | Per-file checks would add millions of lock acquisitions | Cancel latency = one directory enumeration. |
| Determinate progress from asset counts | A file-count pre-pass would double NAS load | Uneven asset sizes make the bar advance unevenly; counters and current folder give real feedback. |
| Embedded JSON + lazy DOM | Keeps a 100k-node report responsive | More JS to write and test than nested `<details>`; covered in Phase 5. |
| Logarithmic heatmap | Sizes span orders of magnitude; linear would flatten everything | Legend states the scale so intensity isn't read as linear. |
| Apparent size (`st_size`) | On-disk size needs a per-file round trip on Windows | Disclosed in the footer; hard links counted per link. |
| Extended-length paths on Windows | Deep VFX paths exceed `MAX_PATH` and would silently truncate the scan | Prefix stripped for display. |
| `threading.Thread` + Qt signal bridge | Crawler stays Qt-free; I/O-bound work releases the GIL | Widget access from the worker would crash Houdini — the bridge makes it structurally impossible. |
| Top-level `houdini/` package name | Follows the brief | Collision risk on a studio `PYTHONPATH`; recommend `storage_report_houdini/` before deployment. |

## 17. Open Questions

1. **PySide6 only, or a PySide2 fallback?** PySide6 means Houdini 20.5+. If any shot is still on
   20.0/19.5, the shim in §11.3 is cheap to add now and expensive to retrofit later.
2. **Hard links.** De-duplicating identical inodes needs an unbounded `(st_dev, st_ino)` set, which
   breaks the memory guarantee. Plan is: count per link, disclose in the footer. Worth confirming how
   heavily the repository uses hard links (published-asset workflows sometimes do).
3. **`max_folder_depth` default.** Unlimited is more correct; a default of ~4 is more forgiving on
   pathological trees. Plan keeps unlimited and documents the knob.
4. **Multiple roots / multiple shares.** Not in v1, but the model already supports it — a synthetic
   parent node above several roots is the whole change, if scanning a whole SAN in one report becomes
   a requirement.
