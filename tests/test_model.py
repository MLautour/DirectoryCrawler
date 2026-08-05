from __future__ import annotations

import unittest
from datetime import datetime

from storage_report.model import Node, NodeType, RootNode, ScanStats, aggregate, sort_tree


def _mk(name: str, type_: str, parent: Node | None, depth: int, own_size: int = 0, file_count: int = 0) -> Node:
    return Node(name=name, type=type_, parent=parent, depth=depth, own_size=own_size, file_count=file_count)


class TestNodePath(unittest.TestCase):
    def test_root_only(self) -> None:
        root = _mk("D:\\Projects", NodeType.ROOT, None, 0)
        self.assertEqual(root.path, "D:\\Projects")

    def test_nested_path_and_caching(self) -> None:
        root = _mk("D:\\Projects", NodeType.ROOT, None, 0)
        char = _mk("Characters", "type", root, 1)
        dragon = _mk("Dragon", "asset", char, 2)
        self.assertIn("Characters", dragon.path)
        self.assertIn("Dragon", dragon.path)
        self.assertTrue(dragon.path.endswith("Dragon"))
        # cached: mutating name after first access must not change the cached path
        first = dragon.path
        dragon.name = "Renamed"
        self.assertEqual(dragon.path, first)


class TestAggregate(unittest.TestCase):
    def _build_brief_example(self) -> RootNode:
        """Characters/Dragon/{High,Low}, Props/Sword/Default, with known byte counts."""
        stats = ScanStats(
            root="root", start_time=datetime(2024, 1, 1), end_time=datetime(2024, 1, 1),
            total_files=0, total_dirs=0, total_size=0,
        )
        root = RootNode(name="root", type=NodeType.ROOT, depth=0, stats=stats)

        characters = _mk("Characters", "type", root, 1)
        dragon = _mk("Dragon", "asset", characters, 2)
        high = _mk("High", "variant", dragon, 3, own_size=1000, file_count=2)
        low = _mk("Low", "variant", dragon, 3, own_size=500, file_count=1)
        dragon.children = [high, low]
        characters.children = [dragon]

        props = _mk("Props", "type", root, 1)
        sword = _mk("Sword", "asset", props, 2)
        default = _mk("Default", "variant", sword, 3, own_size=250, file_count=1)
        sword.children = [default]
        props.children = [sword]

        root.children = [characters, props]
        return root

    def test_totals_roll_up(self) -> None:
        root = self._build_brief_example()
        aggregate(root)
        self.assertEqual(root.size, 1750)
        self.assertEqual(root.file_count, 4)
        # 2 types + 2 assets + 3 variants = 7 real directories
        self.assertEqual(root.dir_count, 7)

    def test_asset_total_includes_all_descendants(self) -> None:
        root = self._build_brief_example()
        aggregate(root)
        characters = root.children[0]
        dragon = characters.children[0]
        self.assertEqual(dragon.size, 1500)
        self.assertEqual(dragon.file_count, 3)

    def test_leaf_variant_size_equals_own_size(self) -> None:
        root = self._build_brief_example()
        aggregate(root)
        characters = root.children[0]
        dragon = characters.children[0]
        high = dragon.children[0]
        self.assertEqual(high.size, 1000)
        self.assertEqual(high.dir_count, 0)

    def test_root_files_contributes_to_parent_but_not_dir_count(self) -> None:
        stats = ScanStats(
            root="root", start_time=datetime(2024, 1, 1), end_time=datetime(2024, 1, 1),
            total_files=0, total_dirs=0, total_size=0,
        )
        root = RootNode(name="root", type=NodeType.ROOT, depth=0, stats=stats)
        asset_type = _mk("Characters", "type", root, 1)
        asset = _mk("Dragon", "asset", asset_type, 2)
        root_files = _mk("Root Files", NodeType.ROOT_FILES, asset, 3, own_size=42, file_count=1)
        root_files.size = 42
        asset.children = [root_files]
        asset_type.children = [asset]
        root.children = [asset_type]

        aggregate(root)
        self.assertEqual(asset.size, 42)
        self.assertEqual(asset.file_count, 1)
        self.assertEqual(asset.dir_count, 0)  # root_files is not a directory
        self.assertEqual(root.dir_count, 2)  # type + asset only

    def test_folder_direct_files_fold_into_own_size_no_synthetic_node(self) -> None:
        stats = ScanStats(
            root="root", start_time=datetime(2024, 1, 1), end_time=datetime(2024, 1, 1),
            total_files=0, total_dirs=0, total_size=0,
        )
        root = RootNode(name="root", type=NodeType.ROOT, depth=0, stats=stats)
        folder = _mk("deep", NodeType.FOLDER, root, 1, own_size=99, file_count=3)
        root.children = [folder]

        aggregate(root)
        self.assertIsNone(folder.children)
        self.assertEqual(folder.size, 99)
        self.assertEqual(root.size, 99)


class TestSortTree(unittest.TestCase):
    def _tree_with_sizes(self) -> RootNode:
        stats = ScanStats(
            root="root", start_time=datetime(2024, 1, 1), end_time=datetime(2024, 1, 1),
            total_files=0, total_dirs=0, total_size=0,
        )
        root = RootNode(name="root", type=NodeType.ROOT, depth=0, stats=stats)
        small = _mk("small", "type", root, 1, own_size=10)
        big = _mk("big", "type", root, 1, own_size=100)
        medium = _mk("medium", "type", root, 1, own_size=50)
        root.children = [small, big, medium]
        aggregate(root)
        return root

    def test_sort_by_size_largest_first(self) -> None:
        root = self._tree_with_sizes()
        sort_tree(root, "size")
        self.assertEqual([c.name for c in root.children], ["big", "medium", "small"])

    def test_sort_by_name(self) -> None:
        root = self._tree_with_sizes()
        sort_tree(root, "name")
        self.assertEqual([c.name for c in root.children], ["big", "medium", "small"])

    def test_root_files_always_sorts_last(self) -> None:
        stats = ScanStats(
            root="root", start_time=datetime(2024, 1, 1), end_time=datetime(2024, 1, 1),
            total_files=0, total_dirs=0, total_size=0,
        )
        root = RootNode(name="root", type=NodeType.ROOT, depth=0, stats=stats)
        big_asset = _mk("ZZZ_huge_asset", "asset", root, 1, own_size=1)
        root_files = _mk("Root Files", NodeType.ROOT_FILES, root, 1, own_size=99999, file_count=1)
        root.children = [root_files, big_asset]
        aggregate(root)
        sort_tree(root, "size")
        self.assertEqual(root.children[-1].type, NodeType.ROOT_FILES)


if __name__ == "__main__":
    unittest.main()
