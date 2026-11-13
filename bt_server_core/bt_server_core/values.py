"""Blackboard entry types and values: checking, converting, comparing, parsing, and the
three encodings a value has (in the files, on the browser wire, as Python literal text).

Shared by every part of the server. Ported from ``behavior_tree_widget`` (``blackboard/values.py``,
``blackboard_nodes.py`` and ``serialization.py``) and extended with the operators of the
Evaluation node (design 6.3) and the wire forms of design 10.3.

No ROS or web imports: the standard library only.
"""

from __future__ import annotations

import ast
import copy
import json
import math
import reprlib
from dataclasses import dataclass
from typing import Any, TypeGuard

__all__ = [
    "CONTAINER_TYPES",
    "DISPLAY_LIMIT",
    "ENTRY_TYPES",
    "FIELD_KINDS",
    "INT_MAX",
    "INT_MIN",
    "INVALID_NAME_CHARACTERS",
    "LITERAL_MAX_CHARS",
    "LITERAL_MAX_DEPTH",
    "MAX_NESTING",
    "OPERATORS",
    "ORDER_OPERATORS",
    "EntryType",
    "NotOrderable",
    "ValueFileError",
    "canonical_type",
    "coerce_value",
    "compare",
    "convert_literal",
    "decode_value",
    "default_value",
    "dumps_json",
    "encode_value",
    "entry_name_problem",
    "equal_values",
    "eval_equal",
    "field_from_wire",
    "field_kind_of",
    "field_to_wire",
    "from_wire",
    "infer_type",
    "is_encodable",
    "is_finite_number",
    "literal_eval_extended",
    "literal_text",
    "loads_json",
    "parse_double_text",
    "parse_literal",
    "safe_repr",
    "safe_str",
    "to_wire",
    "typed_from_wire",
    "typed_to_wire",
]

INT_MIN = -(2**31)
INT_MAX = 2**31 - 1
#: Whole numbers a JavaScript number holds exactly.
JS_SAFE_INT = 2**53

#: Display texts (read-only Object values, previews) are cut to this many characters.
DISPLAY_LIMIT = 500


@dataclass(frozen=True)
class EntryType:
    name: str
    default: Any
    user_creatable: bool = True
    container: bool = False
    example: str = ""


ENTRY_TYPES: dict[str, EntryType] = {
    "Integer": EntryType("Integer", 0),
    "Double": EntryType("Double", 0.0),
    "String": EntryType("String", ""),
    "Bool": EntryType("Bool", False),
    "List": EntryType("List", [], container=True, example="[1, 2.5, 'text']"),
    "Dictionary": EntryType("Dictionary", {}, container=True, example="{'key': 'value', 'n': 3}"),
    "Set": EntryType("Set", set(), container=True, example="{1, 2, 3}"),
    "Object": EntryType("Object", None, user_creatable=False),
}
CONTAINER_TYPES = frozenset(name for name, entry in ENTRY_TYPES.items() if entry.container)

TYPE_ALIASES = {
    "int": "Integer", "integer": "Integer",
    "double": "Double", "float": "Double",
    "str": "String", "string": "String",
    "bool": "Bool", "boolean": "Bool",
    "list": "List",
    "dict": "Dictionary", "dictionary": "Dictionary",
    "set": "Set",
    "object": "Object",
}

#: Characters an entry name cannot contain (design 7.1).
INVALID_NAME_CHARACTERS = "./"

#: The Evaluation node's operators (design 6.3). The first is the default.
OPERATORS = ("==", "!=", "<", "<=", ">", ">=")
ORDER_OPERATORS = frozenset(("<", "<=", ">", ">="))

#: Limits on literal text typed in a browser (design 9.3).
LITERAL_MAX_CHARS = 1_000_000
LITERAL_MAX_DEPTH = 100

#: Containers inside containers in the files, counted the same way when writing and reading.
MAX_NESTING = 256

#: Field kinds of node types (design 6.4): the widget's five; ``entry``, the name of a
#: blackboard entry or ``None`` (Evaluation and Set ``CompareTo``, Print Message ``Source``),
#: sent as it is; and ``other`` for values of any other type (shown read-only, or edited as a
#: Python literal, such as a ROS goal dictionary).
FIELD_KINDS = ("int", "float", "str", "bool", "choice", "entry", "other")


class NotOrderable(ValueError):
    """``<``, ``<=``, ``>`` or ``>=`` on values that are not numbers."""


class ValueFileError(ValueError):
    """A value in a file is malformed (a broken tag, nested too deeply)."""


# --------------------------------------------------------------------------------------------
# Types


def canonical_type(type_name: object) -> str:
    """The canonical entry type name for ``type_name`` (case-insensitive, aliases allowed).

    Raises:
        ValueError: not an entry type.
    """
    if isinstance(type_name, str) and type_name in ENTRY_TYPES:
        return type_name
    canonical = TYPE_ALIASES.get(str(type_name).strip().lower())
    if canonical is None:
        raise ValueError(f"unknown blackboard entry type {type_name!r}; expected one of {', '.join(ENTRY_TYPES)}")
    return canonical


def default_value(type_name: str) -> Any:
    """A fresh default value of an entry type."""
    return copy.deepcopy(ENTRY_TYPES[type_name].default)


def infer_type(value: Any) -> str:
    """The entry type an entry created for ``value`` gets."""
    if isinstance(value, bool):
        return "Bool"
    if isinstance(value, int):
        return "Integer" if INT_MIN <= value <= INT_MAX else "Object"
    if isinstance(value, float):
        return "Double"
    if isinstance(value, str):
        return "String"
    if isinstance(value, (list, tuple)):
        return "List"
    if isinstance(value, dict):
        return "Dictionary"
    if isinstance(value, (set, frozenset)):
        return "Set"
    return "Object"


def _deepcopy_container(type_name: str, value: Any) -> Any:
    try:
        return copy.deepcopy(value)
    except Exception as error:
        raise TypeError(f"{type_name} entries must hold copyable values ({error}); use an Object entry") from error


def coerce_value(type_name: str, value: Any) -> Any:
    """Check ``value`` for an entry of ``type_name`` and return the value to store.

    A whole float fits an Integer, an int fits a Double; nothing else is converted.

    Raises:
        TypeError: the value has an incompatible type.
        ValueError: the value is out of range (an Integer outside 32 bits).
    """
    if type_name == "Integer":
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"Integer entries need an int, not {type(value).__name__}")
        value = int(value)
        if not INT_MIN <= value <= INT_MAX:
            raise ValueError(f"{value} is outside the Integer entry range [{INT_MIN}, {INT_MAX}]")
        return value
    if type_name == "Double":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"Double entries need a float, not {type(value).__name__}")
        try:
            return float(value)
        except OverflowError as error:
            raise ValueError(f"{value} is too large for a Double entry") from error
    if type_name == "String":
        if not isinstance(value, str):
            raise TypeError(f"String entries need a str, not {type(value).__name__}")
        return value
    if type_name == "Bool":
        if not isinstance(value, bool):
            raise TypeError(f"Bool entries need a bool, not {type(value).__name__}")
        return value
    if type_name == "List":
        if not isinstance(value, (list, tuple)):
            raise TypeError(f"List entries need a list, not {type(value).__name__}")
        return _deepcopy_container(type_name, list(value))
    if type_name == "Dictionary":
        if not isinstance(value, dict):
            raise TypeError(f"Dictionary entries need a dict, not {type(value).__name__}")
        return _deepcopy_container(type_name, value)
    if type_name == "Set":
        if isinstance(value, dict) and not value:
            return set()
        if not isinstance(value, (set, frozenset, list, tuple)):
            raise TypeError(f"Set entries need a set, not {type(value).__name__}")
        items = _deepcopy_container(type_name, list(value))
        try:
            return set(items)
        except TypeError as error:
            raise TypeError(f"Set entries can only hold hashable values: {error}") from error
    if type_name == "Object":
        return value
    raise ValueError(f"unknown blackboard entry type {type_name!r}")


def parse_double_text(text: str) -> float:
    """A Double typed as text: a number with ``.`` or ``,`` as the decimal point, or ``nan``,
    ``inf``, ``-inf`` (also ``infinity``), in any case (design 11.4).

    Raises:
        ValueError: not a number.
    """
    cleaned = text.strip().replace(",", ".")
    lowered = cleaned.lower()
    if lowered in ("nan", "+nan", "-nan"):
        return math.nan
    if lowered in ("inf", "+inf", "infinity", "+infinity"):
        return math.inf
    if lowered in ("-inf", "-infinity"):
        return -math.inf
    if not cleaned or cleaned.count(".") > 1:
        raise ValueError(f"{text!r} is not a number")
    try:
        return float(cleaned)
    except ValueError as error:
        raise ValueError(f"{text!r} is not a number") from error


def _parse_number(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return parse_double_text(value)
    except ValueError:
        return None


def convert_literal(value: Any, type_name: str) -> Any:
    """``value`` as a literal for an entry of ``type_name`` (the widget's rule for the literal of
    an Evaluation or Set node when the type of its entry changes).

    Numbers, booleans and number text are converted between Integer, Double and Bool, anything
    becomes text for String, and a value that cannot be converted gives the type's default.
    Object (and unknown) types keep the value as it is.
    """
    if type_name == "Integer":
        if isinstance(value, int):  # also bool
            return min(max(int(value), INT_MIN), INT_MAX)
        number = value if isinstance(value, float) else _parse_number(value)
        if number is None or not math.isfinite(number):
            return 0
        return min(max(round(number), INT_MIN), INT_MAX)
    if type_name == "Double":
        if isinstance(value, (int, float)):  # also bool
            try:
                return float(value)
            except OverflowError:
                return 0.0
        number = _parse_number(value)
        return 0.0 if number is None else number
    if type_name == "String":
        if isinstance(value, str):
            return value
        if value is None:
            return ""
        return str(value) if isinstance(value, (bool, int, float)) else literal_text(value)
    if type_name == "Bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes", "on")
        return False
    if type_name in CONTAINER_TYPES:
        try:
            return coerce_value(type_name, value)
        except (TypeError, ValueError):
            pass
        if isinstance(value, str):
            try:
                return parse_literal(value, type_name)
            except ValueError:
                pass
        return default_value(type_name)
    return value


def entry_name_problem(name: object) -> str | None:
    """Why ``name`` cannot name an entry, or None if it can (design 7.1)."""
    if not isinstance(name, str):
        return "an entry name must be text"
    if not name:
        return "an entry name cannot be empty"
    if name != name.strip():
        return "an entry name cannot start or end with spaces"
    bad = [character for character in INVALID_NAME_CHARACTERS if character in name]
    if bad:
        return f"an entry name cannot contain {' or '.join(repr(c) for c in bad)}"
    return None


def is_finite_number(value: Any) -> TypeGuard[int | float]:
    """True for ints and floats (not bools) with a finite value."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except OverflowError:
        return False


# --------------------------------------------------------------------------------------------
# Comparing


def equal_values(a: Any, b: Any) -> bool:
    """Whether storing ``b`` over ``a`` changes nothing: the same types at every level of a
    container and the same scalars (``nan`` equals ``nan``; ``-0.0`` differs from ``0.0``;
    ``[1, 0]`` differs from ``[True, False]``; dicts compared in order). Used to tell a changed
    value from a repeated one."""
    try:
        return _equal_values(a, b)
    except RecursionError:
        return False


def _same_scalar(a: Any, b: Any) -> bool:
    if type(a) is not type(b):
        return False
    if isinstance(a, float):
        if math.isnan(a) or math.isnan(b):
            return math.isnan(a) and math.isnan(b)
        return a == b and math.copysign(1.0, a) == math.copysign(1.0, b)
    try:
        return bool(a == b)
    except Exception:  # noqa: BLE001
        return False


def _equal_values(a: Any, b: Any) -> bool:
    if type(a) is not type(b):
        return False
    if isinstance(a, (list, tuple)):
        return len(a) == len(b) and all(_equal_values(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, dict):
        return len(a) == len(b) and all(
            _equal_values(ka, kb) and _equal_values(va, vb)
            for (ka, va), (kb, vb) in zip(a.items(), b.items(), strict=True)
        )
    if isinstance(a, (set, frozenset)):
        try:
            return len(a) == len(b) and {(type(x), x) for x in a} == {(type(y), y) for y in b}
        except Exception:  # noqa: BLE001
            return False
    return _same_scalar(a, b)


def eval_equal(a: Any, b: Any) -> bool:
    """``==`` of the Evaluation node (the widget's rule): numbers compare by value
    (``1 == 1.0``), but a Bool only equals a Bool, also inside lists, tuples, sets and dict
    values (``[True] != [1]``). ``nan`` equals nothing, not even ``nan``."""
    try:
        return _eval_equal(a, b)
    except Exception:  # noqa: BLE001 - a failing __eq__, or nested too deeply
        return False


def _eval_equal(a: Any, b: Any) -> bool:
    if isinstance(a, bool) is not isinstance(b, bool):
        return False
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return type(a) is type(b) and len(a) == len(b) and all(_eval_equal(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_eval_equal(value, b[key]) for key, value in a.items())
    if isinstance(a, (set, frozenset)) and isinstance(b, (set, frozenset)):
        return a == b and {x for x in a if isinstance(x, bool)} == {y for y in b if isinstance(y, bool)}
    return bool(a == b)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def compare(operator: str, a: Any, b: Any) -> bool:
    """``a <operator> b`` with the rules of design 6.3.

    ``==`` and ``!=`` work for every type (:func:`eval_equal`). The order operators work for
    numbers only (Integer and Double); a comparison with ``nan`` is false, except ``!=``.

    Raises:
        NotOrderable: an order operator on a value that is not a number.
        ValueError: an unknown operator.
    """
    if operator == "==":
        return eval_equal(a, b)
    if operator == "!=":
        return not eval_equal(a, b)
    if operator not in ORDER_OPERATORS:
        raise ValueError(f"unknown operator {operator!r}")
    if not (_is_number(a) and _is_number(b)):
        raise NotOrderable("cannot be ordered")
    if operator == "<":
        return a < b
    if operator == "<=":
        return a <= b
    if operator == ">":
        return a > b
    return a >= b


# --------------------------------------------------------------------------------------------
# Python literal text


class _NonFiniteNames(ast.NodeTransformer):
    """Accept ``nan`` / ``inf`` (as written by ``repr``) inside container literals."""

    NAMES = {"nan": float("nan"), "inf": float("inf"), "infinity": float("inf")}

    def visit_Name(self, node: ast.Name) -> ast.AST:  # noqa: N802 - ast API
        value = self.NAMES.get(node.id.lower())
        if value is None:
            return node
        return ast.copy_location(ast.Constant(value=value), node)


def _depth(node: ast.AST) -> int:
    deepest = 0
    stack = [(node, 0)]
    while stack:
        current, level = stack.pop()
        if isinstance(current, (ast.List, ast.Tuple, ast.Set, ast.Dict)):
            level += 1
            deepest = max(deepest, level)
        stack.extend((child, level) for child in ast.iter_child_nodes(current))
    return deepest


def literal_eval_extended(text: str) -> Any:
    """``ast.literal_eval`` that also accepts ``nan``, ``inf`` and ``-inf``."""
    tree = ast.parse(text.strip(), mode="eval")
    tree = ast.fix_missing_locations(_NonFiniteNames().visit(tree))
    return ast.literal_eval(tree)


def parse_literal(text: str, type_name: str | None = None) -> Any:
    """Parse Python literal text typed in a browser (design 9.3): refused when longer than
    1 MB or nested deeper than 100 levels; evaluated with ``ast.literal_eval``, which evaluates
    literals only. With ``type_name`` the value must fit that entry type (an empty text gives
    its default).

    Raises:
        ValueError: the text is refused or is not a literal (of that type); the message says why.
    """
    if not isinstance(text, str):
        raise ValueError("a literal must be text")
    if len(text) > LITERAL_MAX_CHARS:
        raise ValueError(f"the text is longer than {LITERAL_MAX_CHARS // 1_000_000} MB")
    stripped = text.strip()
    if type_name is not None and not stripped:
        return default_value(type_name)
    try:
        tree = ast.parse(stripped, mode="eval")
    except (SyntaxError, ValueError, MemoryError, RecursionError) as error:
        raise ValueError(_literal_error(type_name, f"not a Python literal ({error.__class__.__name__})")) from error
    if _depth(tree) > LITERAL_MAX_DEPTH:
        raise ValueError(f"the value is nested deeper than {LITERAL_MAX_DEPTH} levels")
    try:
        tree = ast.fix_missing_locations(_NonFiniteNames().visit(tree))
        value = ast.literal_eval(tree)
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError) as error:
        raise ValueError(_literal_error(type_name, "not a Python literal")) from error
    if type_name is None:
        return value
    try:
        return coerce_value(type_name, value)
    except (TypeError, ValueError) as error:
        raise ValueError(_literal_error(type_name, str(error))) from error


def _literal_error(type_name: str | None, reason: str) -> str:
    example = ENTRY_TYPES[type_name].example if type_name in ENTRY_TYPES else ""
    return f"{reason}; enter a Python literal such as {example}" if example else reason


def literal_text(value: Any) -> str:
    """Python literal text of a value ('' if it has none)."""
    try:
        return repr(value)
    except Exception:  # noqa: BLE001
        return ""


def _shorten(text: str) -> str:
    return text if len(text) <= DISPLAY_LIMIT else text[: DISPLAY_LIMIT - 3] + "..."


_bounded_repr = reprlib.Repr()
_bounded_repr.maxstring = DISPLAY_LIMIT
_bounded_repr.maxother = DISPLAY_LIMIT
_bounded_repr.maxlong = DISPLAY_LIMIT
for _name in ("maxlist", "maxtuple", "maxset", "maxfrozenset", "maxdeque", "maxarray", "maxdict"):
    setattr(_bounded_repr, _name, 100)
_bounded_repr.maxlevel = 10


def safe_repr(value: Any) -> str:
    """A repr for display: bounded in length and cost, and never raising."""
    try:
        if isinstance(value, (bytes, bytearray, memoryview)) and len(value) > DISPLAY_LIMIT:
            head = bytes(value[: DISPLAY_LIMIT // 4])
            return _shorten(f"{head!r}... ({len(value)} bytes)")
        if isinstance(value, (list, tuple, set, frozenset, dict)) and len(value) <= 100:
            return _shorten(repr(value))
        return _shorten(_bounded_repr.repr(value))
    except Exception:  # noqa: BLE001 - a broken __repr__
        return f"<{type(value).__name__} object>"


def safe_str(value: Any) -> str:
    """``str(value)`` that never raises (falls back to a bounded repr)."""
    try:
        return _shorten(str(value))
    except Exception:  # noqa: BLE001
        return safe_repr(value)


# --------------------------------------------------------------------------------------------
# Files: JSON with tags for what JSON lacks (the widget's encoding)

_TAG_TUPLE = "__tuple__"
_TAG_SET = "__set__"
_TAG_FROZENSET = "__frozenset__"
_TAG_DICT = "__dict__"
_TAGS = frozenset((_TAG_TUPLE, _TAG_SET, _TAG_FROZENSET, _TAG_DICT))


def encode_value(value: Any) -> Any:
    """A JSON-compatible structure for ``value``, as the widget writes it: ``None``, bools,
    ints, floats (``nan`` and ``inf`` stay floats; :func:`dumps_json` writes them as ``NaN`` and
    ``Infinity``) and strings as they are; lists as arrays; dicts with string keys as objects;
    tuples, sets, frozensets and other dicts as single-key tagged objects.

    Raises:
        TypeError: an unsupported type, a circular reference, nesting deeper than
            :data:`MAX_NESTING`, or an integer too long to write.
    """
    return _encode(value, set(), 0)


def _encode(value: Any, active: set[int], depth: int) -> Any:
    if isinstance(value, int) and not isinstance(value, bool):
        try:
            int.__repr__(value)
        except ValueError as error:
            raise TypeError("integers this large cannot be saved to JSON") from error
        return value
    if value is None or isinstance(value, (bool, float, str)):
        return value
    if not isinstance(value, (list, tuple, set, frozenset, dict)):
        raise TypeError(f"values of type {type(value).__name__!r} cannot be saved to JSON")
    if depth >= MAX_NESTING:
        raise TypeError("the value is nested too deeply to be saved to JSON")
    marker = id(value)
    if marker in active:
        raise TypeError("values containing a circular reference cannot be saved to JSON")
    active.add(marker)
    depth += 1
    try:
        if isinstance(value, list):
            return [_encode(item, active, depth) for item in value]
        if isinstance(value, tuple):
            return {_TAG_TUPLE: [_encode(item, active, depth) for item in value]}
        if isinstance(value, (set, frozenset)):
            items = [_encode(item, active, depth) for item in value]
            items.sort(key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=True))
            tag = _TAG_FROZENSET if isinstance(value, frozenset) else _TAG_SET
            return {tag: items}
        if all(isinstance(key, str) for key in value) and not (_TAGS & value.keys()):
            return {key: _encode(item, active, depth) for key, item in value.items()}
        return {_TAG_DICT: [[_encode(key, active, depth), _encode(item, active, depth)] for key, item in value.items()]}
    except RecursionError as error:
        raise TypeError("the value is nested too deeply to be saved to JSON") from error
    finally:
        active.discard(marker)


def _hashable(value: Any) -> Any:
    if isinstance(value, set):
        return frozenset([_hashable(item) for item in value])
    if isinstance(value, tuple):
        return tuple([_hashable(item) for item in value])
    return value


def decode_value(data: Any) -> Any:
    """Inverse of :func:`encode_value`.

    Raises:
        ValueFileError: a tagged value is malformed, or the value is nested too deeply.
    """
    try:
        return _decode(data, 0)
    except RecursionError as error:
        raise ValueFileError("value is nested too deeply") from error


def _decode(data: Any, depth: int) -> Any:
    if not isinstance(data, (list, dict)):
        return data
    if depth >= MAX_NESTING:
        raise ValueFileError("value is nested too deeply")
    depth += 1
    if isinstance(data, list):
        return [_decode(item, depth) for item in data]
    if len(data) == 1:
        ((tag, payload),) = data.items()
        if tag in _TAGS and isinstance(payload, list):
            try:
                if tag == _TAG_TUPLE:
                    return tuple([_decode(item, depth) for item in payload])
                if tag == _TAG_SET:
                    return {_hashable(_decode(item, depth)) for item in payload}
                if tag == _TAG_FROZENSET:
                    return frozenset([_hashable(_decode(item, depth)) for item in payload])
                result = {}
                for entry in payload:
                    if not (isinstance(entry, list) and len(entry) == 2):
                        raise ValueFileError(f"malformed {tag} value: expected [key, value] pairs, got {entry!r}")
                    result[_hashable(_decode(entry[0], depth))] = _decode(entry[1], depth)
                return result
            except TypeError as error:
                raise ValueFileError(f"malformed {tag} value: {error}") from error
    return {key: _decode(item, depth) for key, item in data.items()}


def is_encodable(value: Any) -> bool:
    """True if ``value`` can be written by :func:`encode_value`."""
    try:
        encode_value(value)
    except TypeError:
        return False
    return True


def dumps_json(data: Any, *, indent: int | None = 2) -> str:
    """JSON text as the widget writes its files: ``NaN`` and ``Infinity`` allowed, UTF-8 text
    kept as it is. ``data`` must already be encoded (:func:`encode_value`)."""
    return json.dumps(data, indent=indent, ensure_ascii=False, allow_nan=True)


def loads_json(text: str | bytes) -> Any:
    """Parse JSON text that may hold ``NaN``, ``Infinity`` and ``-Infinity``."""
    return json.loads(text)


# --------------------------------------------------------------------------------------------
# The browser wire (design 10.3)
#
# A JavaScript number cannot tell 1.0 from 1, and a browser's JSON parser refuses NaN. So:
#   Integer          -> a JSON number (text beyond +-2**53)
#   Double           -> its text: "1.0", "nan", "inf", "-inf"
#   String, Bool     -> as it is
#   List, Dictionary, Set -> Python literal text: "[1, 2.5, 'a']"
#   Object           -> its bounded repr, read-only (never decoded)


def to_wire(type_name: str, value: Any) -> Any:
    """The browser form of a value of an entry type."""
    if type_name == "Integer":
        if isinstance(value, int) and not isinstance(value, bool) and abs(value) > JS_SAFE_INT:
            return str(value)
        return value
    if type_name == "Double":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return repr(float(value))
        return safe_repr(value)
    if type_name in ("String", "Bool"):
        return value
    if type_name in CONTAINER_TYPES:
        return literal_text(value)
    return safe_repr(value)


def from_wire(type_name: str, wire: Any) -> Any:
    """The value of an entry type from its browser form, checked for the type.

    Raises:
        ValueError: the wire value does not give a value of that type; the message says why.
    """
    if type_name == "Object":
        raise ValueError("Object entries cannot be changed from a browser")
    if type_name == "Integer":
        if isinstance(wire, str):
            try:
                wire = int(wire.strip())
            except ValueError as error:
                raise ValueError(f"{wire!r} is not a whole number") from error
    elif type_name == "Double":
        if isinstance(wire, str):
            wire = parse_double_text(wire)
    elif type_name in CONTAINER_TYPES:
        if not isinstance(wire, str):
            raise ValueError(f"{type_name} values are sent as Python literal text")
        return parse_literal(wire, type_name)
    try:
        return coerce_value(type_name, wire)
    except (TypeError, ValueError) as error:
        raise ValueError(str(error)) from error


def typed_to_wire(value: Any, type_name: str | None = None) -> dict:
    """``{"type": <entry type>, "value": <wire>}``: the form of a value whose type is not
    fixed by a field declaration, such as the literal of an Evaluation or Set node."""
    if type_name is None:
        type_name = "Object" if value is None else infer_type(value)
    if value is None:
        return {"type": type_name, "value": None}
    return {"type": type_name, "value": to_wire(type_name, value)}


def typed_from_wire(data: Any) -> Any:
    """Inverse of :func:`typed_to_wire`. ``None`` and ``{"value": null}`` give ``None``.

    Raises:
        ValueError: malformed, or the value does not fit its type.
    """
    if data is None:
        return None
    if not isinstance(data, dict) or "type" not in data:
        raise ValueError("a typed value is {'type': ..., 'value': ...}")
    if data.get("value") is None:
        return None
    return from_wire(canonical_type(data["type"]), data["value"])


def field_kind_of(default: Any) -> str:
    """The field kind of a field declared with ``default`` (the widget's ``_fields`` rule)."""
    if isinstance(default, bool):
        return "bool"
    if isinstance(default, int):
        return "int"
    if isinstance(default, float):
        return "float"
    if isinstance(default, str):
        return "str"
    if isinstance(default, list) and default and all(isinstance(item, str) for item in default):
        return "choice"
    return "other"


def field_to_wire(kind: str, value: Any) -> Any:
    """The browser form of a field value of a field kind."""
    if kind == "float":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return repr(float(value))
        return value
    if kind == "int":
        if isinstance(value, int) and not isinstance(value, bool) and abs(value) > JS_SAFE_INT:
            return str(value)
        return value
    if kind in ("str", "bool", "choice", "entry"):
        return value
    if value is None:
        return None
    return literal_text(value)


def field_from_wire(kind: str, wire: Any) -> Any:
    """A field value from its browser form.

    Raises:
        ValueError: the value does not fit the field kind.
    """
    if kind == "float":
        if isinstance(wire, str):
            return parse_double_text(wire)
        if isinstance(wire, (int, float)) and not isinstance(wire, bool):
            return float(wire)
        raise ValueError("a float field needs a number")
    if kind == "int":
        if isinstance(wire, str):
            try:
                wire = int(wire.strip())
            except ValueError as error:
                raise ValueError(f"{wire!r} is not a whole number") from error
        if isinstance(wire, float) and wire.is_integer():
            wire = int(wire)
        if isinstance(wire, bool) or not isinstance(wire, int):
            raise ValueError("an integer field needs a whole number")
        if not INT_MIN <= wire <= INT_MAX:
            raise ValueError(f"{wire} is outside the 32-bit range")
        return wire
    if kind == "str":
        if not isinstance(wire, str):
            raise ValueError("a text field needs text")
        return wire
    if kind == "bool":
        if not isinstance(wire, bool):
            raise ValueError("a check box field needs true or false")
        return wire
    if kind == "entry":
        if wire is None or wire == "":
            return None
        if not isinstance(wire, str):
            raise ValueError("an entry field needs the name of an entry, or null")
        return wire
    if kind == "choice":
        if not (isinstance(wire, list) and wire and all(isinstance(item, str) for item in wire)):
            raise ValueError("a choice field needs a list of texts")
        return list(wire)
    if wire is None:
        return None
    if not isinstance(wire, str):
        raise ValueError("this field is sent as Python literal text")
    return parse_literal(wire)
