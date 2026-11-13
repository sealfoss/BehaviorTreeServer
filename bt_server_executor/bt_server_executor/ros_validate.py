"""The checks of a bundle that need ROS (design 8.3, 7.4), next to the ones of
:func:`bt_server_core.bundle.validate_bundle`, which this module does not repeat.

For every Call Action, Call Service, Publish and Remote Sub-Tree node connected to the Root of
the tree to run (Sub-Trees followed; the trees of Remote Sub-Tree nodes are checked by the
executors that run them):

* a type name that is not an installed interface (checked before anything is imported; S-12,
  L-15) or that cannot be loaded;
* a target that is not on the network now (a warning: it may come up later; not for Publish,
  whose topic may be new), or that offers another type;
* field values (goal, request, message) that do not fit the interface;
* bindings into the goal, request or message whose path the interface does not have, or whose
  entry has a type that does not suit the field; outputs (``Result.``, ``Feedback.``,
  ``Response.``) likewise, by the table of 7.4.

For every entry with a source: a topic that is not on the network (a warning), a type that is
not an installed interface or differs from the topic's, a field the type does not have, an entry
type that does not fit the field (7.4).

Findings are ``{"path": "tree:<key>/<node id>" | "blackboard:<entry name>", "severity": "error" |
"warning", "text"}``, as those of ``validate_bundle``. Safe to call from a worker thread.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from rclpy.node import Node

from bt_server_core.leaves import split_path
from bt_server_core.values import decode_value

from . import ros_types
from .ros_types import FieldInfo

__all__ = ["validate_ros"]

_EXECUTE_TREE = "bt_server_interfaces/action/ExecuteTree"

#: Per ROS node type: (interface kind, target field, the field holding the values, its part of
#: the interface, {output prefix: part}).
_LEAVES: dict[str, tuple[str, str, str, str, dict[str, str]]] = {
    "CallAction": ("action", "Action", "Goal", "Goal", {"Result": "Result", "Feedback": "Feedback"}),
    "CallService": ("srv", "Service", "Request", "Request", {"Response": "Response"}),
    "Publish": ("msg", "Topic", "Message", "", {}),
}
_TARGET_TEXT = {"action": "action", "srv": "service", "msg": "topic"}

#: Entry types a binding may give a field of each kind (6.4; as ``bundle._BINDABLE`` for fields).
_BINDABLE = {"bool": ("Bool",), "integer": ("Integer",), "float": ("Double", "Integer"), "string": ("String",),
             "array": ("List",), "nested": ("Dictionary",)}


class _Graph:
    """What is on the ROS network, asked once per check."""

    def __init__(self, node: Node) -> None:
        self._node = node
        self._cache: dict[str, dict[str, list[str]]] = {}

    def _get(self, kind: str, fetch: Callable[[], Iterable[tuple[str, list[str]]]]) -> dict[str, list[str]]:
        if kind not in self._cache:
            self._cache[kind] = {name: list(types) for name, types in fetch()}
        return self._cache[kind]

    def types(self, kind: str, name: str) -> list[str] | None:
        """The types of ``name`` (``action``, ``srv`` or ``msg``), or None when it is not there."""
        fetch = {"action": self._node.get_action_names_and_types,
                 "srv": self._node.get_service_names_and_types,
                 "msg": self._node.get_topic_names_and_types}[kind]
        return self._get(kind, fetch).get(name)


def _reachable(tree: dict) -> list[dict]:
    """The nodes of a tree file connected to its Root."""
    nodes = {item.get("id"): item for item in tree.get("nodes", ()) if isinstance(item, dict)}
    children: dict[Any, list[Any]] = {}
    for link in tree.get("connections", ()):
        if isinstance(link, dict):
            children.setdefault(link.get("parent"), []).append(link.get("child"))
    stack = [item["id"] for item in nodes.values() if item.get("type") == "Root"][:1]
    result, seen = [], set()
    while stack:
        node_id = stack.pop()
        if node_id in seen or node_id not in nodes:
            continue
        seen.add(node_id)
        result.append(nodes[node_id])
        stack.extend(reversed(children.get(node_id, [])))
    return result


def _fields(item: dict) -> dict:
    fields = item.get("fields")
    if not isinstance(fields, dict):
        return {}
    result = {}
    for key, value in fields.items():
        try:
            result[key] = decode_value(value)
        except ValueError:
            result[key] = value
    return result


def validate_ros(node: Node, bundle: dict, entries: list[dict]) -> list[dict]:
    """The ROS checks of design 8.3 for the run of ``bundle["root"]`` with the blackboard
    ``entries`` (file form). See the module docs."""
    graph = _Graph(node)
    board = {entry["name"]: entry for entry in entries
             if isinstance(entry, dict) and isinstance(entry.get("name"), str)}
    findings: list[dict] = []
    trees = bundle.get("trees") if isinstance(bundle.get("trees"), dict) else {}
    stack, seen = [bundle.get("root")], set()
    while stack:
        key = stack.pop()
        if key in seen or not isinstance(trees.get(key), dict):
            continue
        seen.add(key)
        for item in _reachable(trees[key]):
            kind = item.get("type")
            path = f"tree:{key}/{item.get('id')}"

            def report(severity: str, text: str, path: str = path) -> None:
                findings.append({"path": path, "severity": severity, "text": text})

            if kind == "Sub-Tree" and isinstance(item.get("tree"), str):
                stack.append(item["tree"])
            elif kind in _LEAVES:
                _check_leaf(kind, item, graph, board, report)
            elif kind == "RemoteSubTree":
                _check_remote(item, graph, report)
    for name, entry in board.items():
        if isinstance(entry.get("source"), dict):
            def report(severity: str, text: str, path: str = f"blackboard:{name}") -> None:
                findings.append({"path": path, "severity": severity, "text": text})

            _check_source(entry, graph, report)
    return findings


def _check_leaf(kind: str, item: dict, graph: _Graph, board: dict, report: Callable[..., None]) -> None:
    interface_kind, target_field, values_field, part, outputs = _LEAVES[kind]
    fields = _fields(item)
    type_name = fields.get("Type", "")
    try:
        cls = ros_types.load_interface(type_name, interface_kind)
    except ValueError as error:
        report("error", f"Type: {error}")
        return
    type_name = type_name.strip()
    target = fields.get(target_field, "")
    what = _TARGET_TEXT[interface_kind]
    if not isinstance(target, str) or not target.strip():
        report("error", f"no {what} is chosen ({target_field})")
    else:
        offered = graph.types(interface_kind, target.strip())
        if offered is None and kind != "Publish":
            report("warning", f"the {what} {target.strip()} is not on the network now")
        elif offered and type_name not in offered:
            report("error", f"the {what} {target.strip()} has the type {', '.join(offered)}, not {type_name}")
    part_cls = getattr(cls, part) if part else cls
    try:
        ros_types.build_message(part_cls, fields.get(values_field, {}), values_field)
    except ValueError as error:
        report("error", f"the {values_field.lower()} does not fit {type_name}: {error}")
    bindings = item.get("bindings")
    for field_path, entry_name in (bindings.items() if isinstance(bindings, dict) else ()):
        info = _part_field(field_path, {values_field: part_cls}, report, "binding")
        entry = board.get(entry_name)
        if info is not None and entry is not None and entry.get("type") not in _BINDABLE[info.kind]:
            report("error", f"the {entry.get('type')} entry {entry_name!r} does not suit {field_path} "
                            f"({info.ros_type or 'a message'}: {' or '.join(_BINDABLE[info.kind])})")
    links = item.get("outputs")
    parts = {prefix: getattr(cls, name) for prefix, name in outputs.items()}
    for source_path, entry_name in (links.items() if isinstance(links, dict) else ()):
        if not parts:
            report("error", f"a {item.get('type')} node has no outputs ({source_path})")
            continue
        info = _part_field(source_path, parts, report, "output")
        entry = board.get(entry_name)
        if info is not None and entry is not None:
            problem = ros_types.entry_type_problem(info, entry.get("type"))
            if problem is not None:
                report("error", f"the output {source_path} -> {entry_name!r}: {problem}")


def _part_field(path: Any, parts: dict[str, Any], report: Callable[..., None], what: str) -> FieldInfo | None:
    """The field a binding or output path leads to inside an interface part, or None (reported
    when the path names a part but not a field of it; paths outside the parts are the core's)."""
    try:
        steps = split_path(path)
    except ValueError as error:
        report("error", f"the {what} {path!r}: {error}")
        return None
    head = steps[0]
    if head not in parts:
        if what == "output":
            report("error", f"the output {path!r} does not start with {' or '.join(f'{p}.' for p in parts)}")
        return None
    rest = path[len(head):].lstrip(".")
    try:
        return ros_types.resolve_field(parts[head], rest)
    except ValueError as error:
        report("error", f"the {what} {path!r}: {error}")
        return None


def _check_remote(item: dict, graph: _Graph, report: Callable[..., None]) -> None:
    executor = _fields(item).get("Executor", "")
    if not isinstance(executor, str) or not executor.strip():
        return  # validate_bundle reports it
    action = f"{executor.strip().rstrip('/')}/execute"
    offered = graph.types("action", action)
    if offered is None:
        report("warning", f"the executor {executor.strip()} is not on the network now")
    elif _EXECUTE_TREE not in offered:
        report("error", f"{action} is not an executor's action: it has the type {', '.join(offered)}")


def _check_source(entry: dict, graph: _Graph, report: Callable[..., None]) -> None:
    source = entry["source"]
    topic = source.get("topic", "")
    if not isinstance(topic, str) or not topic.strip():
        report("error", "the source names no topic")
        return
    topic = topic.strip()
    offered = graph.types("msg", topic)
    if offered is None:
        report("warning", f"the topic {topic} is not on the network now")
    type_name = source.get("type", "")
    if not isinstance(type_name, str) or not type_name.strip():
        if not offered or len(offered) != 1:
            return
        type_name = offered[0]
    try:
        cls = ros_types.load_interface(type_name, "msg")
    except ValueError as error:
        report("error", f"the source's type: {error}")
        return
    type_name = type_name.strip()
    if offered and type_name not in offered:
        report("error", f"the topic {topic} has the type {', '.join(offered)}, not {type_name}")
    field = source.get("field", "")
    try:
        info = ros_types.resolve_field(cls, field)
    except ValueError as error:
        report("error", f"the source's field: {error}")
        return
    if field and entry.get("type") not in ("Set", "Object"):  # those and the whole message: validate_bundle
        problem = ros_types.entry_type_problem(info, entry.get("type"))
        if problem is not None:
            report("error", f"the source's field {field}: {problem}")
