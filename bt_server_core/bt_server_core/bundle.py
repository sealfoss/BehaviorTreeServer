"""Bundles (design 4.3) and the checks of a bundle that need no ROS (design 8.3).

The executor reads no tree files: the sender resolves every Sub-Tree and Remote Sub-Tree node
and ships all trees in one bundle. Each value of ``trees`` is a tree file as the document holds
it (unsaved changes included); a Sub-Tree or Remote Sub-Tree node carries ``"tree": <key>`` next
to its ``File``; ``blackboards`` holds the blackboard file entries of each tree a Remote
Sub-Tree node runs. A sub-tree that would contain itself is not put in again: its node has no
``tree`` key, fails when it runs, and :func:`validate_bundle` reports it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from . import node_types
from .document import NEGATION, REMOTE_SUB_TREE, SUB_TREE, TreeDocument
from .files import PathError, subtree_key, tree_file_name

__all__ = ["BUNDLE_FORMAT", "build_bundle", "sub_bundle", "validate_bundle"]

BUNDLE_FORMAT = "behavior_tree_bundle"
_SUBTREE_TYPES = (SUB_TREE, REMOTE_SUB_TREE)

#: Entry types a binding may give a field of each kind (design 6.4).
_BINDABLE = {"int": {"Integer"}, "float": {"Double", "Integer"}, "str": {"String"}, "bool": {"Bool"}}


def _file_of(item: dict) -> Any:
    fields = item.get("fields")
    return fields.get("File", "") if isinstance(fields, dict) else ""


def _target(holder: str, item: dict) -> str | None:
    """The key a Sub-Tree or Remote Sub-Tree node names, or None (no file, or one that cannot
    be followed)."""
    try:
        return subtree_key(holder, _file_of(item))
    except PathError:
        return None


def build_bundle(
    root_key: str,
    get_document: Callable[[str], TreeDocument | None],
    get_blackboard: Callable[[str], list[dict]] | None = None,
) -> dict:
    """The bundle that runs the tree ``root_key``.

    Args:
        root_key: The document key of the tree to run.
        get_document: Key -> the open document, or one loaded from disk; None (or an exception)
            when the tree does not exist or cannot be read: nodes naming it then have no
            ``tree`` key and fail when they run.
        get_blackboard: Key -> the entries of its blackboard file (file form), for the trees
            Remote Sub-Tree nodes run; without it they get none.

    Raises:
        KeyError: the root tree cannot be had.
    """
    documents: dict[str, TreeDocument] = {}

    def document(key: str) -> TreeDocument | None:
        if key not in documents:
            try:
                found = get_document(key)
            except (OSError, ValueError, KeyError):
                found = None
            if found is None:
                return None
            documents[key] = found
        return documents[key]

    files: dict[str, dict] = {}
    edges: dict[str, list[tuple[dict, str, str | None]]] = {}

    def load(name: str) -> str | None:
        """The key of the tree ``name`` as its document spells it (``robot`` for a node naming
        ``Robot_BT.json`` where case does not matter), or None when it cannot be had; its file
        data and Sub-Tree nodes are noted."""
        holder = document(name)
        if holder is None:
            return None
        key = holder.key or name
        if key not in files:
            files[key] = holder.to_file()
            edges[key] = [
                (item, item["type"], _target(key, item))
                for item in files[key]["nodes"]
                if item.get("type") in _SUBTREE_TYPES and not holder.is_placeholder(item["id"])
            ]
        return key

    loaded_root = load(root_key)
    if loaded_root is None:
        raise KeyError(f"the tree {root_key!r} cannot be read")
    root_key = loaded_root
    trees: dict[str, dict] = {}
    remote: list[str] = []

    def expand(key: str) -> None:
        """Name the trees of ``key``'s Sub-Tree nodes and put them in, depth first. A node that
        would contain itself keeps its ``tree`` key too: whether it does depends on the chain of
        Sub-Tree nodes that leads to it, so the engine decides it per chain when it builds the
        run (it then fails that node on that chain only, as the widget does)."""
        trees[key] = files[key]
        for item, kind, name in edges[key]:
            target = load(name) if name is not None else None
            if target is None:
                continue
            item["tree"] = target
            if kind == REMOTE_SUB_TREE:
                remote.append(target)  # a run of its own, on another executor
            elif target not in trees:
                expand(target)

    expand(root_key)
    for key in remote:  # grows while remote trees name further remote trees
        if key not in trees:
            expand(key)
    bundle: dict[str, Any] = {"format": BUNDLE_FORMAT, "version": 1, "root": root_key, "trees": trees}
    if remote and get_blackboard is not None:
        bundle["blackboards"] = {key: list(get_blackboard(key)) for key in dict.fromkeys(remote)}
    return bundle


def sub_bundle(bundle: dict, key: str) -> dict:
    """The part of ``bundle`` that runs the tree ``key`` (the tree of a Remote Sub-Tree node, sent
    to the executor that runs it): that tree and the trees its nodes name, with their
    blackboards."""
    trees: dict[str, dict] = {}
    stack = [key]
    while stack:
        current = stack.pop()
        if current in trees or current not in bundle["trees"]:
            continue
        trees[current] = bundle["trees"][current]
        stack.extend(item["tree"] for item in trees[current].get("nodes", ()) if isinstance(item.get("tree"), str))
    result: dict[str, Any] = {"format": BUNDLE_FORMAT, "version": 1, "root": key, "trees": trees}
    blackboards = {name: items for name, items in bundle.get("blackboards", {}).items() if name in trees}
    if blackboards:
        result["blackboards"] = blackboards
    return result


def _reachable(tree: dict) -> list[dict]:
    """The nodes of a tree file connected to its Root, Root first."""
    nodes = {item.get("id"): item for item in tree.get("nodes", ()) if isinstance(item, dict)}
    children: dict[str, list[str]] = {}
    for link in tree.get("connections", ()):
        children.setdefault(link.get("parent"), []).append(link.get("child"))
    root = next((item for item in nodes.values() if item.get("type") == "Root"), None)
    result, stack, seen = [], [root["id"]] if root else [], set()
    while stack:
        node_id = stack.pop()
        if node_id in seen or node_id not in nodes:
            continue
        seen.add(node_id)
        result.append(nodes[node_id])
        stack.extend(reversed(children.get(node_id, [])))
    return result


def _field(item: dict, name: str, default: Any) -> Any:
    fields = item.get("fields")
    return fields.get(name, default) if isinstance(fields, dict) else default


def _no_timeout(value: Any) -> bool:
    return not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0


def validate_bundle(
    bundle: dict,
    blackboard_entries: Iterable[dict],
    catalog: Iterable[dict],
    *,
    object_names: Iterable[str] = (),
) -> list[dict]:
    """The checks of design 8.3 that need no ROS, for the run of ``bundle["root"]``: findings
    ``{"path": "tree:<key>/<node id>" | "blackboard:<entry name>", "severity", "text"}``.

    Trees run by Remote Sub-Tree nodes are checked by the executor that runs them (send it
    :func:`sub_bundle`); here only the Remote Sub-Tree node itself is.

    Args:
        blackboard_entries: The entries of the run's blackboard (``name``, ``type``, ``source``).
        catalog: The node types of the executor that runs it.
        object_names: The names of the executor's Object entries, which no entry may take.
    """
    types = {item["type"]: item for item in catalog}
    entries = {entry["name"]: entry for entry in blackboard_entries}
    findings: list[dict] = []

    def report(path: str, severity: str, text: str) -> None:
        findings.append({"path": path, "severity": severity, "text": text})

    trees = bundle.get("trees", {})
    checked: set[str] = set()
    walked: set[tuple[str, frozenset[str]]] = set()
    looping: set[str] = set()

    def walk(key: str, chain: frozenset[str]) -> None:
        """Check the tree ``key`` (its nodes once) and follow its Sub-Tree nodes; ``chain``: the
        trees that contain it on this way down. A Sub-Tree node naming one of them contains
        itself on this chain (the widget fails it there, and only there)."""
        if (key, chain) in walked or key not in trees:
            return
        walked.add((key, chain))
        first = key not in checked
        checked.add(key)
        for item in _reachable(trees[key]):
            path = f"tree:{key}/{item.get('id')}"
            kind = item.get("type")
            builtin = node_types.spec(kind) if isinstance(kind, str) else None
            if (builtin is None or builtin.kind == "leaf") and kind not in types:
                if first:
                    report(path, "error", f"unknown node type {kind!r}")
                continue
            if kind == SUB_TREE and isinstance(item.get("tree"), str):
                if item["tree"] in chain:
                    if path not in looping:
                        looping.add(path)
                        report(path, "error", f"the sub-tree {item['tree']!r} contains itself")
                else:
                    walk(item["tree"], chain | {item["tree"]})
            if first:
                _check_node(key, item, kind, builtin, bundle, types, entries, report, path)

    walk(bundle["root"], frozenset({bundle["root"]}))
    for name, entry in entries.items():
        _check_source(name, entry, report)
        if name in set(object_names):
            report(f"blackboard:{name}", "error", f"the entry {name!r} has the name of an Object entry of the executor")
    return findings


def _check_node(key: str, item: dict, kind: Any, builtin: Any, bundle: dict, types: dict, entries: dict,
                report: Callable, path: str) -> None:
    """The checks of one node that do not depend on the way it is reached."""
    trees = bundle.get("trees", {})
    if kind == NEGATION and not any(link.get("parent") == item.get("id") for link in trees[key]["connections"]):
        report(path, "error", "the Negation has no child")
    if kind in _SUBTREE_TYPES:
        _check_subtree(key, item, report, path)
    if kind == "Evaluation" and _field(item, "Wait", False) is True and _no_timeout(_field(item, "WaitTimeout", 0.0)):
        report(path, "warning", "the Evaluation waits without a timeout")
    if kind in ("CallAction", REMOTE_SUB_TREE) and _no_timeout(_field(item, "Timeout", 0.0)):
        report(path, "warning", f"the {builtin.title if builtin else kind} node has no Timeout: it may wait for ever")
    if kind == REMOTE_SUB_TREE:
        _check_remote_links(item, bundle, entries, report, path)
    else:
        _check_links(item, types.get(kind, {}), entries, report, path)


def _check_subtree(key: str, item: dict, report: Callable, path: str) -> None:
    if isinstance(item.get("tree"), str):
        if item["type"] == REMOTE_SUB_TREE and not str(_field(item, "Executor", "")).strip():
            report(path, "error", "the Remote Sub-Tree names no executor")
        return
    name = _file_of(item)
    if not isinstance(name, str) or not name.strip():
        report(path, "error", "the node has no file")
        return
    try:
        target = subtree_key(key, name)
    except PathError as error:
        report(path, "error", str(error))
        return
    report(path, "error", f"the sub-tree file '{tree_file_name(target)}' is missing")


def _check_links(item: dict, spec: dict, entries: dict, report: Callable, path: str) -> None:
    """Bindings name existing entries of a type that suits the field; outputs name entries of
    any type (a missing one is created)."""
    kinds = {field["name"]: field.get("kind", "other") for field in spec.get("fields", ())}
    bindings = item.get("bindings")
    for field_path, name in (bindings.items() if isinstance(bindings, dict) else ()):
        entry = entries.get(name)
        if entry is None:
            report(path, "error", f"the binding of {field_path!r} names the missing entry {name!r}")
            continue
        allowed = _BINDABLE.get(kinds.get(field_path, "other"))
        if allowed is not None and entry["type"] not in allowed:
            report(path, "error", f"the {entry['type']} entry {name!r} does not suit the field {field_path!r}")


def _check_remote_links(item: dict, bundle: dict, entries: dict, report: Callable, path: str) -> None:
    """Remote Sub-Tree bindings and outputs: remote entry name -> local entry name. A remote
    entry with a source cannot be bound (design 6.8); the two types must agree."""
    remote = {entry.get("name"): entry for entry in bundle.get("blackboards", {}).get(item.get("tree"), ())}
    for key, bound in (("bindings", True), ("outputs", False)):
        links = item.get(key)
        for remote_name, local_name in (links.items() if isinstance(links, dict) else ()):
            remote_entry, local_entry = remote.get(remote_name), entries.get(local_name)
            if bound and local_entry is None:
                report(path, "error", f"the binding of {remote_name!r} names the missing entry {local_name!r}")
            if bound and remote_entry is not None and remote_entry.get("source") is not None:
                report(path, "error", f"the remote entry {remote_name!r} has a source and cannot be bound")
            if remote_entry is not None and local_entry is not None and remote_entry.get("type") != local_entry["type"]:
                report(path, "error", f"the remote entry {remote_name!r} is {remote_entry.get('type')}, "
                                      f"the entry {local_name!r} {local_entry['type']}")


def _check_source(name: str, entry: dict, report: Callable) -> None:
    """Sources whose entry type cannot fit any field of a message (design 7.4), as far as that
    is known without the message type."""
    source = entry.get("source")
    if source is None:
        return
    path = f"blackboard:{name}"
    if entry["type"] in ("Set", "Object"):
        report(path, "error", f"a {entry['type']} entry cannot take its value from a topic")
    elif not source.get("field") and entry["type"] != "Dictionary":
        report(path, "error", "the whole message needs a Dictionary entry")
