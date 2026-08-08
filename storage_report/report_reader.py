"""read_report() -- reconstruct a scanned tree from a previously generated HTML report.

Every report embeds the complete directory tree as `const NODES=[...]`, so a
report can be mined for data the original render did not show, with no re-scan
and therefore no load on the storage. See docs/variant-summary-plan.md §2.

Stdlib only. Touches the filesystem exactly once, to read the report file.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from html import unescape
from pathlib import Path

from storage_report.model import Node, NodeType, RootNode

logger = logging.getLogger(__name__)

_SCAN_ROOT_RE = (
    r'<span class="summary-label">Scan Root</span>'
    r'<span class="summary-value">(.*?)</span>'
)


class ReportFormatError(ValueError):
    """A file is not a readable storage_report HTML report."""


@dataclass(frozen=True, slots=True)
class ReadReport:
    """One report, parsed back into a tree.

    `tree` carries `size` and `file_count` already aggregated -- they are read
    from the report, not recomputed. `own_size` and `dir_count` are NOT
    recoverable (a folder's direct-file bytes are folded into its own size with
    no separate node), so the reconstructed tree must never be passed to
    `model.aggregate()`, which would zero every size.
    """

    source: str
    scan_root: str
    type_name: str
    tree: RootNode
    node_count: int


def read_report(path: str | os.PathLike[str]) -> ReadReport:
    """Parse a generated report back into a tree. No filesystem access beyond
    reading `path` itself.
    """
    report_path = Path(path)
    html = report_path.read_text(encoding="utf-8")

    raw_nodes = _const(html, "NODES", report_path)
    parents = _const(html, "PARENT", report_path)
    meta = _const(html, "META", report_path)

    if not isinstance(raw_nodes, list) or not isinstance(parents, list):
        raise ReportFormatError(f"{report_path}: NODES/PARENT are not arrays")
    if len(raw_nodes) != len(parents):
        raise ReportFormatError(
            f"{report_path}: NODES has {len(raw_nodes)} entries but PARENT has {len(parents)}"
        )
    if not raw_nodes:
        raise ReportFormatError(f"{report_path}: report contains no nodes")

    tree = _build_tree(raw_nodes, parents, meta.get("types") or [])
    scan_root = _scan_root(html, tree)

    logger.info("read report %s: %d nodes, scan root %s", report_path, len(raw_nodes), scan_root)
    return ReadReport(
        source=str(report_path),
        scan_root=scan_root,
        type_name=last_path_token(scan_root),
        tree=tree,
        node_count=len(raw_nodes),
    )


def last_path_token(path: str) -> str:
    """Final component of a path, tolerating either separator and a trailing one.

    For these reports the scan root's last token is the asset type, so this is
    how each merged report gets its TYPE name.
    """
    cleaned = path.replace("/", "\\").rstrip("\\")
    token = cleaned.rpartition("\\")[2]
    return token or cleaned or "(unknown)"


def _const(html: str, name: str, source: Path) -> object:
    match = re.search(rf"const {name}=(.*?);\n", html, re.DOTALL)
    if match is None:
        raise ReportFormatError(
            f"{source}: no 'const {name}' found -- not a storage_report HTML report?"
        )
    try:
        return json.loads(_unescape_payload(match.group(1)))
    except json.JSONDecodeError as exc:
        raise ReportFormatError(f"{source}: 'const {name}' is not valid JSON: {exc}") from exc


def _unescape_payload(payload: str) -> str:
    r"""Undo the writer's HTML-comment guard before JSON parsing.

    `html_report` escapes `<!--` as `<\!--` so a filename cannot open an HTML
    comment inside the script block. `\!` is a legal (ignored) escape in
    JavaScript, so browsers read the report correctly -- but it is *invalid*
    JSON and `json.loads` rejects it outright. `<\/` needs no handling: `\/` is
    a valid JSON escape that decodes to `/`.
    """
    return payload.replace("<\\!--", "<!--")


def _build_tree(raw_nodes: list, parents: list, types: list) -> RootNode:
    """Rebuild `model.Node` objects from the positional payload.

    `NODES` is parent-major (a parent always precedes its children), which is
    what lets depth be computed in a single forward pass.
    """
    built: list[Node] = []
    for index, entry in enumerate(raw_nodes):
        name, type_index, size, file_count = entry[0], entry[1], entry[2], entry[3]
        node_type = types[type_index] if 0 <= type_index < len(types) else str(NodeType.FOLDER)
        cls = RootNode if index == 0 else Node
        built.append(cls(name=name, type=node_type, size=size, file_count=file_count))

    for index, entry in enumerate(raw_nodes):
        child_indices = entry[4]
        if child_indices:
            built[index].children = [built[c] for c in child_indices]
            for c in child_indices:
                built[c].parent = built[index]

    for index, parent_index in enumerate(parents):
        built[index].depth = 0 if parent_index == -1 else built[parent_index].depth + 1

    root = built[0]
    assert isinstance(root, RootNode)
    return root


def _scan_root(html: str, tree: RootNode) -> str:
    """Prefer the explicitly labelled Scan Root row; fall back to the root node's
    name, which the crawler sets to the display path.
    """
    match = re.search(_SCAN_ROOT_RE, html, re.DOTALL)
    if match is not None:
        recovered = unescape(match.group(1)).strip()
        if recovered:
            return recovered
    return tree.name
