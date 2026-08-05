"""show() -- the only symbol a shelf tool needs.

Shelf tool body is just:

    from houdini import launcher
    launcher.show()
"""

from __future__ import annotations

import importlib
import logging

logger = logging.getLogger(__name__)

_dialog = None  # module-level reference: a locally-scoped PySide dialog is
# garbage-collected the moment a function returns and its window vanishes --
# the single most common Houdini/PySide bug. Keeping it here is what prevents it.


def show(reload_modules: bool = False) -> None:
    """Create-or-raise the dialog. Safe to call repeatedly from a shelf tool."""
    global _dialog

    if reload_modules:
        _reload_for_development()

    from houdini.dialog import StorageReportDialog

    if _dialog is not None:
        try:
            _dialog.raise_()
            _dialog.activateWindow()
            return
        except RuntimeError:
            # underlying Qt widget was already destroyed (e.g. closed by the user)
            _dialog = None

    _dialog = StorageReportDialog()
    _dialog.show()
    _dialog.raise_()
    _dialog.activateWindow()


def _reload_for_development() -> None:
    """Reload storage_report + houdini.dialog for iterative development inside
    a running Houdini session, where Python modules otherwise stay cached for
    the life of the process.
    """
    import storage_report
    import storage_report.config
    import storage_report.crawler
    import storage_report.html_report
    import storage_report.model
    import storage_report.utils

    import houdini.dialog

    for module in (
        storage_report.utils,
        storage_report.config,
        storage_report.model,
        storage_report.crawler,
        storage_report.html_report,
        storage_report,
        houdini.dialog,
    ):
        try:
            importlib.reload(module)
        except Exception:
            logger.exception("failed to reload %s", module.__name__)
