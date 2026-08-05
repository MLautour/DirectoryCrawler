from __future__ import annotations

import sys
import unittest

from storage_report.utils import (
    build_exclusion_matcher,
    display_path,
    format_duration,
    format_size,
    normalize_root_for_scan,
)
from storage_report.config import DEFAULT_EXCLUDES


class TestFormatSize(unittest.TestCase):
    def test_zero(self) -> None:
        self.assertEqual(format_size(0), "0 B")

    def test_bytes_below_1024(self) -> None:
        self.assertEqual(format_size(1), "1 B")
        self.assertEqual(format_size(1023), "1023 B")

    def test_kb_boundary(self) -> None:
        self.assertEqual(format_size(1024), "1.00 KB")

    def test_mb_boundary(self) -> None:
        self.assertEqual(format_size(1024 * 1024), "1.00 MB")

    def test_gb_boundary(self) -> None:
        self.assertEqual(format_size(1024**3), "1.00 GB")

    def test_tb_boundary(self) -> None:
        self.assertEqual(format_size(1024**4), "1.00 TB")

    def test_pb_boundary(self) -> None:
        self.assertEqual(format_size(1024**5), "1.00 PB")

    def test_beyond_pb_stays_in_pb(self) -> None:
        self.assertEqual(format_size(1024**6), "1024.00 PB")

    def test_just_below_kb_boundary(self) -> None:
        self.assertEqual(format_size(1048000), "1023.44 KB")

    def test_rounding_up_to_next_unit_boundary_rolls_over(self) -> None:
        # 1024*1024 - 1 bytes is 1023.9990234375 KB, which rounds to "1024.00 KB"
        # at 2 decimals -- that reads as belonging to the next unit, so it should
        # display as the next unit instead of a misleading "1024.00 KB".
        self.assertEqual(format_size(1024 * 1024 - 1), "1.00 MB")

    def test_negative_raises(self) -> None:
        with self.assertRaises(ValueError):
            format_size(-1)


class TestFormatDuration(unittest.TestCase):
    def test_zero(self) -> None:
        self.assertEqual(format_duration(0), "0s")

    def test_seconds_only(self) -> None:
        self.assertEqual(format_duration(45), "45s")

    def test_minutes_and_seconds(self) -> None:
        self.assertEqual(format_duration(187), "3m 07s")

    def test_hours_minutes_seconds(self) -> None:
        self.assertEqual(format_duration(3723), "1h 02m 03s")

    def test_negative_raises(self) -> None:
        with self.assertRaises(ValueError):
            format_duration(-1)


class TestExclusionMatcher(unittest.TestCase):
    def test_empty_patterns_matches_nothing(self) -> None:
        is_excluded = build_exclusion_matcher(())
        self.assertFalse(is_excluded("anything.tmp"))

    def test_default_excludes(self) -> None:
        is_excluded = build_exclusion_matcher(tuple(DEFAULT_EXCLUDES))
        self.assertTrue(is_excluded("file.tmp"))
        self.assertTrue(is_excluded("backup.bak"))
        self.assertTrue(is_excluded("Thumbs.db"))
        self.assertTrue(is_excluded("__pycache__"))
        self.assertTrue(is_excluded(".git"))
        self.assertFalse(is_excluded("shot010.ma"))
        self.assertFalse(is_excluded("file.tmpx"))

    @unittest.skipUnless(sys.platform == "win32", "case-insensitivity is a Windows-only guarantee")
    def test_case_insensitive_on_windows(self) -> None:
        is_excluded = build_exclusion_matcher(("*.TMP",))
        self.assertTrue(is_excluded("file.tmp"))
        self.assertTrue(is_excluded("FILE.TMP"))

    @unittest.skipIf(sys.platform == "win32", "case-sensitivity is a non-Windows guarantee")
    def test_case_sensitive_elsewhere(self) -> None:
        is_excluded = build_exclusion_matcher(("*.TMP",))
        self.assertTrue(is_excluded("file.TMP"))
        self.assertFalse(is_excluded("file.tmp"))


class TestPathHelpers(unittest.TestCase):
    def test_display_path_roundtrips_non_windows_prefix(self) -> None:
        self.assertEqual(display_path("D:\\Projects\\Show"), "D:\\Projects\\Show")

    @unittest.skipUnless(sys.platform == "win32", "extended-length prefixing is Windows-only")
    def test_normalize_and_display_roundtrip_local(self) -> None:
        normalized = normalize_root_for_scan("C:\\Projects")
        self.assertTrue(normalized.startswith("\\\\?\\"))
        self.assertEqual(display_path(normalized), "C:\\Projects")

    @unittest.skipUnless(sys.platform == "win32", "extended-length prefixing is Windows-only")
    def test_normalize_and_display_roundtrip_unc(self) -> None:
        normalized = normalize_root_for_scan("\\\\server\\share\\Projects")
        self.assertTrue(normalized.startswith("\\\\?\\UNC\\"))
        self.assertEqual(display_path(normalized), "\\\\server\\share\\Projects")


if __name__ == "__main__":
    unittest.main()
