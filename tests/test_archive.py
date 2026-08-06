from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from storage_report import archive
from storage_report.config import Config
from storage_report.crawler import scan

from tests.make_tree import build_archive_example, build_brief_example


class TestAnalyzeOnFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        build_archive_example(self.root)
        self.tree = scan(str(self.root), Config())

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _by_variant(self, name: str) -> archive.ArchiveInfo:
        return next(a for a in archive.analyze(self.tree) if a.variant_path.endswith(name))

    def test_mixed_date_formats_all_parse(self) -> None:
        info = self._by_variant("High")
        self.assertEqual(info.archive_count, 4)
        by_name = {d.name: d for d in info.dated}
        self.assertEqual(by_name["2024-01-15"].date, date(2024, 1, 15))
        self.assertEqual(by_name["2024-02-20"].date, date(2024, 2, 20))
        self.assertEqual(by_name["2024_04_05"].date, date(2024, 4, 5))

    def test_unparsed_name_excluded_from_first_but_still_counted(self) -> None:
        info = self._by_variant("High")
        self.assertIn("misc_backup", info.unparsed)
        self.assertEqual(info.archive_count, 4)  # unparsed folder still counts
        self.assertNotEqual(info.first_rstexbin, "misc_backup")

    def test_first_rstexbin_is_earliest_dated_with_marker(self) -> None:
        info = self._by_variant("High")
        self.assertEqual(info.first_rstexbin, "2024-01-15")
        self.assertEqual(info.first_rstexbin_date, date(2024, 1, 15))
        self.assertEqual(info.first_rstexbin_index, 1)
        self.assertEqual(info.rstexbin_count, 1)

    def test_empty_archive_folder_yields_zero_count_not_dropped(self) -> None:
        info = self._by_variant("Low")
        self.assertEqual(info.archive_count, 0)
        self.assertEqual(info.dated, ())
        self.assertIsNone(info.first_rstexbin)

    def test_variant_with_no_archive_folder_yields_no_entry(self) -> None:
        variants = {a.variant_path for a in archive.analyze(self.tree)}
        self.assertFalse(any(p.endswith("Default") for p in variants))

    def test_levels_reflect_ancestor_chain(self) -> None:
        info = self._by_variant("High")
        self.assertEqual(info.levels, {"type": "Characters", "asset": "Dragon", "variant": "High"})

    def test_zero_filesystem_calls(self) -> None:
        with mock.patch("os.scandir") as mocked:
            archive.analyze(self.tree)
        mocked.assert_not_called()


class TestAnalyzeNoArchives(unittest.TestCase):
    def test_no_variants_have_archive_returns_empty_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_brief_example(root)
            tree = scan(str(root), Config())
            self.assertEqual(archive.analyze(tree), [])


class TestArchiveMarkerRecursive(unittest.TestCase):
    def test_recursive_marker_found_deeper_than_direct_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dated = root / "Type" / "Asset" / "Variant" / "ARCHIVE" / "2024-05-01"
            (dated / "textures" / "RSTEXBIN").mkdir(parents=True, exist_ok=True)
            (dated / "textures" / "RSTEXBIN" / "a.bin").write_bytes(b"x" * 10)

            tree = scan(str(root), Config())

            non_recursive = archive.analyze(tree, Config())[0]
            self.assertEqual(non_recursive.rstexbin_count, 0)

            recursive = archive.analyze(tree, Config(archive_marker_recursive=True))[0]
            self.assertEqual(recursive.rstexbin_count, 1)
            self.assertEqual(recursive.first_rstexbin, "2024-05-01")


if __name__ == "__main__":
    unittest.main()
