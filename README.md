# storage_report

A reusable Python package that scans a configurable VFX asset hierarchy on shared
NAS/SAN storage, aggregates apparent file sizes into an in-memory tree, and renders
that tree to one self-contained HTML report. The core package is standard-library
only; a thin PySide6 dialog makes it usable from Houdini today, and the same
package drops into Maya, Nuke, or a render-farm job with no changes.

See [docs/implementation-plan.md](docs/implementation-plan.md) for the full design
rationale behind every decision below.

## Requirements

- Python 3.11+ for `storage_report` (core package: standard library only).
- PySide6 for `houdini/` (the Houdini front-end only; never required to use the core package).

## Install

Nothing to build — put the repo root on `sys.path` (or install it editable) and import:

```bash
pip install -e .   # if you add a pyproject.toml/setup.cfg for your deployment; not required otherwise
```

Or simply add the repo root to `PYTHONPATH`. There is no compiled extension and no
external dependency for the core package.

## Usage

```python
from storage_report import crawler, html_report, Config

tree = crawler.scan(r"D:\Projects\ShowA\assets", Config())
html_report.write(tree, "report.html")
```

With progress reporting and cancellation (e.g. from a UI or a long batch job):

```python
import threading
from storage_report import crawler, html_report, Config

cancel_event = threading.Event()

def on_progress(p):
    print(f"{p.current_folder}  files={p.files}  dirs={p.directories}  "
          f"{p.completed_units}/{p.total_units} assets")

tree = crawler.scan(root, Config(), progress_callback=on_progress, cancel_event=cancel_event)
html_report.write(tree, "report.html", sort="size")
```

`crawler.scan` never follows symlinks, never opens file contents, and performs at
most one `stat()` per file and zero `stat()` calls on directories. It is safe to
call from any thread (it spawns none of its own) and returns a valid, aggregated
partial tree if cancelled mid-scan.

### Configuration

```python
from storage_report import Config

config = Config(
    levels=("show", "sequence", "shot"),  # any depth; defaults to ("type", "asset", "variant")
    excludes=("*.tmp", "*.bak", "Thumbs.db", "__pycache__", ".git"),
    sort="size",              # or "name"
    max_folder_depth=None,    # cap Node creation below the last structural level; None = unlimited
    progress_interval=0.5,    # seconds between progress_callback invocations
)
```

Everything in the report — node types, colours, the toolbar's per-level
expand/collapse buttons, and the "Largest X" summary rows — is derived from
`levels`, so reconfiguring it reconfigures the whole tool with no other change.

## Houdini integration

Shelf tool body (already wired by the package, see below):

```python
from houdini import launcher
launcher.show()
```

### Deploying the Houdini package

1. Point Houdini at this repo via a package file. The one shipped at
   `houdini/package/storage_report.json` does this automatically when copied
   (or symlinked) into a package search path, e.g.
   `$HOUDINI_USER_PREF_DIR/packages/`:

   ```bash
   cp houdini/package/storage_report.json "$HOUDINI_USER_PREF_DIR/packages/"
   ```

   The package computes the repo root as `$HOUDINI_PACKAGE_DIR/../..` relative to
   its own file, i.e. it assumes `storage_report.json` stays at
   `<repo>/houdini/package/storage_report.json`. If you'd rather deploy the
   package file itself elsewhere (e.g. a studio-wide packages directory), set
   `STORAGE_REPORT_ROOT` explicitly as a real environment variable before
   Houdini launches instead of relying on the relative computation.

2. Launch Houdini. The package puts the repo root on `PYTHONPATH` (so
   `import storage_report` and `import houdini.launcher` both resolve) and adds
   `houdini/package` to `HOUDINI_PATH`, which auto-registers the "Storage Report"
   shelf tool from `houdini/package/toolbar/storage_report.shelf`.

3. Click the shelf tool, or run `from houdini import launcher; launcher.show()`
   from the Python Shell.

**Naming note:** the top-level `houdini/` package name is generic enough to risk
colliding with something else on a studio `PYTHONPATH`. Consider renaming it to
`storage_report_houdini/` before a wider deployment (see plan §16).

### Reuse from Maya / Nuke / a render-farm job

The core package has no dependency on Houdini or any DCC. To reuse it:

```python
from storage_report import crawler, html_report, Config
tree = crawler.scan(root, Config())
html_report.write(tree, output_path)
```

Wire this into a Maya shelf button, a Nuke menu item, or a farm job's Python step
exactly the same way `houdini/launcher.py` wires it into a shelf tool — the only
DCC-specific work is the UI (or lack of one) around this call. `tests/test_layering.py`
enforces that `storage_report` never imports `hou`/Qt, so this is a guaranteed
contract, not a convention that can silently rot.

## Development

Run the full test suite (stdlib `unittest`, no pytest dependency — runs inside `hython` too):

```bash
python -m unittest discover -s tests -v
```

Generate a synthetic tree for manual/perf testing:

```bash
python tests/make_tree.py /path/to/dest --types 10 --assets 100 --variants 5 --files-per-variant 20
```

`tests/test_layering.py` enforces the `storage_report` / `houdini` boundary: no
`hou`/Qt imports in the core package, no `os.walk`/`open()`/hashing/thread-spawning
shortcuts, and `storage_report` never imports `houdini`.

## Known limitations (see plan §16–17)

- Apparent size (`st_size`), not on-disk/allocated size; disclosed in every report's footer.
- Hard-linked files are counted once per link, not deduplicated by inode.
- Single scan root per report; the model supports a synthetic multi-root parent
  if that becomes a requirement, but `crawler.scan` doesn't expose it yet.
- The Houdini layer targets PySide6 (Houdini 20.5+); a ~12-line PySide2 fallback
  shim is already in `houdini/dialog.py`'s imports for older Qt5 builds.
