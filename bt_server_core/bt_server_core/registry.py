"""The node type registry of an executor (design 6.4, 6.6, 7.5).

It knows the classes of the built-in leaves that run without ROS (:mod:`.builtin_leaves`), the
ROS leaves ``bt_server_executor`` adds (:meth:`Registry.register_ros_leaf`), and the custom leaf
classes of the modules named in ``leaf_modules``.

API::

    registry = Registry(["my_robot.leaves"])      # imports the modules; raises RegistryError
    registry.register(MyLeaf)                     # one class; returns its type name
    registry.register_ros_leaf("CallAction", CallActionLeaf)   # the executor's hook
    registry.leaf_class("Move To")                # the class of a type, or None
    registry.catalog()                            # the catalog sent to browsers (~/describe)
    registry.run_setup()                          # setup(blackboard) hooks: Object entries
    registry.objects                              # {name: object}, added to every run's blackboard
    registry.missing_reason("CallAction")         # why a type without a class fails when run

Registering a module registers every leaf class it defines (subclasses of
:class:`~bt_server_core.leaves.Leaf`, ``compat.LeafNodeWidget`` included, defined in that module and
not marked ``ABSTRACT = True`` in their own class body). A type name must be unique and must not
be the name of a built-in node type (the widget let applications take ``Notes``, ``UDP Message``,
``Sub-Tree``, ``Group`` and ``Print Message``; the server does not).

A module may define ``setup(blackboard)``: :meth:`Registry.run_setup` calls it once, when the
executor starts, with a blackboard of its own; the Object entries it creates there (device
handles and the like, design 7.5) are added to the blackboard of every run, where they win over
an entry of the same name.
"""

from __future__ import annotations

import importlib
import inspect
import logging
from collections.abc import Callable, Iterable
from concurrent.futures import Future
from typing import Any

from .blackboard import Blackboard
from .builtin_leaves import BUILTIN_LEAF_CLASSES
from .compat import LeafNodeWidget
from .leaves import Leaf
from .node_types import BUILTIN_TYPES, RESERVED_TYPE_NAMES, ROS_TYPES, spec

__all__ = ["Registry", "RegistryError"]

log = logging.getLogger("bt_server_core")


class RegistryError(ValueError):
    """A leaf module or class cannot be registered; the message says why."""


class Registry:
    """The node types of one executor (see the module docs)."""

    def __init__(self, leaf_modules: Iterable[str] = ()) -> None:
        self._custom: dict[str, type[Leaf]] = {}
        self._ros: dict[str, type[Leaf]] = {}
        self._setup_hooks: list[tuple[str, Callable[[Blackboard], Any]]] = []
        self.objects: dict[str, Any] = {}
        #: Calls of thread leaves still running, by instance path: a node does not start again
        #: until its earlier call returned, also in the next run (design 5.6, 6.6).
        self.outstanding_calls: dict[str, Future] = {}
        self.warnings: list[str] = []
        for name in leaf_modules:
            self.load_module(name)

    # ----------------------------------------------------------------- registering
    def load_module(self, module_name: str) -> list[str]:
        """Import ``module_name`` and register every leaf class it defines; returns their names.

        Raises:
            RegistryError: the module cannot be imported, or a class cannot be registered.
        """
        try:
            module = importlib.import_module(module_name)
        except Exception as error:  # noqa: BLE001 - any import error is reported the same way
            raise RegistryError(f"the leaf module {module_name!r} cannot be imported: {error}") from error
        names = []
        for value in list(vars(module).values()):
            if (
                inspect.isclass(value)
                and issubclass(value, Leaf)
                and value.__module__ == module.__name__
                and not value.__dict__.get("ABSTRACT", False)
            ):
                names.append(self.register(value))
        setup = getattr(module, "setup", None)
        if callable(setup):
            self._setup_hooks.append((module_name, setup))
        return names

    def register(self, cls: type) -> str:
        """Register a leaf class; returns its type name.

        Raises:
            RegistryError: not a leaf class, an abstract one, a built-in name, or a name taken.
        """
        if not inspect.isclass(cls) or not issubclass(cls, Leaf):
            raise RegistryError(f"leaf types are subclasses of Leaf or LeafNodeWidget, not {cls!r}")
        if cls.__dict__.get("ABSTRACT", False):
            raise RegistryError(f"{cls.__name__} is abstract; register a subclass")
        name = cls.type_name()
        if not isinstance(name, str) or not name.strip():
            raise RegistryError(f"{cls.__name__}.TYPE_NAME must be a non-empty string")
        if name in RESERVED_TYPE_NAMES:
            raise RegistryError(f"{name!r} is the name of a built-in node type; give {cls.__qualname__} "
                                "another TYPE_NAME")
        existing = self._custom.get(name)
        if existing is not None and existing is not cls:
            raise RegistryError(f"the node type name {name!r} is already registered to "
                                f"{existing.__module__}.{existing.__qualname__}; give {cls.__qualname__} "
                                "another TYPE_NAME")
        if issubclass(cls, LeafNodeWidget) and cls.UI_FILE:
            warning = f"{name}: UI_FILE is not supported; the browser builds the node from its fields"
            self.warnings.append(warning)
            log.warning("%s", warning)
        self._custom[name] = cls
        return name

    def register_ros_leaf(self, type_name: str, cls: type) -> None:
        """Give a ROS node type (``CallAction``, ``CallService``, ``Publish``,
        ``RemoteSubTree``) its class. Used by ``bt_server_executor``.

        Raises:
            RegistryError: not a ROS node type, or not a leaf class.
        """
        if type_name not in ROS_TYPES:
            raise RegistryError(f"{type_name!r} is not a ROS node type")
        if not inspect.isclass(cls) or not issubclass(cls, Leaf):
            raise RegistryError(f"{cls!r} is not a leaf class")
        self._ros[type_name] = cls

    # ----------------------------------------------------------------- looking up
    def leaf_class(self, type_name: str) -> type[Leaf] | None:
        """The class that runs leaves of ``type_name``, or None (unknown, or ROS without ROS)."""
        return BUILTIN_LEAF_CLASSES.get(type_name) or self._ros.get(type_name) or self._custom.get(type_name)

    def custom_types(self) -> dict[str, type[Leaf]]:
        """The custom leaf classes, by type name."""
        return dict(self._custom)

    def knows(self, type_name: str) -> bool:
        """True for a built-in type (ROS ones included) or a registered custom type."""
        return spec(type_name) is not None or type_name in self._custom

    def missing_reason(self, type_name: str) -> str:
        """Why a node of ``type_name`` without a class fails when executed."""
        node_type = spec(type_name)
        if node_type is not None and node_type.needs_ros:
            return f"{node_type.title} needs ROS: it runs only in an executor under ROS"
        return f"unknown node type {type_name!r} (no leaf module defines it here)"

    def catalog(self) -> list[dict]:
        """The catalog of node types (contract 5.1): the built-in types, ROS types only when
        their classes are registered, then the custom ones (``builtin: false``, category
        ``Custom``) sorted by title."""
        result = [item.to_catalog() for item in BUILTIN_TYPES if not item.needs_ros or item.type in self._ros]
        for name, cls in sorted(self._custom.items(), key=lambda pair: (pair[1].default_title().lower(), pair[0])):
            kind = cls.leaf_kind()
            doc = inspect.getdoc(cls) or ""
            result.append({
                "type": name,
                "title": cls.default_title(),
                "kind": "leaf",
                "category": "Custom",
                "fields": [item.to_catalog() for item in cls.field_specs()],
                "leaf_kind": kind,
                "special": None,
                "waits_for_cancels": kind != "instant" and cls.waits_for_cancels(),
                "needs_ros": False,
                "builtin": False,
                "doc": doc.splitlines()[0] if doc else "",
            })
        return result

    # ----------------------------------------------------------------- setup hooks
    def run_setup(self, blackboard: Blackboard | None = None) -> list[str]:
        """Call the ``setup(blackboard)`` hook of every leaf module and keep the Object entries
        they create in :attr:`objects`. Returns the problems (a hook that raised, entries that
        are not Object entries)."""
        board = blackboard if blackboard is not None else Blackboard()
        problems = []
        for module_name, hook in self._setup_hooks:
            try:
                hook(board)
            except Exception as error:  # noqa: BLE001 - a hook's error is reported, not fatal
                problems.append(f"setup() of {module_name!r} raised {type(error).__name__}: {error}")
        for name, type_name in board.types().items():
            if type_name == "Object":
                self.objects[name] = board.get(name)
            else:
                problems.append(f"setup() created the {type_name} entry {name!r}; only Object entries are "
                                "added to the runs")
        for problem in problems:
            log.warning("%s", problem)
        return problems
