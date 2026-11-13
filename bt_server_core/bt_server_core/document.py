"""A tree file in memory: :class:`TreeDocument` (design 9.3, 12; contract 5.1, 5.3).

Loading follows the widget (``tree_io.prepare_tree``): the data is checked completely before a
document exists; unknown node types become placeholders that keep all their saved data as it
was read (F-08); invalid connections (to unknown ids, from nodes that take no children, to
nodes that take no parent such as Notes and Groups, second parents, cycles, a second child of a
Negation) are dropped and reported; saved field values override the defaults of declared
fields when their kind matches (an int also loads into a float field), others are ignored and
reported; nodes outside the drawing area are moved inside it. ``problems`` lists what was
reported.

**Child order.** The order of a parent's connections in the file is the execution order
(design 12). A document keeps it as read and changes it only by an operation.

**Versions.** :meth:`TreeDocument.to_file` writes the widget's layout (key order, ``log_level``
only when not 4, new keys only when not at their defaults), with version 1 unless the tree uses a
feature the widget lacks (design 12), then 2. For a version 1 file,
:meth:`TreeDocument.save_warnings` names siblings whose centres are closer than 40 pixels
sideways: the widget orders children by the centres it draws, and those may come out in another
order there.

**Operations.** :meth:`TreeDocument.apply` checks an operation of contract 5.3 (``bb.*``
excepted), applies it, raises the revision and returns it normalised for the broadcast. A
refused operation raises :class:`OpRefused` and leaves the document and its revision unchanged.
"""

from __future__ import annotations

import copy
import math
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from . import node_types
from .files import TREE_FORMAT, PathError, check_tree_data, subtree_key, tree_file_name
from .values import (
    INT_MAX,
    OPERATORS,
    ValueFileError,
    coerce_value,
    decode_value,
    encode_value,
    entry_name_problem,
    field_from_wire,
    field_to_wire,
    infer_type,
    typed_from_wire,
    typed_to_wire,
)

__all__ = [
    "BOX_LIMITS",
    "DEFAULT_GROUP_COLOR",
    "NODE_SIZES",
    "ORDER_WARNING_DISTANCE",
    "SCENE_LIMIT",
    "Applied",
    "clamp_position",
    "OpRefused",
    "TreeDocument",
    "default_group_name",
    "default_group_number",
    "node_size",
]

ROOT = "Root"
SEQUENCE, SELECTOR, PARALLEL, NEGATION = "Sequence", "Selector", "Parallel", "Negation"
NOTES, GROUP, SUB_TREE, REMOTE_SUB_TREE = "Notes", "Group", "Sub-Tree", "RemoteSubTree"
ROOT_COMPOSITES = (SEQUENCE, SELECTOR)
COMPOSITES = (SEQUENCE, SELECTOR, PARALLEL)
POLICIES = ("all", "any")

#: Positions are kept inside the widget's scene: SCENE_EXTENT / 2 - SCENE_MARGIN (``geometry``).
SCENE_LIMIT = 48_000.0

#: The widget's box sizes (``notes.BoxNodeWidget``, ``groups.GroupNodeWidget``): default,
#: minimum, and the maximum of each side.
BOX_LIMITS = {
    NOTES: {"default": (220, 120), "min": (60, 30), "max": 2000},
    GROUP: {"default": (360, 240), "min": (120, 60), "max": 2000},
}
DEFAULT_GROUP_COLOR = "#3d7cc9"

#: Sizes (width, height) in pixels of nodes as the widget 0.10.0 draws them by default, measured
#: with its default font (Qt, offscreen). They give the centres the widget orders children by
#: (the 40-pixel warning of design 12) and decide which nodes lie inside a group (Promote to
#: Sub-Tree). Types not listed (custom and ROS leaves, placeholders) take
#: :data:`DEFAULT_LEAF_SIZE`, the size of a leaf without fields; Notes and Groups their own size.
NODE_SIZES = {
    ROOT: (210, 74),
    SEQUENCE: (130, 102),
    SELECTOR: (130, 102),
    PARALLEL: (130, 102),
    NEGATION: (130, 102),
    "Evaluation": (382, 164),
    "Set": (382, 164),
    "Restart": (130, 74),
    "Pause": (130, 74),
    "Stop": (130, 74),
    "UDP Message": (440, 155),
    "Print Message": (306, 164),
    SUB_TREE: (352, 110),
}
DEFAULT_LEAF_SIZE = (246, 74)

#: Siblings whose centres are closer than this sideways get a warning when saved as version 1.
ORDER_WARNING_DISTANCE = 40.0

#: Node ids made by browsers: 32 hex digits (``uuid4().hex``).
NODE_ID = re.compile(r"[0-9a-f]{32}")
_HEX_COLOR = re.compile(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})")
_DEFAULT_GROUP_NAME = re.compile(r"group_(\d+)\Z")
_PATH_PART = re.compile(r"\[(\d+)\]|([^.\[\]]+)")

#: Keys every saved node has; a placeholder keeps all other keys of its saved node as read.
STANDARD_KEYS = ("id", "type", "title", "x", "y")
LEAF_OPTIONS = ("wait_for_cancels", "cancel_timeout_ms")

#: Tree options (design 12): their defaults, in the order the file holds them.
CONFIG_DEFAULTS: dict[str, Any] = {
    "tick_interval_ms": 100,
    "repeat": False,
    "restore_blackboard": False,
    "default_memory": True,
    "log_level": 4,
    "wait_for_cancels": True,
    "cancel_timeout_ms": 5000,
    "on_unconfirmed_cancel": "stop",
}
WIDGET_CONFIG = ("tick_interval_ms", "repeat", "restore_blackboard", "default_memory")
NEW_CONFIG = ("wait_for_cancels", "cancel_timeout_ms", "on_unconfirmed_cancel")
MIN_TICK_MS, MAX_TICK_MS = 1, 60_000

#: Operations that change the structure: refused while the tree executes (design 9.3, G-04).
STRUCTURAL_OPS = frozenset({
    "node.add", "node.remove", "conn.add", "conn.remove", "paste", "group.promote",
    "composite.set_type", "composite.set_memory", "composite.set_policy",
})

#: Fields of the Evaluation, Set and Print Message nodes that name an entry (or are null).
_ENTRY_NAME_FIELDS = {"Evaluation": ("CompareTo",), "Set": ("CompareTo",), "Print Message": ("Source",)}
#: Types whose saved data the widget writes as ``{"fields": ...}`` only (no field_selections).
_FIELDS_ONLY_TYPES = frozenset(("Evaluation", "Set", "Print Message"))
#: Built-in leaf types whose names an application's own leaf type could have used in widget
#: files (``registry.NodeKind.yielding``): their saved data must look like theirs.
_YIELDING_LEAVES = frozenset(("UDP Message", "Print Message", SUB_TREE))
_BOX_SAVED_KEYS = {NOTES: frozenset(("text", "width", "height")), GROUP: frozenset(("width", "height", "color"))}
_LOG_LEVELS = {"NONE": 0, "ERROR": 1, "WARNING": 2, "DEBUG": 3, "INFO": 4}


class OpRefused(ValueError):
    """An operation that cannot be applied; ``reason`` says why. Nothing was changed."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class Applied:
    """An applied operation.

    Attributes:
        op: The operation as applied, normalised: what is broadcast to every browser.
        rev: The document's revision after it.
        reorder: For a ``node.move`` sent at a stale revision: the parents whose new child
            order was dropped; that browser works them out again (contract 5.3).
    """

    op: dict
    rev: int
    reorder: list[str] = field(default_factory=list)


def default_group_name(number: int) -> str:
    """``group_001`` for 1: the widget's default group name."""
    return f"group_{number:03d}"


def default_group_number(name: Any) -> int | None:
    """``number`` if ``name`` is a default group name (``group_<number>``), else None."""
    match = _DEFAULT_GROUP_NAME.match(name) if isinstance(name, str) else None
    return int(match.group(1)) if match else None


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _finite(value: Any) -> bool:
    if not _is_number(value):
        return False
    try:
        return math.isfinite(float(value))
    except OverflowError:
        return False


def clamp_position(value: float) -> float:
    """A coordinate kept inside the drawing area (``geometry.clamp_to_scene`` for finite values)."""
    return min(max(float(value), -SCENE_LIMIT), SCENE_LIMIT)


def _widget_kind(value: Any) -> str:
    """The widget's ``nodes.field_kind``: bool, int, float, str, list or other."""
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "list"
    return "other"


def _json_safe(value: Any) -> Any:
    """Raw file JSON for a browser: ``NaN`` and infinities, which a browser's JSON parser
    refuses, become their text (``"nan"``, ``"inf"``, ``"-inf"``)."""
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    return value


def _parse_path(path: str) -> list[str | int]:
    """``pose.position.x`` -> ``['pose', 'position', 'x']``; ``waypoints[0].x`` -> ``['waypoints', 0, 'x']``."""
    parts: list[str | int] = []
    position = 0
    for match in _PATH_PART.finditer(path):
        if match.start() != position and path[position:match.start()] != ".":
            raise OpRefused(f"the path {path!r} is malformed")
        parts.append(int(match.group(1)) if match.group(1) is not None else match.group(2))
        position = match.end()
    if not parts or position != len(path):
        raise OpRefused(f"the path {path!r} is malformed")
    return parts


def node_size(node_type: str, item: dict | None = None) -> tuple[float, float]:
    """The size of a node of ``node_type`` (see :data:`NODE_SIZES`); Notes and Groups take their
    ``width`` and ``height`` from ``item``."""
    if node_type in BOX_LIMITS and item is not None:
        return float(item.get("width", BOX_LIMITS[node_type]["default"][0])), float(
            item.get("height", BOX_LIMITS[node_type]["default"][1])
        )
    return NODE_SIZES.get(node_type, DEFAULT_LEAF_SIZE)


@dataclass
class _Node:
    """One node. ``data`` holds what its type saves besides the standard keys, with decoded
    values: ``composite``/``memory`` (Root), ``memory`` (Sequence, Selector), ``policy``
    (Parallel); for leaves ``fields``, ``selections``, ``bindings``, ``outputs`` and the cancel
    options; ``text``/``width``/``height`` (Notes), ``width``/``height``/``color`` (Group).
    ``raw`` is a placeholder's saved node, kept as it was read."""

    id: str
    type: str
    title: str
    x: float
    y: float
    data: dict
    raw: dict | None = None


class TreeDocument:
    """A tree file in memory with a revision number.

    Args:
        data: The tree file's data (checked as the widget checks a tree file).
        catalog: The node types known (``NodeTypeSpec.to_catalog()`` of the built-in and custom
            types). Leaf types it lacks become placeholders. Root, the composites, Negation,
            Notes and Group are always known.
        key: The document key (``robot``, ``cells/patrol``).
        path: The file it was read from, relative to the trees folder (default: the key's tree
            file).
        rev: The first revision.

    Attributes:
        key, path, rev: As given; ``rev`` rises with every applied operation.
        problems: What loading reported.
        unsaved: Whether an operation changed the document since it was loaded or saved.
        legacy_blackboard: The ``blackboard`` list of a single-file tree of the widget's early
            versions; written back unchanged as long as it is set (the widget keeps it in a
            sub-tree's file). Set it to None when the tree's blackboard file is written.

    Raises:
        TreeFileError: ``data`` is not a tree file this server reads.
    """

    def __init__(self, data: Any, catalog: Iterable[dict], *, key: str = "", path: str | None = None, rev: int = 0):
        check_tree_data(data, path or tree_file_name(key or "tree"))
        self.key = key
        self.path = path if path is not None else tree_file_name(key)
        self.rev = rev
        self.unsaved = False
        self.problems: list[str] = []
        self._catalog = {item["type"]: item for item in catalog if isinstance(item, dict) and "type" in item}
        self._nodes: dict[str, _Node] = {}
        self._children: dict[str, list[str]] = {}
        self._parent: dict[str, str] = {}
        self.config = self._load_config(data.get("config"))
        self.view = self._load_view(data.get("view"))
        self.legacy_blackboard = copy.deepcopy(data["blackboard"]) if isinstance(data.get("blackboard"), list) else None
        self._load_nodes(data.get("nodes", []), self.problems)
        self._load_connections(data.get("connections", []), self.problems)
        numbers = [default_group_number(node.title) for node in self._nodes.values() if node.type == GROUP]
        self._group_counter = max((number for number in numbers if number is not None), default=0)
        self.problems += [f"node {self.title(found['node'])!r}: {found['text']}" for found in self.subtree_problems()]

    # ================================================================================ queries
    @property
    def root_id(self) -> str:
        return next(node.id for node in self._nodes.values() if node.type == ROOT)

    def node_ids(self) -> list[str]:
        """Every node id, in the order of the file (then the order they were added)."""
        return list(self._nodes)

    def has_node(self, node_id: str) -> bool:
        return node_id in self._nodes

    def node_type(self, node_id: str) -> str:
        return self._nodes[node_id].type

    def title(self, node_id: str) -> str:
        return self._nodes[node_id].title

    def position(self, node_id: str) -> tuple[float, float]:
        node = self._nodes[node_id]
        return node.x, node.y

    def size(self, node_id: str) -> tuple[float, float]:
        """The node's size (see :func:`node_size`)."""
        node = self._nodes[node_id]
        return node_size(node.type, node.data)

    def is_placeholder(self, node_id: str) -> bool:
        return self._nodes[node_id].raw is not None

    def children(self, node_id: str) -> list[str]:
        """The node's children in execution order."""
        return list(self._children.get(node_id, ()))

    def parent(self, node_id: str) -> str | None:
        return self._parent.get(node_id)

    def connections(self) -> list[tuple[str, str]]:
        """``(parent, child)`` pairs as the file holds them: by parent in node order, children
        in execution order (the widget's layout)."""
        return [(parent, child) for parent in self._nodes for child in self._children.get(parent, ())]

    def fields(self, node_id: str) -> dict:
        """A copy of a known leaf's decoded field values (empty for other nodes)."""
        return copy.deepcopy(self._nodes[node_id].data.get("fields", {}))

    def file_item(self, node_id: str) -> dict:
        """The node as the file holds it."""
        return self._file_item(self._nodes[node_id])

    @property
    def catalog(self) -> list[dict]:
        """The catalog the document was loaded with."""
        return list(self._catalog.values())

    def spec(self, type_name: str) -> dict | None:
        """The catalog entry of a leaf type (None when the catalog lacks it)."""
        return self._catalog.get(type_name)

    # ================================================================================ loading
    @staticmethod
    def _load_config(data: Any) -> dict:
        """The tree options (``TreeConfig.from_dict`` plus the new ones): unknown keys and invalid
        values are ignored."""
        config = dict(CONFIG_DEFAULTS)
        if not isinstance(data, dict):
            return config
        interval = data.get("tick_interval_ms")
        if isinstance(interval, int) and not isinstance(interval, bool):
            config["tick_interval_ms"] = max(MIN_TICK_MS, min(MAX_TICK_MS, interval))
        for name in ("repeat", "restore_blackboard", "default_memory", "wait_for_cancels"):
            if isinstance(data.get(name), bool):
                config[name] = data[name]
        level = data.get("log_level")
        if isinstance(level, int) and not isinstance(level, bool) and 0 <= level <= 4:
            config["log_level"] = level
        timeout = data.get("cancel_timeout_ms")
        if isinstance(timeout, int) and not isinstance(timeout, bool) and timeout >= 0:
            config["cancel_timeout_ms"] = timeout
        if data.get("on_unconfirmed_cancel") in ("stop", "continue"):
            config["on_unconfirmed_cancel"] = data["on_unconfirmed_cancel"]
        return config

    @staticmethod
    def _load_view(view: Any) -> dict | None:
        """The saved zoom and centre (``tree_io._view_state``); None when missing or invalid."""
        if not isinstance(view, dict):
            return None
        zoom, center = view.get("zoom"), view.get("center")
        zoom = float(zoom) if _finite(zoom) and zoom > 0 else 1.0
        if isinstance(center, list) and len(center) == 2 and all(_finite(value) for value in center):
            return {"zoom": zoom, "center": [float(center[0]), float(center[1])]}
        return {"zoom": zoom, "center": [0.0, 0.0]}

    def _load_nodes(self, items: Any, problems: list[str], with_root: bool = True) -> list[_Node]:
        """``tree_io.prepare_nodes``: create the nodes of ``items`` (``with_root`` False: Root
        items are skipped, as for pasted nodes). Returns them; they are added to the document."""
        seen: set[str] = set()
        root_seen = False
        loaded: list[_Node] = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                problems.append(f"node {index} is not an object and was skipped")
                continue
            node_id, type_name, saved_title = item.get("id"), item.get("type"), item.get("title")
            label = f"{saved_title!r} (#{index + 1})" if isinstance(saved_title, str) and saved_title else f"#{index + 1}"
            if not isinstance(node_id, str) or not node_id or node_id in seen or node_id in self._nodes:
                problems.append(f"node {label} has a missing or duplicate id and was skipped")
                continue
            if not isinstance(type_name, str) or not type_name:
                problems.append(f"node {label} has no type and was skipped")
                continue
            if type_name == ROOT:
                if root_seen or not with_root:
                    problems.append(f"extra root node {label} was skipped")
                    continue
                root_seen = True
            seen.add(node_id)
            node = self._node_from_file(item, label, problems)
            self._nodes[node.id] = node
            loaded.append(node)
        if with_root and not root_seen:
            problems.append("the file has no root node; a new root was created")
            root = _Node(uuid.uuid4().hex, ROOT, ROOT, 0.0, 0.0, {"composite": SEQUENCE, "memory": True})
            self._nodes = {root.id: root, **self._nodes}
            loaded.insert(0, root)
        return loaded

    def _recognised(self, type_name: str, item: dict) -> bool:
        """Whether a saved node of a type the widget added later was saved by that type
        (``registry.NodeKind.recognises``), not by an application's own type of that name."""
        if type_name in _BOX_SAVED_KEYS:
            return bool(_BOX_SAVED_KEYS[type_name] & item.keys()) or "fields" not in item
        if type_name in _YIELDING_LEAVES:
            fields = item.get("fields")
            declared = {spec["name"] for spec in self._catalog.get(type_name, {}).get("fields", ())}
            return not isinstance(fields, dict) or set(fields) <= declared
        return True

    def _known_kind(self, type_name: str) -> str | None:
        """root, composite, decorator, note, group or leaf for a known type; None for a type that
        becomes a placeholder."""
        builtin = node_types.spec(type_name)
        if builtin is not None and builtin.kind != "leaf":
            return builtin.kind
        return "leaf" if type_name in self._catalog else None

    def _node_from_file(self, item: dict, label: str, problems: list[str]) -> _Node:
        type_name = item["type"]
        kind = self._known_kind(type_name)
        if kind is None or not self._recognised(type_name, item):
            problems.append(f"node type {type_name!r} is not registered; a placeholder node was created")
            raw = copy.deepcopy(item)
            node = _Node(item["id"], type_name, type_name, 0.0, 0.0, {}, raw)
        else:
            node = _Node(item["id"], type_name, type_name, 0.0, 0.0, self._load_data(kind, item, label, problems))
        title = item.get("title")
        if isinstance(title, str) and title:
            node.title = title
        x, y = item.get("x", 0.0), item.get("y", 0.0)
        if not (_finite(x) and _finite(y)):
            problems.append(f"node {label} has an invalid position")
            x, y = 0.0, 0.0
        node.x, node.y = clamp_position(x), clamp_position(y)
        if (node.x, node.y) != (float(x), float(y)):
            problems.append(f"node {label} was outside the drawing area and was moved inside it")
        return node

    def _load_data(self, kind: str, item: dict, label: str, problems: list[str]) -> dict:
        type_name = item["type"]
        if kind == "root":
            composite = item.get("composite")
            if composite is not None and composite not in ROOT_COMPOSITES:
                problems.append(f"the Root's composite type {composite!r} is unknown; Sequence is used")
            memory = item.get("memory")
            return {
                "composite": composite if composite in ROOT_COMPOSITES else SEQUENCE,
                "memory": memory if isinstance(memory, bool) else True,
            }
        if kind == "composite" and type_name != PARALLEL:
            memory = item.get("memory")
            return {"memory": memory if isinstance(memory, bool) else True}
        if kind == "composite":
            policy = item.get("policy", "all")
            if policy not in POLICIES:
                problems.append(f"node {label} has an unknown policy {policy!r}; 'all' is used")
            return {"policy": policy if policy in POLICIES else "all"}
        if kind == "decorator":
            return {}
        if kind in ("note", "group"):
            return self._load_box(type_name, item, label, problems)
        return self._load_leaf(type_name, item, label, problems)

    @staticmethod
    def _clamped_size(type_name: str, width: Any, height: Any) -> tuple[int, int]:
        """``BoxNodeWidget.clamped_size``: rounded and limited to the type's limits."""
        limits = BOX_LIMITS[type_name]
        return tuple(  # type: ignore[return-value]
            min(max(round(value), minimum), limits["max"]) for value, minimum in zip((width, height), limits["min"])
        )

    def _load_box(self, type_name: str, item: dict, label: str, problems: list[str]) -> dict:
        size = list(BOX_LIMITS[type_name]["default"])
        for index, key in enumerate(("width", "height")):
            value = item.get(key)
            if value is None:
                continue
            if not _finite(value):
                problems.append(f"node {label}: ignoring saved {key} {value!r}")
                continue
            size[index] = value
        width, height = self._clamped_size(type_name, *size)
        if type_name == NOTES:
            text = item.get("text")
            if text is not None and not isinstance(text, str):
                problems.append(f"node {label}: ignoring saved text of type {type(text).__name__}")
            return {"text": text if isinstance(text, str) else "", "width": width, "height": height}
        color = item.get("color")
        parsed = _parse_color(color) if color is not None else DEFAULT_GROUP_COLOR
        if parsed is None:
            problems.append(f"node {label}: ignoring saved color {color!r}")
        return {"width": width, "height": height, "color": parsed or DEFAULT_GROUP_COLOR}

    def _declared(self, type_name: str) -> list[dict]:
        return list(self._catalog[type_name].get("fields", ()))

    def _load_leaf(self, type_name: str, item: dict, label: str, problems: list[str]) -> dict:
        """``LeafNodeWidget._load_dict`` and the special rules of Evaluation, Set and Print Message."""
        declared = {spec["name"]: spec for spec in self._declared(type_name)}
        fields = {name: copy.deepcopy(spec.get("default")) for name, spec in declared.items()}
        saved = item.get("fields")
        if isinstance(saved, dict):
            for key, encoded in saved.items():
                try:
                    value = decode_value(encoded)
                except ValueFileError as error:
                    problems.append(f"field {key!r} of node {label} could not be read ({error}); default kept")
                    continue
                if key not in declared:
                    problems.append(f"node {label}: ignoring saved field {key!r} that the node no longer declares")
                    continue
                accepted = self._accept_field(type_name, declared[key], value)
                if accepted is _REJECTED:
                    problems.append(
                        f"node {label}: ignoring saved field {key!r} of type {type(value).__name__} "
                        f"(expected {declared[key].get('kind')})"
                    )
                    continue
                fields[key] = accepted
        selections: dict[str, int] = {}
        saved_selections = item.get("field_selections")
        if isinstance(saved_selections, dict):
            for key, index in saved_selections.items():
                value = fields.get(key)
                if isinstance(value, list) and isinstance(index, int) and not isinstance(index, bool):
                    selections[key] = min(max(index, 0), max(len(value) - 1, 0))
        data: dict[str, Any] = {"fields": fields, "selections": selections}
        for key in ("bindings", "outputs"):
            mapping = item.get(key)
            if mapping is None:
                continue
            if isinstance(mapping, dict) and all(
                isinstance(name, str) and name and isinstance(entry, str) and entry_name_problem(entry) is None
                for name, entry in mapping.items()
            ):
                data[key] = dict(mapping)
            else:
                problems.append(f"node {label}: ignoring malformed {key}")
        for option, check in (("wait_for_cancels", _is_bool), ("cancel_timeout_ms", _is_timeout)):
            if option in item:
                if check(item[option]):
                    data[option] = item[option]
                else:
                    problems.append(f"node {label}: ignoring saved {option} {item[option]!r}")
        return data

    @staticmethod
    def _accept_field(type_name: str, spec: dict, value: Any) -> Any:
        """The value a saved field takes, or ``_REJECTED`` (the widget's field loading rules)."""
        name = spec["name"]
        if name in _ENTRY_NAME_FIELDS.get(type_name, ()):
            return (value or None) if value is None or isinstance(value, str) else _REJECTED
        if type_name in ("Evaluation", "Set") and name == "LiteralValue":
            return value if value is None or infer_type(value) != "Object" else _REJECTED
        if type_name == "Print Message" and name == "Level":
            return _log_level(value)
        if type_name == "Evaluation" and name == "Operator":
            return value if value in OPERATORS else _REJECTED
        default_kind = _widget_kind(spec.get("default"))
        kind = _widget_kind(value)
        if kind == default_kind:
            return value
        if default_kind == "float" and kind == "int":
            return float(value)
        return _REJECTED

    def _load_connections(self, items: Any, problems: list[str], among: set[str] | None = None) -> list[tuple[str, str]]:
        """``tree_io.prepare_connections``: add the valid connections of ``items`` (only between
        the nodes ``among``, when given); invalid ones are skipped and reported."""
        added: list[tuple[str, str]] = []
        child_count = {parent: len(children) for parent, children in self._children.items()}
        if not isinstance(items, list):
            return added
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                problems.append(f"connection {index} is not an object and was skipped")
                continue
            parent_id, child_id = item.get("parent"), item.get("child")
            if (
                not isinstance(parent_id, str)
                or not isinstance(child_id, str)
                or parent_id not in self._nodes
                or child_id not in self._nodes
                or (among is not None and not {parent_id, child_id} <= among)
            ):
                problems.append(f"connection {index} refers to a missing node and was skipped")
                continue
            parent, child = self._nodes[parent_id], self._nodes[child_id]
            if child_id in self._parent:
                problems.append(f"connection {index}: node {child.title!r} already has a parent; skipped")
                continue
            if parent is child or not self._takes_children(parent) or not self._takes_parent(child):
                problems.append(f"connection {index} ({parent.title!r} -> {child.title!r}) is invalid; skipped")
                continue
            if parent.type == NEGATION and child_count.get(parent_id, 0) >= 1:
                problems.append(f"connection {index}: node {parent.title!r} accepts only 1 child; skipped")
                continue
            if self._is_ancestor(child_id, parent_id):
                problems.append(f"connection {index} ({parent.title!r} -> {child.title!r}) creates a cycle; skipped")
                continue
            self._link(parent_id, child_id)
            child_count[parent_id] = child_count.get(parent_id, 0) + 1
            added.append((parent_id, child_id))
        return added

    # ================================================================================ structure
    @staticmethod
    def _takes_children(node: _Node) -> bool:
        return node.raw is None and node.type in (ROOT, *COMPOSITES, NEGATION)

    @staticmethod
    def _takes_parent(node: _Node) -> bool:
        return node.raw is not None or node.type not in (ROOT, NOTES, GROUP)

    def _is_ancestor(self, ancestor: str, node_id: str | None) -> bool:
        """Whether ``ancestor`` is ``node_id`` or lies above it."""
        while node_id is not None:
            if node_id == ancestor:
                return True
            node_id = self._parent.get(node_id)
        return False

    def _link(self, parent: str, child: str, index: int | None = None) -> None:
        children = self._children.setdefault(parent, [])
        children.insert(len(children) if index is None else index, child)
        self._parent[child] = parent

    def _unlink(self, child: str) -> str | None:
        parent = self._parent.pop(child, None)
        if parent is not None:
            self._children[parent].remove(child)
        return parent

    # ================================================================================ writing
    def _file_item(self, node: _Node) -> dict:
        """The node as the widget writes it (``tree_io.node_data``)."""
        header = {"id": node.id, "type": node.type, "title": node.title, "x": round(node.x, 2), "y": round(node.y, 2)}
        if node.raw is not None:
            for key in ("x", "y"):  # an unmoved placeholder keeps its saved number as it was
                saved = node.raw.get(key)
                if _is_number(saved) and saved == header[key]:
                    header[key] = saved
            return {**header, **{key: value for key, value in node.raw.items() if key not in STANDARD_KEYS}}
        data = node.data
        if node.type == ROOT:
            return {**header, "composite": data["composite"], "memory": data["memory"]}
        if node.type in (SEQUENCE, SELECTOR):
            return {**header, "memory": data["memory"]}
        if node.type == PARALLEL:
            return {**header, "policy": data["policy"]}
        if node.type == NEGATION:
            return header
        if node.type == NOTES:
            return {**header, "text": data["text"], "width": data["width"], "height": data["height"]}
        if node.type == GROUP:
            return {**header, "width": data["width"], "height": data["height"], "color": data["color"]}
        item = {**header, "fields": self._file_fields(node)}
        if node.type not in _FIELDS_ONLY_TYPES:
            selections = {
                key: data["selections"].get(key, 0) for key, value in data["fields"].items() if isinstance(value, list)
            }
            if selections:
                item["field_selections"] = selections
        for key in ("bindings", "outputs"):
            if data.get(key):
                item[key] = dict(data[key])
        for option in LEAF_OPTIONS:
            if option in data:
                item[option] = data[option]
        return item

    def _file_fields(self, node: _Node) -> dict:
        """Encoded field values; fields the widget lacks only when not at their defaults."""
        new = self._new_fields(node.type)
        result = {}
        for name, value in node.data["fields"].items():
            if name in new and _same_value(value, new[name]):
                continue
            result[name] = encode_value(value)
        return result

    def _new_fields(self, type_name: str) -> dict[str, Any]:
        """Fields of a built-in type that the widget lacks (``FieldSpec.new``), with defaults."""
        builtin = node_types.spec(type_name)
        return {spec.name: spec.default for spec in builtin.fields if spec.new} if builtin is not None else {}

    def _config_file(self) -> dict:
        config = {name: self.config[name] for name in WIDGET_CONFIG}
        for name in ("log_level", *NEW_CONFIG):
            if self.config[name] != CONFIG_DEFAULTS[name]:
                config[name] = self.config[name]
        return config

    def uses_new_features(self) -> list[str]:
        """What makes the file version 2 (design 12), described; empty for version 1."""
        reasons = []
        for name in NEW_CONFIG:
            if self.config[name] != CONFIG_DEFAULTS[name]:
                reasons.append(f"the option {name}")
        for node in self._nodes.values():
            builtin = node_types.spec(node.type)
            if builtin is not None and not builtin.widget:  # also a placeholder (no ROS types here)
                reasons.append(f"the {builtin.title} node {node.title!r}")
                continue
            if node.raw is not None:
                kept = [key for key in ("bindings", "outputs", *LEAF_OPTIONS) if key in node.raw]
                if kept:
                    reasons.append(f"{', '.join(kept)} of {node.title!r}")
                continue
            if "fields" not in node.data:
                continue
            new = self._new_fields(node.type)
            changed = [name for name in new if not _same_value(node.data["fields"].get(name), new[name])]
            if changed:
                reasons.append(f"{', '.join(changed)} of {node.title!r}")
            for key in ("bindings", "outputs", *LEAF_OPTIONS):
                if node.data.get(key) not in (None, {}):
                    reasons.append(f"{key} of {node.title!r}")
        return reasons

    def file_version(self) -> int:
        """The version :meth:`to_file` writes: 2 when the tree uses a feature the widget lacks."""
        return 2 if self.uses_new_features() else 1

    def to_file(self, view: dict | None = None) -> dict:
        """The tree file: the widget's layout, with the version of :meth:`file_version`.

        ``view`` (``{"zoom", "center"}``) replaces the stored view, as Save does with the view
        of the browser that saves."""
        if view is not None:
            self.view = self._load_view(view)
        shown = self.view or {"zoom": 1.0, "center": [0.0, 0.0]}
        data = {
            "format": TREE_FORMAT,
            "version": self.file_version(),
            "config": self._config_file(),
            "view": {"zoom": round(shown["zoom"], 4), "center": [round(value, 2) for value in shown["center"]]},
            "nodes": [self._file_item(node) for node in self._nodes.values()],
            "connections": [{"parent": parent, "child": child} for parent, child in self.connections()],
        }
        if self.legacy_blackboard is not None:
            data["blackboard"] = copy.deepcopy(self.legacy_blackboard)
        return data

    def save_warnings(self) -> list[str]:
        """Warnings for Save: for a version 1 file, siblings whose centres are closer than 40
        pixels sideways, which the widget may run in another order (design 12, F-11). Centres use
        :data:`NODE_SIZES`."""
        if self.file_version() != 1:
            return []
        warnings = []
        for parent, children in self._children.items():
            centres = [(self._nodes[child], self._nodes[child].x + self.size(child)[0] / 2.0) for child in children]
            for index, (first, first_x) in enumerate(centres):
                for second, second_x in centres[index + 1:]:
                    if abs(first_x - second_x) < ORDER_WARNING_DISTANCE:
                        warnings.append(
                            f"{first.title!r} and {second.title!r} below {self._nodes[parent].title!r} are less than "
                            f"{ORDER_WARNING_DISTANCE:.0f} pixels apart sideways: the widget may run them in "
                            "another order; spread them sideways"
                        )
        return warnings

    def mark_saved(self) -> None:
        """The document was written to its file."""
        self.unsaved = False

    # ================================================================================ the wire
    def node_to_wire(self, node_id: str, entry_types: dict[str, str] | None = None) -> dict:
        """The node in wire form (contract 5.1)."""
        node = self._nodes[node_id]
        if node.raw is not None:
            item = _json_safe(self._file_item(node))
            item["unknown"] = True
            return item
        item = self._file_item(node)
        if "fields" in node.data:
            kinds = {spec["name"]: spec.get("kind", "other") for spec in self._declared(node.type)}
            item["fields"] = {
                name: self._field_wire(node, name, kinds.get(name, "other"), value, entry_types or {})
                for name, value in node.data["fields"].items()
            }
        return item

    @staticmethod
    def _field_wire(node: _Node, name: str, kind: str, value: Any, entry_types: dict[str, str]) -> Any:
        if node.type in ("Evaluation", "Set") and name == "LiteralValue":
            return typed_to_wire(value, _literal_type(value, entry_types.get(node.data["fields"].get("ValueName"))))
        return field_to_wire(kind, value)

    def to_wire(self, entry_types: dict[str, str] | None = None) -> dict:
        """The document in wire form (contract 5.1). ``entry_types``: entry name -> type, which
        gives the type of the literal of Evaluation and Set nodes."""
        shown = self.view or {"zoom": 1.0, "center": [0.0, 0.0]}
        return {
            "key": self.key,
            "path": tree_file_name(self.key),
            "rev": self.rev,
            "version": self.file_version(),
            "unsaved": self.unsaved,
            "config": dict(self.config),
            "view": {"zoom": shown["zoom"], "center": list(shown["center"])},
            "nodes": [self.node_to_wire(node_id, entry_types) for node_id in self._nodes],
            "connections": [{"parent": parent, "child": child} for parent, child in self.connections()],
            "problems": list(self.problems),
        }

    def subtree_problems(self) -> list[dict]:
        """Sub-Tree and Remote Sub-Tree nodes whose ``File`` cannot be followed (F-07): an
        absolute path or one leading outside the trees folder. Each is
        ``{"node": id, "file": name, "text": reason}``; the page offers to choose the file again
        (contract 5.4, ``choose_file``)."""
        found = []
        for node in self._nodes.values():
            if node.raw is None and node.type in (SUB_TREE, REMOTE_SUB_TREE):
                name = node.data["fields"].get("File", "")
                if isinstance(name, str) and name.strip():
                    try:
                        subtree_key(self.key, name)
                    except PathError as error:
                        found.append({"node": node.id, "file": name, "text": str(error)})
        return found

    # ================================================================================ operations
    def apply(
        self,
        op: dict,
        *,
        running: bool = False,
        entry_types: dict[str, str] | None = None,
        rev: int | None = None,
    ) -> Applied:
        """Check and apply one operation of contract 5.3 (not ``bb.*``, not ``group.promote``,
        which :func:`bt_server_core.promote.promote` turns into operations of this kind).

        Args:
            op: The operation, values in wire form.
            running: The tree executes: structural changes are refused (G-04).
            entry_types: Entry name -> type, for the literal of Evaluation and Set.
            rev: The revision the browser worked at; a ``node.move`` at another revision keeps
                its positions and drops its child orders (``Applied.reorder``).

        Raises:
            OpRefused: the operation cannot be applied; nothing changed.
        """
        if not isinstance(op, dict) or not isinstance(op.get("op"), str):
            raise OpRefused("an operation is an object with 'op'")
        name = op["op"]
        if running and name in STRUCTURAL_OPS:
            raise OpRefused("the tree is executing: nodes cannot be added, removed or connected, nor composites changed")
        handler = _HANDLERS.get(name)
        if handler is None:
            if name == "group.promote":
                raise OpRefused("group.promote is carried out with promote.promote()")
            raise OpRefused(f"unknown operation {name!r}")
        reorder: list[str] = []
        normalised = handler(self, op, entry_types or {}, rev, reorder)
        self.rev += 1
        self.unsaved = True
        return Applied(normalised, self.rev, reorder)

    def apply_all(self, ops: list[dict], *, running: bool = False, entry_types: dict[str, str] | None = None) -> list[Applied]:
        """Apply several operations as one: if one is refused, none is applied.

        Raises:
            OpRefused: one of them was refused; the document is as before.
        """
        saved = copy.deepcopy((self._nodes, self._children, self._parent, self.config, self._group_counter))
        rev, unsaved = self.rev, self.unsaved
        try:
            return [self.apply(op, running=running, entry_types=entry_types) for op in ops]
        except OpRefused:
            self._nodes, self._children, self._parent, self.config, self._group_counter = saved
            self.rev, self.unsaved = rev, unsaved
            raise

    def set_field_file(self, node_id: str, name: str, encoded: Any, entry_types: dict[str, str] | None = None) -> Applied:
        """Set a field to a value in file encoding (a value a running leaf wrote, as the executor
        reports it on ``~/details``); returns the ``node.set_field`` to broadcast, in wire form.

        Raises:
            OpRefused: no such node or field, or the value is malformed.
        """
        node = self._leaf(node_id)
        if name not in node.data["fields"]:
            raise OpRefused(f"the node {node.title!r} has no field {name!r}")
        try:
            value = decode_value(encoded)
        except ValueFileError as error:
            raise OpRefused(str(error)) from error
        node.data["fields"][name] = value
        self.rev += 1
        kind = self._kind_of(node, name)
        wire = self._field_wire(node, name, kind, value, entry_types or {})
        return Applied({"op": "node.set_field", "id": node_id, "field": name, "value": wire}, self.rev)

    def rename_entry(self, old: str, new: str, entry_types: dict[str, str] | None = None) -> list[Applied]:
        """An entry was renamed: Evaluation and Set (``ValueName``, ``CompareTo``), Print Message
        (``Source``), bindings and outputs that name it follow (design 6.4). Returns the applied
        operations to broadcast."""
        ops: list[dict] = []
        for node in self._nodes.values():
            if node.raw is not None or "fields" not in node.data:
                continue
            fields = node.data["fields"]
            for name in ("ValueName", *_ENTRY_NAME_FIELDS.get(node.type, ())):
                if node.type in _ENTRY_NAME_FIELDS and fields.get(name) == old:
                    ops.append({"op": "node.set_field", "id": node.id, "field": name,
                                "value": field_to_wire(self._kind_of(node, name), new)})
            for key, op_name in (("bindings", "node.set_binding"), ("outputs", "node.set_output")):
                for path, entry in node.data.get(key, {}).items():
                    if entry == old:
                        ops.append({"op": op_name, "id": node.id, "key": path, "entry": new})
        return self.apply_all(ops, entry_types=entry_types)

    # -------------------------------------------------------------------------------- helpers
    def _node(self, op: dict, key: str = "id") -> _Node:
        node_id = op.get(key)
        if not isinstance(node_id, str) or node_id not in self._nodes:
            raise OpRefused(f"no node {node_id!r} in this tree")
        return self._nodes[node_id]

    def _leaf(self, node_id: Any) -> _Node:
        node = self._nodes.get(node_id) if isinstance(node_id, str) else None
        if node is None:
            raise OpRefused(f"no node {node_id!r} in this tree")
        if node.raw is not None:
            raise OpRefused(f"the type {node.type!r} of {node.title!r} is unknown here; its data cannot be edited")
        if "fields" not in node.data:
            raise OpRefused(f"{node.title!r} is not a leaf")
        return node

    def _kind_of(self, node: _Node, name: str) -> str:
        for spec in self._declared(node.type):
            if spec["name"] == name:
                return spec.get("kind", "other")
        return "other"

    def _next_group_name(self, taken: set[str]) -> str:
        """``widget._next_group_name``: counts up from the highest number given so far (or used
        by a group), skipping names in use."""
        names = {node.title for node in self._nodes.values() if node.type == GROUP} | taken
        used = [number for number in map(default_group_number, names) if number is not None]
        number = max([self._group_counter, *used]) + 1
        while default_group_name(number) in names:
            number += 1
        self._group_counter = number
        return default_group_name(number)

    def _check_new_id(self, node_id: Any, taken: set[str]) -> str:
        if not isinstance(node_id, str) or not NODE_ID.fullmatch(node_id):
            raise OpRefused(f"a node id is 32 hex digits, not {node_id!r}")
        if node_id in self._nodes or node_id in taken:
            raise OpRefused(f"the id {node_id} is in use")
        return node_id

    def _check_orders(self, orders: Any, children: dict[str, list[str]]) -> dict[str, list[str]]:
        """Checked child orders: each a permutation of the parent's children (``children``: the
        children as they will be)."""
        if not isinstance(orders, dict):
            raise OpRefused("orders is an object: parent id -> child ids")
        result = {}
        for parent, order in orders.items():
            current = children.get(parent, self._children.get(parent, []))
            if (
                parent not in self._nodes
                or not isinstance(order, list)
                or not all(isinstance(item, str) for item in order)
                or sorted(order) != sorted(current)
            ):
                raise OpRefused(f"the order given for {parent!r} does not list exactly its children")
            if len(set(order)) != len(order):
                raise OpRefused(f"the order given for {parent!r} names a child twice")
            result[parent] = list(order)
        return result

    def _apply_orders(self, orders: dict[str, list[str]]) -> None:
        for parent, order in orders.items():
            self._children[parent] = list(order)

    def _wire_position(self, value: Any, what: str) -> float:
        if not _finite(value):
            raise OpRefused(f"{what} must be a finite number")
        return clamp_position(value)

    def _new_node(self, item: Any, taken: set[str], *, paste: bool) -> _Node:
        """A node from its wire form (``node.add``, ``paste``), checked; not yet added."""
        if not isinstance(item, dict):
            raise OpRefused("a node is an object")
        node_id = self._check_new_id(item.get("id"), taken)
        type_name = item.get("type")
        if type_name == ROOT:
            raise OpRefused("a tree has one Root; another cannot be added")
        if not isinstance(type_name, str) or not type_name:
            raise OpRefused("a node needs a type")
        if item.get("unknown") is True or self._known_kind(type_name) is None:
            if not paste:
                raise OpRefused(f"unknown node type {type_name!r}")
            problems: list[str] = []
            saved = {key: value for key, value in item.items() if key != "unknown"}
            node = self._node_from_file(saved, repr(saved.get("title", type_name)), problems)
        else:
            node = self._node_from_wire(item, type_name)
        node.id = node_id
        title = item.get("title")
        if title is not None and not isinstance(title, str):
            raise OpRefused("a title is text")
        return node

    def _node_from_wire(self, item: dict, type_name: str) -> _Node:
        kind = self._known_kind(type_name)
        title = item.get("title") if isinstance(item.get("title"), str) and item.get("title") else type_name
        x = self._wire_position(item.get("x", 0.0), "x")
        y = self._wire_position(item.get("y", 0.0), "y")
        data: dict[str, Any]
        if kind == "composite" and type_name != PARALLEL:
            memory = item.get("memory", self.config["default_memory"])
            if not isinstance(memory, bool):
                raise OpRefused("memory is true or false")
            data = {"memory": memory}
        elif kind == "composite":
            policy = item.get("policy", "all")
            if policy not in POLICIES:
                raise OpRefused("a Parallel's policy is 'all' or 'any'")
            data = {"policy": policy}
        elif kind == "decorator":
            data = {}
        elif kind in ("note", "group"):
            data = self._box_from_wire(type_name, item)
        else:
            data = self._leaf_from_wire(type_name, item)
        return _Node("", type_name, title, x, y, data)

    def _box_from_wire(self, type_name: str, item: dict) -> dict:
        default = BOX_LIMITS[type_name]["default"]
        width, height = item.get("width", default[0]), item.get("height", default[1])
        if not (_finite(width) and _finite(height)):
            raise OpRefused("a size is a finite number")
        width, height = self._clamped_size(type_name, width, height)
        if type_name == NOTES:
            text = item.get("text", "")
            if not isinstance(text, str):
                raise OpRefused("a note's text is text")
            return {"text": text, "width": width, "height": height}
        color = _parse_color(item.get("color", DEFAULT_GROUP_COLOR))
        if color is None:
            raise OpRefused(f"{item.get('color')!r} is not a colour such as '#3d7cc9'")
        return {"width": width, "height": height, "color": color}

    def _leaf_from_wire(self, type_name: str, item: dict) -> dict:
        node = _Node("", type_name, type_name, 0.0, 0.0, {"fields": {}, "selections": {}})
        declared = self._declared(type_name)
        node.data["fields"] = {spec["name"]: copy.deepcopy(spec.get("default")) for spec in declared}
        wire_fields = item.get("fields", {})
        if not isinstance(wire_fields, dict):
            raise OpRefused("fields is an object")
        for name, wire in wire_fields.items():
            node.data["fields"][name] = self._decode_field(node, name, wire)
        selections = item.get("field_selections", {})
        if not isinstance(selections, dict):
            raise OpRefused("field_selections is an object")
        for name, index in selections.items():
            node.data["selections"][name] = self._check_selection(node, name, index)
        for key in ("bindings", "outputs"):
            mapping = item.get(key)
            if mapping is not None:
                if not isinstance(mapping, dict):
                    raise OpRefused(f"{key} is an object")
                for path, entry in mapping.items():
                    self._check_link(node, key, path, entry)
                node.data[key] = dict(mapping)
        for option in LEAF_OPTIONS:
            if item.get(option) is not None:
                node.data[option] = self._check_option(option, item[option])
        return node.data

    def _decode_field(self, node: _Node, name: str, wire: Any, path: str | None = None, kind: str | None = None) -> Any:
        """A field value from its wire form, checked as the widget's editors would allow it."""
        if not isinstance(name, str) or name not in node.data["fields"]:
            raise OpRefused(f"the node {node.title!r} has no field {name!r}")
        try:
            if path is not None:
                return self._set_inside(node.data["fields"][name], path, field_from_wire(kind or "other", wire))
            if node.type in ("Evaluation", "Set") and name == "LiteralValue":
                value = typed_from_wire(wire)
                if value is not None and infer_type(value) == "Object":
                    raise ValueError("an Object cannot be a literal")
                return value
            value = field_from_wire(self._kind_of(node, name), wire)
        except ValueError as error:
            raise OpRefused(f"{name}: {error}") from error
        if name in _ENTRY_NAME_FIELDS.get(node.type, ()):
            if value is not None and not isinstance(value, str):
                raise OpRefused(f"{name} names an entry or is empty")
            return value or None
        if node.type == "Evaluation" and name == "Operator" and value not in OPERATORS:
            raise OpRefused(f"the operator is one of {' '.join(OPERATORS)}")
        if node.type == "Print Message" and name == "Level" and _log_level(value) is _REJECTED:
            raise OpRefused("the level is 0 to 4")
        return value

    @staticmethod
    def _set_inside(container: Any, path: str, value: Any) -> Any:
        """A copy of the dictionary field ``container`` with ``value`` at ``path``."""
        if not isinstance(container, dict):
            raise ValueError("only a dictionary field holds values at a path")
        result = copy.deepcopy(container)
        parts = _parse_path(path)
        current: Any = result
        for part, following in zip(parts, parts[1:]):
            if isinstance(part, int):
                if not isinstance(current, list) or not 0 <= part < len(current):
                    raise ValueError(f"the path {path!r} names no list item")
                current = current[part]
            else:
                if not isinstance(current, dict):
                    raise ValueError(f"the path {path!r} leads into a value that is not a dictionary")
                current = current.setdefault(part, [] if isinstance(following, int) else {})
        last = parts[-1]
        if isinstance(last, int):
            if not isinstance(current, list) or not 0 <= last < len(current):
                raise ValueError(f"the path {path!r} names no list item")
        elif not isinstance(current, dict):
            raise ValueError(f"the path {path!r} leads into a value that is not a dictionary")
        current[last] = value
        return result

    @staticmethod
    def _check_selection(node: _Node, name: str, index: Any) -> int:
        value = node.data["fields"].get(name) if isinstance(name, str) else None
        if not isinstance(value, list):
            raise OpRefused(f"{name!r} is not a choice field")
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(value):
            raise OpRefused(f"index {index!r} is out of range for {name!r}")
        return index

    def _check_link(self, node: _Node, key: str, path: Any, entry: Any) -> None:
        """A binding (``key`` "bindings") or output: its path and its entry name."""
        if not isinstance(path, str) or not path:
            raise OpRefused(f"a {key[:-1]} needs a path")
        if entry is not None:
            problem = entry_name_problem(entry)
            if problem is not None:
                raise OpRefused(problem)
        if node.type == REMOTE_SUB_TREE:
            problem = entry_name_problem(path)
            if problem is not None:
                raise OpRefused(f"the remote entry: {problem}")
            return
        if key == "bindings":
            if not any(path == name or path.startswith((name + ".", name + "[")) for name in node.data["fields"]):
                raise OpRefused(f"{path!r} names no field of {node.title!r}")
            return
        prefixes = {"CallAction": ("Result.", "Feedback."), "CallService": ("Response.",)}.get(node.type)
        if prefixes is not None and not path.startswith(prefixes) and path not in ("Result", "Feedback", "Response"):
            raise OpRefused(f"an output of {node.title!r} starts with {' or '.join(prefixes)}")

    @staticmethod
    def _check_option(name: Any, value: Any) -> Any:
        if name == "wait_for_cancels" and _is_bool(value):
            return value
        if name == "cancel_timeout_ms" and _is_timeout(value):
            return value
        if name not in LEAF_OPTIONS:
            raise OpRefused(f"unknown option {name!r}")
        raise OpRefused(f"{name} must be {'true or false' if name == 'wait_for_cancels' else 'a whole number of ms, 0 or more'}")

    # -------------------------------------------------------------------------------- handlers
    def _op_node_add(self, op: dict, entry_types: dict, rev: int | None, reorder: list) -> dict:
        node = self._new_node(op.get("node"), set(), paste=False)
        orders = self._check_orders(op["order"], {}) if op.get("order") is not None else {}
        if node.type == GROUP and (not node.title or node.title == GROUP or default_group_number(node.title) is not None):
            node.title = self._next_group_name(set())
        self._nodes[node.id] = node
        self._apply_orders(orders)
        result = {"op": "node.add", "node": self.node_to_wire(node.id, entry_types)}
        if orders:
            result["order"] = orders
        return result

    def _op_node_remove(self, op: dict, entry_types: dict, rev: int | None, reorder: list) -> dict:
        ids = op.get("ids")
        if not isinstance(ids, list) or not ids:
            raise OpRefused("ids lists the nodes to remove")
        for node_id in ids:
            if not isinstance(node_id, str) or node_id not in self._nodes:
                raise OpRefused(f"no node {node_id!r} in this tree")
            if self._nodes[node_id].type == ROOT:
                raise OpRefused("the Root cannot be removed")
        ids = list(dict.fromkeys(ids))
        for node_id in ids:
            self._unlink(node_id)
            for child in self._children.pop(node_id, []):
                self._parent.pop(child, None)
            del self._nodes[node_id]
        return {"op": "node.remove", "ids": ids}

    def _op_node_move(self, op: dict, entry_types: dict, rev: int | None, reorder: list) -> dict:
        positions = op.get("positions", {})
        if not isinstance(positions, dict):
            raise OpRefused("positions is an object: id -> [x, y]")
        moved = {}
        for node_id, position in positions.items():
            if node_id not in self._nodes:
                raise OpRefused(f"no node {node_id!r} in this tree")
            if not isinstance(position, list) or len(position) != 2:
                raise OpRefused("a position is [x, y]")
            moved[node_id] = [self._wire_position(position[0], "x"), self._wire_position(position[1], "y")]
        orders: dict[str, list[str]] = {}
        if op.get("orders"):
            if rev is not None and rev != self.rev:
                if not isinstance(op["orders"], dict):
                    raise OpRefused("orders is an object: parent id -> child ids")
                reorder.extend(op["orders"])
            else:
                orders = self._check_orders(op["orders"], {})
        for node_id, (x, y) in moved.items():
            self._nodes[node_id].x, self._nodes[node_id].y = x, y
        self._apply_orders(orders)
        result: dict[str, Any] = {"op": "node.move", "positions": moved}
        if orders:
            result["orders"] = orders
        return result

    def _op_node_set_title(self, op: dict, entry_types: dict, rev: int | None, reorder: list) -> dict:
        node = self._node(op)
        title = op.get("title")
        if not isinstance(title, str):
            raise OpRefused("a title is text")
        node.title = title or node.type  # an empty title gives the default title, as when loading
        return {"op": "node.set_title", "id": node.id, "title": node.title}

    def _op_node_set_field(self, op: dict, entry_types: dict, rev: int | None, reorder: list) -> dict:
        node = self._leaf(op.get("id"))
        name = op.get("field")
        path, kind = op.get("path"), op.get("kind")
        if path is not None and (not isinstance(path, str) or not path):
            raise OpRefused("a path is text such as 'pose.position.x'")
        if kind is not None and kind not in ("int", "float", "str", "bool", "other"):
            raise OpRefused(f"unknown kind {kind!r}")
        value = self._decode_field(node, name, op.get("value"), path, kind)
        node.data["fields"][name] = value
        if isinstance(value, list):
            index = node.data["selections"].get(name, 0)
            node.data["selections"][name] = min(max(index, 0), max(len(value) - 1, 0))
        else:
            node.data["selections"].pop(name, None)
        result = {"op": "node.set_field", "id": node.id, "field": name}
        if path is not None:
            inner = node.data["fields"][name]
            for part in _parse_path(path):
                inner = inner[part]
            result.update({"value": field_to_wire(kind or "other", inner), "path": path})
            if kind is not None:
                result["kind"] = kind
        else:
            result["value"] = self._field_wire(node, name, self._kind_of(node, name), value, entry_types)
        return result

    def _op_node_set_selection(self, op: dict, entry_types: dict, rev: int | None, reorder: list) -> dict:
        node = self._leaf(op.get("id"))
        name = op.get("field")
        node.data["selections"][name] = self._check_selection(node, name, op.get("index"))
        return {"op": "node.set_selection", "id": node.id, "field": name, "index": op["index"]}

    def _set_link(self, op: dict, key: str) -> dict:
        node = self._leaf(op.get("id"))
        path, entry = op.get("key"), op.get("entry")
        self._check_link(node, key, path, entry)
        mapping = node.data.setdefault(key, {})
        if entry is None:
            mapping.pop(path, None)
        else:
            mapping[path] = entry
        if not mapping:
            del node.data[key]
        return {"op": op["op"], "id": node.id, "key": path, "entry": entry}

    def _op_node_set_binding(self, op: dict, entry_types: dict, rev: int | None, reorder: list) -> dict:
        return self._set_link(op, "bindings")

    def _op_node_set_output(self, op: dict, entry_types: dict, rev: int | None, reorder: list) -> dict:
        return self._set_link(op, "outputs")

    def _op_node_set_option(self, op: dict, entry_types: dict, rev: int | None, reorder: list) -> dict:
        node = self._leaf(op.get("id"))
        name, value = op.get("name"), op.get("value")
        if value is None:
            if name not in LEAF_OPTIONS:
                raise OpRefused(f"unknown option {name!r}")
            node.data.pop(name, None)
        else:
            node.data[name] = self._check_option(name, value)
        return {"op": "node.set_option", "id": node.id, "name": name, "value": value}

    def _box(self, op: dict, *types: str) -> _Node:
        node = self._node(op)
        if node.type not in types or node.raw is not None:
            raise OpRefused(f"{node.title!r} is not a {' or '.join(types)}")
        return node

    def _op_node_resize(self, op: dict, entry_types: dict, rev: int | None, reorder: list) -> dict:
        node = self._box(op, NOTES, GROUP)
        width, height = op.get("width"), op.get("height")
        if not (_finite(width) and _finite(height)):
            raise OpRefused("a size is a finite number")
        x = self._wire_position(op["x"], "x") if op.get("x") is not None else None
        y = self._wire_position(op["y"], "y") if op.get("y") is not None else None
        node.data["width"], node.data["height"] = self._clamped_size(node.type, width, height)
        result: dict[str, Any] = {"op": "node.resize", "id": node.id,
                                  "width": node.data["width"], "height": node.data["height"]}
        if x is not None:
            node.x = result["x"] = x
        if y is not None:
            node.y = result["y"] = y
        return result

    def _op_node_set_text(self, op: dict, entry_types: dict, rev: int | None, reorder: list) -> dict:
        node = self._box(op, NOTES)
        text = op.get("text")
        if not isinstance(text, str):
            raise OpRefused("a note's text is text")
        node.data["text"] = text
        return {"op": "node.set_text", "id": node.id, "text": text}

    def _op_node_set_color(self, op: dict, entry_types: dict, rev: int | None, reorder: list) -> dict:
        node = self._box(op, GROUP)
        color = _parse_color(op.get("color"))
        if color is None:
            raise OpRefused(f"{op.get('color')!r} is not a colour such as '#3d7cc9'")
        node.data["color"] = color
        return {"op": "node.set_color", "id": node.id, "color": color}

    def _composite(self, op: dict, *types: str) -> _Node:
        node = self._node(op)
        if node.type not in types:
            raise OpRefused(f"{node.title!r} is not a {' or '.join(types)}")
        return node

    def _op_composite_set_type(self, op: dict, entry_types: dict, rev: int | None, reorder: list) -> dict:
        node = self._composite(op, ROOT, *COMPOSITES)
        composite = op.get("composite")
        allowed = ROOT_COMPOSITES if node.type == ROOT else COMPOSITES
        if composite not in allowed:
            raise OpRefused(f"{node.title!r} can be {', '.join(allowed)}")
        result = {"op": "composite.set_type", "id": node.id, "composite": composite}
        if node.type == ROOT:
            node.data["composite"] = composite
            return result
        old = node.type
        if composite == PARALLEL:
            node.data = {"policy": "all"}
        elif old == PARALLEL:
            node.data = {"memory": self.config["default_memory"]}
        node.type = composite
        if node.title == old:  # a title that names the type follows it (the widget's rule)
            node.title = composite
        result["title"] = node.title
        return result

    def _op_composite_set_memory(self, op: dict, entry_types: dict, rev: int | None, reorder: list) -> dict:
        node = self._composite(op, ROOT, SEQUENCE, SELECTOR)
        memory = op.get("memory")
        if not isinstance(memory, bool):
            raise OpRefused("memory is true or false")
        node.data["memory"] = memory
        return {"op": "composite.set_memory", "id": node.id, "memory": memory}

    def _op_composite_set_policy(self, op: dict, entry_types: dict, rev: int | None, reorder: list) -> dict:
        node = self._composite(op, PARALLEL)
        policy = op.get("policy")
        if policy not in POLICIES:
            raise OpRefused("a Parallel's policy is 'all' or 'any'")
        node.data["policy"] = policy
        return {"op": "composite.set_policy", "id": node.id, "policy": policy}

    def _check_connection(self, parent_id: Any, child_id: Any) -> tuple[_Node, _Node]:
        parent = self._node({"id": parent_id})
        child = self._node({"id": child_id})
        if not self._takes_children(parent):
            raise OpRefused(f"{parent.title!r} takes no children")
        if not self._takes_parent(child):
            raise OpRefused(f"{child.title!r} cannot be connected below another node")
        if self._is_ancestor(child.id, parent.id):
            raise OpRefused(f"connecting {child.title!r} below {parent.title!r} would make a cycle")
        return parent, child

    def _op_conn_add(self, op: dict, entry_types: dict, rev: int | None, reorder: list) -> dict:
        parent, child = self._check_connection(op.get("parent"), op.get("child"))
        children = list(self._children.get(parent.id, []))
        replaced: list[str] = []
        if child.id not in children:  # connecting it again changes nothing (the widget's rule)
            replaced = children[:1] if parent.type == NEGATION else []  # a second child replaces the first
            children = [item for item in children if item not in replaced] + [child.id]
        orders = self._check_orders({parent.id: op["order"]}, {parent.id: children}) if op.get("order") is not None else {}
        for item in replaced:
            self._unlink(item)
        if self._parent.get(child.id) != parent.id:
            self._unlink(child.id)
            self._link(parent.id, child.id)
        self._apply_orders(orders)
        return {"op": "conn.add", "parent": parent.id, "child": child.id, "order": self.children(parent.id)}

    def _op_conn_remove(self, op: dict, entry_types: dict, rev: int | None, reorder: list) -> dict:
        parent_id, child_id = op.get("parent"), op.get("child")
        if not isinstance(child_id, str) or self._parent.get(child_id) != parent_id:
            raise OpRefused("there is no such connection")
        self._unlink(child_id)
        return {"op": "conn.remove", "parent": parent_id, "child": child_id}

    def _op_paste(self, op: dict, entry_types: dict, rev: int | None, reorder: list) -> dict:
        items, links = op.get("nodes"), op.get("connections", [])
        if not isinstance(items, list) or not items or not isinstance(links, list):
            raise OpRefused("paste needs nodes and connections")
        nodes: list[_Node] = []
        taken: set[str] = set()
        for item in items:
            node = self._new_node(item, taken, paste=True)
            taken.add(node.id)
            nodes.append(node)
        saved = (dict(self._nodes), {key: list(value) for key, value in self._children.items()}, dict(self._parent))
        counter = self._group_counter
        try:
            self._nodes.update((node.id, node) for node in nodes)
            problems: list[str] = []
            self._load_connections(links, problems, among=taken)
            if problems:
                raise OpRefused(f"the pasted connections are invalid: {problems[0]}")
            names: set[str] = set()
            for node in nodes:
                if node.type == GROUP and node.raw is None and default_group_number(node.title) is not None:
                    node.title = self._next_group_name(names)
                    names.add(node.title)
        except OpRefused:
            self._nodes, self._children, self._parent = saved
            self._group_counter = counter
            raise
        return {
            "op": "paste",
            "nodes": [self.node_to_wire(node.id, entry_types) for node in nodes],
            "connections": [{"parent": self._parent[node.id], "child": node.id} for node in nodes if node.id in self._parent],
        }

    def _op_doc_set_config(self, op: dict, entry_types: dict, rev: int | None, reorder: list) -> dict:
        changes = op.get("config")
        if not isinstance(changes, dict):
            raise OpRefused("config is an object")
        checked = {}
        for name, value in changes.items():
            if name not in CONFIG_DEFAULTS:
                raise OpRefused(f"unknown option {name!r}")
            if name in ("repeat", "restore_blackboard", "default_memory", "wait_for_cancels"):
                ok = isinstance(value, bool)
            elif name == "tick_interval_ms":
                ok = _is_int(value) and MIN_TICK_MS <= value <= MAX_TICK_MS
            elif name == "log_level":
                ok = _is_int(value) and 0 <= value <= 4
            elif name == "cancel_timeout_ms":
                ok = _is_int(value) and value >= 0
            else:
                ok = value in ("stop", "continue")
            if not ok:
                raise OpRefused(f"{value!r} is not a valid {name}")
            checked[name] = value
        self.config.update(checked)
        return {"op": "doc.set_config", "config": checked}


_REJECTED = object()


def _is_bool(value: Any) -> bool:
    return isinstance(value, bool)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_timeout(value: Any) -> bool:
    return _is_int(value) and 0 <= value <= INT_MAX


def _log_level(value: Any) -> Any:
    """``LogLevel.coerce`` of the widget: 0 to 4 or a level name; ``_REJECTED`` otherwise."""
    if _is_int(value) and 0 <= value <= 4:
        return value
    if isinstance(value, str) and value.strip().upper() in _LOG_LEVELS:
        return _LOG_LEVELS[value.strip().upper()]
    return _REJECTED


def _parse_color(color: Any) -> str | None:
    """``#rgb`` or ``#rrggbb`` as ``#rrggbb`` in lower case (``QColor.name()``); None otherwise."""
    if not isinstance(color, str) or not _HEX_COLOR.fullmatch(color.strip()):
        return None
    digits = color.strip()[1:]
    if len(digits) == 3:
        digits = "".join(digit * 2 for digit in digits)
    return "#" + digits.lower()


def _same_value(a: Any, b: Any) -> bool:
    """A field at its default (``1 == 1.0`` counts; ``True`` is not ``1``)."""
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    try:
        return type(a) is type(b) and a == b or (_is_number(a) and _is_number(b) and a == b)
    except Exception:  # noqa: BLE001 - a value without a working ==
        return False


def _literal_type(value: Any, entry_type: str | None) -> str:
    """The type a literal is shown with: its entry's type when the literal fits it, else its own."""
    if entry_type is not None and entry_type != "Object":
        if value is None:
            return entry_type
        try:
            coerce_value(entry_type, value)
        except (TypeError, ValueError):
            pass
        else:
            return entry_type
    return "Object" if value is None else infer_type(value)


_HANDLERS = {
    "node.add": TreeDocument._op_node_add,
    "node.remove": TreeDocument._op_node_remove,
    "node.move": TreeDocument._op_node_move,
    "node.set_title": TreeDocument._op_node_set_title,
    "node.set_field": TreeDocument._op_node_set_field,
    "node.set_selection": TreeDocument._op_node_set_selection,
    "node.set_binding": TreeDocument._op_node_set_binding,
    "node.set_output": TreeDocument._op_node_set_output,
    "node.set_option": TreeDocument._op_node_set_option,
    "node.resize": TreeDocument._op_node_resize,
    "node.set_text": TreeDocument._op_node_set_text,
    "node.set_color": TreeDocument._op_node_set_color,
    "composite.set_type": TreeDocument._op_composite_set_type,
    "composite.set_memory": TreeDocument._op_composite_set_memory,
    "composite.set_policy": TreeDocument._op_composite_set_policy,
    "conn.add": TreeDocument._op_conn_add,
    "conn.remove": TreeDocument._op_conn_remove,
    "paste": TreeDocument._op_paste,
    "doc.set_config": TreeDocument._op_doc_set_config,
}
