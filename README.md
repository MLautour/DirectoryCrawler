# storage_report

A small importable Python package that scans a configurable VFX asset hierarchy on shared
NAS/SAN storage, aggregates apparent file sizes into an in-memory tree, and renders that tree
to one self-contained HTML report. Standard library only — no UI, no Qt, no threads, no
executable. It runs identically in the Houdini Python Shell, in `hython`, and in plain CPython.

See [docs/implementation-plan.md](docs/implementation-plan.md) for the full design rationale
behind every decision below.

## Requirements

Python 3.11+. Nothing else — the package imports nothing but the standard library.

## Install

Nothing to build — put the repo root on `sys.path` and import:

```python
import sys
sys.path.append(r"C:\dev\DirectoryCrawler")

from storage_report import run
```

Or add the repo root to `PYTHONPATH`. There is no compiled extension and no external
dependency.

## Usage

The fast path — one call, get an HTML report:

```python
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
    print(a.variant_path, a.archive_count, a.first_rstexbin)
```

With progress reporting and cancellation (e.g. from a UI or a long batch job):

```python
import threading
from storage_report import crawler, html_report, Config

cancel_event = threading.Event()

def on_progress(p):
    print(f"{p.current_folder}  files={p.files}  dirs={p.directories}  "
          f"locations={p.locations}/{p.max_locations}")

tree = crawler.scan(root, Config(), progress_callback=on_progress, cancel_event=cancel_event)
html_report.write(tree, "report.html", sort="size")
```

Omitting `progress_callback` doesn't turn progress reporting off — `crawler.scan` falls back to
a built-in console reporter that prints one line every `progress_interval` seconds:

```
[00:04:28]  Characters / Dragon / High      files 4,253,221   dirs 18,244   locations 18,244/50,000
```

`crawler.scan` never follows symlinks, never opens file contents, and performs at most one
`stat()` per file and zero `stat()` calls on directories. It spawns no threads of its own and
returns a valid, aggregated partial tree if `max_locations` is hit or `cancel_event` fires
mid-scan (`tree.stats.stopped_early` is `True` in either case).

### Configuration

```python
from storage_report import Config

config = Config(
    levels=("show", "sequence", "shot"),  # any depth; defaults to ("type", "asset", "variant")
    excludes=("*.tmp", "*.bak", "Thumbs.db", "__pycache__", ".git"),
    sort="size",                # or "name"
    max_locations=None,         # stop after N directories visited; None = unlimited
    progress_interval=2.0,      # seconds between progress_callback invocations

    # network politeness (see below)
    throttle_ms=0.0,            # fixed pause after every directory, milliseconds
    throttle_ratio=0.0,         # extra pause = this * how long that directory took

    # archive analysis (see below)
    archive_dir="ARCHIVE",              # matched case-insensitively
    archive_marker="RSTEXBIN",          # ditto
    archive_marker_recursive=False,     # marker must be a direct child of the dated folder
    archive_date_patterns=(...),        # see storage_report/config.py for the defaults
)
```

Everything in the report — node types, colours, the toolbar's per-level expand/collapse
buttons, and the "Largest X" summary rows — is derived from `levels`, so reconfiguring it
reconfigures the whole tool with no other change.

### Throttling (keeping load off a shared filer)

By default the crawler runs flat out: each directory costs the bare minimum (one `scandir`,
zero directory `stat`s, one `stat` per file — and on Windows that `stat` is free because the
directory enumeration already carries the size), but they are issued back-to-back with no gaps.
On a busy production share that is still a sustained stream of metadata requests.

`throttle_ms` and `throttle_ratio` pause **between directories**, which on Windows means pausing
between network round trips — the finest granularity that means anything, since one `scandir` is
one network operation:

```python
crawler.scan(root, Config(throttle_ms=100, throttle_ratio=4.0))
```

- **`throttle_ms`** is a fixed floor: a hard cap of `1000 / throttle_ms` directories per second,
  regardless of how fast the filer is.
- **`throttle_ratio`** adds a pause proportional to how long that directory actually took, giving
  a duty cycle of roughly `1 / (1 + ratio)`. It is self-scaling: near-zero on a fast local disk,
  but it backs off hard exactly when the filer is slow — which is when it is busy.

Use both together for a floor plus adaptive backoff. Rough cost for a 50,000-directory repository:

| Setting | Max rate | Added time |
|---|---|---|
| `throttle_ms=20` | 50 dirs/sec | ~17 min |
| `throttle_ms=50` | 20 dirs/sec | ~42 min |
| `throttle_ms=100, throttle_ratio=4.0` | 10 dirs/sec | ~2–3 hours |
| `throttle_ms=250, throttle_ratio=4.0` | 4 dirs/sec | ~3.5–5 hours |

The report's summary gains a **Paused by Throttle** row showing total pause time and what share of
the run it was, so a slow filer is distinguishable from a politely-paced scan.

Cancellation stays responsive: when a `cancel_event` is supplied the crawler waits on the event
rather than sleeping blindly, so it wakes the instant you cancel even mid-pause.

> Run throttled scans from `hython` or a terminal, not the Houdini GUI. The scan is synchronous,
> so Houdini is unresponsive for its full duration — and throttling deliberately makes that
> duration much longer.

### Archive analysis

For every variant (the last configured level) containing an `ARCHIVE` folder, `archive.analyze`
answers two questions: how many dated archives it has, and which is the earliest one containing
an `RSTEXBIN` folder. Dated folder names are matched against `config.archive_date_patterns`
(`YYYY-MM-DD`, `YYYY_MM_DD`, `YYYYMMDD`, each with an optional trailing suffix, and
`YYYYMMDD_HHMM`); anything that doesn't match is still counted but can never be "first", since
ordering without a date is a guess. This costs zero additional filesystem operations — the
crawler already enumerated every directory, so `analyze()` is a pure walk over the tree already
in memory. The HTML report includes a sortable, filterable **Archives** table built from the
same data.

### Variant summary from existing reports

`summarize_report` mines reports you have **already generated** and merges them into one
per-variant table. It re-scans nothing, so it puts no load on the storage at all.

```python
from storage_report import summarize_report

summarize_report(r"C:\reports")                  # every *.html in a folder, merged
summarize_report(r"C:\reports\*.html")           # or a glob
summarize_report([r"C:\reports\characters.html", r"C:\reports\props.html"])
summarize_report(r"C:\reports\characters.html")  # or a single file
# -> C:\reports\variant_summary.html  (+ variant_summary.csv)
```

Each report contributes one **type**, named from the last token of its scan root, so a set of
per-type scans merges into a single `type -> asset -> variant` report. Every variant row carries,
in order: the first `ARCHIVE` dated folder containing an `RSTEXBIN`, the number of dated archives,
and the sizes of `TEX`, `Root Files` and the variant's own `RSTEXBIN` — with the **total size
last**. Asset and type rows roll up to the earliest first-RSTEXBIN across their children.

Only markers inside `ARCHIVE/<dated>/` decide which archive was first; the variant's own
`RSTEXBIN` folder is reported for its size and never treated as a candidate.

> **Level depth.** These reports were scanned with the root pointing at a *type* directory, so the
> level names recorded inside them are shifted by one and are ignored — variants are located by
> depth (2 below each report's root). This is also why the RSTEXBIN badge in the original reports
> came out empty; see `docs/variant-summary-plan.md` §0. A report scanned from a different depth is
> detected and refused rather than silently merged with shifted rows; pass `variant_depth=` to
> override.

The output includes a **Diagnostics** section listing, per source report, how many variants were
found, how many had an `ARCHIVE`, how many folder names parsed as dates, how many contained the
marker, and sample names that failed to parse.

## Development

Run the full test suite (stdlib `unittest`, no pytest dependency — runs inside `hython` too):

```bash
python -m unittest discover -s tests -v
```

Generate a synthetic tree for manual/perf testing:

```bash
python tests/make_tree.py /path/to/dest --types 10 --assets 100 --variants 5 --files-per-variant 20
```

`tests/test_purity.py` is a static guard for the package: no `hou`/Qt/`toolutils` imports, no
`os.walk`/`open()`/hashing/thread-spawning shortcuts anywhere under `storage_report/`, and the
package imports and runs end to end even with `hou`/`PySide6` forced unavailable.

## Known limitations

- Apparent size (`st_size`), not on-disk/allocated size; disclosed in every report's footer.
- Hard-linked files are counted once per link, not deduplicated by inode.
- Single scan root per report; the model supports a synthetic multi-root parent if that becomes
  a requirement, but `crawler.scan` doesn't expose it yet.
- A DFS `max_locations` cap returns a complete prefix in depth-first order, not a representative
  sample of the whole tree — it's a safety valve and smoke test, not statistical sampling.
