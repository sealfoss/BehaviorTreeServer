"""Promote to Sub-Tree, with the widget's rules and refusals (its ``promote.py``; design 9.3).

A group's contents become a tree file of their own and the group is replaced by a Sub-Tree node
running it:

* The new tree gets a Root whose children are the group's *top nodes*: the nodes inside the
  group whose parent lies outside it. When they have such a parent, the Root takes that
  parent's composite type and memory, so the tree runs as before (F-10), and the Sub-Tree node
  takes their place among the parent's children. Group contents without that parent stay
  unconnected in the file; when no top node has a parent, all of them become the Root's
  children, left to right.
* Notes and groups inside the group go into the file too. No blackboard file is written.

"Inside" means lying completely inside the group's rectangle; node sizes are those of
:data:`bt_server_core.document.NODE_SIZES` unless the caller knows better (``sizes``).
"""

from __future__ import annotations

import os
import posixpath
import re
import uuid
from collections.abc import Iterable

from .document import (
    GROUP,
    NODE_SIZES,
    NOTES,
    PARALLEL,
    ROOT,
    SEQUENCE,
    SUB_TREE,
    OpRefused,
    TreeDocument,
    clamp_position,
)
from .files import TREE_FORMAT, document_key, tree_file_name, tree_file_path

__all__ = ["INVALID_FILE_NAME_CHARACTERS", "promote"]

#: Characters a sub-tree's name cannot contain: it names a file (the widget's rule).
INVALID_FILE_NAME_CHARACTERS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

Rect = tuple[float, float, float, float]  # left, top, right, bottom


def _united(rects: Iterable[Rect]) -> Rect:
    rects = list(rects)
    return (min(r[0] for r in rects), min(r[1] for r in rects), max(r[2] for r in rects), max(r[3] for r in rects))


def _check_name(document: TreeDocument, new_key: str, open_keys: Iterable[str]) -> str:
    """The name of the new tree (its file name without ``_BT.json``), checked as the widget
    checks a group name it promotes."""
    try:
        normalised = document_key(new_key)
    except ValueError as error:
        raise OpRefused(str(error)) from error
    name = posixpath.basename(normalised)
    if normalised != new_key or not name.strip():
        raise OpRefused(f"{new_key!r} cannot name a tree; use a name such as 'patrol' or 'cells/patrol'")
    if INVALID_FILE_NAME_CHARACTERS.search(name) or name in (".", "..") or name.endswith("."):
        raise OpRefused(
            f"the name {name!r} cannot be used as a file name: it must not contain "
            '< > : " / \\ | ? * and must not end with a dot'
        )
    if new_key == document.key or new_key in set(open_keys):
        raise OpRefused(f"the name {name!r} names the file of this tree or of a tree containing it; choose another")
    return name


def promote(
    document: TreeDocument,
    group_id: str,
    new_key: str,
    root: str | os.PathLike[str],
    *,
    open_keys: Iterable[str] = (),
    overwrite: bool = False,
    running: bool = False,
    sizes: dict[str, tuple[float, float]] | None = None,
) -> tuple[dict, list[dict]]:
    """Plan Promote to Sub-Tree of the group ``group_id`` of ``document`` into the tree ``new_key``.

    Args:
        root: The trees folder; the new file must lie inside it.
        open_keys: The trees containing ``document`` (zoomed in from), which the new file must
            not replace.
        overwrite: Replace an existing file of that name (the browser asked).
        running: The tree executes: refused, as every structural change.
        sizes: Node id -> (width, height) as drawn, where known.

    Returns:
        ``(file data, ops)``: the new tree file (version per design 12), to be written to
        ``<new_key>_BT.json``, and the operations to apply to ``document`` with
        :meth:`TreeDocument.apply_all` (and broadcast): remove the group and its contents, add
        the Sub-Tree node, connect it in their place.

    Raises:
        OpRefused: the group cannot be promoted; ``reason`` says why (the widget's refusals).
        FileExistsError: the file exists and ``overwrite`` is False.
    """
    if running:
        raise OpRefused("the tree is executing: a group cannot be promoted now")
    if not document.has_node(group_id) or document.node_type(group_id) != GROUP or document.is_placeholder(group_id):
        raise OpRefused("only a group of this tree can be promoted to a sub-tree")
    name = _check_name(document, new_key, open_keys)
    path = tree_file_path(root, new_key)
    if path.exists() and not overwrite:
        raise FileExistsError(f"'{tree_file_name(new_key)}' already exists")

    def rect(node_id: str) -> Rect:
        x, y = document.position(node_id)
        width, height = (sizes or {}).get(node_id) or document.size(node_id)
        return x, y, x + width, y + height

    area = rect(group_id)

    def lies_inside(node_id: str) -> bool:
        left, top, right, bottom = rect(node_id)
        return left >= area[0] and top >= area[1] and right <= area[2] and bottom <= area[3]

    members = [node_id for node_id in document.node_ids() if node_id != group_id and lies_inside(node_id)]
    nodes = [
        node_id
        for node_id in members
        if document.node_type(node_id) not in (NOTES, GROUP) or document.is_placeholder(node_id)
    ]
    if any(document.node_type(node_id) == ROOT for node_id in nodes):
        raise OpRefused("the Root lies inside the group; the Root cannot be part of a sub-tree")
    if not nodes:
        raise OpRefused("no node lies completely inside the group; enlarge the group or move nodes into it")
    inside = set(nodes)
    for node_id in nodes:
        outside = [child for child in document.children(node_id) if child not in inside]
        if outside:
            raise OpRefused(
                f"node {document.title(node_id)!r} has children outside the group ({document.title(outside[0])!r}); "
                "move them into the group or disconnect them"
            )

    def order_key(node_id: str) -> tuple[float, float]:
        left, top, right, _ = rect(node_id)
        return (left + right) / 2.0, top

    tops = sorted((node_id for node_id in nodes if document.parent(node_id) not in inside), key=order_key)
    parents = list(dict.fromkeys(p for p in (document.parent(node_id) for node_id in tops) if p is not None))
    if len(parents) > 1:
        raise OpRefused(
            "the nodes in the group are connected to more than one node outside it "
            f"({', '.join(repr(document.title(p)) for p in parents)}); a Sub-Tree node has one parent"
        )
    parent = parents[0] if parents else None
    siblings = document.children(parent) if parent is not None else []
    root_children = [child for child in siblings if child in inside] if parent is not None else tops
    if parent is not None:
        places = [siblings.index(node_id) for node_id in root_children]
        between = [node_id for node_id in siblings[min(places): max(places) + 1] if node_id not in inside]
        if between:
            raise OpRefused(
                f"node {document.title(between[0])!r} outside the group runs between the group's nodes "
                f"({document.title(parent)!r} runs its children in order); move it into the group or out of their way"
            )
        if document.node_type(parent) == PARALLEL and len(root_children) > 1:
            raise OpRefused(
                f"the group's nodes run in parallel below {document.title(parent)!r}; a sub-tree's Root cannot "
                "run them in parallel"
            )

    data = _subtree_file(document, nodes, members, root_children, parent, rect)
    new_document = TreeDocument(data, document.catalog, key=new_key)
    ops = _replacement_ops(document, group_id, name, new_key, members, inside, tops, root_children, parent, rect)
    return new_document.to_file(), ops


def _subtree_file(document: TreeDocument, nodes, members, root_children, parent, rect) -> dict:
    """The new tree file: a Root over the top nodes (taking the parent's type and memory), then
    the group's contents; the Root is placed above them, centred over its children."""
    parent_type = document.node_type(parent) if parent is not None else None
    if parent_type == ROOT:
        item = document.file_item(parent)
        composite, memory = item["composite"], item["memory"]
    elif parent_type in ("Sequence", "Selector"):
        composite, memory = parent_type, document.file_item(parent)["memory"]
    else:
        composite, memory = SEQUENCE, document.config["default_memory"]
    area = _united(rect(node_id) for node_id in nodes)
    children_area = _united(rect(node_id) for node_id in root_children)
    root_width = NODE_SIZES[ROOT][0]
    root_id = uuid.uuid4().hex
    x = round((children_area[0] + children_area[2]) / 2.0 - root_width / 2.0, 2)
    y = round(area[1] - 160.0, 2)
    root_item = {"id": root_id, "type": ROOT, "title": ROOT, "x": x, "y": y, "composite": composite, "memory": memory}
    connections = [{"parent": root_id, "child": node_id} for node_id in root_children]
    connections += [{"parent": node_id, "child": child} for node_id in nodes for child in document.children(node_id)]
    shown = _united([area, (x, y, x + root_width, y + 1.0)])
    zoom = (document.view or {}).get("zoom", 1.0)
    return {
        "format": TREE_FORMAT,
        "version": 1,
        "config": {
            "tick_interval_ms": document.config["tick_interval_ms"],
            "repeat": False,
            "restore_blackboard": False,
            "default_memory": document.config["default_memory"],
        },
        "view": {"zoom": zoom, "center": [round((shown[0] + shown[2]) / 2, 2), round((shown[1] + shown[3]) / 2, 2)]},
        "nodes": [root_item, *(document.file_item(node_id) for node_id in members)],
        "connections": connections,
    }


def _replacement_ops(document, group_id, name, new_key, members, inside, tops, root_children, parent, rect) -> list[dict]:
    """Remove the group and its contents; add a Sub-Tree node titled with the name, centred over
    the place of the first top node; connect it to the parent there."""
    anchor = rect(root_children[0] if root_children else tops[0])
    width = NODE_SIZES[SUB_TREE][0]
    folder = posixpath.dirname(document.key)
    file_name = posixpath.relpath(tree_file_name(new_key), folder) if folder else tree_file_name(new_key)
    node_id = uuid.uuid4().hex
    node = {
        "id": node_id,
        "type": SUB_TREE,
        "title": name,
        "x": clamp_position((anchor[0] + anchor[2]) / 2.0 - width / 2.0),
        "y": clamp_position(anchor[1]),
        "fields": {"File": file_name},
    }
    ops = [{"op": "node.remove", "ids": [group_id, *members]}, {"op": "node.add", "node": node}]
    if parent is not None:
        siblings = document.children(parent)
        before = [child for child in siblings[: siblings.index(root_children[0])] if child not in inside]
        after = [child for child in siblings if child not in inside and child not in before]
        ops.append({"op": "conn.add", "parent": parent, "child": node_id, "order": [*before, node_id, *after]})
    return ops
