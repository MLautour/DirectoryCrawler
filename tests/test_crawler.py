from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from storage_report.config import Config
from storage_report.crawler import Progress, scan
from storage_report.model import NodeType

from tests.make_tree import build_brief_example


class _CountingEntry:
    """Proxies a real os.DirEntry, counting .stat() calls. Everything else
    (name, path, is_dir, is_file, is_junction, ...) delegates via __getattr__,
    so it behaves like a real DirEntry to code that doesn't stat() it.
    """

    def __init__(self, entry: "os.DirEntry[str]", counters: dict, stated_paths: list) -> None:
        self._entry = entry
        self._counters = counters
        self._stated_paths = stated_paths

    def __getattr__(self, item):
        return getattr(self._entry, item)

    def stat(self, *, follow_symlinks: bool = True):
        self._counters["stat"] += 1
        self._stated_paths.append(self._entry.path)
        return self._entry.stat(follow_symlinks=follow_symlinks)


class _CountingScandirIterator:
    def __init__(self, it, counters: dict, stated_paths: list) -> None:
        self._it = it
        self._counters = counters
        self._stated_paths = stated_paths

    def __iter__(self):
        return self

    def __next__(self):
        return _CountingEntry(next(self._it), self._counters, self._stated_paths)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._it.close()
        return False

    def close(self):
        self._it.close()


def _make_counting_scandir(counters: dict, stated_paths: list):
    real_scandir = os.scandir

    def _scandir(path="."):
        counters["scandir"] += 1
        return _CountingScandirIterator(real_scandir(path), counters, stated_paths)

    return _scandir


class TestScanBriefExample(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.expected = build_brief_example(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_totals(self) -> None:
        tree = scan(str(self.root), Config())
        total_expected = sum(self.expected.values())
        self.assertEqual(tree.size, total_expected)
        self.assertEqual(tree.stats.total_size, total_expected)
        self.assertFalse(tree.stats.cancelled)
        self.assertEqual(tree.stats.total_dirs, tree.dir_count)
        self.assertEqual(tree.stats.total_files, tree.file_count)

    def test_hierarchy_shape(self) -> None:
        tree = scan(str(self.root), Config())
        names = {c.name for c in tree.children}
        self.assertEqual(names, {"Characters", "Props"})

        characters = next(c for c in tree.children if c.name == "Characters")
        self.assertEqual(characters.type, "type")
        dragon = next(c for c in characters.children if c.name == "Dragon")
        self.assertEqual(dragon.type, "asset")
        variant_names = {c.name for c in dragon.children}
        self.assertEqual(variant_names, {"High", "Low"})
        for variant in dragon.children:
            self.assertEqual(variant.type, "variant")

    def test_no_root_files_when_no_direct_files(self) -> None:
        tree = scan(str(self.root), Config())
        self.assertNotIn(NodeType.ROOT_FILES, [c.type for c in tree.children])

    def test_root_files_created_when_direct_files_exist(self) -> None:
        (self.root / "loose_note.txt").write_bytes(b"hello")
        tree = scan(str(self.root), Config())
        root_files = [c for c in tree.children if c.type == NodeType.ROOT_FILES]
        self.assertEqual(len(root_files), 1)
        self.assertEqual(root_files[0].size, 5)
        self.assertEqual(root_files[0].file_count, 1)

    def test_root_files_at_asset_level_too(self) -> None:
        characters_dragon = self.root / "Characters" / "Dragon"
        (characters_dragon / "notes.txt").write_bytes(b"xy")
        tree = scan(str(self.root), Config())
        characters = next(c for c in tree.children if c.name == "Characters")
        dragon = next(c for c in characters.children if c.name == "Dragon")
        root_files = [c for c in dragon.children if c.type == NodeType.ROOT_FILES]
        self.assertEqual(len(root_files), 1)
        self.assertEqual(root_files[0].size, 2)
        # asset total must include the direct files alongside the variant subtrees
        self.assertEqual(dragon.size, self.expected["Characters/Dragon/High"] + self.expected["Characters/Dragon/Low"] + 2)


class TestExclusions(unittest.TestCase):
    def test_excluded_dirs_not_descended_and_excluded_files_never_stat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "__pycache__").mkdir()
            (root / "__pycache__" / "x.pyc").write_bytes(b"x" * 100)
            (root / "keep.txt").write_bytes(b"y" * 10)
            (root / "skip.tmp").write_bytes(b"z" * 20)

            counters = {"scandir": 0, "stat": 0}
            stated_paths: list = []
            with mock.patch("os.scandir", _make_counting_scandir(counters, stated_paths)):
                tree = scan(str(root), Config())

            self.assertEqual(tree.size, 10)
            self.assertFalse(any("skip.tmp" in p for p in stated_paths))
            self.assertFalse(any("__pycache__" in p for p in stated_paths))
            # __pycache__ itself is never scandir()'d since it's excluded before descending
            self.assertEqual(counters["scandir"], 1)


class TestMetadataBudget(unittest.TestCase):
    """§6.3: one scandir() per directory, zero stat() on directories, at most
    one stat() per file, zero on excluded files."""

    def test_scandir_and_stat_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_brief_example(root)

            counters = {"scandir": 0, "stat": 0}
            stated_paths: list = []
            with mock.patch("os.scandir", _make_counting_scandir(counters, stated_paths)):
                tree = scan(str(root), Config())

            # exactly one scandir() per real directory (root + every descendant)
            self.assertEqual(counters["scandir"], 1 + tree.dir_count)
            # exactly one stat() per file
            self.assertEqual(counters["stat"], tree.file_count)
            self.assertGreater(counters["stat"], 0)


@unittest.skipUnless(hasattr(os, "symlink"), "platform has no symlink support")
class TestSymlinks(unittest.TestCase):
    def test_symlinks_are_skipped_and_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "real").mkdir()
            (root / "real" / "f.txt").write_bytes(b"12345")
            try:
                os.symlink(root / "real", root / "link_to_real", target_is_directory=True)
            except OSError:
                self.skipTest("symlink creation not permitted in this environment")

            tree = scan(str(root), Config())
            self.assertEqual(tree.size, 5)  # only "real"'s file counted, symlink not followed
            names = {c.name for c in tree.children}
            self.assertIn("real", names)
            self.assertNotIn("link_to_real", names)
            self.assertEqual(tree.stats.skipped_total, 1)
            self.assertEqual(tree.stats.skipped[0].reason, "symlink")


class TestCancellation(unittest.TestCase):
    def test_cancel_returns_partial_but_valid_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_brief_example(root)

            event = threading.Event()
            call_count = {"n": 0}

            def progress_cb(progress: Progress) -> None:
                call_count["n"] += 1
                event.set()

            tree = scan(str(root), Config(progress_interval=0.0), progress_callback=progress_cb, cancel_event=event)
            self.assertTrue(tree.stats.cancelled)
            # aggregation must still be internally consistent on a partial tree
            self.assertGreaterEqual(tree.size, 0)
            self.assertEqual(tree.file_count, sum(
                c.file_count for c in (tree.children or [])
            ))


class TestErrorHandling(unittest.TestCase):
    def test_unreadable_directory_is_recorded_not_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "good").mkdir()
            (root / "good" / "f.txt").write_bytes(b"abc")
            (root / "bad").mkdir()

            real_scandir = os.scandir

            def _flaky_scandir(path="."):
                if os.path.basename(os.fspath(path)).rstrip("\\/") == "bad":
                    raise PermissionError(13, "Permission denied", str(path))
                return real_scandir(path)

            with mock.patch("os.scandir", _flaky_scandir):
                tree = scan(str(root), Config())

            self.assertEqual(tree.size, 3)
            self.assertEqual(tree.stats.skipped_total, 1)
            self.assertEqual(tree.stats.skipped[0].reason, "permission")
            self.assertFalse(tree.stats.cancelled)

    def test_file_vanishing_between_scandir_and_stat_is_recorded_not_raised(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "gone.txt").write_bytes(b"xyz")
            (root / "stays.txt").write_bytes(b"ab")

            real_stat = os.DirEntry.stat if hasattr(os.DirEntry, "stat") else None

            class _VanishingEntry:
                def __init__(self, entry):
                    self._entry = entry

                def __getattr__(self, item):
                    return getattr(self._entry, item)

                def stat(self, *, follow_symlinks=True):
                    if self._entry.name == "gone.txt":
                        raise FileNotFoundError(2, "No such file or directory", self._entry.path)
                    return self._entry.stat(follow_symlinks=follow_symlinks)

            real_scandir = os.scandir

            class _Iter:
                def __init__(self, it):
                    self._it = it

                def __iter__(self):
                    return self

                def __next__(self):
                    return _VanishingEntry(next(self._it))

                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    self._it.close()
                    return False

            def _scandir(path="."):
                return _Iter(real_scandir(path))

            with mock.patch("os.scandir", _scandir):
                tree = scan(str(root), Config())

            self.assertEqual(tree.size, 2)
            self.assertEqual(tree.stats.skipped_total, 1)
            self.assertEqual(tree.stats.skipped[0].reason, "os-error")


class TestProgressCallback(unittest.TestCase):
    def test_callback_receives_progress_and_is_throttled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_brief_example(root)
            calls: list[Progress] = []
            scan(str(root), Config(progress_interval=1000.0), progress_callback=calls.append)
            # throttled to effectively nothing mid-scan, but always fires once at the end
            self.assertGreaterEqual(len(calls), 1)
            self.assertIsInstance(calls[-1], Progress)
            self.assertEqual(set(calls[-1].levels), {"type", "asset", "variant"})

    def test_throwing_callback_is_disabled_not_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_brief_example(root)

            def bad_cb(progress: Progress) -> None:
                raise RuntimeError("boom")

            tree = scan(str(root), Config(progress_interval=0.0), progress_callback=bad_cb)
            self.assertGreater(tree.size, 0)  # scan still completed normally


class TestMaxFolderDepth(unittest.TestCase):
    def test_deep_folders_fold_into_nearest_retained_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "Type" / "Asset" / "Variant"
            deep = base / "d1" / "d2" / "d3"
            deep.mkdir(parents=True)
            (base / "d1" / "shallow.bin").write_bytes(b"a" * 10)
            (deep / "deep.bin").write_bytes(b"b" * 20)

            config = Config(max_folder_depth=1)
            tree = scan(str(root), config)

            variant = tree.children[0].children[0].children[0]
            self.assertEqual(variant.type, "variant")
            self.assertEqual(variant.size, 30)

            # exactly one folder node created (d1, at folder depth 1); d2/d3 folded in
            folder_nodes = [c for c in variant.children if c.type == NodeType.FOLDER]
            self.assertEqual(len(folder_nodes), 1)
            self.assertEqual(folder_nodes[0].name, "d1")
            self.assertEqual(folder_nodes[0].size, 30)
            self.assertIsNone(folder_nodes[0].children)


if __name__ == "__main__":
    unittest.main()
