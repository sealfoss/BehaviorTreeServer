"""ROS interface types for the executor: the type-name check, loading interface classes, the
questions of ``~/describe`` about the ROS network, and the rules that turn message fields into
blackboard values (design 6.5, 7.4, 13).

**A type name never chooses an import** (design 13). ``get_action`` and its kin import the
Python module a type name points to, and type names come from tree files and browsers. A name is
therefore accepted only when it has the form ``<package>/<msg|srv|action>/<Name>``, its package
is a ROS interface package installed on this host (it has a ``rosidl_interfaces`` resource in
the ament index, which every package of the group ``rosidl_interface_packages`` installs), and
that resource lists the interface. Everything else is refused before anything is imported
(S-12, L-15: ``os/action/Thing`` does not import ``os.action``).

**Fields.** A field path is the contract's (2.1): ``percentage``, ``pose.position.x``,
``ranges[0]``; empty means the whole message. :func:`resolve_field` checks a path against an
interface class without a message; :func:`extract` takes the value out of a message as plain
Python values (messages become dictionaries, arrays lists).

**Entry types** (7.4): ``bool`` -> Bool; integers of up to 32 bits -> Integer or Double;
``uint32`` and 64-bit integers -> Double, or Integer when the value fits 32 bits (a value that
does not is skipped by the source); ``float32``/``float64`` -> Double; strings -> String; arrays
-> List; a nested message or the whole message -> Dictionary.

``describe(node, kind, arg)`` answers ``~/describe`` (contract 5.5, 5.8 item 4; the gateway wraps
the dict as ``{"graph": ...}`` / ``{"interface": ...}``):

* ``"graph"`` -> ``{"actions": [[name, type]], "services": [...], "topics": [...]}`` (hidden
  names, such as an action's own ``_action`` topics, are left out; a name with several types
  has their list as its type);
* ``"interface"``, ``{"type"}`` -> ``{"goal": [F], "result": [F], "feedback": [F]}``,
  ``{"request": [F], "response": [F]}`` or ``{"message": [F]}``, where ``F`` is ``{"name",
  "kind": <ROS type text such as int32, float64[3], sequence<float64>, geometry_msgs/msg/Pose>,
  "default"?, "min"?, "max"?, "fields"?: [F] (a nested message), "constants"?: [{"name",
  "value"}] (named constants of the message offered as choices for the field)}``;
* ``"sample"``, ``{"topic", "type"?, "field"?, "entry_type"?}`` -> ``{"value": {"type": <entry
  type>, "value": <wire>}, "message": text}``: one message taken now (within
  :data:`SAMPLE_TIMEOUT_S`), the value an entry with that source would get (the Source dialog's
  Test).

A refused question raises ValueError with the reason.
"""

from __future__ import annotations

import array
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import rosidl_parser.definition as idl
from ament_index_python import get_resource
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from rosidl_runtime_py import set_message_fields
from rosidl_runtime_py.import_message import import_message_from_namespaced_type
from rosidl_runtime_py.utilities import get_action, get_message, get_service

from bt_server_core.leaves import split_path
from bt_server_core.values import INT_MAX, INT_MIN, coerce_value, typed_to_wire

__all__ = [
    "SAMPLE_TIMEOUT_S",
    "FieldInfo",
    "SkipValue",
    "adapted_qos",
    "build_message",
    "describe",
    "entry_type_problem",
    "entry_types",
    "entry_value",
    "extract",
    "field_parts",
    "kind_of_type",
    "load_interface",
    "resolve_field",
    "to_python",
    "topic_types",
    "type_name_problem",
]

#: How long the Source dialog's Test waits for a message.
SAMPLE_TIMEOUT_S = 2.0

#: ``pkg/msg/Name``: a ROS package name (REP 144) and an interface name.
_TYPE_NAME = re.compile(r"([a-z][a-z0-9_]*)/(msg|srv|action)/([A-Za-z][A-Za-z0-9_]*)")
_KIND_TEXT = {"msg": "message", "srv": "service", "action": "action"}
_LOADERS = {"msg": get_message, "srv": get_service, "action": get_action}

#: Message parts of each interface kind (the section names of ``describe("interface")``).
_PARTS = {"action": ("Goal", "Result", "Feedback"), "srv": ("Request", "Response"), "msg": ()}

#: rosidl's names of basic types -> the ROS type text of ``.msg`` files.
_BASIC_NAMES = {"boolean": "bool", "octet": "byte", "float": "float32", "double": "float64",
                "long double": "float64"}
_INT_RANGES = {
    "int8": (-(2**7), 2**7 - 1), "uint8": (0, 2**8 - 1), "byte": (0, 2**8 - 1), "char": (0, 2**8 - 1),
    "int16": (-(2**15), 2**15 - 1), "uint16": (0, 2**16 - 1), "wchar": (0, 2**16 - 1),
    "int32": (-(2**31), 2**31 - 1), "uint32": (0, 2**32 - 1),
    "int64": (-(2**63), 2**63 - 1), "uint64": (0, 2**64 - 1),
}
#: Integer types whose values may not fit an Integer entry (7.4).
_WIDE_INTS = frozenset(("uint32", "int64", "uint64"))

_classes: dict[str, Any] = {}


# ============================================================================== type names
def type_name_problem(name: Any, kind: str | None = None) -> str | None:
    """Why ``name`` cannot be used as an interface type (of ``kind``: ``msg``, ``srv`` or
    ``action``), or None. Imports nothing."""
    if not isinstance(name, str) or not name.strip():
        return "no type is chosen"
    match = _TYPE_NAME.fullmatch(name.strip())
    if match is None:
        return f"{name!r} is not a ROS interface type name such as std_msgs/msg/Bool"
    package, group, interface = match.groups()
    if kind is not None and group != kind:
        return f"{name!r} is not a ROS {_KIND_TEXT[kind]} type (<package>/{kind}/<Name>)"
    try:
        content, _ = get_resource("rosidl_interfaces", package)
    except (LookupError, OSError, ValueError):
        return f"{package!r} is not an interface package installed on this host"
    listed = {line.strip().rsplit(".", 1)[0] for line in content.splitlines() if line.strip()}
    if f"{group}/{interface}" not in listed:
        return f"the interface package {package!r} has no {_KIND_TEXT[group]} {interface!r}"
    return None


def load_interface(name: str, kind: str) -> Any:
    """The interface class of ``name`` (``kind``: ``msg``, ``srv`` or ``action``), after the
    check of :func:`type_name_problem`.

    Raises:
        ValueError: the name is refused, or the class cannot be loaded; the message says why.
    """
    problem = type_name_problem(name, kind)
    if problem is not None:
        raise ValueError(problem)
    name = name.strip()
    cls = _classes.get(name)
    if cls is None:
        try:
            cls = _LOADERS[kind](name)
        except Exception as error:  # noqa: BLE001 - any import problem is reported the same way
            raise ValueError(f"the type {name} cannot be loaded: {error}") from error
        _classes[name] = cls
    return cls


def kind_of_type(name: str) -> str | None:
    """``msg``, ``srv`` or ``action`` for a well-formed type name, else None."""
    match = _TYPE_NAME.fullmatch(name.strip()) if isinstance(name, str) else None
    return match.group(2) if match else None


# ============================================================================== messages
def _fields(cls: Any) -> dict[str, str]:
    return cls.get_fields_and_field_types()


def _slots(cls: Any) -> dict[str, Any]:
    """``{field name: rosidl type}`` of a message class, in order."""
    return dict(zip(_fields(cls), cls.SLOT_TYPES, strict=True))


def _check_keys(cls: Any, values: Any, where: str) -> None:
    """Every key of ``values`` (recursively) names a field: a goal from a tree file must not set
    other attributes of a message."""
    if not isinstance(values, dict):
        raise ValueError(f"{where} must be a dictionary of field values, not {type(values).__name__}")
    slots = _slots(cls)
    for key, value in values.items():
        if key not in slots:
            raise ValueError(f"{cls.__name__} has no field {key!r}" + (f" ({where})" if where else ""))
        slot = slots[key]
        path = f"{where}.{key}" if where else key
        if isinstance(slot, idl.NamespacedType):
            if isinstance(value, dict):
                _check_keys(import_message_from_namespaced_type(slot), value, path)
        elif isinstance(slot, idl.AbstractNestedType) and isinstance(slot.value_type, idl.NamespacedType):
            if isinstance(value, (list, tuple)):
                item_cls = import_message_from_namespaced_type(slot.value_type)
                for index, item in enumerate(value):
                    _check_keys(item_cls, item, f"{path}[{index}]")


def build_message(cls: Any, values: Any, where: str = "") -> Any:
    """A message of ``cls`` with ``values`` (a nested dictionary) set by
    ``rosidl_runtime_py.set_message_fields``.

    Raises:
        ValueError: a key that is no field, or a value that does not fit; the message says why.
    """
    if values is None:
        values = {}
    _check_keys(cls, values, where)
    message = cls()
    try:
        set_message_fields(message, values)
    except (AttributeError, AssertionError, IndexError, KeyError, OverflowError, TypeError, ValueError) as error:
        prefix = f"{where}: " if where else ""
        raise ValueError(f"{prefix}{error}") from error
    return message


def to_python(value: Any) -> Any:
    """A message value as plain Python: messages -> dictionaries, arrays -> lists, a ``byte`` ->
    its number."""
    if isinstance(value, (bool, int, float, str)):
        return value
    fields = getattr(type(value), "get_fields_and_field_types", None)
    if fields is not None:
        return {name: to_python(getattr(value, name)) for name in fields()}
    if isinstance(value, (bytes, bytearray)):
        return value[0] if len(value) == 1 else list(value)
    if isinstance(value, (list, tuple, array.array)):
        return [to_python(item) for item in value]
    if hasattr(value, "tolist"):  # numpy arrays and scalars
        return to_python(value.tolist())
    return value


# ============================================================================== field paths
@dataclass(frozen=True)
class FieldInfo:
    """What a field path leads to: ``kind`` is ``bool``, ``integer``, ``float``, ``string``,
    ``nested`` (a message, also the whole one) or ``array``; ``ros_type`` its ROS type text."""

    kind: str
    ros_type: str

    @property
    def wide(self) -> bool:
        """An integer type whose values may not fit an Integer entry (``uint32``, 64-bit)."""
        return self.ros_type in _WIDE_INTS


def field_parts(path: Any) -> list[str | int]:
    """``"ranges[0]"`` -> ``["ranges", 0]``; empty -> ``[]`` (the whole message).

    Raises:
        ValueError: a malformed path.
    """
    if path is None or (isinstance(path, str) and not path.strip()):
        return []
    if not isinstance(path, str):
        raise ValueError(f"a field path is text, not {type(path).__name__}")
    return split_path(path.strip())


def _type_text(slot: Any) -> str:
    """The ROS type text of a rosidl type: ``float64``, ``string<=10``, ``float64[3]``,
    ``sequence<int32>``, ``sequence<int32, 5>``, ``geometry_msgs/msg/Pose``."""
    if isinstance(slot, idl.BasicType):
        return _BASIC_NAMES.get(slot.typename, slot.typename)
    if isinstance(slot, idl.NamespacedType):
        return "/".join((*slot.namespaces, slot.name))
    if isinstance(slot, idl.AbstractGenericString):
        base = "wstring" if isinstance(slot, idl.AbstractWString) else "string"
        maximum = getattr(slot, "maximum_size", None)
        return f"{base}<={maximum}" if maximum is not None else base
    if isinstance(slot, idl.Array):
        return f"{_type_text(slot.value_type)}[{slot.size}]"
    if isinstance(slot, idl.BoundedSequence):
        return f"sequence<{_type_text(slot.value_type)}, {slot.maximum_size}>"
    if isinstance(slot, idl.UnboundedSequence):
        return f"sequence<{_type_text(slot.value_type)}>"
    return str(slot)


def _info(slot: Any) -> FieldInfo:
    text = _type_text(slot)
    if isinstance(slot, idl.AbstractNestedType):
        return FieldInfo("array", text)
    if isinstance(slot, idl.NamespacedType):
        return FieldInfo("nested", text)
    if isinstance(slot, idl.AbstractGenericString):
        return FieldInfo("string", text)
    if text == "bool":
        return FieldInfo("bool", text)
    if text in _INT_RANGES:
        return FieldInfo("integer", text)
    return FieldInfo("float", text)


def resolve_field(cls: Any, path: Any) -> FieldInfo:
    """What ``path`` leads to in messages of ``cls`` (empty: the whole message).

    Raises:
        ValueError: a malformed path, or one the type does not have.
    """
    parts = field_parts(path)
    info = FieldInfo("nested", "")
    slot: Any = None
    current = cls
    for part in parts:
        if isinstance(part, int):
            if not isinstance(slot, idl.AbstractNestedType):
                raise ValueError(f"{path!r}: [{part}] needs an array")
            if isinstance(slot, idl.Array) and part >= slot.size:
                raise ValueError(f"{path!r}: the array has {slot.size} items")
            slot = slot.value_type
        else:
            if current is None:
                raise ValueError(f"{path!r}: {_type_text(slot)} has no field {part!r}")
            slots = _slots(current)
            if part not in slots:
                raise ValueError(f"{path!r}: {current.__name__} has no field {part!r}")
            slot = slots[part]
        info = _info(slot)
        current = import_message_from_namespaced_type(slot) if isinstance(slot, idl.NamespacedType) else None
    return info


def extract(message: Any, parts: Iterable[str | int]) -> Any:
    """The value at ``parts`` (from :func:`field_parts`, checked with :func:`resolve_field`) of
    ``message``, as plain Python.

    Raises:
        IndexError: the message has no item at an index of the path.
    """
    value = message
    for part in parts:
        if isinstance(part, int):
            if part >= len(value):
                raise IndexError(f"the array has {len(value)} items, not {part + 1}")
            value = value[part]
        else:
            if part not in _fields(type(value)):
                raise IndexError(f"{type(value).__name__} has no field {part!r}")
            value = getattr(value, part)
    return to_python(value)


# ============================================================================== entry types
def entry_types(info: FieldInfo) -> tuple[str, ...]:
    """The entry types a field may feed (7.4), the usual one first."""
    if info.kind == "bool":
        return ("Bool",)
    if info.kind == "integer":
        return ("Double", "Integer") if info.wide else ("Integer", "Double")
    if info.kind == "float":
        return ("Double",)
    if info.kind == "string":
        return ("String",)
    if info.kind == "array":
        return ("List",)
    return ("Dictionary",)


class SkipValue(ValueError):  # noqa: N818 - reads as "raise SkipValue(reason)"
    """A message value an entry cannot take (it does not fit 32 bits, or not the entry type)."""


def entry_value(value: Any, entry_type: str) -> Any:
    """``value`` (from :func:`extract`) as the value of an entry of ``entry_type``.

    Raises:
        SkipValue: it does not fit; the message says why.
    """
    if entry_type == "Integer" and isinstance(value, int) and not isinstance(value, bool) \
            and not INT_MIN <= value <= INT_MAX:
        raise SkipValue(f"{value} does not fit an Integer entry (32 bits)")
    try:
        return coerce_value(entry_type, value)
    except (TypeError, ValueError) as error:
        raise SkipValue(str(error)) from error


def entry_type_problem(info: FieldInfo, entry_type: str) -> str | None:
    """Why an entry of ``entry_type`` cannot take values of the field ``info``, or None."""
    allowed = entry_types(info)
    if entry_type in allowed:
        return None
    what = f"the {info.ros_type} field" if info.ros_type else "the whole message"
    return f"{what} needs a{'n' if allowed[0][0] in 'AEIOU' else ''} {' or '.join(allowed)} entry, not {entry_type}"


# ============================================================================== QoS
def adapted_qos(node: Node, topic: str) -> QoSProfile:
    """A subscription profile that matches the publishers of ``topic`` now (as ``ros2 topic
    echo`` chooses one): reliable only when every publisher is, transient local only when every
    publisher keeps its last message for late joiners; with no publisher, best effort and
    volatile, which every publisher can serve. History: the last message."""
    profile = QoSProfile(history=HistoryPolicy.KEEP_LAST, depth=1, reliability=ReliabilityPolicy.BEST_EFFORT,
                         durability=DurabilityPolicy.VOLATILE)
    publishers = node.get_publishers_info_by_topic(topic)
    if publishers:
        if all(info.qos_profile.reliability == ReliabilityPolicy.RELIABLE for info in publishers):
            profile.reliability = ReliabilityPolicy.RELIABLE
        if all(info.qos_profile.durability == DurabilityPolicy.TRANSIENT_LOCAL for info in publishers):
            profile.durability = DurabilityPolicy.TRANSIENT_LOCAL
    return profile


def topic_types(node: Node, topic: str) -> list[str]:
    """The types the ROS graph knows for ``topic`` now."""
    for name, types in node.get_topic_names_and_types():
        if name == topic:
            return list(types)
    return []


# ============================================================================== describe
def describe(node: Node, kind: str, arg: Any) -> dict:
    """Answer a ``~/describe`` question (see the module docs).

    Raises:
        ValueError: an unknown question, or one that cannot be answered; the message says why.
    """
    arg = arg if isinstance(arg, dict) else {}
    if kind == "graph":
        return _graph(node)
    if kind == "interface":
        return _interface(arg.get("type"))
    if kind == "sample":
        return _sample(node, arg)
    raise ValueError(f"unknown question {kind!r}")


def _hidden(name: str) -> bool:
    return any(part.startswith("_") for part in name.split("/") if part)


def _pairs(items: Iterable[tuple[str, list[str]]]) -> list[list[Any]]:
    return sorted([name, types[0] if len(types) == 1 else list(types)]
                  for name, types in items if not _hidden(name))


def _graph(node: Node) -> dict:
    return {
        "actions": _pairs(node.get_action_names_and_types()),
        "services": _pairs(node.get_service_names_and_types()),
        "topics": _pairs(node.get_topic_names_and_types()),
    }


def _interface(name: Any) -> dict:
    kind = kind_of_type(name) if isinstance(name, str) else None
    problem = type_name_problem(name)
    if problem is not None or kind is None:
        raise ValueError(problem or f"{name!r} is not a ROS interface type name")
    cls = load_interface(name, kind)
    if kind == "msg":
        return {"message": _struct(cls)}
    return {part.lower(): _struct(getattr(cls, part)) for part in _PARTS[kind]}


def _constants(cls: Any) -> dict[str, Any]:
    meta = type(cls)
    return dict(getattr(meta, f"_{meta.__name__}__constants", {}) or {})


def _struct(cls: Any) -> list[dict]:
    """The field descriptions ``F`` of a message class (contract 5.8 item 4)."""
    defaults = cls()
    slots = _slots(cls)
    constants = _constants(cls)
    matched: dict[str, list[dict]] = {}
    unmatched = []
    for const_name, value in constants.items():
        owner = next((field for field in slots if const_name.startswith(field.upper() + "_")), None)
        if owner is not None:
            matched.setdefault(owner, []).append({"name": const_name, "value": to_python(value)})
        else:
            unmatched.append({"name": const_name, "value": to_python(value)})
    integer_fields = [field for field, slot in slots.items() if _info(slot).kind == "integer"]
    if unmatched and len(integer_fields) == 1:  # the only integer field takes the other constants
        matched.setdefault(integer_fields[0], []).extend(unmatched)
    result = []
    for field, slot in slots.items():
        item = _describe_field(field, slot, getattr(defaults, field))
        if field in matched:
            item["constants"] = matched[field]
        result.append(item)
    return result


def _describe_field(name: str, slot: Any, default: Any) -> dict:
    info = _info(slot)
    item: dict[str, Any] = {"name": name, "kind": info.ros_type}
    if info.kind == "nested":
        item["fields"] = _struct(import_message_from_namespaced_type(slot))
        return item
    item["default"] = to_python(default)
    if info.kind == "integer":
        item["min"], item["max"] = _INT_RANGES[info.ros_type]
    return item


def _sample(node: Node, arg: dict) -> dict:
    topic = arg.get("topic")
    if not isinstance(topic, str) or not topic.strip():
        raise ValueError("no topic is chosen")
    topic = topic.strip()
    type_name = arg.get("type") if isinstance(arg.get("type"), str) and arg.get("type").strip() else None
    if type_name is None:
        types = topic_types(node, topic)
        if len(types) != 1:
            raise ValueError(f"the topic {topic} is not on the network now" if not types
                             else f"the topic {topic} has several types: {', '.join(types)}")
        type_name = types[0]
    cls = load_interface(type_name, "msg")
    parts = field_parts(arg.get("field"))
    info = resolve_field(cls, arg.get("field"))
    message, waited = _take_one(node, topic, cls, SAMPLE_TIMEOUT_S)
    value = extract(message, parts)
    entry_type = arg.get("entry_type")
    if not isinstance(entry_type, str) or not entry_type:
        allowed = entry_types(info)
        entry_type = "Integer" if info.wide and isinstance(value, int) and INT_MIN <= value <= INT_MAX \
            else allowed[0]
    problem = entry_type_problem(info, entry_type)
    if problem is not None:
        raise ValueError(problem)
    try:
        value = entry_value(value, entry_type)
    except SkipValue as error:
        raise ValueError(f"the message's value cannot be taken: {error}") from error
    return {"value": typed_to_wire(value, entry_type),
            "message": f"one message of {topic} ({type_name}), received after {waited:.2f} s"}


def _take_one(node: Node, topic: str, cls: Any, timeout: float) -> tuple[Any, float]:
    """One message of ``topic``, taken now by a short-lived node of the same context with its
    own executor (it shares the node's discovery, so it matches the publishers at once).

    Raises:
        ValueError: none arrived within ``timeout``.
    """
    received: list[Any] = []
    helper = Node(f"bt_sample_{time.monotonic_ns()}", context=node.context, start_parameter_services=False,
                  enable_rosout=False)
    executor = SingleThreadedExecutor(context=node.context)
    try:
        executor.add_node(helper)
        helper.create_subscription(cls, topic, received.append, adapted_qos(node, topic))
        start = time.monotonic()
        while not received:
            remaining = start + timeout - time.monotonic()
            if remaining <= 0:
                raise ValueError(f"no message arrived on {topic} within {timeout:g} s")
            executor.spin_once(timeout_sec=min(remaining, 0.1))
        return received[0], time.monotonic() - start
    finally:
        executor.shutdown(timeout_sec=0)
        helper.destroy_node()
