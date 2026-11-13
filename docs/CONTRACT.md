# Implementation contract

The design is `docs/BehaviorTreeServer.md` ("the design"; section numbers below are its own).
This file fixes what the design leaves open and what several parts must agree on: file
locations, module ownership, data shapes, the browser protocol. Where this file and the design
disagree, this file wins; say so in your report.

The widget whose files, behaviour and semantics the server keeps is the Python package in
`C:\repos\BehaviorTreeWidget\src\behavior_tree_widget` (docs in `C:\repos\BehaviorTreeWidget\docs`,
its tests in `C:\repos\BehaviorTreeWidget\tests`). Port behaviour from there; never import it.

## 0. Working rules

* **Local working copy:** `C:\repos\BehaviorTreeServer` (git repo, branch `master`). Edit only
  here, with the file tools. Python 3.14 venv: `C:\repos\BehaviorTreeServer\.venv`
  (`aiohttp`, `pytest`, `pytest-aiohttp`, `pytest-asyncio`, `py_trees`). Run tests from the repo
  root: `.venv/Scripts/python -m pytest bt_server_core/test -q`.
* **Remote machine:** `reed@MOTHER-BRAIN.local`, Ubuntu 26.04, Python 3.14.4, ROS 2 Lyrical in
  `/opt/ros/lyrical`. The project is at `~/projects/BehaviorTreeServer`, a mirror of the local
  copy, with its own venv `.venv` (`--system-site-packages`, so rclpy is visible once
  `/opt/ros/lyrical/setup.bash` is sourced).
  * Run a remote command (Git Bash): `"$SP/r.sh" '<command>'` where
    `SP=/c/Users/reed/AppData/Local/Temp/claude/3477146f2a7347f4/d4afd8f0-ccaf-4237-af86-644979f30309/scratchpad`.
  * Copy local files to the remote: `"$SP/sync.sh" [paths relative to the repo root]`. It never
    deletes remote files; delete them yourself with `r.sh` if you remove a file.
  * `sudo` on the remote: `sudo <command>` with the account's password (not written down here). Only the orchestrator
    installs system packages. If you need one, say so in your report (or install it and list
    it in your report: name, why).
* **ROS isolation on the remote:** tests and experiments set `ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST`
  and a `ROS_DOMAIN_ID` of their own (each agent is given one). Domain 0 is reserved for the
  final systemd service.
* **Ownership:** write only the files your brief gives you. You may read everything. If you
  need a change in a file someone else owns, work around it and say so in your report.
* **Never commit** with git; the orchestrator commits.
* **Style:** Python 3.14, type hints, docstrings that say what and why, no dead code, standard
  library unless the design names a package. Names and texts as the design gives them.
* **Report:** at the end, a short list: what is done, what is not (and why), the test command
  and its result line, any contract change you had to make.

## 1. Repository layout and ownership

```
BehaviorTreeServer/
├── docs/                      design, this contract
├── pytest.ini                 runs every package's tests from the root (pythonpath set)
├── bt_server_core/            ament_python; no ROS, no web imports
│   └── bt_server_core/
│       ├── values.py          [done] entry types, comparison, literal parsing, file and wire encodings
│       ├── node_types.py      [done] built-in node type declarations (names, fields, defaults)
│       ├── files.py           [core-model] names, paths, atomic writes, reading/writing tree and blackboard files
│       ├── document.py        [core-model] TreeDocument: load checks, edit operations, versions, child order
│       ├── bundle.py          [core-model] bundles (4.3), instance paths, structural validation
│       ├── promote.py         [core-model] Promote to Sub-Tree (the widget's rules)
│       ├── blackboard.py      [core-engine] Blackboard store: ids, types, Resets, restore, sources' freshness, bb.* ops
│       ├── engine.py          [core-engine] TreeRunner, node behaviours, Parallel, the cancelling lifecycle
│       ├── scheduler.py       [core-engine] Scheduler protocol, AsyncioScheduler, FakeScheduler (tests)
│       ├── leaves.py          [core-engine] Leaf, InstantLeaf, AsyncLeaf, ThreadLeaf, LeafContext
│       ├── builtin_leaves.py  [core-engine] Evaluation, Set, Print Message, UDP Message, Restart, Pause, Stop
│       ├── compat.py          [core-engine] LeafNodeWidget (6.6)
│       ├── registry.py        [core-engine] node type registry, leaf_modules import, catalog
│       ├── treelog.py         [core-engine] log levels, line format, the per-run log file writer
│       └── demo_leaves.py     [core-engine] example custom leaves (the widget's demo.py ported)
├── bt_server_interfaces/      [executor] CMake; Appendix A
├── bt_server_executor/        [executor] rclpy: node, ROS leaves, sources, journal, run_tree
├── bt_server_gateway/         [gateway] aiohttp; bt_server_gateway/static/ holds the built page
├── bt_server_bringup/         [orchestrator] launch, config, systemd units
├── web/                       [web] Svelte 5 + TypeScript + Vite; builds into bt_server_gateway/bt_server_gateway/static
└── install.sh, run.sh, setup_system_service.sh   [orchestrator]
```

Tests live in `<package>/test/test_*.py`. Name each Appendix B case in the test name
(`test_E_24_...`) so the cases can be counted. A case that needs ROS is marked
`@pytest.mark.ros` and skipped when `rclpy` cannot be imported.

## 2. Conventions every part shares

### 2.1 Files (design 12)

* Tree file: the widget's format (see the widget's `docs/file-formats.md`), with
  `"version": 1` or `2`. New keys, written only when not at their defaults:
  * node `"type": "Parallel"` with `"policy": "all" | "any"` (no `memory`);
  * Evaluation fields `"Operator"` (text, one of `== != < <= > >=`), `"Wait"` (bool),
    `"WaitTimeout"` (float seconds);
  * on any leaf node: `"bindings": {field path: entry name}`, `"outputs": {source path: entry name}`,
    `"wait_for_cancels": bool`, `"cancel_timeout_ms": int`;
  * `config`: `"wait_for_cancels"` (default true), `"cancel_timeout_ms"` (5000),
    `"on_unconfirmed_cancel"` (`"stop"` | `"continue"`).
* The new node types use the names in `node_types.py`: `CallAction`, `CallService`,
  `Publish`, `RemoteSubTree`, `Parallel`.
* A field path is the field name, then `.`-separated names inside a dictionary field
  (`Goal.pose.pose`); a list index is written `[0]` (`Goal.waypoints[0]`). Output source
  paths start with `Result.`, `Feedback.` (Call Action), `Response.` (Call Service); for a
  Remote Sub-Tree, `bindings` and `outputs` are remote entry name → local entry name.
* Blackboard file: the widget's, plus `"source": {"topic", "type", "field", "max_rate_hz",
  "max_age_s"}` on an entry (version 2 then).
* **Document key:** a tree is named by its path relative to `trees_dir`, POSIX separators,
  without `_BT.json`: `robot`, `cells/patrol`. Bundle keys (4.3) are document keys.
* A Sub-Tree's `File` is relative to the folder of the tree that holds it (the widget's rule),
  `\` read as `/`.

### 2.2 Runs

* **Node index and instance path.** A run's nodes are those reachable from the Root through
  connections, Sub-Tree nodes included and expanded. They are numbered in depth-first
  pre-order, children in connection order; for a Sub-Tree node, its own index comes first,
  then the sub-tree's Root and its nodes. An instance path is the ids of the Sub-Tree nodes
  leading to the node, then its own id, joined with `/`: `9a1d…` or `b912…/4f0c…`.
  `RunInfo.node_paths[i]` is the path of index `i`. Notes, Groups and unconnected nodes have
  no index.
* **Status bytes:** 0 Ready, 1 Running, 2 Succeeded, 3 Failed, 4 Cancelling.
  **States:** 0 IDLE, 1 RUNNING, 2 PAUSED, 3 STOPPING, 4 RESTARTING.
  **Outcomes:** 0 none, 1 succeeded, 2 failed, 3 stopped, 4 error.
* **Log levels:** 0 None, 1 ERROR, 2 WARNING, 3 DEBUG, 4 INFO (the widget's).
* **Details kinds** (`NodeDetails`, `nd`): `error` (failure reason text), `waiting` (text),
  `feedback` (dict), `field` (`{"tree", "id", "field", "value"}`, file encoding),
  `title` (`{"tree", "id", "title"}`), `remote_run` (`{"executor", "run_id"}`). A detail
  with value `null` is cleared. Details of a node are cleared when the node starts anew.
* **Blackboard entries** carry an `"id"` (32 hex digits) in every form except the files. The
  gateway gives ids; the executor keeps them; an entry without one gets a new one.

### 2.3 Values (`bt_server_core.values`, done)

Use it everywhere; do not reimplement. Files: `encode_value`, `decode_value`, `dumps_json`,
`loads_json`. Browser: `to_wire`/`from_wire` (entry values), `field_to_wire`/`field_from_wire`
(field values by kind), `typed_to_wire`/`typed_from_wire` (`{"type", "value"}`, for the
literal of Evaluation and Set and for `entry_value`). Evaluation: `compare`, `eval_equal`,
`NotOrderable`. Change detection: `equal_values`. Typed literals: `parse_literal`.

## 3. Core APIs (design 5.8 is the base)

Owners write these; consumers rely on them. Signatures may grow keyword arguments.

```python
# scheduler.py
class Cancelable(Protocol):
    def cancel(self) -> None: ...
class Scheduler(Protocol):                       # design 5.8
    def now(self) -> float: ...                  # steady clock, s
    def ros_now(self) -> float: ...              # ROS clock; == now() without ROS
    def call_soon(self, fn: Callable[[], None]) -> None: ...      # safe from any thread
    def call_at(self, when: float, fn: Callable[[], None], ros: bool = False) -> Cancelable: ...
class AsyncioScheduler: ...                      # for LocalLink; built on a running loop
class FakeScheduler: ...                         # tests: advance(seconds) runs due callbacks in time order

# engine.py
class State(IntEnum): IDLE, RUNNING, PAUSED, STOPPING, RESTARTING
class Status(IntEnum): READY, RUNNING, SUCCEEDED, FAILED, CANCELLING
class Outcome(IntEnum): NONE, SUCCEEDED, FAILED, STOPPED, ERROR
class BundleError(ValueError): ...               # the bundle cannot be read: reject the goal (R-13)
class CommandRefused(Exception): ...             # a command the state refuses (4.4 table)
@dataclass
class RunResult:
    outcome: Outcome; message: str; loops_completed: int; unconfirmed_cancels: int
    blackboard: list[dict]                       # final entries, file form plus "id"
    stopped_by_command: bool                     # Stop button / goal cancel (vs. a Stop node or watchdog)
class Sink(Protocol):                            # design 5.8; called on the engine thread
    def state(self, state: State, loop: int) -> None: ...
    def statuses(self, changed: Iterable[int]) -> None: ...
    def detail(self, index: int, kind: str, value: object) -> None: ...
    def blackboard(self, changes: dict) -> None: ...   # {"entries": [...], "removed": [ids], "order": [ids] | None}
    def log(self, level: int, text: str, index: int = -1) -> None: ...
class TreeRunner:
    def __init__(self, bundle: dict, blackboard: Blackboard, registry: Registry, scheduler: Scheduler,
                 sink: Sink, on_finished: Callable[[RunResult], None], *, run_context: object | None = None,
                 max_tick_rate_hz: float = 200.0): ...   # raises BundleError
    node_paths: list[str]; statuses: array.array   # 'B', indexed like node_paths
    state: State; loop_count: int; cancelling: int; unsettled: int
    def details(self) -> dict[int, dict[str, object]]: ...
    def start(self) -> None: ...
    def pause(self) -> None: ...; def resume(self) -> None: ...
    def stop(self) -> None: ...; def reset(self) -> None: ...       # may raise CommandRefused
    def request_tick(self) -> None: ...          # merged, rate-capped; from the engine thread
    def apply(self, ops: list[dict]) -> None: ... # bb.* ops, node.set_field (tree, id), doc.set_config; raises OpError
```

`run_context` carries what ROS leaves need (the rclpy node, the executor's name, the run id);
the engine hands it to leaves through `LeafContext.run`. The executor and `LocalLink` both
drive a `TreeRunner`; nothing else runs trees.

`Blackboard`, the leaf base classes and the registry follow design 6.6 and 7; their owner
documents the exact API in the module docstrings. `bb.*` ops (shapes in 5.3 below, values in
file encoding when they come from the executor's `~/apply`, in wire encoding from browsers:
the gateway converts) are applied by `Blackboard.apply_op`, used by the executor during a run
and by the gateway while idle, so the rules (names, types, sources, renames that keep ids)
are one piece of code.

## 4. Decisions on the open questions (design 20.1)

1. **Canvas:** our own canvas layer (one transformed world layer, positioned HTML nodes, SVG
   connections), not Svelte Flow.
2. One session per executor, as designed. 3. No logins. 4. No undo. 5. Watchdog default 5 s.
6. Two Parallel policies. 7. Sources only during a run. 8. The widget is not changed.
9. JSON in typed envelopes, as designed. 10. Both names are published: `bt.<hostname>.local`
   (the design's) and `bt-<hostname>.local`, because Windows 11 does not resolve the three-part
   name (checked, `docs/VERIFIED.md`). The gateway's default `public_names` is
   `["bt.{hostname}.local", "bt-{hostname}.local", "{hostname}.local"]`; it also answers to
   its address.

## 5. The browser protocol (design 10)

One WebSocket per tab at `/ws`, JSON text messages, each an object with `t`. `cid` is the
browser's number for a request; every request with a `cid` is answered by exactly one `ok`
or `no` with that `cid`. Protocol number: **1**.

### 5.1 Wire forms

* **Document** (a tree file in memory):
  `{"key": "robot", "path": "robot_BT.json", "rev": 12, "version": 1, "unsaved": false,
    "config": {...}, "view": {"zoom": 1.0, "center": [x, y]}, "nodes": [<node>], "connections": [{"parent", "child"}],
    "problems": ["text", ...]}`
  `connections` in execution order per parent.
* **Node:** as in the file, plus: field values in wire form by the kind of their declared
  field (`values.field_to_wire`); `LiteralValue` of Evaluation and Set as a typed value
  `{"type": <entry type>, "value": <wire>}` (the type of the `ValueName` entry, or of the value
  itself when that entry does not exist); `"unknown": true` for a type the catalog lacks
  (placeholder; all its data kept; `fields` then raw JSON). Optional keys absent when unset.
* **Entry:** `{"id", "name", "type", "value": <wire>, "reset": bool, "source": {...} | null,
  "freshness": "ok" | "nodata" | "stale" | null, "age": seconds | null, "truncated": bool,
  "problem": text | null}`. `problem` says why a source cannot feed the entry (QoS
  incompatible with the publishers, wrong type, missing field — design 7.4); the executor
  publishes it on its entries (`"problem"` key, absent when none) and the gateway passes it on.
  A value whose wire text is longer than 64 KB is sent as its first 64 KB with
  `"truncated": true`; `ask entry_value` returns all of it. Object entries are read-only.
* **Catalog entry:** `NodeTypeSpec.to_catalog()` of `node_types.py`; custom leaves have
  `"builtin": false`, `"category": "Custom"`.
* **Finding:** `{"path": <instance path or "tree:<key>/<node id>">, "severity": "error" | "warning", "text"}`.

### 5.2 Browser → gateway

| Message | Answer |
|---|---|
| `{"t":"hello","protocol":1}` | `welcome`, or `reload` |
| `{"t":"attach","executor":name}` | `snapshot` |
| `{"t":"op","cid","doc":key,"rev":n,"op":{...}}` | `ok {cid, rev}` (new revision) and the `op` broadcast; or `no {cid, reason}` |
| `{"t":"cmd","cid","name":"execute"\|"pause"\|"resume"\|"stop"\|"reset","confirm"?:bool}` | `ok` / `no {reason, findings?}` |
| `{"t":"file","cid","action":...,...}` | see 5.4 |
| `{"t":"ask","cid","kind":...,...}` | see 5.5 |
| `{"t":"watch","executor":name}` / `{"t":"unwatch"}` | `run`, `st`, `nd` of that executor, tagged `exec` |
| `{"t":"resync","doc":key}` | `doc {event: "resync", document}` |

### 5.3 Operations (`op.op`) — design 9.3

Node operations (`doc` names the document):

```
{"op":"node.add","node":<node, wire form, id made by the browser: 32 hex digits>, "order"?: {parent: [child ids]}}
{"op":"node.remove","ids":[...]}                       # their connections go too; the Root refuses the whole op
{"op":"node.move","positions":{id:[x,y],...},"orders"?:{parent:[child ids],...}}
{"op":"node.set_title","id","title"}
{"op":"node.set_field","id","field","value":<wire>, "path"?: "pose.position.x", "kind"?: "float"}
     # path: a value inside a dictionary field; kind then says how to decode the value
{"op":"node.set_selection","id","field","index"}
{"op":"node.set_binding","id","key":<field path>,"entry":<name>|null}
{"op":"node.set_output","id","key":<source path>,"entry":<name>|null}
{"op":"node.set_option","id","name":"wait_for_cancels"|"cancel_timeout_ms","value":<value>|null}
{"op":"node.resize","id","width","height","x"?,"y"?}   # Notes and Groups
{"op":"node.set_text","id","text"}                      # Notes
{"op":"node.set_color","id","color":"#rrggbb"}          # Groups
{"op":"composite.set_type","id","composite":"Sequence"|"Selector"|"Parallel"}  # Root: Sequence|Selector only
{"op":"composite.set_memory","id","memory":bool}
{"op":"composite.set_policy","id","policy":"all"|"any"}
{"op":"conn.add","parent","child","order"?:[child ids of parent]}
{"op":"conn.remove","parent","child"}
{"op":"paste","nodes":[<node>],"connections":[{"parent","child"}]}   # ids new, positions placed by the browser
{"op":"group.promote","id":<group id>,"name":<new tree name>}
{"op":"doc.set_config","config":{partial config}}
```

Blackboard operations (`doc` is `"@blackboard"`, `rev` ignored; their effect comes back as
`bb` messages, not as an `op` broadcast):

```
{"op":"bb.add","entry":{"id","name","type","value":<wire>,"reset":bool}}
{"op":"bb.remove","ids":[...]}
{"op":"bb.rename","id","name"}
{"op":"bb.retype","id","type"}                 # the value is converted as the widget does
{"op":"bb.set_value","id","value":<wire>}
{"op":"bb.set_reset","id","reset":bool}
{"op":"bb.move","order":[all ids in the new order]}
{"op":"bb.set_source","id","source":{...}|null}
```

The `op` broadcast: `{"t":"op","doc","rev","op":<as applied, normalised>,"by":<client id or "executor">,"cid"}`.
A `node.move` whose `orders` were dropped (stale `rev`) is answered `ok` with
`"reorder": [parent ids]`: that browser works the order out again and sends it.

### 5.4 File actions (`t: file`)

Paths are tree names relative to `trees_dir` (`robot`, `cells/patrol`; any of the four
spellings of the widget is accepted).

```
list                      -> ok {"trees": [{"key","dirs","mtime","open_in": executor|null}], "dirs": [...]}
new  {name, choice?, replace?}   load {name, choice?}
save {doc, view?}         -> ok {"warnings": [...]}         # the document shown; the root tree also writes its blackboard
save_as {doc, name, replace?, view?}
rename {name, new_name}   delete {name}
zoom_in {doc, node, path} -> ok {"doc": key, "document": <document>}   # opens the Sub-Tree's (or Remote Sub-Tree's) file
go_back {doc}             -> ok                                        # this browser no longer shows doc
revert {doc}              recover {doc, accept: bool}
```

A request that would lose unsaved changes, replace a file, or needs a missing sub-tree file is
answered `no {cid, reason, "question": {"kind": "unsaved", "docs": [keys]} | {"kind": "replace", "name"} |
{"kind": "choose_file", "node", "file"}}`; sent again with `choice: "save" | "discard"`,
`replace: true` or, for `zoom_in`, `file: <name>` it goes ahead.

### 5.5 Questions (`t: ask`)

```
graph                              -> ok {"graph": {"actions": [[name, type]], "services": [...], "topics": [...]}}
interface {type}                   -> ok {"interface": <field tree>}       # executor's Describe "interface"
sample {topic, type, field}        -> ok {"value": <typed>, "message": text}
parse_literal {type, text}         -> ok {"value": <wire>}  /  no {reason}
entry_value {id}                   -> ok {"value": <wire, whole>}
log_before {before: seq, count}    -> ok {"first": seq, "lines": [...]}
executors                          -> ok {"executors": [...]}
```

### 5.6 Gateway → browser

```
{"t":"welcome","protocol":1,"client":<id>,"executors":[{"name","online":bool,"state":n}],"time":<epoch s>,"local":bool}
{"t":"reload"}
{"t":"snapshot","exec":name,"client":<id>,"root":<root doc key>|null,"docs":{key:<document>},
     "blackboard":[<entry>],"catalog":[<catalog entry>],"online":bool,"starting":bool,
     "run":<run>|null,"st":<st body>,"details":{index:{kind:value}},"log":{"first":seq,"lines":[...]},
     "recovery":[keys],"attached":n,"findings":[...]}
{"t":"doc","doc":key,"event":"opened"|"saved"|"reverted"|"closed"|"resync"|"renamed","document":<document>|null}
{"t":"catalog","exec","types":[...]}
{"t":"op",...}  {"t":"ok","cid",...}  {"t":"no","cid","reason",...}
{"t":"st","exec","run_id":hex|null,"state","loop","cancelling","unsettled","outcome","message","v":"2210400"}
{"t":"bb","exec","entries":[<entry>],"removed":[ids],"order":[ids]|null}
{"t":"nd","exec","details":{index:{kind:value|null}}}
{"t":"log","exec","first":seq,"lines":[[time_text, level, text],...],"skipped"?:n}
{"t":"run","exec","event":"started"|"ended","run_id","label","requested_by","node_paths":[...],
     "own":bool,"bundle":<bundle>|null,"outcome"?,"message"?}
{"t":"notice","level":"info"|"warning"|"error","text"}
{"t":"presence","attached":n}
```

`time_text` is the log file's time column (`2026-11-13 15:04:07.414`). Log sequence numbers
count from 0 per run. `run.bundle` is sent for a watched executor and for runs the session did
not start.

### 5.8 Settled by the page (binding for the gateway and the executor)

1. `node.set_field` of `LiteralValue` sends the typed form `{"type", "value"}`.
2. `paste` (and the clipboard text) carries nodes in **wire form**, marked `"encoding": "wire"`
   on the op, plus `"sizes": {id: [w, h]}` used for placement; clipboard text in tree-file form
   (from another page or older data) is converted by the page with the catalog first.
3. After New, Load or Save As of the root tree the page sends `attach` again and takes the new
   `snapshot`; the gateway may also push one.
4. `ask interface {type}` answers `{"interface": {"goal": [F], "result": [F], "feedback": [F]}}`
   for an action, `{"request": [F], "response": [F]}` for a service, `{"message": [F]}` for a
   message (section keys case-insensitive; a section may also be `{"fields": [F]}`). `F` is
   `{"name", "kind", "default"?, "fields"?: [F], "constants"?: [{"name","value"}]}`; `kind`
   is the ROS type text (`bool`, `int32`, `uint8`, `float64`, `string`, `sequence<float64>`,
   `geometry_msgs/msg/Pose`, `float64[3]` …); a nested message has `fields`.
5. `ask graph` items are `[name, type]`; `type` may be a list of types.
6. Pinned inspector fields live in each browser's localStorage (no op carries them).
7. After `watch {executor}` the gateway sends that executor's current `run` (with `bundle`)
   and `st`, then its updates; the watched run's node paths count from its own Root.
8. The page clears its log pane when a `run` `started` arrives or log numbers restart at 0.
9. `file zoom_in` carries `path` = the instance path of the Sub-Tree node zoomed through.
10. Only the first `welcome` of a connection leads to an `attach`; later ones update the
    executor list.
11. Cancelling is drawn violet (`#7b4fc4`) with a dashed border.

## 6. The executor link (design 3.3, 9.1) and the processes

`bt_server_gateway/bt_server_gateway/link.py` [gateway] defines the protocol and data classes;
`local_link.py` [gateway] and `ros_link.py` [executor] implement it. All methods run on the
gateway's asyncio loop; listener callbacks are called on that loop.

```python
@dataclass
class LinkRun:            # RunInfo
    run_id: str | None    # 32 hex digits; None: nothing loaded
    label: str; requested_by: str; bundle: dict | None; node_paths: list[str]
@dataclass
class LinkStatus:         # TreeStatus
    run_id: str | None; seq: int; state: int; loop: int; cancelling: int; unsettled: int
    outcome: int; message: str; statuses: bytes
@dataclass
class LinkRecord:         # LogRecord, numbered
    seq: int; stamp: float; level: int; node_index: int; text: str
@dataclass
class LinkResult:         # the ExecuteTree result
    run_id: str; outcome: int; message: str; loops_completed: int; unconfirmed_cancels: int
    blackboard: list[dict]                 # file encoding, decoded (values are Python values), each with "id"
    goal_status: str                       # "succeeded" | "aborted" | "canceled"
class GoalRejected(Exception): ...         # str(e) is the reason (busy: names who started the run)
class CommandRefused(Exception): ...
class LinkDown(Exception): ...             # the executor is not reachable
class RunListener(Protocol):
    def on_online(self, online: bool) -> None: ...
    def on_run(self, run: LinkRun) -> None: ...
    def on_status(self, status: LinkStatus) -> None: ...
    def on_blackboard(self, run_id: str, seq: int, full: bool, update: dict) -> None: ...   # values decoded
    def on_details(self, run_id: str, seq: int, details: list[dict]) -> None: ...
    def on_log(self, run_id: str, records: list[LinkRecord]) -> None: ...
    def on_result(self, result: LinkResult) -> None: ...
    def on_catalog(self, types: list[dict]) -> None: ...
class ExecutorLink(Protocol):
    name: str                                         # "/cell1/bt_executor"; LocalLink: "local"
    online: bool
    async def start(self) -> None: ...
    async def close(self) -> None: ...
    async def describe(self, kind: str, arg: dict) -> dict: ...         # node_types | graph | interface | sample
    async def validate(self, bundle: dict, blackboard: list[dict]) -> list[dict]: ...
    async def execute(self, bundle: dict, blackboard: list[dict], label: str, requested_by: str) -> str: ...
        # returns the run id once the goal is accepted; raises GoalRejected
    async def stop(self, run_id: str) -> None: ...   # through ~/execute/_action/cancel_goal, by id (4.5)
    async def pause(self, paused: bool) -> None: ...  # raises CommandRefused
    async def reset(self) -> None: ...
    async def apply(self, ops: list[dict]) -> tuple[int, str]: ...      # (APPLIED 0 | NO_RUN 1 | REJECTED 2, message)
    async def snapshot(self) -> dict: ...  # {"run": LinkRun, "status": LinkStatus, "blackboard_seq", "blackboard": [...],
                                           #  "details_seq", "details": [...], "log_seq"}
    async def get_log(self, run_id: str | None, from_seq: int, max_records: int) -> list[LinkRecord]: ...
    def subscribe(self, listener: RunListener) -> None: ...

class LinkFactory(Protocol):     # one per gateway; makes the links and finds executors (3.4)
    async def start(self, on_executors: Callable[[list[str]], None]) -> None: ...
        # on_executors(names) whenever the set of executors found on the network changes
    def link(self, name: str) -> ExecutorLink: ...   # the same object for the same name
    async def close(self) -> None: ...
```

`local_link.LocalLinkFactory(settings)` offers one executor, `"local"`. `ros_link.RosLinkFactory(settings)`
spins one rclpy node (`bt_gateway`) on a second thread, discovers `ExecuteTree` action servers
(`get_action_names_and_types`; the executor name is the action name without `/execute`) and
hands every ROS callback to asyncio with `loop.call_soon_threadsafe`.

Ops sent through `apply` carry values already decoded to Python values except that, over
ROS, `ops_json` uses the file encoding (`encode_value` + `dumps_json`); `RosLink` and the
executor convert. `blackboard` lists are entries in file form with `"id"`.

**Processes and entry points** (the systemd units and `run.sh` use exactly these):

* Executor: `python -m bt_server_executor [--ros-args ...]` and the console script
  `executor` (`ros2 run bt_server_executor executor`). Parameters of design 8.1.
  `ros2 run bt_server_executor run_tree <tree file> [--executor /cell1/bt_executor]`.
* Gateway: `python -m bt_server_gateway --config <gateway.toml>` and the console script
  `gateway`. Settings of design 9.1, plus `local_log_dir` (LocalLink's tree logs; default
  `<trees_dir>/.logs`), `leaf_modules` (LocalLink), `state_dir` (catalog cache, session
  memory; default `<trees_dir>/.gateway`), `ros_domain_id` is not a setting (environment).
  `Host` and `Origin` checks compare host names case-insensitively (browsers send them in
  lower case: `bt.mother-brain.local`).

## 7. HTTP (section 5.7 in earlier copies)

`GET /` (index.html, `Cache-Control: no-cache`), `GET /assets/<hashed>` (one year),
`GET /ws`, `POST /upload` (multipart: `file` fields, `dir`; 10 MB), `GET /download/<tree key>`
(a zip of the tree's `_BT.json` and `_BB.json`), `GET /download-log/<executor>/<run id>`.
Every request passes the `Host` check; `POST /upload` and the WebSocket also the `Origin` check
(design 13).
