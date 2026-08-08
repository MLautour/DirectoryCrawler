from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import date
from pathlib import Path

from storage_report import run, summarize_report
from storage_report.config import Config
from storage_report.report_reader import read_report
from storage_report.variant_summary import ReportStructureError, summarize


def _read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def build_variant(path: Path, *, tex=0, root_files=0, rstexbin=0, archives=()) -> None:
    """A variant shaped like the real ones: TEX, its own RSTEXBIN, and an
    ARCHIVE of dated folders some of which contain their own RSTEXBIN.
    """
    path.mkdir(parents=True, exist_ok=True)
    if tex:
        (path / "TEX").mkdir(exist_ok=True)
        (path / "TEX" / "t.exr").write_bytes(b"x" * tex)
    if root_files:
        (path / "asset.ma").write_bytes(b"x" * root_files)
    if rstexbin:
        (path / "RSTEXBIN").mkdir(exist_ok=True)
        (path / "RSTEXBIN" / "r.bin").write_bytes(b"x" * rstexbin)
    for name, has_marker in archives:
        dated = path / "ARCHIVE" / name
        dated.mkdir(parents=True, exist_ok=True)
        if has_marker:
            (dated / "RSTEXBIN").mkdir(exist_ok=True)
            (dated / "RSTEXBIN" / "r.bin").write_bytes(b"x" * 50)


class TestVariantMetrics(unittest.TestCase):
    """Scans with the root pointing at a TYPE directory, matching the real
    reports (plan §0), so the stored types are shifted and depth must be used.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.reports = base / "reports"
        self.reports.mkdir()

        characters = base / "assets" / "Characters"
        build_variant(
            characters / "Dragon" / "High",
            tex=9000, root_files=1200, rstexbin=5000,
            archives=[
                ("2026-08-05_09-00-00", False),          # earlier, but NO marker
                ("2026-08-07_10-30-00", True),           # the expected answer
                ("2026-08-08_11-00-00-noProcess", True),
            ],
        )
        build_variant(characters / "Dragon" / "Low", tex=3000, root_files=800)
        build_variant(
            characters / "Knight" / "High",
            tex=1000, root_files=100, rstexbin=200,
            archives=[("backup_final", True)],           # marker, unparseable name
        )
        props = base / "assets" / "Props"
        build_variant(props / "Sword" / "Default", tex=500, root_files=50,
                      archives=[("2025-01-02_08-00-00", True)])

        run(characters, self.reports / "characters.html")
        run(props, self.reports / "props.html")

        parsed = [read_report(p) for p in sorted(self.reports.glob("*.html"))]
        self.summaries, self.diagnostics = summarize(parsed, config=Config())
        self.by_key = {(s.type_name, s.asset_name, s.variant_name): s for s in self.summaries}

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_type_name_comes_from_each_report_root(self) -> None:
        self.assertEqual({s.type_name for s in self.summaries}, {"Characters", "Props"})

    def test_variant_is_a_child_of_the_asset(self) -> None:
        self.assertIn(("Characters", "Dragon", "High"), self.by_key)
        self.assertIn(("Characters", "Knight", "High"), self.by_key)

    def test_first_archive_skips_earlier_ones_without_a_marker(self) -> None:
        s = self.by_key[("Characters", "Dragon", "High")]
        self.assertEqual(s.first_archive_with_marker, "2026-08-07_10-30-00")
        self.assertEqual(s.first_archive_date, date(2026, 8, 7))
        self.assertEqual(s.first_archive_index, 2)   # 2nd of 3 chronologically

    def test_dated_archive_count(self) -> None:
        self.assertEqual(self.by_key[("Characters", "Dragon", "High")].dated_archive_count, 3)
        self.assertEqual(self.by_key[("Characters", "Dragon", "Low")].dated_archive_count, 0)
        self.assertEqual(self.by_key[("Props", "Sword", "Default")].dated_archive_count, 1)

    def test_sizes_are_reported_separately(self) -> None:
        s = self.by_key[("Characters", "Dragon", "High")]
        self.assertEqual(s.tex_size, 9000)
        self.assertEqual(s.root_files_size, 1200)
        self.assertEqual(s.rstexbin_size, 5000)

    def test_variant_rstexbin_is_never_the_first_archive(self) -> None:
        """The variant's own RSTEXBIN has a size but must not be considered when
        deciding which ARCHIVE folder was first to contain one.
        """
        s = self.by_key[("Characters", "Dragon", "Low")]
        build = self.by_key[("Characters", "Knight", "High")]
        self.assertEqual(s.rstexbin_size, 0)
        self.assertIsNone(s.first_archive_with_marker)
        # Knight has a variant-level RSTEXBIN of 200 and an undated archive one;
        # the variant-level folder must not leak into the archive answer.
        self.assertEqual(build.rstexbin_size, 200)
        self.assertIsNone(build.first_archive_with_marker)

    def test_undated_archive_with_marker_is_not_reported_as_none(self) -> None:
        s = self.by_key[("Characters", "Knight", "High")]
        self.assertEqual(s.first_archive_undated, "backup_final")
        self.assertEqual(s.first_label, "backup_final (undated)")

    def test_missing_folders_are_zero_not_errors(self) -> None:
        s = self.by_key[("Characters", "Dragon", "Low")]
        self.assertEqual(s.rstexbin_size, 0)
        self.assertEqual(s.dated_archive_count, 0)
        self.assertEqual(s.first_label, "—")

    def test_total_size_is_the_variant_total(self) -> None:
        s = self.by_key[("Characters", "Dragon", "Low")]
        self.assertEqual(s.total_size, 3000 + 800)

    def test_diagnostics_describe_each_source(self) -> None:
        chars = next(d for d in self.diagnostics if d.type_name == "Characters")
        self.assertEqual(chars.variants, 3)
        self.assertEqual(chars.with_archive, 2)
        self.assertEqual(chars.dated_total, 4)
        self.assertEqual(chars.dated_parsed, 3)
        self.assertEqual(chars.dated_with_marker, 3)
        self.assertIn("backup_final", chars.unparsed_samples)


class TestEndToEnd(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.reports = base / "reports"
        self.reports.mkdir()
        for type_name in ("Characters", "Props"):
            root = base / "assets" / type_name
            build_variant(root / "AssetA" / "Main", tex=100, root_files=10, rstexbin=5,
                          archives=[("2026-08-07_10-30-00", True)])
            run(root, self.reports / f"{type_name.lower()}.html")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_directory_input_merges_every_report(self) -> None:
        out = summarize_report(self.reports)
        self.assertTrue(out.is_file())
        text = out.read_text(encoding="utf-8")
        self.assertIn("Characters", text)
        self.assertIn("Props", text)
        self.assertIn("2026-08-07_10-30-00", text)

    def test_csv_written_alongside(self) -> None:
        out = summarize_report(self.reports)
        rows = _read_csv(out.with_suffix(".csv"))
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["type"] for r in rows}, {"Characters", "Props"})
        self.assertEqual(rows[0]["first_rstexbin_archive"], "2026-08-07_10-30-00")

    def test_explicit_list_input(self) -> None:
        out = summarize_report(sorted(self.reports.glob("*.html")),
                               output=self.reports / "merged.html")
        self.assertEqual(out.name, "merged.html")

    def test_single_report_input(self) -> None:
        out = summarize_report(self.reports / "props.html",
                               output=self.reports / "one.html")
        rows = _read_csv(out.with_suffix(".csv"))
        self.assertEqual({r["type"] for r in rows}, {"Props"})

    def test_rerunning_does_not_consume_its_own_output(self) -> None:
        summarize_report(self.reports)
        out = summarize_report(self.reports)          # variant_summary.html now exists
        rows = _read_csv(out.with_suffix(".csv"))
        self.assertEqual(len(rows), 2)

    def test_no_external_resources(self) -> None:
        text = summarize_report(self.reports).read_text(encoding="utf-8")
        for forbidden in ("http://", "https://", "<link", "<script src"):
            self.assertNotIn(forbidden, text)


class TestDepthValidation(unittest.TestCase):
    def test_wrong_depth_is_refused_with_guidance(self) -> None:
        """A report scanned from a different level must not be silently merged
        with rows shifted; it should name the file and show what it saw.
        """
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            # Root one level too high: root/Type/Asset/Variant means variants
            # sit at depth 3, not 2.
            build_variant(base / "assets" / "Characters" / "Dragon" / "High", tex=100)
            out = base / "r.html"
            run(base / "assets", out)
            with self.assertRaises(ReportStructureError) as ctx:
                summarize([read_report(out)])
            message = str(ctx.exception)
            self.assertIn("depth 2", message)
            self.assertIn("variant_depth", message)

    def test_override_makes_the_shifted_report_readable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            build_variant(base / "assets" / "Characters" / "Dragon" / "High",
                          tex=100, archives=[("2026-08-07_10-30-00", True)])
            out = base / "r.html"
            run(base / "assets", out)
            summaries, _ = summarize([read_report(out)], variant_depth=3)
            self.assertEqual(len(summaries), 1)
            self.assertEqual(summaries[0].variant_name, "High")
            self.assertEqual(summaries[0].first_archive_with_marker, "2026-08-07_10-30-00")


if __name__ == "__main__":
    unittest.main()
