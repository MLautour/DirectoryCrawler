"""StorageReportDialog -- PySide6 QDialog front-end for storage_report, meant to
run inside Houdini. See docs/implementation-plan.md §11 for the threading design
this file implements: the crawler runs on a plain threading.Thread (never a
QThread, and the crawler package itself never imports Qt), and a small QObject
bridge is the only thing allowed to touch a widget as a result of worker-thread
activity -- it does so by emitting a queued Qt signal, never by calling into a
widget directly.
"""

from __future__ import annotations

import logging
import threading
import webbrowser
from pathlib import Path

try:
    from PySide6 import QtCore, QtWidgets
except ImportError:  # pragma: no cover - older Houdini builds (PySide2 / Qt5)
    from PySide2 import QtCore, QtWidgets  # type: ignore[no-redef]

import hou

from storage_report import crawler, html_report
from storage_report.config import Config, DEFAULT_EXCLUDES
from storage_report.crawler import Progress
from storage_report.model import RootNode

logger = logging.getLogger(__name__)


class _ProgressBridge(QtCore.QObject):
    """Created on the UI thread. `emit_progress`/`emit_finished` are called
    from the worker thread but only ever emit a Qt signal there -- the queued
    connection means the connected slots run on the UI thread. This is the
    only thing standing between the worker thread and touching a widget (or
    `hou`) directly, which is the classic way to crash Houdini.
    """

    progress = QtCore.Signal(object)  # Progress
    finished = QtCore.Signal(object, object)  # (RootNode | None, Exception | None)

    def emit_progress(self, progress: Progress) -> None:
        self.progress.emit(progress)

    def emit_finished(self, tree: RootNode | None, error: Exception | None) -> None:
        self.finished.emit(tree, error)


class StorageReportDialog(QtWidgets.QDialog):
    def __init__(self, parent: "QtWidgets.QWidget | None" = None) -> None:
        super().__init__(parent or _main_window())
        self.setWindowTitle("Storage Report")
        self.setMinimumWidth(560)

        self._bridge = _ProgressBridge()
        self._bridge.progress.connect(self._on_progress, QtCore.Qt.QueuedConnection)
        self._bridge.finished.connect(self._on_finished, QtCore.Qt.QueuedConnection)

        self._cancel_event: threading.Event | None = None
        self._worker: threading.Thread | None = None

        self._build_ui()

    # ---- UI construction ----

    def _build_ui(self) -> None:
        layout = QtWidgets.QVBoxLayout(self)

        form = QtWidgets.QFormLayout()

        self._root_edit = QtWidgets.QLineEdit()
        root_browse = QtWidgets.QPushButton("Browse…")
        root_browse.clicked.connect(self._pick_root)
        form.addRow("Root Directory", _row(self._root_edit, root_browse))

        self._output_edit = QtWidgets.QLineEdit()
        output_browse = QtWidgets.QPushButton("Browse…")
        output_browse.clicked.connect(self._pick_output)
        form.addRow("Output HTML", _row(self._output_edit, output_browse))

        self._exclude_edit = QtWidgets.QLineEdit(", ".join(DEFAULT_EXCLUDES))
        form.addRow("Exclude Patterns", self._exclude_edit)

        self._log_edit = QtWidgets.QLineEdit()
        log_browse = QtWidgets.QPushButton("Browse…")
        log_browse.clicked.connect(self._pick_log)
        form.addRow("Log File (optional)", _row(self._log_edit, log_browse))

        self._sort_combo = QtWidgets.QComboBox()
        self._sort_combo.addItems(["size", "name"])
        form.addRow("Sort", self._sort_combo)

        self._open_after_checkbox = QtWidgets.QCheckBox("Open report in browser when finished")
        self._open_after_checkbox.setChecked(True)
        form.addRow("", self._open_after_checkbox)

        layout.addLayout(form)

        self._progress_bar = QtWidgets.QProgressBar()
        self._progress_bar.setRange(0, 0)  # indeterminate until the asset count is known
        layout.addWidget(self._progress_bar)

        self._current_folder_label = QtWidgets.QLabel("")
        self._current_folder_label.setWordWrap(True)
        layout.addWidget(self._current_folder_label)

        self._counters_label = QtWidgets.QLabel("")
        layout.addWidget(self._counters_label)

        button_row = QtWidgets.QHBoxLayout()
        self._start_button = QtWidgets.QPushButton("Start")
        self._start_button.clicked.connect(self._on_start)
        self._cancel_button = QtWidgets.QPushButton("Cancel")
        self._cancel_button.clicked.connect(self._on_cancel)
        self._cancel_button.setEnabled(False)
        button_row.addStretch(1)
        button_row.addWidget(self._cancel_button)
        button_row.addWidget(self._start_button)
        layout.addLayout(button_row)

    # ---- pickers ----

    def _pick_root(self) -> None:
        path = QtWidgets.QFileDialog.getExistingDirectory(self, "Select Root Directory")
        if not path:
            return
        self._root_edit.setText(path)
        if not self._output_edit.text().strip():
            self._output_edit.setText(str(Path(path) / "storage_report.html"))

    def _pick_output(self) -> None:
        path, _filter = QtWidgets.QFileDialog.getSaveFileName(self, "Select Output HTML", filter="HTML Files (*.html)")
        if path:
            self._output_edit.setText(path)

    def _pick_log(self) -> None:
        path, _filter = QtWidgets.QFileDialog.getSaveFileName(self, "Select Log File", filter="Log Files (*.log *.txt)")
        if path:
            self._log_edit.setText(path)

    # ---- scan lifecycle ----

    def _on_start(self) -> None:
        root = self._root_edit.text().strip()
        output = self._output_edit.text().strip()
        if not root or not output:
            QtWidgets.QMessageBox.warning(self, "Storage Report", "Root directory and output HTML are required.")
            return

        log_path = self._log_edit.text().strip()
        if log_path:
            _attach_file_handler(log_path)

        excludes = tuple(p.strip() for p in self._exclude_edit.text().split(",") if p.strip())
        config = Config(excludes=excludes or tuple(DEFAULT_EXCLUDES), sort=self._sort_combo.currentText())

        self._cancel_event = threading.Event()
        self._start_button.setEnabled(False)
        self._cancel_button.setEnabled(True)
        self._progress_bar.setRange(0, 0)
        self._counters_label.setText("")
        self._current_folder_label.setText("Starting…")

        self._worker = threading.Thread(
            target=_run_scan,
            args=(root, output, config, self._bridge, self._cancel_event),
            daemon=True,
        )
        self._worker.start()

    def _on_cancel(self) -> None:
        if self._cancel_event is not None:
            self._cancel_event.set()
        self._cancel_button.setEnabled(False)
        self._current_folder_label.setText("Cancelling…")

    def _on_progress(self, progress: Progress) -> None:
        if progress.total_units:
            self._progress_bar.setRange(0, progress.total_units)
            self._progress_bar.setValue(progress.completed_units)
        else:
            self._progress_bar.setRange(0, 0)
        self._current_folder_label.setText(progress.current_folder)
        level_text = " / ".join(f"{k}: {v}" for k, v in progress.levels.items() if v)
        self._counters_label.setText(
            f"{level_text}    Files: {progress.files:,}    Directories: {progress.directories:,}"
        )

    def _on_finished(self, tree: RootNode | None, error: Exception | None) -> None:
        self._start_button.setEnabled(True)
        self._cancel_button.setEnabled(False)
        self._worker = None
        self._cancel_event = None

        if error is not None or tree is None:
            self._current_folder_label.setText("Failed.")
            QtWidgets.QMessageBox.critical(self, "Storage Report", f"Scan failed: {error}")
            return

        self._progress_bar.setRange(0, 1)
        self._progress_bar.setValue(1)

        if tree.stats is not None and tree.stats.cancelled:
            self._current_folder_label.setText("Cancelled — partial report written.")
        else:
            self._current_folder_label.setText("Done.")

        output = self._output_edit.text().strip()
        if self._open_after_checkbox.isChecked() and output:
            webbrowser.open(Path(output).resolve().as_uri())

    def closeEvent(self, event: "QtCore.QEvent") -> None:  # noqa: N802 - Qt override
        if self._cancel_event is not None:
            self._cancel_event.set()
        if self._worker is not None and self._worker.is_alive():
            self._worker.join(timeout=5.0)
        super().closeEvent(event)


def _row(*widgets: "QtWidgets.QWidget") -> QtWidgets.QHBoxLayout:
    layout = QtWidgets.QHBoxLayout()
    for widget in widgets:
        layout.addWidget(widget)
    return layout


def _run_scan(
    root: str,
    output: str,
    config: Config,
    bridge: _ProgressBridge,
    cancel_event: threading.Event,
) -> None:
    try:
        tree = crawler.scan(root, config, progress_callback=bridge.emit_progress, cancel_event=cancel_event)
        html_report.write(tree, output, sort=config.sort)
    except Exception as exc:  # surfaced to the UI thread via the bridge, never swallowed
        logger.exception("storage_report scan failed")
        bridge.emit_finished(None, exc)
        return
    bridge.emit_finished(tree, None)


def _attach_file_handler(log_path: str) -> None:
    root_logger = logging.getLogger("storage_report")
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.INFO)


def _main_window() -> "QtWidgets.QWidget | None":
    try:
        return hou.qt.mainWindow()
    except Exception:
        return None
