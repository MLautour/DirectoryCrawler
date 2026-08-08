from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from storage_report import crawler, html_report
from storage_report.config import Config
from storage_report.model import Node, NodeType, RootNode, ScanStats, aggregate
from storage_report.report_reader import ReportFormatError, last_path_token, read_report

from tests.make_tree import build_archive_example, build_brief_example


def _walk(node: Node):
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        for c in n.children or ():
            stack.append(c)


class TestRoundTrip(unittest.TestCase):
    """scan -> write -> read must recover the tree the report was built from."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        build_brief_example(self.root)
        build_archive_example(self.root)
        self.tree = crawler.scan(str(self.root), Config())
        self.out = self.root / "report.html"
        html_report.write(self.tree, self.out)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_structure_matches_node_for_node(self) -> None:
        recovered = read_report(self.out).tree

        def shape(node):
            return sorted(
                (n.name, str(n.type), n.size, n.file_count, n.depth) for n in _walk(node)
            )

        self.assertEqual(shape(recovered), shape(self.tree))

    def test_parent_links_and_paths_are_rebuilt(self) -> None:
        recovered = read_report(self.out).tree
        for node in _walk(recovered):
            for child in node.children or ():
                self.assertIs(child.parent, node)
        names = {n.name: n for n in _walk(recovered)}
        self.assertTrue(names["High"].path.endswith("Characters\\Dragon\\High")
                        or names["High"].path.endswith("Characters/Dragon/High"))

    def test_root_is_a_RootNode(self) -> None:
        self.assertIsInstance(read_report(self.out).tree, RootNode)

    def test_scan_root_and_type_name_recovered(self) -> None:
        report = read_report(self.out)
        self.assertTrue(report.scan_root.endswith(self.root.name))
        self.assertEqual(report.type_name, self.root.name)


class TestAwkwardNames(unittest.TestCase):
    """Built in memory: Windows forbids '<' and '>' in real directory names, but
    the escaping path still has to survive them (e.g. on a Linux-hosted share).
    """

    def _report_with(self, name: str) -> Path:
        tmp = Path(tempfile.mkdtemp())
        stats = ScanStats(
            root="root", start_time=datetime(2026, 1, 1), end_time=datetime(2026, 1, 1),
            total_files=0, total_dirs=1, total_size=0, levels=("type", "asset", "variant"),
        )
        root = RootNode(name="root", type=NodeType.ROOT, depth=0, stats=stats)
        child = Node(name=name, type="type", parent=root, depth=1, size=10, own_size=10)
        root.children = [child]
        aggregate(root)
        out = tmp / "r.html"
        html_report.write(root, out)
        return out

    def test_name_containing_script_close_tag(self) -> None:
        out = self._report_with("a</script>b")
        names = {n.name for n in _walk(read_report(out).tree)}
        self.assertIn("a</script>b", names)

    def test_name_containing_html_comment_open(self) -> None:
        """The writer escapes '<!--' as '<\\!--', which is valid JS but invalid
        JSON -- the reader must undo it or json.loads raises.
        """
        out = self._report_with("a<!--b")
        names = {n.name for n in _walk(read_report(out).tree)}
        self.assertIn("a<!--b", names)

    def test_unicode_name(self) -> None:
        out = self._report_with("Ünïcode 名前")
        names = {n.name for n in _walk(read_report(out).tree)}
        self.assertIn("Ünïcode 名前", names)


class TestErrors(unittest.TestCase):
    def test_non_report_html_raises_with_a_clear_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "other.html"
            path.write_text("<html><body>not a report</body></html>", encoding="utf-8")
            with self.assertRaises(ReportFormatError) as ctx:
                read_report(path)
            self.assertIn("const NODES", str(ctx.exception))


class TestLastPathToken(unittest.TestCase):
    def test_windows_unc_and_trailing_separators(self) -> None:
        self.assertEqual(last_path_token(r"\\nas\projects\assets\Characters"), "Characters")
        self.assertEqual(last_path_token(r"D:\assets\Props\\"), "Props")
        self.assertEqual(last_path_token("/mnt/assets/Vehicles"), "Vehicles")
        self.assertEqual(last_path_token("Characters"), "Characters")


if __name__ == "__main__":
    unittest.main()
