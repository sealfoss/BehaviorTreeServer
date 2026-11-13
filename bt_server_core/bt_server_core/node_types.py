"""Declarations of the built-in node types: their names in the files, titles, fields and
defaults, and how the browser shows them.

This is data only. The engine's classes for these types (``bt_server_core``), the ROS leaves
(``bt_server_executor``) and the catalog the executor publishes through ``~/describe``
(design 6.4) all take the names and fields from here, so the file format has one source.

Field names and defaults of the widget's types are the widget's (``behavior_tree_widget``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "BUILTIN_TYPES",
    "COMPOSITE_TYPES",
    "CONTROL_TYPES",
    "FieldSpec",
    "NodeTypeSpec",
    "RESERVED_TYPE_NAMES",
    "ROS_TYPES",
    "WIDGET_TYPES",
    "builtin_catalog",
    "spec",
]


@dataclass(frozen=True)
class FieldSpec:
    """One field of a node type (design 6.4).

    ``kind`` is one of :data:`bt_server_core.values.FIELD_KINDS`. A ``choice`` field's default is
    its list of options; the selected index is stored in ``field_selections`` of the node, as
    in the widget. ``new`` marks fields the widget does not know: a node that holds one at a
    value other than its default makes its file version 2 (design 12), and such a field is
    not written at its default.
    """

    name: str
    kind: str
    default: Any
    doc: str = ""
    new: bool = False

    def to_catalog(self) -> dict:
        return {"name": self.name, "kind": self.kind, "default": self.default, "doc": self.doc}


@dataclass(frozen=True)
class NodeTypeSpec:
    """A node type.

    kind:
        ``root``, ``composite`` (Sequence, Selector, Parallel), ``decorator`` (Negation),
        ``leaf``, ``note`` or ``group``.
    leaf_kind:
        For leaves (design 6.1): ``instant``, ``waiting`` (Evaluation may be either: it is
        declared ``instant``), ``async`` or ``thread``.
    special:
        Names the browser editor a type needs beyond plain field editors: ``evaluation``,
        ``set``, ``print``, ``subtree``, ``remote_subtree``, ``call_action``,
        ``call_service``, ``publish``; ``None`` for plain fields.
    category:
        The group of the Add Node menu.
    waits_for_cancels:
        Whether the leaf's start waits for earlier cancels (design 5.6).
    needs_ros:
        The type works only in an executor under ROS; with ``LocalLink`` it fails when run.
    widget:
        The widget knows the type: a file that uses only such types can be version 1.
    """

    type: str
    title: str
    kind: str
    category: str
    fields: tuple[FieldSpec, ...] = ()
    leaf_kind: str | None = None
    special: str | None = None
    waits_for_cancels: bool = False
    needs_ros: bool = False
    widget: bool = True
    doc: str = ""
    extra: dict = field(default_factory=dict)

    def field(self, name: str) -> FieldSpec | None:
        for item in self.fields:
            if item.name == name:
                return item
        return None

    def defaults(self) -> dict[str, Any]:
        """Field values of a new node of this type, as written in a file (``new`` fields at
        their defaults included)."""
        import copy

        return {item.name: copy.deepcopy(item.default) for item in self.fields}

    def to_catalog(self) -> dict:
        """The form of the type in the catalog sent to browsers (``~/describe`` ``node_types``)."""
        return {
            "type": self.type,
            "title": self.title,
            "kind": self.kind,
            "category": self.category,
            "fields": [item.to_catalog() for item in self.fields],
            "leaf_kind": self.leaf_kind,
            "special": self.special,
            "waits_for_cancels": self.waits_for_cancels,
            "needs_ros": self.needs_ros,
            "builtin": True,
            "doc": self.doc,
            **self.extra,
        }


_F = FieldSpec

#: Operators are kept as text in the field ``Operator`` (not as a widget choice list).
EVALUATION_FIELDS = (
    _F("ValueName", "str", "", "The entry to look at."),
    _F("CompareTo", "other", None, "null: compare with the literal; otherwise the name of another entry."),
    _F("LiteralValue", "other", None, "The literal; its type follows the ValueName entry."),
    _F("Operator", "str", "==", "One of == != < <= > >=.", new=True),
    _F("Wait", "bool", False, "Keep running until the comparison holds.", new=True),
    _F("WaitTimeout", "float", 0.0, "Seconds while waiting; 0 waits without limit.", new=True),
)

SET_FIELDS = (
    _F("ValueName", "str", "", "The entry to set."),
    _F("CompareTo", "other", None, "null: set to the literal; otherwise the name of the entry to copy."),
    _F("LiteralValue", "other", None, "The literal; its type follows the ValueName entry."),
)

BUILTIN_TYPES: tuple[NodeTypeSpec, ...] = (
    NodeTypeSpec("Root", "Root", "root", "Control", doc="The top of the tree: a Sequence or a Selector."),
    NodeTypeSpec("Sequence", "Sequence", "composite", "Control",
                 doc="Runs its children in order until one fails."),
    NodeTypeSpec("Selector", "Selector", "composite", "Control",
                 doc="Runs its children in order until one succeeds."),
    NodeTypeSpec("Parallel", "Parallel", "composite", "Control", widget=False,
                 doc="Runs its children at the same time; policy All or Any."),
    NodeTypeSpec("Negation", "Negation", "decorator", "Control",
                 doc="One child: Succeeded becomes Failed and the reverse."),
    NodeTypeSpec("Evaluation", "Evaluation", "leaf", "Blackboard", EVALUATION_FIELDS, "instant", "evaluation",
                 doc="Compares an entry with another entry or a literal; can wait until it holds."),
    NodeTypeSpec("Set", "Set", "leaf", "Blackboard", SET_FIELDS, "instant", "set",
                 doc="Sets an entry to another entry or a literal."),
    NodeTypeSpec("Restart", "Restart", "leaf", "Execution", (), "instant",
                 doc="Restarts the tree from the Root."),
    NodeTypeSpec("Pause", "Pause", "leaf", "Execution", (), "instant",
                 doc="Pauses the tree; it continues when resumed."),
    NodeTypeSpec("Stop", "Stop", "leaf", "Execution", (), "instant",
                 doc="Stops the tree, as the Stop button does."),
    NodeTypeSpec("UDP Message", "UDP Message", "leaf", "Messages",
                 (_F("Destination IP", "str", "127.0.0.1"), _F("Destination Port", "int", 5005),
                  _F("Message", "str", "")),
                 "thread", waits_for_cancels=True,
                 doc="Sends one UDP datagram from the executor's host."),
    NodeTypeSpec("Print Message", "Print Message", "leaf", "Messages",
                 (_F("Level", "int", 3, "1 ERROR, 2 WARNING, 3 DEBUG, 4 INFO."),
                  _F("Source", "other", None, "null, or the name of an entry to print as <entry> = <value>."),
                  _F("Message", "str", "")),
                 "instant", "print",
                 doc="Writes the message, or <entry> = <value>, to the tree's log."),
    NodeTypeSpec("Sub-Tree", "Sub-Tree", "leaf", "Sub-Trees", (_F("File", "str", ""),), None, "subtree",
                 doc="Runs the tree of its file as part of this tree."),
    NodeTypeSpec("Notes", "Notes", "note", "Annotation", doc="A note on the canvas."),
    NodeTypeSpec("Group", "Group", "group", "Annotation", doc="A frame around nodes."),
    NodeTypeSpec("CallAction", "Call Action", "leaf", "ROS",
                 (_F("Action", "str", "", "The action name, such as /robot1/navigate_to_pose.", new=True),
                  _F("Type", "str", "", "The action type, such as nav2_msgs/action/NavigateToPose.", new=True),
                  _F("Goal", "other", {}, "The goal: a dictionary of field values.", new=True),
                  _F("Timeout", "float", 0.0, "Seconds; 0: none.", new=True),
                  _F("Feedback Timeout", "float", 0.0, "Seconds without feedback; 0: none.", new=True)),
                 "async", "call_action", waits_for_cancels=True, needs_ros=True, widget=False,
                 doc="Sends a goal to a ROS action server and waits for the result."),
    NodeTypeSpec("CallService", "Call Service", "leaf", "ROS",
                 (_F("Service", "str", "", new=True), _F("Type", "str", "", new=True),
                  _F("Request", "other", {}, new=True), _F("Timeout", "float", 5.0, new=True),
                  _F("Success Field", "str", "success", new=True)),
                 "async", "call_service", waits_for_cancels=True, needs_ros=True, widget=False,
                 doc="Calls a ROS service and waits for the reply."),
    NodeTypeSpec("Publish", "Publish", "leaf", "ROS",
                 (_F("Topic", "str", "", new=True), _F("Type", "str", "", new=True),
                  _F("Message", "other", {}, new=True), _F("Wait For Subscribers", "int", 0, new=True),
                  _F("Keep For Late Joiners", "bool", False, new=True)),
                 "async", "publish", waits_for_cancels=True, needs_ros=True, widget=False,
                 doc="Publishes one message on a ROS topic."),
    NodeTypeSpec("RemoteSubTree", "Remote Sub-Tree", "leaf", "Sub-Trees",
                 (_F("Executor", "str", "", "The other executor, such as /robot2/bt_executor.", new=True),
                  _F("File", "str", "", new=True), _F("Timeout", "float", 0.0, new=True),
                  _F("Watchdog", "float", 5.0, new=True)),
                 "async", "remote_subtree", waits_for_cancels=True, needs_ros=True, widget=False,
                 doc="Runs a tree on another executor."),
)

_BY_NAME = {item.type: item for item in BUILTIN_TYPES}

#: Every built-in type name; custom leaf types cannot take them (design 6.6).
RESERVED_TYPE_NAMES = frozenset(_BY_NAME)
#: Types of the widget (a file that uses only these, and no new field or option, is version 1).
WIDGET_TYPES = frozenset(item.type for item in BUILTIN_TYPES if item.widget)
COMPOSITE_TYPES = frozenset(("Sequence", "Selector", "Parallel"))
CONTROL_TYPES = frozenset(("Root", "Sequence", "Selector", "Parallel", "Negation"))
ROS_TYPES = frozenset(item.type for item in BUILTIN_TYPES if item.needs_ros)


def spec(type_name: str) -> NodeTypeSpec | None:
    """The built-in type of that name, or None."""
    return _BY_NAME.get(type_name)


def builtin_catalog(*, ros: bool = True) -> list[dict]:
    """Catalog entries of the built-in types. ``ros=False`` leaves out the types that need ROS
    (``LocalLink``)."""
    return [item.to_catalog() for item in BUILTIN_TYPES if ros or not item.needs_ros]
