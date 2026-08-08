"""Verify the summary report's expand/collapse logic.

There is no browser here, so the report's row markup is parsed out of the
generated HTML and the script's visibility rules are re-implemented against it.
That checks the state machine -- which rows *should* be visible after each
action -- while the accompanying CSS assertions check that a hidden row can
actually disappear, the two halves that together broke expand/collapse.
"""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from storage_report import run, summarize_report

from tests.test_variant_summary import build_variant

_ROW_RE = re.compile(
    r'<div class="row row-(?P<kind>\w+)" data-kind="[^"]*" '
    r'data-key="(?P<key>[^"]*)" data-parent="(?P<parent>[^"]*)"'
)


class Row:
    __slots__ = ("kind", "key", "parent", "index")

    def __init__(self, kind, key, parent, index):
        self.kind, self.key, self.parent, self.index = kind, key, parent, index

    def __repr__(self):
        return f"<{self.kind} {self.key or self.index}>"


class RowModel:
    """Python port of the report's visibility rules."""

    def __init__(self, html: str):
        self.rows = [
            Row(m.group("kind"), m.group("key"), m.group("parent"), i)
            for i, m in enumerate(_ROW_RE.finditer(html))
        ]
        self.by_key = {r.key: r for r in self.rows if r.key}
        self.closed: set[str] = set()

    def _ancestors_open(self, row: Row) -> bool:
        parent = row.parent
        while parent:
            if parent in self.closed:
                return False
            found = self.by_key.get(parent)
            parent = found.parent if found else ""
        return True

    def visible(self) -> list[Row]:
        return [r for r in self.rows if self._ancestors_open(r)]

    # --- the toolbar actions ---
    def expand_all(self):
        self.closed = set()

    def collapse_all(self):
        self.closed = {r.key for r in self.rows if r.key}

    def expand_assets(self):
        self.closed = {r.key for r in self.rows if r.kind == "asset"}

    def click(self, key: str):
        self.closed.symmetric_difference_update({key})


class TestExpandCollapse(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.reports = base / "reports"
        self.reports.mkdir()

        characters = base / "assets" / "Characters"
        build_variant(characters / "Dragon" / "High", tex=100,
                      archives=[("2026-08-07_10-30-00", True)])
        build_variant(characters / "Dragon" / "Low", tex=50)
        build_variant(characters / "Knight" / "High", tex=25)
        props = base / "assets" / "Props"
        build_variant(props / "Sword" / "Default", tex=10)

        run(characters, self.reports / "characters.html")
        run(props, self.reports / "props.html")

        self.out = summarize_report(self.reports)
        self.html = self.out.read_text(encoding="utf-8")
        self.model = RowModel(self.html)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _kinds(self):
        return [r.kind for r in self.model.visible()]

    def test_markup_parsed_as_expected(self) -> None:
        kinds = [r.kind for r in self.model.rows]
        self.assertEqual(kinds.count("type"), 2)
        self.assertEqual(kinds.count("asset"), 3)     # Dragon, Knight, Sword
        self.assertEqual(kinds.count("variant"), 4)

    def test_everything_visible_initially(self) -> None:
        self.assertEqual(len(self.model.visible()), len(self.model.rows))

    def test_collapse_all_leaves_only_types(self) -> None:
        self.model.collapse_all()
        self.assertEqual(set(self._kinds()), {"type"})
        self.assertEqual(len(self.model.visible()), 2)

    def test_expand_all_restores_everything(self) -> None:
        self.model.collapse_all()
        self.model.expand_all()
        self.assertEqual(len(self.model.visible()), len(self.model.rows))

    def test_expand_assets_shows_types_and_assets_only(self) -> None:
        self.model.expand_assets()
        self.assertEqual(set(self._kinds()), {"type", "asset"})
        self.assertEqual(len(self.model.visible()), 5)

    def test_clicking_a_type_hides_its_whole_subtree(self) -> None:
        self.model.click("Characters")
        visible = self.model.visible()
        self.assertNotIn("Dragon", [r.key.split("/")[-1] for r in visible if r.kind == "asset"])
        # Props is untouched: its type, asset and variant all remain.
        self.assertEqual(len([r for r in visible if r.kind == "variant"]), 1)

    def test_clicking_an_asset_hides_only_its_variants(self) -> None:
        self.model.click("Characters/Dragon")
        variants = [r for r in self.model.visible() if r.kind == "variant"]
        self.assertEqual(len(variants), 2)   # Knight/High and Sword/Default survive

    def test_clicking_twice_toggles_back(self) -> None:
        before = len(self.model.visible())
        self.model.click("Characters")
        self.model.click("Characters")
        self.assertEqual(len(self.model.visible()), before)

    def test_collapsed_type_hides_variants_even_when_asset_is_open(self) -> None:
        """Visibility must follow the whole ancestor chain, not just the parent."""
        self.model.click("Characters")          # close the type, assets stay 'open'
        visible_keys = {r.key for r in self.model.visible()}
        self.assertNotIn("Characters/Dragon", visible_keys)


class TestHiddenRowsCanActuallyHide(unittest.TestCase):
    """The state machine above is useless if a hidden row still renders.

    `.row` sets `display:grid`, which is author-level and therefore outranks the
    user-agent's `[hidden] { display: none }`. Without an explicit rule the
    script sets `.hidden` correctly and nothing moves on screen.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.reports = base / "reports"
        self.reports.mkdir()
        build_variant(base / "assets" / "Characters" / "Dragon" / "High", tex=100)
        run(base / "assets" / "Characters", self.reports / "characters.html")
        self.html = summarize_report(self.reports).read_text(encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_explicit_hidden_rule_overrides_display_grid(self) -> None:
        # `.row[hidden]` is class+attribute (0,2,0) against `.row` (0,1,0), so it
        # wins on specificity; the ordering check below is belt and braces.
        self.assertIn(".row[hidden]{display:none}", self.html)
        self.assertLess(
            self.html.index(".head,.row{display:grid"),
            self.html.index(".row[hidden]{display:none}"),
        )

    def test_striping_is_script_driven_not_nth_child(self) -> None:
        """`:nth-child` would count hidden rows and stripe collapsed sections at
        random once anything is collapsed.
        """
        self.assertIn(".row.alt{background:var(--row-alt)}", self.html)
        self.assertNotIn(".row:nth-child(even)", self.html)


if __name__ == "__main__":
    unittest.main()
