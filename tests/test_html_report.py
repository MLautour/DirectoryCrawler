from __future__ import annotations

import json
import re
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from storage_report.config import Config
from storage_report import crawler, html_report
from storage_report.model import Node, NodeType, RootNode, ScanStats, SkippedPath, aggregate, sort_tree

from tests.make_tree import build_brief_example


def _extract_const(html: str, name: str) -> object:
    match = re.search(rf"const {name}=(.*?);\n", html, re.DOTALL)
    assert match is not None, f"const {name} not found in report"
    return json.loads(match.group(1))


class TestHtmlReportStructure(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        build_brief_example(self.root)
        self.tree = crawler.scan(str(self.root), Config())
        self.out = Path(self._tmp.name) / "report.html"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_writes_file(self) -> None:
        html_report.write(self.tree, self.out)
        self.assertTrue(self.out.is_file())
        self.assertGreater(self.out.stat().st_size, 0)

    def test_no_external_resources(self) -> None:
        html_report.write(self.tree, self.out)
        text = self.out.read_text(encoding="utf-8")
        self.assertNotIn("http://", text)
        self.assertNotIn("https://", text)
        self.assertNotIn("//cdn", text)
        self.assertNotIn("<link", text)
        self.assertNotIn("<script src", text)

    def test_payload_round_trips_through_json(self) -> None:
        html_report.write(self.tree, self.out)
        text = self.out.read_text(encoding="utf-8")
        nodes = _extract_const(text, "NODES")
        parents = _extract_const(text, "PARENT")
        meta = _extract_const(text, "META")
        self.assertEqual(len(nodes), len(parents))
        self.assertIn("types", meta)
        # root is index 0 with parent -1
        self.assertEqual(parents[0], -1)
        self.assertEqual(nodes[0][2], self.tree.size)

    def test_summary_contains_expected_fields(self) -> None:
        html_report.write(self.tree, self.out)
        text = self.out.read_text(encoding="utf-8")
        self.assertIn("Scan Root", text)
        self.assertIn("Total Files", text)
        self.assertIn("Total Directories", text)
        self.assertIn("Total Storage", text)
        self.assertIn("Largest Type", text)
        self.assertIn("Largest Asset", text)
        self.assertIn("Largest Variant", text)

    def test_no_skipped_section_when_nothing_skipped(self) -> None:
        html_report.write(self.tree, self.out)
        text = self.out.read_text(encoding="utf-8")
        self.assertNotIn('class="skipped"', text)

    def test_incomplete_banner_absent_when_not_cancelled(self) -> None:
        html_report.write(self.tree, self.out)
        text = self.out.read_text(encoding="utf-8")
        self.assertNotIn("INCOMPLETE SCAN", text)

    def test_custom_title_used(self) -> None:
        html_report.write(self.tree, self.out, title="My Custom Report")
        text = self.out.read_text(encoding="utf-8")
        self.assertIn("<title>My Custom Report</title>", text)

    def test_sort_override_reorders_tree(self) -> None:
        html_report.write(self.tree, self.out, sort="name")
        names_before = [c.name for c in self.tree.children]
        self.assertEqual(names_before, sorted(names_before))


class TestHtmlReportSafeEmbedding(unittest.TestCase):
    def _tree_with_name(self, name: str) -> RootNode:
        stats = ScanStats(
            root="root", start_time=datetime(2024, 1, 1), end_time=datetime(2024, 1, 1),
            total_files=1, total_dirs=1, total_size=10, levels=("type",),
        )
        root = RootNode(name="root", type=NodeType.ROOT, depth=0, stats=stats)
        child = Node(name=name, type="type", parent=root, depth=1, own_size=10, file_count=1)
        root.children = [child]
        aggregate(root)
        sort_tree(root, "size")
        return root

    def test_script_breakout_name_survives_embedding(self) -> None:
        dangerous = "</script><img src=x onerror=alert(1)>"
        tree = self._tree_with_name(dangerous)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "report.html"
            html_report.write(tree, out)
            text = out.read_text(encoding="utf-8")
            self.assertNotIn("</script><img", text)  # the literal breakout sequence must not appear
            nodes = _extract_const(text, "NODES")
            names = [n[0] for n in nodes]
            self.assertIn(dangerous, names)  # but the real text must round-trip intact

    def test_unicode_name_survives_embedding(self) -> None:
        name = "\u00dcn\u00efcode \u540d\u524d"
        tree = self._tree_with_name(name)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "report.html"
            html_report.write(tree, out)
            text = out.read_text(encoding="utf-8")
            nodes = _extract_const(text, "NODES")
            names = [n[0] for n in nodes]
            self.assertIn(name, names)
            self.assertIn(name, text)  # stored raw (unescaped), per §10.2


class TestHtmlReportSkippedAndCancelled(unittest.TestCase):
    def test_skipped_section_present_when_skips_exist(self) -> None:
        stats = ScanStats(
            root="root", start_time=datetime(2024, 1, 1), end_time=datetime(2024, 1, 1),
            total_files=0, total_dirs=0, total_size=0,
            skipped=[SkippedPath(path="D:\\x\\y", reason="permission", detail="denied")],
            skipped_total=1, levels=("type",),
        )
        root = RootNode(name="root", type=NodeType.ROOT, depth=0, stats=stats)
        aggregate(root)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "report.html"
            html_report.write(root, out)
            text = out.read_text(encoding="utf-8")
            self.assertIn('class="skipped"', text)
            self.assertIn("permission", text)

    def test_incomplete_banner_present_when_cancelled(self) -> None:
        stats = ScanStats(
            root="root", start_time=datetime(2024, 1, 1), end_time=datetime(2024, 1, 1),
            total_files=0, total_dirs=0, total_size=0, cancelled=True, levels=("type",),
        )
        root = RootNode(name="root", type=NodeType.ROOT, depth=0, stats=stats)
        aggregate(root)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "report.html"
            html_report.write(root, out)
            text = out.read_text(encoding="utf-8")
            self.assertIn("INCOMPLETE SCAN", text)


class TestHtmlReportAtomicWrite(unittest.TestCase):
    def test_no_temp_file_left_behind(self) -> None:
        stats = ScanStats(
            root="root", start_time=datetime(2024, 1, 1), end_time=datetime(2024, 1, 1),
            total_files=0, total_dirs=0, total_size=0, levels=(),
        )
        root = RootNode(name="root", type=NodeType.ROOT, depth=0, stats=stats)
        aggregate(root)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "report.html"
            html_report.write(root, out)
            leftovers = [p for p in Path(tmp).iterdir() if p.name != "report.html"]
            self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
