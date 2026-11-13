# Behavior Tree Server

A design for offering what `behavior-tree-widget` does as a web application. One machine on a
LAN hosts it, any browser on that LAN draws, runs and watches behavior trees, and the running
tree coordinates work on several machines through ROS 2.

**Status:** a design proposal, last revised on 2026-11-13. Nothing in it is built or measured.
It starts from first principles and reuses none of the widget's implementation (Qt, py_trees),
but it keeps the widget's features, execution semantics and file format;
[section 19](#19-feature-map-and-differences-from-the-widget) maps every feature and lists what
is new.

**Assumed platform:** the server is an Ubuntu 26.04 machine running ROS 2 Lyrical Luth: a
long-term release supported until May 2031, Tier 1 on Ubuntu 26.04 for amd64 and arm64, with
Fast DDS as its default middleware. Avahi is installed. Platform facts this design relies on
that could not be confirmed while writing are listed in
[section 20.2](#202-to-verify-on-the-server).

## Contents

1. [Goals and scope](#1-goals-and-scope)
2. [What decides the design](#2-what-decides-the-design)
3. [Architecture](#3-architecture)
4. [The tree API](#4-the-tree-api)
5. [The engine](#5-the-engine)
6. [Leaf nodes](#6-leaf-nodes)
7. [The blackboard](#7-the-blackboard)
8. [The executor node](#8-the-executor-node)
9. [The gateway](#9-the-gateway)
10. [The browser protocol](#10-the-browser-protocol)
11. [The browser application](#11-the-browser-application)
12. [Files and compatibility](#12-files-and-compatibility)
13. [Security](#13-security)
14. [Deployment](#14-deployment)
15. [Performance](#15-performance)
16. [Failure handling](#16-failure-handling)
17. [Testing](#17-testing)
18. [Build order](#18-build-order)
19. [Feature map and differences from the widget](#19-feature-map-and-differences-from-the-widget)
20. [Open questions and things to verify](#20-open-questions-and-things-to-verify)

[Appendix A: interface definitions](#appendix-a-interface-definitions) ·
[Appendix B: test cases](#appendix-b-test-cases) ·
[References](#references)

---

## 1. Goals and scope

**Goals**

1. Everything the widget does, in a browser: the canvas, every built-in node, custom leaf nodes,
   the blackboard, execution with live statuses, the log, and the tree files.
2. The tree runs on the server and keeps running when no browser is connected.
3. Leaves act on other machines through ROS 2, with defined behaviour when a machine or the
   network fails.
4. Machines can work at the same time (the Parallel node), and a machine can run a tree of its
   own (several executors, the Remote Sub-Tree node).
5. The tree can see the state of other machines: ROS topics feed blackboard entries, and the
   Evaluation node can compare and wait.
6. The browsing machines need no ROS installation and do not join the robot network. They open
   `http://bt.<hostname>.local`, where `<hostname>` is the server's host name
   ([section 14.3](#143-the-address)).
7. Trees and blackboards stay in the widget's two JSON files, so trees move between the widget
   and the server.
8. Leaf nodes stay Python. Leaf classes written for the widget keep working with small changes.

**Not goals**

* **A safety system.** Stop travels from a browser through two processes and a network. Each
  machine must stop by itself when it loses its commander ([section 16](#16-failure-handling)).
* **Fast control loops.** A tree that must re-check conditions 100 times a second inside one
  robot belongs in BehaviorTree.CPP. This design coordinates machines at one message per action.
* **User accounts.** There are no logins. Everyone who can open the page can use all of it
  ([section 13](#13-security)).
* **Offline or internet use.** Browsers must reach the server over the LAN. The server is not
  hardened for exposure to the internet.
* **Replacing the widget.** The Qt widget remains the way to embed a tree editor in a Qt
  application.

**Terms**

| Term | Meaning |
|---|---|
| Executor | A ROS 2 node that runs trees: engine, blackboard and leaves. There can be several. |
| Gateway | The web server: tree files, edits and browser connections. |
| Tree API | The ROS interfaces through which anything, including the gateway, uses an executor. |
| Session | One tree open in the gateway for one executor; what one `BehaviorTreeWidget` is in a Qt application. |
| Leaf | A node without children that does work or checks something. |
| Bundle | One self-contained JSON document holding a tree and every sub-tree it uses. |
| Run | One execution of a bundle, from Execute until it finishes or is stopped. |
| Loop | One pass of a run from the Root until the Root returns, as in the widget. |
| Halt | The engine's decision that a node must stop running. |
| Cancel | The request a halted leaf sends to the other machine, and the wait for its confirmation. |
| Source | The ROS topic that feeds a blackboard entry. |
| Instance path | The id of a node within a run: the ids of the Sub-Tree nodes leading to it, then its own. |

---

## 2. What decides the design

1. **The tree has to run on the server.** Leaves need the server's network and devices, a run
   must survive a closed tab, and browsers slow timers in background tabs to about once a
   second. The browser is only an editor and monitor.
2. **Interaction has to stay in the browser.** Panning, zooming, dragging and typing need
   instant response, so they cannot wait for the server. The server is told about changes; it
   does not draw them.
3. **There are three kinds of traffic, each with its own delivery rule.**
   * *Edits and commands* are rare and small, and none may be lost.
   * *Statuses and values* change fast, but only the latest matters: nobody reads more than
     about 30 updates a second.
   * *Log lines* are a stream in which nothing may be dropped.
4. **A leaf is a remote request.** It can succeed, fail, never answer, or lose its peer halfway.
   ROS 2 puts no timeout on a service call or an action result, so a request to a server that
   died waits forever unless the caller sets a deadline.
5. **Stopping takes time and can fail.** A cancel is a request that another machine must
   confirm. The engine and the page have to show the time in between.
6. **Machines are independent.** Two machines can act at the same time, and one machine's
   unfinished cancel must not hold up another. The tree needs a way to say what runs together,
   and the engine must wait only where order matters.
7. **Ticks never cross the network.** A tick stays inside one process and takes microseconds.
   The network is crossed once per action, not once per tick.
8. **State from other machines arrives late.** A value fed from a topic has an age, and a check
   must tell "false" from "no recent data".
9. **The network is not the bottleneck, and neither is the engine.** A LAN round trip takes a
   millisecond or two and a 300-node tree is about 100 KB of JSON. The costs that matter are
   drawing in the browser and the Python ROS client library (rclpy) on the server.
10. **The language follows the leaves.** In a ROS 2 system most leaves are generic ("call this
    action with these values"). Python can build such a leaf from an interface definition at
    run time in a few lines, with the mechanism `ros2 action send_goal` uses. Users' own leaves
    are Python too.

---

## 3. Architecture

```
 Browsers on the LAN          Server (Ubuntu 26.04, ROS 2 Lyrical)                Other machines
 (no ROS installed)
 ┌────────────────┐  HTTP    ┌──────────────────────┐  tree API  ┌──────────────────────┐
 │ editor and     │ ───────► │ gateway              │ ◄────────► │ executor (ROS node)  │
 │ monitor        │ ◄──────► │  tree files, edits,  │  (ROS)     │  engine, blackboard, │
 │ (TypeScript)   │ WebSocket│  browser sync        │            │  leaves, tree log    │
 └────────────────┘          └──────────────────────┘            └──────────┬───────────┘
                                                              actions, services, topics
                                                         ┌───────────┬──────┴────┬───────────┐
                                                         │ machine A │ machine B │ machine C │
                                                         └───────────┴───────────┴───────────┘
```

### 3.1 Who owns what

| | Browser | Gateway | Executor |
|---|---|---|---|
| Drawing, pan, zoom, selection | owns | | |
| Tree documents and their files | shows a copy | owns | receives a bundle per run |
| Blackboard while idle | shows a copy | owns (the blackboard file) | |
| Blackboard during a run | shows a copy | mirrors, writes the file | owns |
| Node statuses, the run | shows | relays | owns |
| Tree log | shows the latest lines | keeps the latest lines | owns (writes the file) |
| ROS entities, leaf code | | | owns |

### 3.2 Why two parts

* **Each part has one event loop.** The executor runs on rclpy's loop, where every reply from
  another machine arrives. The gateway runs on asyncio, which web servers are built on. Neither
  has to host the other's loop.
* **A fault in the web part cannot stall a running tree**, and a crashed executor does not take
  the editor and its unsaved changes with it.
* **The executor works without the web part.** A small command, `run_tree`, reads a tree's
  files, builds the bundle and sends the goal, from a terminal or a launch file. (A bundle is
  too long to type into `ros2 action send_goal`.) `ros2 bag record` can record a run's status
  for later study.
* **One gateway can show several executors** ([3.4](#34-several-executors)).
* **Executing a tree is itself a ROS action.** A tree on one machine can therefore run a tree
  on another machine as one of its leaves ([6.8](#68-remote-sub-tree)).

The cost is one more package to build (the interface definitions) and two processes to deploy.

### 3.3 Processes

Under ROS the two parts are **two processes even on one host**, each a systemd service
([14.2](#142-starting-it)). Putting both in one Python process would save nothing that matters
and would give up the fault isolation.

The gateway reaches an executor through a small interface, `ExecutorLink`, which has two
implementations:

| Link | Used for | How it works |
|---|---|---|
| `RosLink` | Deployment | The tree API of [section 4](#4-the-tree-api), over ROS. The executor may be on the same host or another one. |
| `LocalLink` | Development, tests, use without ROS | The engine runs inside the gateway process. Every node that needs no ROS works, including Parallel, Evaluation and custom Python leaves. The ROS leaves, sources and other executors do not exist. It passes the same messages as the tree API through direct calls, so the hand-over rules of this document are the same code in both links. |

`LocalLink` makes the application runnable on a machine without ROS, including Windows, with
the widget's feature set. It is also how the gateway and the browser are tested.

### 3.4 Several executors

An executor is one ROS node, named by its namespace: `/cell1/bt_executor`,
`/robot2/bt_executor`. A deployment may have one, or one per robot or cell.

* **Each executor runs one tree at a time**, with its own blackboard, its own leaf modules and
  its own log folder and journal.
* **The gateway knows executors in two ways**: those named in its settings, which have a
  session even while they are down, and every action server of the type
  `bt_server_interfaces/action/ExecuteTree` that it finds on the ROS network. With more than
  one, the page shows a picker.
* **Each executor has its own session** in the gateway: its own tree, blackboard and run.
* **Trees reach across executors** with the Remote Sub-Tree node: a tree on one executor
  starts a tree on another and waits for its result ([6.8](#68-remote-sub-tree)).
* **Why more than one.** An executor on a robot's own computer keeps running its tree when the
  link to the rest drops, reads that robot's topics without sending them over the network, and
  can hold leaf code that only makes sense on that machine.

### 3.5 Repository layout

```
bt_server/
├── bt_server_interfaces/   ROS interface definitions (.action, .msg, .srv); CMake-built
├── bt_server_core/         Python, no ROS imports: document model, engine, blackboard, leaf base classes
├── bt_server_executor/     Python + rclpy: the executor node, the ROS leaves, sources
├── bt_server_gateway/      Python + aiohttp (+ rclpy for RosLink): the web server
├── bt_server_bringup/      launch files, parameter files, the systemd units
└── web/                    TypeScript front end; its build output is installed with the gateway
```

`bt_server_core` imports neither ROS nor a web framework. Both other Python packages depend on
it: the executor for the engine, the gateway for reading, checking and bundling tree files.

---

## 4. The tree API

Everything an executor offers is a ROS interface under its node name (`~` below), for example
`/cell1/bt_executor/execute`. The gateway is one client of it; a command line, a launch file,
`ros2 bag` or another executor are others.

### 4.1 Interfaces

| Name | Kind | Type | Delivery | Purpose |
|---|---|---|---|---|
| `~/execute` | action | `ExecuteTree` | | Runs a bundle. Cancelling the goal is Stop. |
| `~/run` | topic | `RunInfo` | reliable; the last message is kept for late joiners | What is loaded: the bundle and the table of its nodes. Published empty when the executor starts, so "nothing loaded" can be told from "not there", and again when a run starts. |
| `~/status` | topic | `TreeStatus` | as `~/run`, with a deadline of 1 s | Run state and one status byte per node. Published on change, at most 30 times a second, and at least every 0.5 s. |
| `~/blackboard` | topic | `BlackboardUpdate` | reliable, history 100 | Changed entries, at most 10 batches a second. |
| `~/details` | topic | `NodeDetails` | reliable, history 100 | Per node: failure reasons, what a node waits for, latest action feedback, fields and titles changed by leaf code. |
| `~/log` | topic | `LogBatch` | reliable, history 200, kept for late joiners | Log lines in batches. |
| `~/get_snapshot` | service | `GetSnapshot` | | Run, statuses, blackboard and details in one consistent reply. |
| `~/get_log` | service | `GetLog` | | Log records from a given number on, read from the log file. Repairs a gap and pages back through a long log. |
| `~/apply` | service | `Apply` | | Changes during a run: blackboard edits, field edits, options. |
| `~/describe` | service | `Describe` | | Read-only questions: node types, what is on the ROS network, interface definitions, one sample of a topic. |
| `~/validate` | service | `Validate` | | Checks a bundle without running it. |
| `~/pause` | service | `std_srvs/srv/SetBool` | | Pause (`true`) and resume (`false`). |
| `~/reset` | service | `std_srvs/srv/Trigger` | | The Reset button. |

"Kept for late joiners" is the ROS setting *transient local*. The definitions are in
[Appendix A](#appendix-a-interface-definitions).

### 4.2 Rules behind the API

* **Typed envelope, JSON content.** Tree documents and blackboard values are recursive and
  hold values of several types, which ROS message definitions express badly, and the browser
  needs JSON anyway. So they travel as JSON text inside typed messages. Everything other ROS
  tools can use is typed: states, status bytes, outcomes, time stamps, ids, sequence numbers.
* **Statuses are sent whole, not as changes.** `TreeStatus.node_status` holds one byte per node
  in the order of `RunInfo.node_paths`: 300 bytes for 300 nodes. A full vector needs no
  bookkeeping, cannot have gaps, serves late joiners, and makes a recording replayable from any
  point. rclpy sends a byte array without converting it element by element.
* **`~/status` is also the heartbeat.** It is published at least every 0.5 s and declares a
  deadline of 1 s. A machine that must stop when the tree goes away subscribes to it and
  either runs its own timeout or lets ROS report the missed deadline.
* **Blackboard, details and log are numbered updates.** A client subscribes, calls
  `~/get_snapshot`, and applies the updates with a higher sequence number. A gap means: call
  `~/get_snapshot` again, or `~/get_log` for the log.
* **The result closes a run.** Once the result of a run has arrived, a late update that
  carries that run's id is ignored: the result's values are final.
* **`~/status` also carries the outcome of the last run**, so an observer that did not send
  the goal learns how it ended.
* **The run id is the goal id.** Whoever sent the goal can recognise the run on `~/run` and
  `~/status`.
* **Outcome maps to the action's final state.** Root succeeded: goal *succeeded*. Root failed,
  or the tick itself failed: goal *aborted*. Stopped by cancelling the goal: goal *canceled*.
  A generic action client therefore reads the result of a tree correctly without knowing the
  result's fields. A tree that stops itself (a Stop node, a silent watchdog) also ends as
  *aborted*, because ROS lets a server report *canceled* only after a cancel request; the
  result's `outcome` says *stopped*.

### 4.3 The bundle

The executor reads no tree files. The sender resolves every Sub-Tree and Remote Sub-Tree node
and ships all documents in one JSON text:

```json
{
  "format": "behavior_tree_bundle",
  "version": 1,
  "root": "robot",
  "trees": {
    "robot":        { "config": {}, "nodes": [], "connections": [] },
    "cells/patrol": { "config": {}, "nodes": [], "connections": [] },
    "robot2/dock":  { "config": {}, "nodes": [], "connections": [] }
  },
  "blackboards": {
    "robot2/dock": [ { "name": "dock_id", "type": "Integer", "value": 1 } ]
  }
}
```

* Each value of `trees` is a tree file as it is on disk or, for a tree being edited, as it is
  in the gateway's memory, unsaved changes included. What is shown is what runs.
* A Sub-Tree or Remote Sub-Tree node carries `"tree": "<key>"` next to its `File` field; the
  key names an entry of `trees`.
* `blackboards` holds the blackboard file of each tree that a Remote Sub-Tree node runs
  ([6.8](#68-remote-sub-tree)). The blackboard of the tree itself travels in the goal.
* The order of a parent's entries in `connections` is the execution order of its children
  ([section 12](#12-files-and-compatibility)).
* A sub-tree that would contain itself is not put into the bundle again. Its node fails when it
  is executed, as in the widget, and validation reports it beforehand.
* Positions, notes and groups stay in the bundle. An observer that did not start the run can
  then draw it from `~/run` alone.

### 4.4 Run lifecycle

```
                Execute (goal accepted)              Root returned, run once
     IDLE ───────────────────────────► RUNNING ───────────────────────────────► IDLE
      ▲                                 │   ▲                              (statuses kept)
      │                 Pause, or a     │   │  resume
      │                 Pause node      ▼   │
      │                               PAUSED
      │
      │   every cancel confirmed            Stop (goal cancelled), a Stop node, or a silent
      │   or timed out                      watchdog; from RUNNING or PAUSED
      └──────────────────────────── STOPPING ◄─────────────────────────────────
                (statuses Ready)
```

* **One run at a time per executor.** A goal that arrives while the state is not IDLE is
  rejected, as is a bundle that cannot be read. A bundle that can be read always runs: a node
  with a problem fails when its turn comes, as in the widget.
* **After a run-once finish the statuses stay** as they ended, as in the widget, until Reset or
  the next Execute.
* **Feedback** of the action is sent when the state or the loop count changes, not per tick.
* **With Loop Execution on, the goal ends only** by Stop, by an error of the tick, or when the
  option is switched off during the run.
* **A Reset during a run passes through RESTARTING**, which the diagram leaves out: the leaves
  are halted, their cancels awaited, and a new loop starts ([5.5](#55-loops-pause-stop-and-reset)).

What each command does in each state:

| | IDLE | RUNNING | PAUSED | STOPPING | RESTARTING |
|---|---|---|---|---|---|
| Execute (a goal) | starts a run | rejected | rejected | rejected | rejected |
| Pause | refused | pauses | no effect | refused | pauses once the new loop has started |
| Resume | refused | no effect | resumes | refused | no effect |
| Stop | no effect | STOPPING | STOPPING | no effect | STOPPING |
| Reset | statuses to Ready | RESTARTING | halts the leaves, stays paused; the resume starts a new loop | no effect | no effect |
* **A goal may name a watchdog**: a `TreeStatus` topic, a run id and a time. The executor stops
  the run when, for that time, the topic has carried no message that shows that run as still
  going. Remote Sub-Tree uses it so that a tree does not run on after the tree that started it
  is gone ([6.8](#68-remote-sub-tree)).

### 4.5 Runs the gateway did not start

A run started by `run_tree` or by another executor appears on `~/run` like any other.

* The gateway shows it in a view of its own, read-only, drawn from the bundle in `RunInfo`,
  with live statuses.
* The session's own tree, its unsaved changes and its blackboard file are not touched. Its
  Execute is unavailable while the executor is busy.
* Stop is offered. The gateway has no goal handle for a run it did not start, so it always
  stops a run by its id, through the action's cancel service
  (`~/execute/_action/cancel_goal`). The same call works after a gateway restart.

---

## 5. The engine

The engine is a few hundred lines in `bt_server_core`. It is written for this design instead of
taken from py_trees or BehaviorTree.CPP for one reason: in both, halting a node is a single
synchronous call, so the tree either moves on right after sending a cancel or blocks every
other node while it waits. With remote actions the first means a pre-empted machine may still
be moving when the next action starts; the second freezes the tree, including the branches
that command other machines. This engine gives the time between the halt and the confirmation
a status of its own.

### 5.1 Statuses

| Status | Byte | Meaning |
|---|---|---|
| Ready | 0 | Not run in this loop, or reset. |
| Running | 1 | Running, or waiting to start ([5.6](#56-cancelling-is-a-state)). |
| Succeeded | 2 | |
| Failed | 3 | The reason is in the node's details and in the log. |
| Cancelling | 4 | New. Halted; waiting for the other side to confirm. Only leaves show it. |

### 5.2 What each node does when ticked

| Node or setting | Behaviour |
|---|---|
| Root | A Sequence (default) or a Selector, with a Memory flag. A tree has one. |
| Sequence | Runs its children in order until one fails. Succeeds when all succeeded, and when it has no children. |
| Selector | Runs its children in order until one succeeds. Fails when all failed, and when it has no children. |
| Memory on | A Sequence or Selector resumes at its running child on the next tick. |
| Memory off | A Sequence or Selector starts at its first child on every tick. A child running behind the one that now decides the result is halted. Earlier children should be quick checks. |
| Negation | One child. Succeeded becomes Failed and the reverse; Running stays. Without a child it fails. |
| Parallel | Runs its children at the same time ([5.3](#53-parallel)). |
| Leaf | See [section 6](#6-leaf-nodes). |

Children run in the order of their connections in the document, which the editor keeps equal to
left to right on the canvas. Nodes not connected to the Root, Notes and Groups are ignored.

Two rules decide what the statuses show, both as in the widget:

* **A composite that starts anew** first sets every node below it to Ready, except a leaf that
  is still Cancelling.
* **Halting a composite** sets it and every node below it to Ready, also the ones that had
  finished. Leaves that were active pass through Cancelling on the way
  ([5.6](#56-cancelling-is-a-state)).

### 5.3 Parallel

New. A Parallel has any number of children and runs them at the same time, so that one machine
can work while another does. It has a **Policy**: *All* (the default) or *Any*.

| Policy | Succeeds | Fails |
|---|---|---|
| All | when every child has succeeded | as soon as one child fails |
| Any | as soon as one child succeeds | as soon as one child fails |

* **The rule is py_trees's.** A Parallel fails as soon as one child fails, whatever the policy;
  the policy only says how many successes it takes. The children still running when the
  Parallel ends are halted.
* **With *Any*, the first child to finish decides**: its result is the Parallel's.
* **A failure wins a tie.** If one child fails in the same tick in which another succeeds, the
  Parallel fails.
* **On every tick it ticks each child that has not finished**, left to right, and decides only
  after all of them were ticked. The order decides only which child's code runs first within
  a tick.
* **A finished child keeps its result** and is not run again until the Parallel itself starts
  anew. There is no Memory flag.
* **The result is returned in the tick that decides it.** Children halted at that moment show
  Cancelling until the other side confirms. What follows the Parallel in a Sequence or Selector
  waits for them ([5.6](#56-cancelling-is-a-state)).
* **Without children** a Parallel succeeds, under either policy.
* **Halting a Parallel** halts every child that is running.
* **"At the same time" means the remote work overlaps.** The engine has one thread, so the
  code of two nodes never runs at the same instant, and the children share the one blackboard
  without locks or races. Two branches that write the same entry do so in tick order.
* **Pause, Stop and Restart nodes** in a branch act on the whole tree, as everywhere. The tick
  ends the moment one of them issues its command, so the branches after it are not ticked in
  that tick.
* **The Root is not a Parallel.** Put one below it.

Typical uses: *All* to send two robots to their places and go on when both have arrived; *Any*
to run an action next to an Evaluation that waits for a condition, so that whichever finishes
first ends the other. The Parallel succeeds if the action succeeds or the condition comes
true, and fails if the action fails. A Timeout on that Evaluation then works as a time limit
for the action: when it passes, the Evaluation fails, and so does the Parallel.

In the editor a Parallel looks like a Sequence or Selector, with its policy in brackets:
"Parallel (All)". The right-click menu of all three offers Sequence, Selector and Parallel, and
for a Parallel the two policies; the Root's menu keeps to Sequence and Selector. In the file it
is a node of type `Parallel` with `"policy": "all"` or `"any"`.

### 5.4 When the tree ticks

The widget ticks on a timer. This engine ticks when something outside the tick can have changed
the outcome:

* a started leaf finished, or a cancel was confirmed or timed out;
* a blackboard entry was changed from outside the tick: by a source, by a browser, or by leaf
  code on a worker thread;
* a deadline passed, or a fed entry went stale;
* a command arrived (resume, reset);
* the tick interval elapsed (the tree's *Tick interval* option, 100 ms by default).

Four rules go with this:

* **Requests are merged**: any number of them before the next tick cause one tick. Ticks from
  events are capped at `max_tick_rate_hz` (200 by default).
* **Changes made inside a tick do not request another tick.** A Set node, or leaf code that
  runs on the engine thread, changes the blackboard during a tick; nodes ticked later in the
  same tick see the change, the others at the next tick. Otherwise a node that writes the
  blackboard on every tick would make the tree tick as fast as the cap allows.
* **The periodic tick** serves nodes that are re-run on every tick and picks up the changes
  just described. It keeps its own rhythm from the start of the run; ticks from events do not
  shift it. A tree that only waits for one action costs this tick and nothing else.
* **A node must not count ticks as time.** Ticks come at least once per interval, and more
  often when events arrive. The exception is made for code written for the widget: a leaf
  that runs on the engine thread and returns Running is called again only on periodic ticks,
  once per interval as in the widget ([6.6](#66-custom-leaves)).

A status can also change outside a tick: a cancel is confirmed, or a deadline passes during a
pause. Such a change is reported when it happens, not at the next tick.

```python
def _tick(self) -> None:
    self._tick_requested = False
    if self.state is not State.RUNNING:
        return
    self._root.tick()         # walks the active paths; notes every node whose status changed
    self._commit()            # 1. send the cancels of leaves halted in this tick
                              # 2. start the waiting leaves that are allowed to start (5.6)
    self._sink.statuses(self._changed)
    if self._root.finished and not self._cancelling:
        self._end_loop()
```

**Two clocks.** Timeouts that a user sets on a node (a leaf's *Timeout*, an Evaluation's
*Timeout*) follow the ROS clock, so they also work in simulated time. Everything about the
plumbing follows the steady clock of the host: the tick interval, the heartbeat, the wait for a
goal reply, the cancel timeout, the age of fed entries.

### 5.5 Loops, Pause, Stop and Reset

* **A loop** runs from the Root until the Root returns. Its end is delayed until no leaf is
  Cancelling. At the end of a loop, entries with **Resets** checked get their loop-start values
  back ([7.3](#73-resets-and-restore)).
* **Loop Execution** starts the next loop at the next periodic tick, so the tick interval is
  the only pause, as in the widget. Statuses go back to Ready at the start of each loop.
* **Pause** stops ticking. Actions already running on other machines carry on, and their
  results are kept for the resume. Deadlines keep counting and still cancel an overdue action
  during a pause; only the tree's progress waits.
* **Stop** halts every running leaf and enters STOPPING. When every cancel is confirmed or has
  timed out, the loop ends, every node goes back to Ready and the state is IDLE. The result
  says how many cancels were not confirmed.
* **Reset** while idle sets every node to Ready. While running it enters RESTARTING: the
  running leaves are halted and nothing is ticked until their cancels are confirmed or have
  timed out; then the loop ends, and a new one starts in RUNNING at the next periodic tick. A
  Restart node does the same, so a tree that always reaches one runs once per tick interval.
  While paused, Reset halts the leaves and stays paused; the tick of the resume starts a new
  loop, as in the widget.
* **A command issued by node code during a tick ends that tick at once.** No further node code
  runs in it, and the command is carried out next. This holds for the Restart, Pause and Stop
  nodes and for `tree.Stop()` and its kin in leaf code.

### 5.6 Cancelling is a state

Instant leaves, such as Set, do their work inside the tick and are never cancelled. Every other
leaf is *started* and goes through this lifecycle:

```
            ticked                 allowed to start             result arrives
 not started ───► waiting to start ─────────────────► active ─────────────────► finished
                    │ no longer ticked                  │ halted: pre-empted, Stop, Reset, deadline
                    ▼                                   ▼
                 not started                        cancelling ───────────────► not started,
                                                        confirmed, or             or finished as Failed
                                                        cancel timeout            after a deadline
```

| Leaf state | Status shown |
|---|---|
| not started | Ready |
| waiting to start | Running |
| active | Running |
| cancelling | Cancelling |
| finished | Succeeded or Failed |

* **A tick decides; the work happens after it.** During the tick a leaf only notes that it
  wants to start, and a halted leaf only notes that it must be cancelled. When the tick is
  over, the engine first sends every cancel and then starts the leaves that may start. A leaf
  that replaces a halted one therefore always sees it as Cancelling, whatever the order in
  which the tick visited them.
* **Halting returns at once.** A halted active leaf becomes Cancelling. A leaf that was only
  waiting to start goes back to not started; it never ran.
* **A start waits for the cancels that come before it.** A leaf that is about to start waits
  while a leaf is Cancelling whose work it follows: precisely, when the nearest composite that
  contains both is a Sequence or a Selector. It does not wait for a leaf in another branch of
  a Parallel: those branches are independent by the tree's own statement, and one machine's
  slow cancel must not hold up another machine.
* **While it waits, the leaf shows Running**, with "waiting for *node* to cancel" in its
  details.
* **The wait can be switched off.** The tree option **Wait for cancelled actions** is on by
  default, with an override per node. Switch it off for a node whose server replaces a running
  goal by itself, where waiting would make the machine stop and start again.
* **Local leaves never wait for cancels.** Evaluation, Set, Print Message and the execution
  nodes have no effect outside the executor. Every started leaf waits unless its class says
  otherwise: Call Action, Call Service, Publish, UDP Message, Remote Sub-Tree, custom leaves.
* **Stop, Reset and the end of a loop wait for every cancel**, in all branches.
* **A cancel has a time limit**, the tree option *Cancel timeout* (5 s by default, with an
  override per node). When it passes, the cancel counts as unconfirmed and an ERROR is logged.
  The tree no longer knows what that machine is doing, so by default it stops: the option
  *When a cancel is not confirmed* chooses between *stop the tree* (default) and *continue*.
* **An unconfirmed goal is not forgotten.** It stays on a list of unsettled goals, the cancel
  is sent again every few seconds and when its server reappears, and `~/status` shows how many
  there are. The list is the goal journal ([8.4](#84-the-goal-journal)).
* **A node never runs twice at once.** A leaf that is ticked again while its own earlier work
  is outstanding (a cancel not yet confirmed, an unsettled goal, a function that has not
  returned) waits for it, whatever the options say. It tells its parent Running and goes on
  showing Cancelling until that work has settled.
* **Results of a cancelled leaf are ignored**, as in the widget.
* **Timeouts count from the real start.** A leaf's *Timeout* starts when the leaf becomes
  active, not when it began to wait.

### 5.7 Errors

* An exception in leaf code, or a wrong return value, fails that node only. The message goes to
  the node's details and to the log at ERROR.
* If the tick itself fails, execution stops, the Root shows Failed with the error and the run
  ends with the outcome *error*, also with Loop Execution on.

### 5.8 Structure

```python
class Scheduler(Protocol):            # the engine's only link to an event loop
    def now(self) -> float: ...                              # steady clock, seconds
    def ros_now(self) -> float: ...                          # ROS clock; equals now() without ROS
    def call_soon(self, fn: Callable[[], None]) -> None: ... # safe from any thread
    def call_at(self, when: float, fn: Callable[[], None], ros: bool = False) -> Cancelable: ...

class Sink(Protocol):                 # where the engine reports
    def state(self, state: State, loop: int) -> None: ...
    def statuses(self, changed: Iterable[int]) -> None: ...  # node indices
    def detail(self, index: int, kind: str, value: object) -> None: ...
    def blackboard(self, changes: BlackboardChanges) -> None: ...
    def log(self, level: LogLevel, text: str, index: int = -1) -> None: ...

class TreeRunner:
    def __init__(self, bundle, blackboard, registry, scheduler: Scheduler, sink: Sink,
                 on_finished: Callable[[Outcome], None]): ...
    def start(self) -> None: ...
    def pause(self) -> None: ...
    def resume(self) -> None: ...
    def stop(self) -> None: ...
    def reset(self) -> None: ...
    def request_tick(self) -> None: ...                      # merged, rate-capped
```

* The engine runs on one thread and holds no locks of its own. Everything that arrives from
  elsewhere enters through `Scheduler.call_soon`.
* Two schedulers exist: one on rclpy and one on asyncio for `LocalLink`.
* Node statuses live in one `array('B')`, indexed like `RunInfo.node_paths`. A tick records the
  indices that changed, so reporting costs what ran, not what the tree holds.

---

## 6. Leaf nodes

### 6.1 Kinds

| Kind | Runs | Its result is known | Examples |
|---|---|---|---|
| Instant | on the engine thread, inside the tick | within the tick | Evaluation, Set, Print Message, Restart, Pause, Stop |
| Waiting | nothing runs; it is checked again on every tick | when its condition holds | Evaluation with **Wait** |
| Started, asynchronous | started after the tick, on the engine thread; returns at once | when a reply arrives on the engine thread | Call Action, Call Service, Publish, Remote Sub-Tree |
| Started, thread | a plain function on a worker thread | when the function returns | UDP Message, custom leaves written for the widget |

Instant and waiting leaves act only inside the executor. Started leaves follow the lifecycle of
[5.6](#56-cancelling-is-a-state). "Instant" says where the work is done, not what is returned:
Restart, Pause and Stop do their work inside the tick and return Running.

### 6.2 Built-in leaves

| Leaf | Behaviour | Compared with the widget |
|---|---|---|
| Evaluation | Compares a blackboard entry with another entry or a literal, and can wait until the comparison holds ([6.3](#63-evaluation)). | Adds operators besides equality, **Wait** and a timeout. |
| Set | Sets the *Value Name* entry to the *Set To* entry or the literal. | Same. It cannot write an entry that has a source. |
| Restart, Pause, Stop | Act on the tree that runs them, like the buttons. Each returns Running when it issues its command. | Stop and Restart now wait for cancels. |
| UDP Message | Sends one datagram from the executor's host. Fields *Destination IP*, *Destination Port*, *Message*. | Same. |
| Print Message | Writes the message, or `<entry> = <value>`, to the tree's log at the chosen level. | Same. |
| Sub-Tree | Runs the tree of its file as part of this tree ([6.7](#67-sub-tree)). | Same; the file is read by the gateway. |
| Call Action | Sends a goal to a ROS action server and waits for the result ([6.5](#65-the-ros-leaves)). | New. |
| Call Service | Calls a ROS service and waits for the reply. | New. |
| Publish | Publishes one message on a ROS topic. | New. |
| Remote Sub-Tree | Runs a tree on another executor ([6.8](#68-remote-sub-tree)). | New. |

### 6.3 Evaluation

The widget's Evaluation succeeds when an entry equals another entry or a literal, and fails
otherwise. That is enough for flags a tree sets itself. Values that arrive from other machines
need two more things: an order ("battery above 20") and patience ("until the door is open").

**Editors**

| Editor | Field in the file | Purpose |
|---|---|---|
| Value Name | `ValueName` | The entry to look at. Lists every entry. |
| Operator | `Operator` | `==` (default), `!=`, `<`, `<=`, `>`, `>=`. New. |
| Compare To | `CompareTo` | *Literal* (`null`), or another entry of the same type. |
| Literal Value | `LiteralValue` | Shown while *Literal* is chosen; its editor follows the type of the Value Name entry. |
| Wait | `Wait` | Off (default): answer at once. On: keep running until the comparison holds. New. |
| Timeout | `WaitTimeout` | Shown while Wait is on. Seconds; 0 waits without limit. New. |

**Comparing**

* `==` and `!=` work for every type, with the widget's rules: numbers compare by value
  (`1 == 1.0`), and a Bool never equals a number, inside lists and dictionaries too.
* `<`, `<=`, `>` and `>=` work for Integer and Double entries. For any other type the node
  fails with "cannot be ordered".
* A comparison with `nan` is false, except `!=`.

**Result**

| Situation | Wait off | Wait on |
|---|---|---|
| The comparison holds | Succeeded | Succeeded |
| It does not hold | Failed | Running |
| An entry it needs does not exist | Failed, with the reason | Running: another branch may create it |
| An entry it needs is stale ([7.4](#74-entries-fed-from-ros-topics)) | Failed, "no recent data" | Running |
| No entry is chosen; an Object entry is compared with a literal; the type cannot be ordered | Failed, with the reason | Failed, with the reason: waiting cannot fix it |
| The timeout passes | | Failed, "timed out" |

* **While it waits**, the node's details say what it waits for and what it sees:
  `waiting for battery >= 20.0 (now 17.3)`.
* **It is checked on every tick.** A change of either entry from outside the tick requests one
  at once ([5.4](#54-when-the-tree-ticks)), so a waiting node reacts to a source within
  milliseconds.
* **The timeout** counts from the tick in which the node began to wait, on the ROS clock.
* **Halting** a waiting Evaluation sets it to Ready at once. It has nothing to cancel, never
  shows Cancelling and never waits for other cancels.
* **In a composite without memory**, an Evaluation with Wait off is the usual guard: it is
  checked again on every tick and halts what runs behind it when it fails. With Wait on, the
  composite stays Running at that child, and a later child that was running is halted.
* **With a Parallel of policy Any**, a waiting Evaluation next to an action ends the action
  when the condition comes true, and is ended by the action when that finishes first
  ([5.3](#53-parallel)).

### 6.4 Fields, bindings and outputs

* **A node type declares its fields**: name, type and default. The types are the widget's:
  integer (32 bit), float, string, bool and choice (a list of options with one selected).
  Other values are shown read-only.
* **The executor publishes the catalog of its node types** through `~/describe`. The browser
  builds the Add Node menu and every node's editors from it, so a new leaf type needs no
  front-end code.
* **A field holds a literal or a binding to a blackboard entry.** A bound field takes the
  entry's value when the leaf starts. Bindings are stored next to the fields:

  ```json
  {"id": "9a1d…", "type": "CallAction", "title": "Go to dock", "x": -40.0, "y": 180.0,
   "fields": {"Action": "/robot1/navigate_to_pose", "Type": "nav2_msgs/action/NavigateToPose",
              "Goal": {"pose": {"header": {"frame_id": "map"}}}, "Timeout": 120.0},
   "bindings": {"Goal.pose.pose": "dock_pose"},
   "outputs": {"Result.error_code": "nav_error", "Feedback.distance_remaining": "distance"}}
  ```

* **`outputs` copy values the other way**: parts of a result when the leaf succeeds, and parts
  of an action's feedback while it runs (at most 10 times a second). Together with sources
  ([7.4](#74-entries-fed-from-ros-topics)) this is how ROS data reaches the blackboard.
* Renaming an entry updates the bindings and outputs that name it in every open document, as
  Evaluation and Set follow renames in the widget. A binding to a missing entry is shown in red
  as "(missing)" and fails the node when it runs. An output to a missing entry creates it.

### 6.5 The ROS leaves

**Choosing a target.** The executor lists what is on the ROS network with
`rclpy.action.get_action_names_and_types`, `get_service_names_and_types` and
`get_topic_names_and_types`. The browser offers the names in a dropdown; a name can also be
typed, for a server that is not up yet.

**Building the form.** For the chosen type the executor loads the interface class and describes
its fields:

```python
from rosidl_runtime_py import message_to_ordereddict, set_message_fields
from rosidl_runtime_py.utilities import get_action

NavigateToPose = get_action("nav2_msgs/action/NavigateToPose")
defaults = message_to_ordereddict(NavigateToPose.Goal())   # nested dict of default values
kinds = NavigateToPose.Goal.SLOT_TYPES                     # field types, walked recursively

goal = NavigateToPose.Goal()
set_message_fields(goal, values)                           # nested dict from fields + bindings
```

* The description is a tree of fields: name, kind (bool, integer with its range, float, string,
  nested message, array) and default. Named constants of the type are offered as choices.
* Only the executor needs the interface packages installed. The gateway and the browser handle
  JSON only.
* **A type name is checked before it is used.** `get_action` and its kin import the Python
  module that the type name points to, and type names come from tree files and browsers. The
  executor therefore accepts only names whose package is a ROS interface package installed on
  its host.
* Arrays and deeply nested values are edited as text in the first version.

**Call Action.** Fields: *Action*, *Type*, *Goal*, *Timeout* (0: none), *Feedback timeout*
(0: none).

| Step | What happens | Fails with |
|---|---|---|
| Start | The leaf looks for the server every 100 ms, for 2 s at most, without blocking. The goal is sent. | "server not available" |
| Goal reply | Accepted: the result is requested. | "goal rejected"; "no reply to the goal" after 5 s |
| Running | Each feedback message updates the node's details, its outputs and its liveness time. Once a second the leaf checks that the server is still on the network. | "timed out" after *Timeout*; "no feedback" after *Feedback timeout*; "server lost" |
| Result | Succeeded: the outputs are written, the node succeeds. | "aborted"; "cancelled by the server" |
| Halted before the goal reply | The leaf waits for the reply and cancels then. Without this a goal accepted after the halt would run on with nobody watching. | |
| Halted while running | A cancel is sent. The leaf settles when the result arrives with the state *canceled*. | Unconfirmed after the cancel timeout, or when the cancel is refused and no result comes |

* A deadline that passes also sends a cancel: the node shows Cancelling, then Failed.
* The 2 s and 5 s above are fixed, not fields.
* **"Server lost" is quick only for a server that exits.** A machine that vanishes is noticed
  when ROS discovery gives up on it, about 20 s with Fast DDS's usual settings. *Feedback
  timeout* is the faster check for an action that sends feedback.

```python
client = ActionClient(node, action_class, name)            # created when the run is set up
client.send_goal_async(goal, feedback_callback=self._on_feedback) \
      .add_done_callback(self._on_goal_reply)              # a ClientGoalHandle: .accepted
handle.get_result_async().add_done_callback(self._on_result)       # .status, .result
handle.cancel_goal_async().add_done_callback(self._on_cancel_reply)
```

**Call Service.** Fields: *Service*, *Type*, *Request*, *Timeout* (5 s), *Success field*.

* The node succeeds when the reply arrives. If the reply has the bool field named in *Success
  field* (default `success`, as in `std_srvs/srv/Trigger`), `false` fails the node, with the
  reply's `message` as the reason if it has one.
* ROS 2 cannot cancel a service call. A halted Call Service drops the pending request
  (`Client.remove_pending_request`), ignores a late reply and settles at once.

**Publish.** Fields: *Topic*, *Type*, *Message*, *Wait for subscribers* (0), *Keep for late
joiners* (off).

* The publisher is reliable with a history of 10. *Keep for late joiners* makes it hand its
  last message to subscribers that arrive later.
* Publishers are created when the run is set up, not when the leaf runs. A publisher created
  just before its first message often loses that message, because the subscribers have not
  found it yet.
* With *Wait for subscribers* above 0 the leaf is Running until that many subscribers are
  matched, and fails if they are not within 5 s.

### 6.6 Custom leaves

**For code written for the widget**, a compatibility base class keeps the widget's names:

```python
import time

from bt_server_core.compat import LeafNodeWidget


class MoveTo(LeafNodeWidget):
    _title = "Move To"
    _fields = {"x": 0.0, "y": 0.0, "speed": 5, "mode": ["walk", "run"], "avoid_obstacles": True}

    def OnRun(self, tree):                        # on a worker thread: RUN_IN_THREAD = True, the default
        for _ in range(100):
            if self.CancelRequested():
                return False
            time.sleep(0.01)
        tree.SetEntry([self.GetField("x"), self.GetField("y")], "position")
        return True
```

* Supported: `_title`, `_fields`, `TYPE_NAME`, `RUN_IN_THREAD`, `OnRun`, `OnStart`,
  `OnTerminate`, the field getters and setters, `CancelRequested`, and on `tree`: `GetEntry`,
  `SetEntry`, `HasEntry`, `Log`, `Execute`, `Pause`, `Stop`, `Reset`.
* Not supported: `UI_FILE` (the browser builds the node from its fields; a warning is logged),
  anything that touches Qt, `THREAD_WAIT` (ignored: a finished call triggers a tick at once).
* A cancelled call that tries to change the tree raises `ExecutionCancelled`, as in the widget.
* **A halted call is abandoned at once, as in the widget.** The node goes back to Ready, the
  call runs on in its thread, and its result is ignored. The node does not start again until
  that call has returned. Such a leaf never shows Cancelling and never holds up the tree.
* With `RUN_IN_THREAD = False`, `OnRun` runs on the engine thread inside the tick. While it
  returns Running it is called again once per tick interval, as in the widget. It must not
  block: in the widget a blocking call froze the window, here it stops the ticks, the
  heartbeat and every reply of the executor.
* **Leaf objects are made anew for each run**, from the bundle. In the widget a leaf object
  lived as long as its node, so state kept on `self` survived from one run to the next. Keep
  such state on the blackboard instead.

**For new code**, the native base classes are smaller:

```python
from bt_server_core import AsyncLeaf, ThreadLeaf
from std_msgs.msg import Bool


class Beep(ThreadLeaf):                 # a plain function on a worker thread
    fields = {"times": 1}

    def run(self, ctx) -> bool:
        for _ in range(self.get("times")):
            if ctx.cancel_requested():
                return False
            sound_the_horn()            # your own blocking code
        return True


class WaitForDoor(AsyncLeaf):           # everything on the engine thread, nothing blocks
    def on_start(self, ctx) -> None:
        self._sub = ctx.node.create_subscription(Bool, "door_open", self._on_msg, 1)

    def _on_msg(self, msg) -> None:
        if msg.data:
            self.succeed()              # also requests a tick

    def on_cancel(self, ctx) -> None:
        self.settled()                  # nothing to wait for

    def on_end(self, ctx) -> None:      # always called: finished, failed or cancelled
        ctx.node.destroy_subscription(self._sub)
```

* **Registering.** The executor imports the modules named in its `leaf_modules` parameter and
  registers every leaf class they define. A type name must be unique and must not be the name
  of a built-in node type. The widget let an application's own type take the names `Notes`,
  `UDP Message`, `Sub-Tree`, `Group` and `Print Message`; the server does not.
* **Unknown types** load as placeholders marked "(unknown type)". They keep all saved data and
  fail when executed, so saving a tree on a server that lacks a leaf module loses nothing.
* **Worker threads** are reused daemon threads, each running one call at a time, as in the
  widget. A call that never returns occupies one thread and delays nothing else.
* **A halted `ThreadLeaf` shows Cancelling until its function returns.** A function that checks
  `cancel_requested()` returns quickly. One that does not holds the node in Cancelling until
  the cancel timeout and then counts as an unconfirmed cancel
  ([5.6](#56-cancelling-is-a-state)). Leaves written for the widget are spared this, as said
  above.
* **A class says whether its starts wait for cancels** with `WAITS_FOR_CANCELS` (default
  `True`). Set it to `False` for a leaf that only computes.

### 6.7 Sub-Tree

As in the widget, a Sub-Tree node runs the tree of its file as part of the running tree and
succeeds or fails with that tree's Root.

* Its nodes are built into the run. They tick with it, use the one blackboard, and are halted
  like any others.
* The file's options (tick interval, Loop Execution) are not used, and a blackboard file next
  to it is not loaded.
* Pause, Stop and Restart nodes in it act on the tree that runs it.
* The node fails with the reason when it has no file, the file is missing or not a tree file,
  or the sub-tree contains itself.

### 6.8 Remote Sub-Tree

New. A Remote Sub-Tree node runs a tree on another executor and succeeds or fails with it. It
is how one machine's tree hands a whole job to another machine.

| | Sub-Tree | Remote Sub-Tree |
|---|---|---|
| Runs | inside this run, on this executor | as a run of its own, on the executor it names |
| Ticks | with this tree | by itself; only the start, the result and a cancel cross the network |
| Blackboard | this tree's | its own; values are copied in at the start and back at the end |
| The file's options | not used | used, except Loop Execution: the tree runs once |
| Leaf modules and sources | this executor's | the other executor's |
| If the link breaks | not applicable | the remote tree goes on or stops, as its watchdog says |

**Fields**

| Field | Default | Meaning |
|---|---|---|
| *Executor* | | The other executor's name, such as `/robot2/bt_executor`; a dropdown of the executors found. |
| *File* | | The tree file to run, as for Sub-Tree. |
| *Timeout* | 0 | Seconds; 0 waits without limit. |
| *Watchdog* | 5 | Seconds this executor may be silent before the remote tree stops itself; 0 lets it run on. |
| `bindings` | | Entries copied in: remote entry name to local entry name. |
| `outputs` | | Entries copied back: remote entry name to local entry name. |

**What happens**

1. **When the run is bundled**, the gateway adds the remote tree, the sub-trees it uses and its
   blackboard file to the bundle ([4.3](#43-the-bundle)).
2. **When the leaf starts**, it builds the remote blackboard: the entries of the remote tree's
   blackboard file, with the bound entries replaced by this tree's current values. It sends an
   `ExecuteTree` goal to the other executor, naming its own `~/status` topic and the id of its
   own run as the watchdog.
3. **While it runs**, the leaf behaves as Call Action does. It also listens to the other
   executor's `~/status`: when that falls silent for 3 s, the leaf fails with "executor lost".
4. **When the remote run ends**, succeeded or failed, the leaf writes its outputs from the
   remote blackboard's final values and takes the remote Root's result as its own.
5. **When the leaf is halted**, it cancels the goal. The other executor stops its tree, which
   waits for its own cancels, and the leaf settles when that is done. The cancel timeout of
   this node should be longer than the remote tree's.

**Rules**

* **The other executor must be idle.** The leaf knows the other executor's state from its
  `~/status`. If a tree is running there, the node fails with "executor busy" without sending
  a goal. A goal rejected by an idle executor fails the node with "the tree was refused".
* **The remote tree runs once.** Loop Execution in its file is ignored; a tree that never
  returned would hold this node for ever.
* **Outputs overwrite.** A local entry named in `outputs` gets the remote value when the remote
  run ends, whatever happened to it locally in the meantime. An entry of the remote tree that
  has a source cannot be bound: validation reports it.
* **The remote tree is checked on the remote executor** before Execute, because node types and
  ROS interfaces may differ between the two ([8.3](#83-checking-a-bundle)).
* **The watchdog is the remote tree's own safety rule.** With a value above 0, the remote
  executor stops its run when, for that long, it has not heard on this executor's `~/status`
  that the run which started it is still going. That covers a crash of this executor, a failed
  network between the two, and a run here that ended while its cancel never arrived there. A
  Stop that could not reach the remote executor therefore still takes effect. With 0 the
  remote tree runs on by itself, which suits a tree whose job is to bring its machine to a safe
  place.
* **Nothing is shared while it runs.** Each tree reads and writes its own blackboard. A remote
  tree that needs live data declares sources of its own.
* **Remote trees can start remote trees.** A chain that leads back to an executor already in
  it fails there with "executor busy".

**In the page**

* While idle, Zoom In on the node opens the remote tree's file for editing, as for a Sub-Tree.
* During a run, Zoom In shows the remote run, read-only: its tree and live statuses, taken from
  the other executor's `~/run` and `~/status`. The leaf publishes the remote run's id in its
  details, so the gateway can tell that run from any other.
* The other executor's own session shows the same run as one it did not start
  ([4.5](#45-runs-the-gateway-did-not-start)).

---

## 7. The blackboard

### 7.1 Types

As in the widget: Integer (32 bit), Double, String, Bool, List, Dictionary, Set, and Object for
run-time values. Names must be non-empty, must not start or end with spaces, and cannot contain
`.` or `/`. A value written to an existing entry must fit its type.

### 7.2 Who holds it

* **While idle, the gateway holds the blackboard** as part of the tree and writes the
  blackboard file.
* **The goal hands it over.** A run starts with a copy sent in the goal. From the moment the
  goal is accepted until the result arrives, the executor's copy is the truth: it publishes
  changes, and the gateway mirrors them, shows them and writes the file.
* **Edits made in the browser during a run** go to the executor through `~/apply` and come back
  like any other change. Add, remove, rename, retype, reorder and the Resets box all work
  during a run.
* **The result hands it back.** It carries the final values, which become the gateway's copy.
  Updates of that run that arrive after the result are ignored.
* **An edit that crosses the start of a run is not lost.** From the click on Execute until the
  goal is accepted or rejected, the gateway holds blackboard edits back. It then sends them to
  the executor, or applies them itself if the goal was rejected.
* **An edit that crosses the end of a run is not lost.** If the executor answers `~/apply` with
  "no run is active", the gateway applies the edit to its own copy, which by then is the
  result's.
* **Entries have ids.** The gateway gives every entry an id that lasts for the session, and
  both sides name entries by it. A rename is then seen as a rename, so the nodes and bindings
  that use the entry can follow it, also when Restore renames an entry back.

### 7.3 Resets and restore

* **Resets.** Each entry has a Resets box. At the end of every loop a checked entry gets back
  the value it had when the loop started. The executor records an entry's value just before
  its first change in a loop, so the cost follows the changes, not the size of the blackboard.
  The rules for entries renamed, retyped, created or removed during a loop are the widget's.
* **Restore blackboard values on Stop / Reset.** The blackboard sent in the goal is the
  snapshot. The executor restores from it on Stop and on a Reset during the run. The gateway
  keeps its own copy of what it sent and restores from that on a Reset after the run. Editing
  the blackboard while idle discards the snapshot, as in the widget.
* The snapshot knows which entry is which, so a renamed entry is renamed back instead of
  removed and created again.

### 7.4 Entries fed from ROS topics

New. An entry may have a **source**: a ROS topic whose messages set its value. This is how the
state of other machines (a battery level, a door switch, a position) becomes visible to
Evaluation nodes and bindings without any leaf code.

```json
{"name": "battery", "type": "Double", "value": 0.0,
 "source": {"topic": "/robot1/battery_state", "type": "sensor_msgs/msg/BatteryState",
            "field": "percentage", "max_rate_hz": 2.0, "max_age_s": 5.0}}
```

**Setting one up.** Each row of the Blackboard tab has a **Source…** button. Its dialog offers:

| Setting | Meaning |
|---|---|
| Topic | A dropdown of the topics on the ROS network; a name can also be typed. |
| Type | Filled in from the topic. |
| Field | Chosen from the fields of the type: `percentage`, `pose.position.x`, `ranges[0]`. Empty takes the whole message. |
| Max rate | Updates per second the entry accepts (default 2). |
| Max age | Seconds after which a value counts as stale; 0 (default) never. |
| **Test** | Takes one message from the topic now and shows the value the entry would get. |

**Types.** The entry's type must fit the field:

| Field of the message | Entry type |
|---|---|
| `bool` | Bool |
| Integers of up to 32 bits | Integer, or Double |
| `uint32`, 64-bit integers | Double, or Integer if every value fits 32 bits; a message whose value does not fit is skipped and logged once |
| `float32`, `float64` | Double |
| `string` | String |
| An array | List |
| A nested message, or the whole message | Dictionary |

**Behaviour**

* **The topic owns the value.** Set nodes, leaf code and the Blackboard tab cannot write an
  entry that has a source; an attempt fails with the reason. The Resets box does not apply,
  and Restore leaves the entry alone.
* **The subscription exists during a run.** Entries that read the same topic share it. While
  idle the entry shows the last value it received.
* **Live values are not saved.** The blackboard file keeps the entry, its source and its
  declared value, but is not rewritten for every message.
* **An entry has an age.** Until the first message of a run arrives, the entry has *no data*.
  With *Max age* set, it becomes *stale* when its last message is older. Both mean the same to
  the tree: an Evaluation fails with "no recent data", or keeps waiting if **Wait** is on; a
  binding to it fails its leaf. The Blackboard tab greys such an entry out and shows its age.
* **A tree that depends on a source should start by waiting for it**: an Evaluation with Wait
  on and a timeout. The first tick usually comes before the first message.
* **A changed value requests a tick** ([5.4](#54-when-the-tree-ticks)). A message that repeats
  the value only renews the age. Going stale requests a tick too.
* **The age is measured on the executor**, from the moment a message arrives, not from a time
  stamp inside the message: clocks of different machines may disagree.

**How the subscription is made**

* **It adapts to the publisher.** Reliability and durability are taken from the publishers
  found with `get_publishers_info_by_topic`, as `ros2 topic echo` does; otherwise a mismatch
  delivers nothing, silently. A topic that keeps its last message for late joiners is
  subscribed in that way, so its value is there at once.
* **Without a publisher yet**, the subscription accepts anything (best effort, no history). It
  is checked again whenever the topic's publishers change, and made anew if their settings
  call for it. If ROS still reports the two as incompatible, the entry says so in the
  Blackboard tab and a WARNING is logged.
* **Only messages that are used are decoded.** The subscription is created with `raw=True`,
  and a message is decoded (`rclpy.serialization.deserialize_message`) only when *Max rate*
  allows the entry another update.
* **The latest message is never lost to the rate limit.** A message that arrives too soon is
  kept, undecoded, and applied when the entry may next be updated. A topic that sends `false`,
  then `true` a moment later, then nothing, leaves the entry at `true`.
* **Checked before Execute** ([8.3](#83-checking-a-bundle)): a topic that is not on the
  network (a warning), a type that differs from the topic's, an interface package that is not
  installed, a field that the type does not have, an entry type that does not fit.

**Limits**

* **Sensor streams do not belong here.** rclpy still handles every message of a subscribed
  topic. For a topic that is large or fast (images, point clouds, 100 Hz odometry), run a
  small node next to the data that publishes the answer, such as `obstacle_detected`, and feed
  that.
* **Sub-trees have no blackboard**, so sources belong to the tree that is executed. A Remote
  Sub-Tree's tree has its own blackboard and its own sources, read on its own executor.

### 7.5 Object entries

Object entries hold things like device handles. They exist only in the executor, are created
by leaf modules (a module-level `setup(blackboard)` hook runs when the executor starts), are
added to the blackboard of every run, are never sent or saved, and appear read-only in the
browser as their `repr`. A name used by an Object entry is taken on that executor: validation
reports a tree whose blackboard uses it too, and in a run the Object entry wins.

### 7.6 Threads and publishing

* **Threads.** Leaf code on worker threads reads and writes the blackboard, so the store is
  guarded by a lock, as in the widget. Change notices are delivered on the engine thread.
* **Publishing.** Changes are sent in batches at most 10 times a second, the latest value per
  entry, always in full: the gateway's copy and the blackboard file are made from them.
* **Long values are shortened only on the way to a browser.** The gateway sends a value whose
  text is longer than 64 KB as a preview marked `truncated`, and the whole value when the user
  opens it.

---

## 8. The executor node

### 8.1 Parameters

| Parameter | Default | Meaning |
|---|---|---|
| `leaf_modules` | `[]` | Python modules that define custom leaves. |
| `log_dir` | `~/.ros/bt_server/<name>/log` | Folder of the tree logs. `<name>` is the executor's full name with `/` replaced, such as `cell1_bt_executor`, so two executors on one host never share files. |
| `log_keep_days` | `30` | Tree logs older than this are deleted. |
| `max_tick_rate_hz` | `200` | Cap on ticks requested by events. |
| `status_rate_hz` | `30` | Cap on `~/status`. |
| `heartbeat_period_s` | `0.5` | Longest gap between two `~/status` messages. |
| `blackboard_rate_hz` | `10` | Cap on `~/blackboard` and `~/details`. |
| `journal_file` | `~/.ros/bt_server/<name>/goals.jsonl` | Record of goals in flight ([8.4](#84-the-goal-journal)). |
| `use_sim_time` | `false` | Standard ROS parameter; node timeouts then follow simulated time. |

### 8.2 The node

**The event loop.** One `rclpy.experimental.EventsExecutor`, single-threaded. The announcement
of the `EventsExecutor` reports as little as a tenth of the default executor's CPU use for some
workloads. The default `SingleThreadedExecutor` is a drop-in fallback. A multi-threaded
executor is not used: one thread is what keeps the engine free of locks.

**Timers.** The same announcement says that the `EventsExecutor` slows down with many ROS
timers in one process. The engine therefore keeps its deadlines in a list of its own and arms
one timer for the earliest, next to the timers of the periodic tick and the heartbeat.

**The action server.**

```python
ActionServer(
    node, ExecuteTree, "~/execute",
    goal_callback=self._on_goal,        # REJECT while busy, or when the bundle cannot be read;
                                        # on ACCEPT mark the executor busy at once
    cancel_callback=self._on_cancel,    # ACCEPT, and ask the runner to stop
    execute_callback=self._execute,
)

async def _execute(self, goal_handle):
    finished = rclpy.task.Future()                       # completed by the runner
    self._runner = TreeRunner(..., on_finished=finished.set_result)
    self._runner.start()
    outcome = await finished                             # no polling; ticks arrive as events
    ...                                                  # succeed() or abort(); canceled() only
    return result                                        # if goal_handle.is_cancel_requested
```

The executor is marked busy in `goal_callback`, not in `_execute`, which runs later: otherwise
two goals arriving together would both be accepted.

**Setting up a run.** Before the first tick the executor creates every action client, service
client, publisher and subscription the bundle needs, so the ROS network has time to connect
them before the leaves run. Nothing waits here: a leaf whose server is not connected yet looks
for it when it starts ([6.5](#65-the-ros-leaves)). Clients are kept between runs and dropped
when unused for 10 minutes; a re-run then starts without the discovery delay.

**Worker threads to the engine.** A finished thread leaf puts its result on a queue and triggers
a guard condition; the engine thread drains the queue. This is the rclpy form of
`Scheduler.call_soon`.

**The watchdog.** When a goal names a watchdog, the executor subscribes to that topic for the
length of the run. A message counts when it carries the named run id and a state other than
IDLE. When none has counted for the given time, the executor stops the run as Stop does.

**Shutting down.** On SIGINT or SIGTERM the executor stops a running tree as Stop does, waits
for the cancels up to the cancel timeout, and exits. It installs its own signal handlers for
this (`rclpy.init(signal_handler_options=SignalHandlerOptions.NO)`): rclpy's own shut ROS down
at once, and no cancel would leave the machine.

### 8.3 Checking a bundle

`~/validate` and the start of a run apply the same checks:

* unknown node types; a Negation without a child; a sub-tree that contains itself;
* a target that is not on the network now (a warning: it may come up later);
* a type that differs from the one the target offers; an interface package that is not
  installed on this host;
* field values that do not fit the interface; bindings that name missing entries; bindings
  and outputs whose entry has an unsuitable type;
* sources: see [7.4](#74-entries-fed-from-ros-topics);
* a Call Action or Remote Sub-Tree without a *Timeout*, and an Evaluation that waits without
  one (warnings).

Each finding has a node path, a severity and a text. Findings do not block Execute, as nodes
with problems simply fail in the widget; the browser shows them first and asks.

The tree of a Remote Sub-Tree node is checked by the executor that will run it: the gateway
sends that part of the bundle to the other executor's `~/validate` and merges the findings.

### 8.4 The goal journal

If the executor dies during a run, the actions it started keep running on the other machines.

* Each goal is appended to `journal_file` when it is sent (action name, goal id, time) and
  marked done when it settles: a result arrived, or the goal was rejected. A goal whose cancel
  was not confirmed stays in it ([5.6](#56-cancelling-is-a-state)).
* While the executor runs, it sends the cancel of every unsettled goal again every few seconds
  and when the goal's server reappears.
* When the executor starts, it cancels every goal the journal lists as unfinished. This needs
  no interface package: every action server offers the service
  `<action name>/_action/cancel_goal` of type `action_msgs/srv/CancelGoal`, which cancels one
  goal by id.
* Each executor has a journal of its own ([8.1](#81-parameters)). With a shared one, a starting
  executor would cancel the live goals of another.
* This is a clean-up, not a safety measure. Until the restart, only the other machine's own
  timeout stops it ([section 16](#16-failure-handling)).

### 8.5 The tree log

* The executor writes the log, because it is the source and must work without a gateway. Each
  run has a file of its own in `log_dir`:
  `<label>_<month>-<day>-<year>_<hour>-<minute>-<second>.log`, with the time the run started.
  The widget named its file after the time the application started; a service runs for months.
* The label comes from whoever sent the goal, so only letters, digits, `-`, `_` and `.` of it
  are kept; a label cannot name another folder.
* Files older than `log_keep_days` are deleted. At the default level a busy tree writes
  hundreds of lines a second; the tree's *Log level* option is the way to write fewer.
* The lines have the widget's format:

  ```text
  2026-11-13 15:04:07.414 INFO    Node 'Move To' (MoveTo) started: Running
  2026-11-13 15:04:07.918 INFO    Node 'patrol' > 'Check' (Evaluation) executed: Failed (no recent data)
  ```

* Levels and their numbers are the widget's (0 None, 1 ERROR, 2 WARNING, 3 DEBUG, 4 INFO, INFO
  being the most detailed). The tree's *Log level* option is the threshold.
* Every node that runs is logged at INFO when it starts, finishes, runs within one tick, is
  halted, and now also when its cancel is confirmed or times out.
* Logging never waits for the disk: records go to a queue that one background thread writes.
* ERROR and WARNING records also go to the ROS log (`/rosout`). The node-by-node INFO lines do
  not, so they do not flood it.
* The same records are published on `~/log` in batches, and `~/get_log` reads them back from
  the file by number.

---

## 9. The gateway

### 9.1 Process structure

* **aiohttp on asyncio** serves the static front end, the WebSocket and a few HTTP routes. One
  apt package (`python3-aiohttp`) covers all three.
* **With `RosLink`, an ordinary rclpy node spins on a second thread.** Messages from ROS are
  handed to asyncio with `loop.call_soon_threadsafe`; calls into ROS are queued to the ROS
  thread. All of this is inside `RosLink`; the rest of the gateway sees only `ExecutorLink`.
* rclpy has an experimental asyncio-based node (`AsyncNode`) that would remove the second
  thread. Its pull request does not list actions, and the gateway needs an action client for
  `~/execute`. Whether Lyrical ships it, and with what, is to be checked
  ([section 20.2](#202-to-verify-on-the-server)).
* **File writes run in a worker thread** (`asyncio.to_thread`), so a slow disk never delays a
  browser.

| HTTP route | Purpose |
|---|---|
| `GET /` and `GET /assets/…` | The page and its files. |
| `GET /ws` | The WebSocket. |
| `POST /upload` | A tree file, or its two files, into a folder of `trees_dir`. |
| `GET /download/<path>` | A tree's files, or a log. |

Renaming and deleting files go over the WebSocket, like the other file commands.

```python
class ExecutorLink(Protocol):
    async def describe(self, kind: str, arg: dict) -> dict: ...
    async def validate(self, bundle: dict, blackboard: list) -> list[Finding]: ...
    async def execute(self, bundle: dict, blackboard: list, label: str, requested_by: str) -> None: ...
    async def stop(self) -> None: ...
    async def pause(self, paused: bool) -> None: ...
    async def reset(self) -> None: ...
    async def apply(self, ops: list[dict]) -> ApplyResult: ...
    async def snapshot(self) -> Snapshot: ...
    def subscribe(self, listener: RunListener) -> None: ...   # status, blackboard, details, log
```

**Settings.** The gateway reads its settings from a TOML file, not from ROS parameters, so that
it also starts with `LocalLink` on a machine without ROS. Python's standard library reads TOML.

| Setting | Default | Meaning |
|---|---|---|
| `link` | `ros` | `ros` or `local` ([3.3](#33-processes)). |
| `http_host` | | The LAN address to listen on. |
| `http_port` | `80` | 443 with TLS. |
| `public_names` | `["bt.{hostname}.local"]` | The names the gateway answers to, besides its own address ([section 13](#13-security)). `{hostname}` stands for the server's short host name. |
| `executors` | `[]` | Executors that have a session even while they are down, such as `/cell1/bt_executor`. Others are added when found. |
| `allowed_networks` | `[]` | Address ranges that may connect, such as `192.168.1.0/24`; empty allows every address that reaches the port. |
| `trees_dir` | | The folder of all tree files. |
| `tls_cert`, `tls_key` | | Set both to serve HTTPS. |

### 9.2 Sessions

A **session** is the server-side counterpart of one `BehaviorTreeWidget`: one tree, its
blackboard, the sub-tree documents opened with Zoom In, and one executor.

* There is one session per executor. Every browser attached to that executor sees the same
  tree. New or Load replaces it for all of them, and both are refused while that executor is
  not IDLE.
* Shared: documents, blackboard, unsaved-changes state, the run.
* Per browser: pan, zoom, selection, which sub-tree is shown, the open dialog.
* **A document belongs to the gateway, not to a session.** Each tree file is in memory once.
  Two sessions that show the same sub-tree show the same document and see each other's edits.
* **A tree is the tree of one session at a time**, because its blackboard file and its run
  belong to that session. Loading it into a second session is refused, with the name of the
  executor that has it.
* **Go Back only changes what one browser shows.** It never discards: the sub-tree's unsaved
  changes stay on the server, where another browser may be working on them. Throwing changes
  away is a command of its own, **Revert**, which reloads the file for everyone and says so.
* A sub-tree document is closed when no browser shows it and it has no unsaved changes.
* The gateway remembers, in a small file in `trees_dir`, which tree each session has open and
  the id of the run it started. After a restart it opens the same trees and recognises its own
  runs.
* The gateway also keeps each executor's last catalog of node types on disk. While an executor
  is down, its tree can still be edited and saved; only Execute is unavailable.

### 9.3 Documents and edit operations

A document is a tree file in memory with a revision number. The browser changes it by sending
operations. The gateway checks each one, applies it, raises the revision and sends it to every
browser of the session.

| Operation | Notes |
|---|---|
| `node.add`, `node.remove` | The id is made by the browser (32 hex digits); the gateway rejects one in use. The Root cannot be removed. |
| `node.move` | A list of positions, plus the new child order of each parent whose order changed. |
| `node.set_title`, `node.set_field`, `node.set_selection`, `node.set_binding`, `node.set_output` | |
| `node.resize`, `node.set_text`, `node.set_color` | Notes and Groups, within the widget's size limits. |
| `composite.set_type`, `composite.set_memory`, `composite.set_policy` | Sequence, Selector or Parallel; Memory for the first two; the policy of a Parallel. |
| `conn.add`, `conn.remove` | No cycles. A node has one parent: connecting replaces the old one. A second child of a Negation replaces the first. |
| `paste` | Nodes and connections in tree-file form with positions; the gateway gives default-named groups their next number. |
| `group.promote` | Promote to Sub-Tree, done wholly by the gateway with the widget's rules and refusals. |
| `doc.set_config` | The tree's options. |
| `bb.add`, `bb.remove`, `bb.rename`, `bb.retype`, `bb.set_value`, `bb.set_reset`, `bb.move`, `bb.set_source` | During a run these are forwarded to the executor, except `bb.set_source`, which waits for the next run. |

* **The last write wins.** Operations are applied in the order they arrive. Two people editing
  the same field see the later value. Everyone edits the one copy on the server and sees every
  change at once, so nothing more elaborate is needed.
* **A browser applies every operation the gateway sends, in order, its own included.** It
  shows its own change at once, and applies it again when it comes back. Without that, a
  browser whose change arrived second would go on showing the first.
* **A rejected operation** (a cycle, a structural change during a run) is answered with the
  reason. The browser then asks for the document again and redraws.
* **A child order is accepted only if it is current.** A move names the revision it was worked
  out at. If the document has moved on, the gateway applies the positions, drops the order and
  asks that browser to work it out again.
* **Operations sent just before a connection broke** and never answered are not sent again.
  After the reconnect the browser shows the server's state and says which changes were lost.
* **The structure is locked while a tree executes**, as in the widget: no adding, removing,
  connecting, pasting, promoting or changing a composite, in the tree and in its sub-trees.
  Moving, renaming, editing fields, notes, groups and the blackboard stay possible.
* **During a run the executor holds the fields**, as it holds the blackboard. A field edit from
  a browser is sent to the executor (`~/apply`), which names the node by its tree and id, and
  the gateway changes the document only when the change comes back on `~/details`. Leaf code
  that sets a field or a title takes the same way back. Two writers can then never leave the
  two sides with different values. A leaf reads its fields when it starts.
* **Values written by running leaves** are not unsaved changes, as in the widget, but a later
  Save writes them. This holds for nodes of the tree itself. Inside a sub-tree they change
  that run only, as the widget's hidden sub-tree nodes did.
* **Collections are typed as Python literals** (`[1, 2.5, 'text']`, `{'key': 3}`, `{1, 2}`),
  as in the widget. The gateway refuses text longer than 1 MB or nested deeper than 100
  levels, parses the rest with `ast.literal_eval`, which evaluates literals only, and answers
  with the value or the error.

### 9.4 Files

* **All trees live under one folder**, the gateway's `trees_dir`. New, Load and Save show the
  server's files instead of a local file dialog. Every path is resolved and refused unless it
  lies inside `trees_dir`.
* **Upload and download** move trees between the browser's machine and the server. An upload
  is limited to 10 MB and is checked as a tree file before it is stored.
* **Names** follow the widget: `robot`, `robot.json`, `robot_BT.json` and `robot_BB.json` all
  mean the tree stored in `robot_BT.json` and `robot_BB.json`.
* **Sub-Tree file names from Windows.** The widget stores a Sub-Tree's file relative to its
  tree, with `\` on Windows, or as an absolute path when the file lies elsewhere. The gateway
  reads `\` as a separator. A path it cannot follow is reported when the tree is loaded, and
  the page offers to choose the file again.
* **Every write is atomic**: a temporary file in the same folder, flushed, then renamed over
  the old one.
* **The blackboard file is written on every change**, at most once every 250 ms, and never
  counts as an unsaved change. Values of entries with a source are not written
  ([7.4](#74-entries-fed-from-ros-topics)). If the write fails, the browsers are told once and
  the next change tries again.
* **Save** writes the document shown: the tree, or the sub-tree zoomed into, to its own file.
* **Unsaved changes live on the server**, so closing a tab loses nothing. New, Load and Go Back
  still ask (Save, Discard, Cancel) for each document with unsaved changes.
* **Recovery.** A document with unsaved changes is also written to `trees_dir/.recovery/`,
  under its own path, within 2 s of each change. After a gateway restart the page offers to restore it, so a
  crash costs at most the last two seconds of editing.

### 9.5 Starting a run

The click on Execute puts the session into a state of its own, **starting**, which lasts until
the goal is accepted or rejected or the user backs out. In it the structure is locked as
during a run and blackboard edits are held back ([7.2](#72-who-holds-it)), so what runs is
what was on the page at the click.

1. Build the bundle from the session's documents, reading from disk only the sub-tree files
   that are not open.
2. Ask the executor to validate it, and the other executors to validate the trees that Remote
   Sub-Tree nodes send them. With findings, show them and wait for the user's go-ahead.
3. Keep a copy of the blackboard that is about to be sent ([7.3](#73-resets-and-restore)).
4. Send the goal with the bundle, the blackboard and the address of the browser that asked.

* While a sub-tree is shown, Execute still runs the tree itself, as in the widget.
* A sub-tree that was not open at the start is, during the run, shown from the bundle, so the
  page shows what runs even if its file has changed since.

### 9.6 What the gateway keeps for late browsers

The latest status vector, the details per node, the mirrored blackboard and the last 10,000
log lines. A browser that connects in the middle of a run gets all of it in its first message,
with the newest 500 log lines; older lines are fetched as the user scrolls back, from the
gateway's 10,000 and beyond them from the executor's file (`~/get_log`).

---

## 10. The browser protocol

One WebSocket per tab, JSON text messages, each with a type `t`.

### 10.1 Messages

| From the browser | Purpose |
|---|---|
| `hello` `{protocol}` | First message. Answered with `welcome`, or with `reload` for a wrong protocol number. |
| `attach` `{executor}` | Join that executor's session. Answered with `snapshot`. |
| `op` `{cid, doc, rev, op}` | An edit operation ([9.3](#93-documents-and-edit-operations)). `cid` is the browser's own number for it, `rev` the revision it was made at. |
| `cmd` `{cid, name, confirm?}` | `execute`, `pause`, `resume`, `stop`, `reset`. An `execute` whose validation has findings is answered with `no` and the findings; sent again with `confirm`, it goes ahead. |
| `file` `{cid, action, …}` | `list`, `new`, `load`, `save`, `save_as`, `rename`, `delete`, `zoom_in`, `go_back`, `revert`, `recover`. A command that would lose unsaved changes, replace a file or need a missing sub-tree file is answered with `no` and the question; sent again with the user's choice, it goes ahead. |
| `ask` `{cid, kind, …}` | Read-only questions: `graph`, `interface`, `sample`, `parse_literal`, `entry_value`. |
| `watch` `{executor}`, `unwatch` | Also receive, read-only, the run of another executor: used while zoomed into a Remote Sub-Tree node. |
| `resync` `{doc}` | Send that document again. |

| From the gateway | Purpose |
|---|---|
| `welcome` `{executors, time}` | The executors known, for the picker, and the server's time. Sent again when the list changes. |
| `snapshot` | The whole session: documents, blackboard, options, node types, run state, the table of node paths, statuses, details, recent log. |
| `doc` `{doc, event}` | A document was opened, saved, reverted or closed, for the other browsers. |
| `catalog` `{exec, types}` | An executor's node types changed, or it came back with others. |
| `op` `{doc, rev, op, by, cid}` | An applied operation, to every browser of the session. |
| `ok` `{cid, …}` / `no` `{cid, reason}` | The answer to `op`, `cmd`, `file` and `ask`. |
| `st` `{exec, state, loop, cancelling, unsettled, v}` | The run state of executor `exec`, the counts the toolbar shows, and every node's status as one string with a digit per node, in the order of the table of node paths: `"v": "2210400"`. |
| `bb` `{changes}` | Changed, removed and reordered entries, with the age of fed ones. |
| `nd` `{exec, details}` | Per node: failure reason, what it waits for, latest feedback. |
| `log` `{lines, skipped?}` | Log lines as `[time, level, text]`. |
| `run` `{exec, …}` | A run started or ended, with who asked, the table of node paths and, at the end, the outcome. For a watched executor it carries the bundle, so the tab can draw the tree. |
| `notice` `{level, text}` | For example: the blackboard file could not be written. |
| `reload` | The page is older than the server; load it again. |

### 10.2 Delivery

* **The first message of a session is a full snapshot.** A reconnect gets a new one. A 300-node
  session is about 100 KB, small enough that replaying missed messages is not worth building.
* **Statuses and values are latest-value streams.** Each connection has a sender loop: wait
  until something changed, build one message from the *current* state, send it and wait for
  the socket to accept it, then pause until the next frame is due (33 ms). A slow browser gets
  fewer, current messages. Nothing queues up, so the gateway's memory per browser is bounded.
* **Statuses are sent whole.** A digit per node is 300 characters for 300 nodes, less than the
  names of a few changed nodes would take, and the gateway keeps no record per browser of what
  it sent.
* **The log is a queue**, sent in batches. If a browser falls more than 10,000 lines behind,
  the oldest are dropped and the next batch says how many were skipped; the browser can fetch
  them again.
* **Liveness.** The gateway sends a WebSocket ping every 20 s and closes a connection that does
  not answer. A browser that sees its connection close shows that it is offline and reconnects.
* **Dragging** moves the node in the dragging browser on every frame and sends positions at
  most 20 times a second and on release. Other browsers see the node follow.
* **Compression is off** (`permessage-deflate`). On a LAN it costs more CPU than it saves.
* **A message from a browser may be 4 MB at most**, which holds a paste of several thousand
  nodes. A longer one closes the connection.

### 10.3 Values on the wire

Two things keep a value from passing through a browser unharmed. Python writes `NaN` and
`Infinity` into the files, which are not JSON and which a browser's parser refuses. And
JavaScript has one kind of number, so `1.0` comes back as `1`, which the blackboard counts as
a changed value. The gateway therefore sends values in these forms:

| Value | In the files | To and from the browser |
|---|---|---|
| A whole number, a text, a Bool | as it is | as it is |
| A Double | a JSON number, or `NaN`, `Infinity`, `-Infinity` | its text: `"1.0"`, `"nan"`, `"inf"`. The gateway knows from the entry or field that it is a Double. |
| Integers beyond ±2⁵³ (64-bit ROS fields) | a JSON number | their text, because a JavaScript number would round them |
| Lists, dictionaries, sets, tuples | JSON, with tags such as `{"__set__": […]}` for what JSON lacks | their Python literal, as text: `"[1, 2.5, 'a']"`. The browser shows and edits the text and never rebuilds the value. |

### 10.4 Versions

The page and the gateway come from the same build. `index.html` is served with `no-cache` and
names its script and style files by content hash; those are served with a year-long cache
lifetime. A browser therefore loads a new build on its next visit, and `hello` catches a tab
that stayed open across an upgrade.

---

## 11. The browser application

### 11.1 Tools

* **TypeScript, built with Vite.** The server needs no Node.js: the build output is static
  files installed with the gateway package.
* **Svelte 5** for the interface. It compiles components to direct page updates instead of
  comparing virtual copies of the page, and the bundle stays small.
* **The canvas.** Start with Svelte Flow for pan, zoom, selection and edge plumbing, behind a
  component of our own (`Canvas`) so it can be replaced. The widget's canvas has rules of its
  own (groups that move what lies inside them, click-through groups, notes below nodes, order
  numbers on lines, exact paste placement). If more than a few of them have to be forced onto
  Svelte Flow, write the canvas layer directly; it is one transformed layer with positioned
  nodes and SVG paths, roughly 1,500 lines of code. Decide with a two-day trial before building
  on either ([section 20.1](#201-open-questions)).
* **Supported browsers** are desktop ones with a mouse and a keyboard: Chrome, Edge, Firefox
  and Safari. A phone or tablet can open the page and watch a run; editing needs a right
  button, a wheel and a Delete key, and is not designed for touch.

### 11.2 State

One store per kind of data, each updated only by protocol messages and local gestures:

| Store | Holds |
|---|---|
| `docs` | The session's documents: nodes by id, connections, options, revision. |
| `blackboard` | Entries in order, with value, type, Resets box, source, age. |
| `run` | State, loop count, status per instance path, details per instance path. |
| `log` | The latest lines, as a ring. |
| `catalog` | Node types and their fields, from the executor. |
| `executors` | The executors found, and which one this tab is attached to. |
| `view` | Per tab: pan, zoom, selection, the stack of sub-trees zoomed into. |

A tab that shows a sub-tree reads statuses under the instance path of the Sub-Tree node it
zoomed through, so the same file used by two Sub-Tree nodes shows the right one.

### 11.3 Drawing

* **One world layer holds everything**; pan and zoom set a single CSS `transform` on it. The
  browser's compositor applies it on the GPU without laying the page out again.
* **Nodes are HTML elements** placed with `transform: translate(x, y)`. Their spin boxes,
  dropdowns, check boxes and text fields are the browser's own, with focus, text selection,
  input methods and accessibility for free. A canvas or WebGL renderer would only pay off at
  thousands of nodes and would mean rebuilding every form control.
* **Connections are SVG paths** in the same layer. Only the paths attached to a moved node are
  recomputed. Each carries its order number.
* **A status change is one attribute**: `data-status="running"`, and CSS sets the colours.
  Changes are applied once per animation frame.
* **Stacking**, bottom to top: groups, notes, connection lines, nodes, as in the widget.
* **Guards for very large trees**: below about 40% zoom the field editors are hidden and nodes
  show title and status only; nodes outside the view are not drawn. Trees of a few hundred
  nodes need neither.
* **The inspector.** A node of a ROS leaf shows only the fields the user pinned. The whole form
  (a NavigateToPose goal has some twenty values) opens in a side panel when the node is
  selected. Bindings and outputs are edited there too.

### 11.4 Interaction

Gestures are the widget's. The table lists them with what a browser needs in addition.

| Action | Gesture | In a browser |
|---|---|---|
| Add a node | Right-click the canvas | The page's own menu replaces the browser's (`contextmenu` is cancelled on the canvas). |
| Add a connected node | Drag from a Children or Parent label and release over empty canvas | |
| Connect | Drag from a Children label to a Parent label, or the reverse. Red while dragging, green over a compatible label, blue when connected. | |
| Select | Click; Ctrl+click toggles; Shift+click adds; drag a rectangle; Ctrl+A; Esc | Ctrl+A is taken over only while the canvas has the focus, not while a field is edited. |
| Move | Drag a node; a group's title bar moves what lies completely inside it | |
| Delete | Delete or Backspace | Backspace must not trigger the browser's Back. |
| Copy and paste | Ctrl+C, Ctrl+V | Through the `copy` and `paste` events ([11.6](#116-copy-and-paste)). |
| Pan | Ctrl+drag, or drag with the middle button; the wheel scrolls | The middle button's auto-scroll, and on Linux its paste, are cancelled on the canvas. |
| Zoom | Ctrl+wheel, 0.2× to 3× | Ctrl+wheel normally zooms the whole page; the canvas cancels that with a non-passive `wheel` listener. |
| Composite type, Memory, Policy, Select Children, Rename, Zoom In | The node's right-click menu | |
| Notes | Click the text to edit; drag an edge or corner to resize | The text uses the largest font at which all of it fits, found by measuring, as in the widget. |
| Groups | Title bar: move, rename, menu (Select Contents, Border Color, Promote to Sub-Tree) | Only the title bar and edges take clicks; the inside lets them through (`pointer-events: none`). |

* **Child order** follows the canvas: left to right by the centre of each node, the higher node
  first on a tie. The browser that moves a node, or changes its size by an edit, works out the
  new order and sends it, because only a browser knows how wide a node is drawn. Other
  browsers take the order from the document, so two browsers with different fonts cannot
  disagree.
* **During a run** the numbers on the lines show the order the run uses; a move changes the
  order at the next Execute.
* **On a Mac, Cmd takes the place of Ctrl** in every gesture above. Ctrl+click there opens the
  context menu.
* **A Double has an editor of its own**: a text box that accepts numbers, `nan`, `inf` and
  `-inf`, and `,` as the decimal point, as the widget's does. The browser's number box accepts
  none of the three words.
* **The view saved in a tree file** is that of the browser that saves. A browser that opens
  the tree starts from it and then keeps its own.

### 11.5 Panels

* **Toolbar:** New, Load, Save, Go Back, Execute, Pause, Stop, Reset, Configure, Loop
  Execution, the executor picker, and the run state. During STOPPING it shows how many cancels
  are still outstanding. A small count shows how many browsers are attached.
* **Blackboard tab:** the widget's table (type, name, value, Resets, up and down arrows, Add
  Entry, Remove Entries, Select All), plus a **Source…** button per row and, for fed entries,
  the topic and the age ([7.4](#74-entries-fed-from-ros-topics)).
* **Log pane:** the latest lines with a level filter, following the end unless the user
  scrolled up.
* **Configure dialog:** the widget's options and the three new ones
  ([section 12](#12-files-and-compatibility)).
* **Findings list:** the results of validation, each linked to its node.
* **Node details:** the tooltip of a Status label shows the failure reason, the latest
  feedback, or what the node is waiting for.

### 11.6 Copy and paste

* Ctrl+C and Ctrl+V are handled in the `copy` and `paste` events, which give access to the
  clipboard without HTTPS. The asynchronous clipboard API (`navigator.clipboard`) is not used,
  because a page served over plain HTTP from a LAN address may not call it.
* The data is the widget's clipboard JSON (the `nodes` and `connections` of a tree file, with a
  format name) as plain text, so it pastes between tabs and between browsers.
* It does not paste into the Qt widget, which looks for its own clipboard type; a browser
  cannot write that type.
* The pasting browser places the copies with the widget's rule: the free area nearest to the
  originals, at least 20 pixels from every node, arrangement and connections kept.
* Whether every browser fires `copy` when no text is selected is to be tested
  ([section 20.2](#202-to-verify-on-the-server)).

---

## 12. Files and compatibility

The server reads and writes the widget's files: `<name>_BT.json` with
`"format": "behavior_tree_widget"` and `<name>_BB.json` with
`"format": "behavior_tree_widget_blackboard"`. Single-file trees of the widget's early versions
load as they do in the widget.

**Two format versions.** The widget refuses a file whose `version` is higher than the one it
knows, with a message that says so. The server uses that:

* **Version 1** is written for a file that uses nothing the widget lacks. Such a tree moves
  between the widget and the server in both directions without loss.
* **Version 2** is written for a file that uses any of the features below. The widget then
  refuses it, instead of running it differently or losing parts of it when it saves.
* The server reads both.

| Feature | In the file | Why the widget must not open it |
|---|---|---|
| Parallel | a node of type `Parallel` with `policy` | It would become an "(unknown type)" leaf and lose its children's connections. |
| Evaluation with an operator other than `==`, or with Wait | `Operator`, `Wait`, `WaitTimeout` in `fields` | It would run as a plain equality check: the tree would behave differently without any sign. |
| Call Action, Call Service, Publish, Remote Sub-Tree | nodes of those types | They would become placeholders; harmless, but the tree could not run. |
| Bindings and outputs | `bindings`, `outputs` on a node | They would be lost on save for node types the widget knows. |
| The cancel options, when not at their defaults | `wait_for_cancels`, `cancel_timeout_ms`, `on_unconfirmed_cancel` in `config`; `wait_for_cancels`, `cancel_timeout_ms` on a node | They would be lost on save. |
| Sources | `source` on an entry of the blackboard file (which then has version 2) | The widget would drop it at its next blackboard write, which happens at every change. |

Each file has its own version: a version 1 sub-tree can be used by a version 2 tree.

**Child order.** The widget works out the order of a composite's children from their positions
and writes each parent's connections in that order. The server makes the written order the
rule: *the order of a parent's connections in the file is the execution order.* Files written
by the widget already follow it.

The other direction has a gap. The widget, reading a file written by the server, ignores the
written order and works it out again from the centre of each node as Qt draws it. A browser
draws nodes with other widths, so the centres differ. Two siblings whose centres lie closer
together than that difference, for example two stacked at the same `x`, can come out in the
other order in the widget, which then saves its order. Three things limit this:

* When it saves a version 1 file, the gateway warns about siblings whose centres are closer
  than 40 pixels sideways, and names them.
* Spreading siblings sideways, as trees are normally drawn, avoids it.
* The real cure is a small change to the widget: use the written order when the file says so.
  It is listed in [section 20.1](#201-open-questions).

**Options.** `tick_interval_ms` (100), `repeat`, `restore_blackboard`, `default_memory` and
`log_level` keep their meaning. `tick_interval_ms` is now the period of the periodic tick and
the pause between loops ([5.4](#54-when-the-tree-ticks)). New: `wait_for_cancels` (true),
`cancel_timeout_ms` (5000) and `on_unconfirmed_cancel` (`"stop"`).

**Loading** follows the widget: both files are checked completely before the current tree is
replaced; unknown node types become placeholders; invalid connections and invalid entries are
dropped and reported.

---

## 13. Security

**There are no logins.** Everyone who can open the page can edit and save trees and can start
and stop runs. This is a decision for now, not an oversight; the design therefore depends on
who can reach the page, and on keeping web pages elsewhere from using a LAN browser as their
way in.

**Who can reach the page**

* **Bind to the LAN interface**, and open the port in the firewall for that interface only.
* **`allowed_networks`** narrows this to address ranges, for example the operator stations.
* **Browsers never join the ROS network**, and the gateway exposes only the tree API. A generic
  bridge such as rosbridge would by default let every browser publish to any topic and call any
  service.

**Pages from elsewhere.** A web page on the internet, opened in a browser on the LAN, can try
to talk to the gateway. Without logins nothing but these two checks stops it:

* **`Origin` is checked** on the WebSocket and on every HTTP request that changes something.
  Browsers do not apply the same-origin rule to WebSockets, and they always send the true
  origin of the page that opens one. Only the gateway's own origins are accepted. The gateway
  sends no headers that would allow other origins.
* **`Host` is checked.** A request is accepted when its `Host` header is one of the gateway's
  `public_names` (by default `bt.<hostname>.local`) or the address the gateway listens on;
  any other is refused. This stops a web site from reaching the gateway by pointing a name of
  its own at the server's LAN address: such a request carries that site's name. The plain
  address stays usable for machines that cannot look the name up.

**Input**

* **Paths.** Every file name from a browser is resolved and refused outside `trees_dir`.
* **Sizes.** Uploads, messages and literals have limits ([section 9](#9-the-gateway),
  [10.2](#102-delivery)).
* **Leaf modules are code.** Only the server's administrator names them, in a parameter file.
* **A tree file must not choose what the executor imports.** ROS type names in a tree lead to
  an import, so the executor accepts only the names of interface packages installed on its
  host ([6.5](#65-the-ros-leaves)).

**A record.** Every Execute, Stop, Reset and Save is written to the gateway's log with the
address of the browser and the time, and the address goes into the goal and the tree log. With
several people on one page, this answers "who stopped it".

**HTTPS is optional.** There are no passwords to protect. It still keeps others on the LAN from
altering the traffic, and it lifts the limits browsers put on plain-HTTP pages (the
asynchronous clipboard, `crypto.randomUUID`, service workers), none of which the design needs.
aiohttp can serve TLS itself from a certificate made with a local authority such as mkcert.

**The ROS network itself** has no access control by default: any machine in the same domain
can send goals to the executor or to the robots. Keep it on its own network segment, or enable
ROS 2 security (SROS2). No web-side measure, logins included, can make up for an open ROS
network.

**If logins are wanted later**, they fit in one place: every operation, command and file
request already passes through one function of the gateway that decides whether it is allowed.
Today it allows everything.

---

## 14. Deployment

### 14.1 Python and packages

* **The server uses Ubuntu's own Python** (3.14 expected on 26.04). rclpy is built for that
  interpreter, so a Python from uv, conda or another archive cannot import it.
* **Ubuntu refuses `pip install` into the system Python.** Dependencies come from apt, declared
  in each `package.xml` so that `rosdep` installs them. A virtual environment created with
  `--system-site-packages` is the alternative when a newer aiohttp is wanted.
* **Run-time dependencies:** ROS 2 Lyrical, `python3-aiohttp`. Nothing else: the engine and the
  protocol use the standard library.

```bash
# after adding the ROS 2 apt repository, as the Lyrical installation guide describes
sudo apt install ros-lyrical-ros-base ros-dev-tools python3-aiohttp
mkdir -p ~/bt_ws/src && cd ~/bt_ws/src && git clone <repository>
cd ~/bt_ws
source /opt/ros/lyrical/setup.bash
sudo rosdep init && rosdep update        # once per machine
rosdep install --from-paths src --ignore-src -y
colcon build --install-base /opt/bt_server/install
sudo useradd --system --create-home --home-dir /var/lib/bt_server btserver
```

* `/opt/bt_server` must be writable by the user who builds. `/etc/bt_server` belongs to root
  and is readable by `btserver`.
* **The built front end is part of the repository**, under `bt_server_gateway/static`, put
  there by the release build. That is why the server needs no Node.js: `colcon build` only
  copies it.

### 14.2 Starting it

Each process is a systemd service of its own. systemd then restarts each one separately, keeps
their logs apart, and gives only the gateway the right to use port 80. Both run as `btserver`,
a system user whose home folder is `/var/lib/bt_server`; ROS writes its own logs below the
home folder, so the user needs one.

```ini
# /etc/systemd/system/bt-executor.service
[Unit]
Description=Behavior Tree Server: executor
After=network-online.target
Wants=network-online.target

[Service]
User=btserver
StateDirectory=bt_server
LogsDirectory=bt_server
Environment=ROS_DOMAIN_ID=0
ExecStart=/bin/bash -c 'source /opt/bt_server/install/setup.bash && \
  exec ros2 run bt_server_executor executor --ros-args \
  -r __ns:=/cell1 --params-file /etc/bt_server/executor.yaml'
KillSignal=SIGINT
TimeoutStopSec=20
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
```

```ini
# /etc/systemd/system/bt-gateway.service
[Unit]
Description=Behavior Tree Server: gateway
After=network-online.target
Wants=network-online.target

[Service]
User=btserver
StateDirectory=bt_server
AmbientCapabilities=CAP_NET_BIND_SERVICE
Environment=ROS_DOMAIN_ID=0
ExecStart=/bin/bash -c 'source /opt/bt_server/install/setup.bash && \
  exec ros2 run bt_server_gateway gateway --config /etc/bt_server/gateway.toml'
KillSignal=SIGINT
TimeoutStopSec=10
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
```

```toml
# /etc/bt_server/gateway.toml
link = "ros"
http_host = "192.168.1.10"                # the LAN address, not 0.0.0.0
http_port = 80
public_names = ["bt.{hostname}.local"]    # {hostname} is filled in
trees_dir = "/var/lib/bt_server/trees"
```

```yaml
# /etc/bt_server/executor.yaml
/**:
  ros__parameters:
    leaf_modules: ["my_robot_leaves.leaves"]
    log_dir: /var/log/bt_server
    journal_file: /var/lib/bt_server/goals.jsonl
```

* `StateDirectory` and `LogsDirectory` make systemd create `/var/lib/bt_server` and
  `/var/log/bt_server` for that user.
* An executor on another machine needs only `bt-executor.service` there, with its own
  namespace.
* For several executors on one host, make the unit a template (`bt-executor@.service`) and use
  `%i` for the namespace and in the file names: `bt-executor@cell1`, `bt-executor@cell2`.
* For development, a launch file in `bt_server_bringup` starts both processes in one terminal.

### 14.3 The address

The page is served at **`http://bt.<hostname>.local`**, where `<hostname>` is the server's
short host name: `http://bt.tower.local` on a machine named `tower`. Avahi already answers for
`<hostname>.local`; the `bt.` name has to be added. The same host name appears below as `%l`,
which is how a systemd unit writes it, and as `{hostname}` in the gateway's settings.

**Publishing the name.** A small unit keeps one extra address record alive:

```ini
# /etc/systemd/system/bt-server-name.service
[Unit]
Description=Publish bt.<hostname>.local
Wants=avahi-daemon.service network-online.target
After=avahi-daemon.service network-online.target

[Service]
Environment=BT_ADDRESS=192.168.1.10
ExecStart=/usr/bin/avahi-publish -a -R bt.%l.local ${BT_ADDRESS}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

* `-a` publishes an address record. `-R` leaves out the reverse entry, which is what allows a
  second name for an address that `<hostname>.local` already has.
* `BT_ADDRESS` is the server's address on the LAN, the one browsers connect to. The server
  therefore needs a fixed address or a DHCP reservation.
* The record exists while `avahi-publish` runs. If Avahi restarts, `avahi-publish` exits and
  systemd starts it again.
* `avahi-publish` is in the package `avahi-utils`, which is not always installed with the Avahi
  daemon.
* `/etc/avahi/hosts` is not used: it is reported not to work for names with a sub-domain part.

**No port in the address.** The gateway listens on port 80 (443 with TLS). Its unit grants that
with `AmbientCapabilities=CAP_NET_BIND_SERVICE` instead of running as root.

**If port 80 is taken** by a web server already on the machine, give that server a virtual host
for `bt.<hostname>.local` that forwards to the gateway on `127.0.0.1:8080`, WebSocket upgrade
included, and set `http_host` and `http_port` to match. The web server must pass the `Host`
and `Origin` headers on unchanged, or the gateway's checks refuse the request; nginx, for one,
replaces `Host` unless told otherwise.

**Which machines can use the name.** `bt.<hostname>.local` has three parts, and not every
system looks such a name up:

| Browser's system | `bt.<hostname>.local` |
|---|---|
| macOS, iOS | Resolves: every name ending in `.local` goes to multicast DNS. |
| Windows 10 and 11 | Has a built-in resolver for `.local` names. A name with a sub-domain part was not confirmed; test with `ping bt.<hostname>.local`. |
| Linux with Avahi's usual setup | **Does not resolve.** The standard module (`mdns4_minimal`) rejects `.local` names with more than two parts. Per machine: use `mdns4` in place of `mdns4_minimal` in `/etc/nsswitch.conf`, and create `/etc/mdns.allow` with the two lines `.local.` and `.local`. Check with `getent hosts bt.<hostname>.local`. |
| Android | Not confirmed. |

* The server is such a Linux machine. To open the page in a browser on the server itself,
  apply the fix there or add the name to `/etc/hosts`.
* A machine that cannot resolve the name can open the server's address instead,
  `http://192.168.1.10`, which the gateway always answers to. Or add a line to its hosts file,
  or add `<hostname>.local` to `public_names`.

### 14.4 The host

**The network.** The design assumes the server has two network interfaces: one on the LAN of
the browsing machines, one on the network of the robots. The gateway listens on the first and
ROS uses the second. Then no browsing machine is on the ROS network, and a PC on the LAN that
happens to have ROS installed cannot send goals to the robots.

```bash
# with ufw on and incoming traffic denied by default
sudo ufw allow in on enp3s0 to any port 80 proto tcp     # the page, LAN interface only
sudo ufw allow in on enp3s0 to any port 5353 proto udp   # Avahi, so the name can be looked up
sudo ufw allow in on enp4s0                              # ROS, robot interface: all traffic
sudo apt install chrony                                  # keep clocks in step with the other machines
```

* **ROS must be let through the firewall.** It uses many UDP ports, chosen from the domain
  number upward from 7400. Opening only port 80 would cut the executor off from the robots.
* **Keep ROS off the LAN interface.** By default the middleware announces itself on every
  interface. Limit it to the robot interface in the middleware's own settings (for Fast DDS,
  an interface whitelist in its profile file), and Avahi to the LAN interface
  (`allow-interfaces` in `avahi-daemon.conf`).
* **With one network for everything**, all of this still works, but every machine on it that
  runs ROS can command the executor and the robots ([section 13](#13-security)).
* **ROS domain.** The executor needs the same `ROS_DOMAIN_ID` and middleware as the machines it
  commands. Keep whatever they already use; nothing here depends on the choice.
* **Discovery.** ROS 2 finds other machines with multicast by default, which wireless networks
  and routed subnets often block. Put the server on the wired robot network, or use the
  middleware's discovery server.
* **Clocks.** Time stamps from different machines can only be compared when their clocks are
  synchronised. The tree log orders events by the executor's own clock.
* **arm64.** Nothing here is built for one processor type, so an executor also runs on an
  arm64 robot computer.

### 14.5 Developing on Windows

The Ubuntu ROS packages are not available on Windows.

* `bt_server_core`, the gateway with `LocalLink`, and the front end have no ROS imports and run
  and test on Windows.
* The executor and `RosLink` run and test in WSL 2 with Ubuntu 26.04, in a Lyrical container,
  or on the server.

---

## 15. Performance

**Principles**

* Do work when something changes, not on a timer: event-driven ticks, statuses published on
  change, pages redrawn per animation frame.
* Send the latest value, not every value: one status message per frame at most.
* Keep fast data out of Python: no sensor streams in the executor; decode only the messages
  that are used.
* Let the browser's compositor move the picture: one transform for pan and zoom, one attribute
  per status change.
* Do not compress on a LAN.

**Budgets.** These are targets to measure against, not measurements.

| Path | Target | Why it should hold |
|---|---|---|
| One tick of a 300-node tree | under 1 ms | A tick walks the active paths, not the tree. |
| Leaf finished to next leaf started | under 5 ms | The result triggers a tick; no timer is waited for. |
| Source message to a waiting Evaluation's result | under 10 ms | The change triggers a tick. |
| Status change to pixels in the browser | under 100 ms for 95% of changes | 33 ms frame period, a LAN hop, one animation frame. |
| Pan and zoom | 60 frames a second with 500 nodes | Compositor only. |
| Executor CPU, tree waiting on one action | under 1% of a core | Ten periodic ticks a second and a heartbeat. |
| Gateway memory per browser | bounded | Latest-value frames and at most 10,000 log lines. |
| Opening a 300-node tree in a new tab | under 1 s | One 100 KB message and one render. |

**What to measure first:** the cost of one rclpy callback with each executor; the cost of
publishing `TreeStatus` at 30 Hz; the cost of a raw subscription per message; tick time against
tree size; browser frame time while 50 statuses change per second; time to first paint against
node count.

**When this design is the wrong tool:** when conditions must be re-checked more than about 100
times a second, or when the tree needs data that only exists as a sensor stream. The first is
BehaviorTree.CPP's field; a web editor for it would read and write its XML instead. The second
needs a node next to the data.

---

## 16. Failure handling

| What fails | What happens | What the design does |
|---|---|---|
| A browser tab closes, or its network drops | The run continues. | A reconnect gets a full snapshot. Unsaved edits are on the server. |
| A browser cannot look up `bt.<hostname>.local` | The page does not open. | Depends on the browser's system ([14.3](#143-the-address)); the server's address or `<hostname>.local` can be added to `public_names`. |
| Two people press Execute | The second goal is rejected. | The second browser is told which address started the run. |
| The gateway crashes | The run continues. | systemd restarts it. It opens the same tree, recognises its run on `~/run`, takes a snapshot and offers the recovery files. |
| The executor crashes | Actions it started keep running on the other machines. | The gateway shows "executor lost" when `~/status` stops. systemd restarts the executor, and the goal journal cancels the leftovers. The run is not resumed. |
| The server loses power | The gateway and the executor are gone; actions that were started keep running. | When it is back, both restart as in the two rows above. Until then the other machines must stop by themselves. |
| The services are stopped, or the server is shut down properly | The executor gets a signal. | It stops the running tree as Stop does, waits for the cancels within the time systemd allows, and exits. The gateway writes the blackboard and the recovery files first. |
| An action server is not up | The leaf fails with "server not available" after 2 s. | Validation warns before Execute. |
| A goal is never answered | The leaf fails after 5 s. | A goal accepted late is cancelled. |
| An action never finishes | The leaf's *Timeout* cancels it and fails the node. | Without a *Timeout* the tree waits, as asked; validation warns about actions without one. |
| A machine dies during its action | No result will come. | *Feedback timeout* fails the leaf with "no feedback". Without one, "server lost" follows when ROS discovery gives up on the machine, about 20 s later with the usual settings. |
| A cancel is refused or never confirmed | The leaf stays Cancelling. | After the cancel timeout: logged at ERROR, and the tree stops, or continues if the option says so. The goal stays on the list of unsettled goals and its cancel is sent again. Other branches of a Parallel are not held up meanwhile. |
| A service never replies | The leaf fails after its *Timeout*. | The pending request is dropped. |
| A source's topic stops, or never starts | Its entry is stale, or has no data. | Checks fail with "no recent data"; an Evaluation that waits keeps waiting until its timeout. |
| Another executor is busy | A Remote Sub-Tree's goal is rejected. | The node fails with "executor busy". |
| Another executor, or the network to it, fails during a remote tree | No result will come. | The node fails with "executor lost" after 3 s of silence. The remote tree stops itself after its watchdog time, or runs on if that is 0. |
| The disk is full or read-only | The blackboard file, the recovery files, the tree log or the goal journal cannot be written. | A notice in the page, once; each is retried at its next change. A Save reports its error and keeps the document unsaved. The run goes on. |
| Leaf code raises | That node fails. | Reason in the details and the log. |
| The tick itself raises | The run ends with the outcome *error*. | The Root shows the error. |

**What the server cannot do.** If the executor, its host or the network to a machine fails,
nothing here can stop that machine. Every action server that moves something needs its own
rule: stop when no command or heartbeat has arrived for a set time. `~/status` is published at
least every 0.5 s for exactly this use, and the executor applies the same rule to itself for
remote trees. A Stop button on a web page is a convenience on top of that rule, never a
substitute.

---

## 17. Testing

The table says what is tested and where. [Appendix B](#appendix-b-test-cases) lists concrete
cases, each with its input and the output it must produce.

| Part | How | Runs on |
|---|---|---|
| Engine | Unit tests with a fake clock and scripted leaves: every node kind, memory on and off, both Parallel policies, halting, which starts wait for which cancels, cancels that are never confirmed, timeouts, loops, Resets, every command in every run state. | Anywhere |
| Engine against py_trees | Random trees of Sequence, Selector, Negation and Parallel (both policies) with instant leaves, ticked in both; after every tick each node's result must match, with py_trees's "invalid" read as Ready. Checked while writing: py_trees gives the statuses of cases E-01 to E-15 of Appendix B, except E-09, which it cannot build. py_trees is a test dependency only. | Anywhere |
| Evaluation | Every operator against every pair of types; Wait with entries that appear, change, go stale and time out. | Anywhere |
| Document model | The widget's own test files (`tests/data/`) load, save and load again unchanged; child order; bundling; Promote to Sub-Tree; which files get version 2. | Anywhere |
| ROS leaves | Against scripted action servers that accept, reject, abort, hang, die, cancel slowly and refuse to cancel; services that never reply. | Lyrical |
| Sources | Topics of each field type; publishers with each delivery setting, present before and after the run starts; rates above the limit; topics that stop. | Lyrical |
| Executor node | The tree API end to end: run, pause, stop, snapshot after a gap, a second goal, restart with a journal, a watchdog that falls silent. | Lyrical |
| Two executors | Remote Sub-Tree: values in and out, halt, a busy executor, a killed executor on either side. | Lyrical |
| Gateway | aiohttp's test client against `LocalLink`: every operation and its refusals, `Origin` and `Host` checks, two browsers editing the same field and moving siblings at once, edits that cross the start and the end of a run, reconnect, a slow browser, a failed file write, a restart. | Anywhere |
| Front end | Unit tests of the stores; browser tests (Playwright) that draw, connect, copy, paste, run and watch a tree against a real gateway. | Anywhere |
| Whole system | The services with scripted machines; a browser test on top. | Lyrical |
| Performance | The measurements of [section 15](#15-performance), kept as tests with thresholds once the numbers are known. | Both |

---

## 18. Build order

Each step ends in something that can be used and tested. Sizes are relative: S small, M
medium, L large.

| Step | Result | Size |
|---|---|---|
| 1. Core | Document model that reads and writes the widget's files; engine with the widget's node kinds, Parallel and the cancelling lifecycle; Evaluation with operators and Wait; blackboard; compatibility leaf class. Tested against py_trees. | L |
| 2. Gateway and editor without ROS | aiohttp gateway with `LocalLink`; the canvas, blackboard tab, log pane, files, execution. This is the widget's feature set in a browser, plus Parallel and the new Evaluation, on any machine. | L |
| 3. Interfaces and executor node | The tree API; `RosLink`; two services. Trees built from non-ROS leaves run under ROS. | M |
| 4. ROS leaves | Call Action, Call Service, Publish; the list of what is on the network; forms from interface definitions; bindings and outputs; the inspector. | L |
| 5. ROS to blackboard | Sources: the dialog with Test, subscriptions, ages, and their checks. | M |
| 6. Failures | Deadlines and liveness; validation before Execute; the goal journal; STOPPING in the page. | M |
| 7. Several executors | The picker and a session per executor; Remote Sub-Tree with its watchdog and its view in the page. | M |
| 8. Service | The systemd units, the published name, recovery files, optional HTTPS. | S |

Step 2 is worth finishing before any ROS work: it proves the protocol, the document model and
the browser, and every later step is tested through it.

In step 3, create the package skeletons with `ros2 pkg create` on the server, so their build
files match what Lyrical expects.

---

## 19. Feature map and differences from the widget

| Widget | Server |
|---|---|
| Canvas: pan, zoom 0.2× to 3×, select, move, connect, order numbers | The same gestures in the browser ([11.4](#114-interaction)). |
| Root, Sequence, Selector, Memory, Negation | The same behaviour ([5.2](#52-what-each-node-does-when-ticked)). |
| Evaluation | The same by default; more operators and Wait ([6.3](#63-evaluation)). |
| Set, Print Message, UDP Message | The same. They run in the executor. Set cannot write an entry that has a source. |
| Restart, Pause and Stop nodes | The same; Stop and Restart wait for cancels. |
| Sub-Tree, Zoom In, Go Back | The same. Each browser zooms on its own; the documents are shared. |
| Notes, Groups, Promote to Sub-Tree | The same; the gateway does the promotion. |
| Custom leaves (`LeafNodeWidget`) | A compatibility base class ([6.6](#66-custom-leaves)). `UI_FILE` is not supported. |
| A leaf object lives as long as its node | Leaf objects are made anew for each run; state kept on `self` between runs must move to the blackboard. |
| A blocking `RUN_IN_THREAD = False` leaf freezes the window | It stops the ticks, the heartbeat and every reply of the executor. |
| An application's own leaf type may be named `Notes`, `UDP Message`, `Sub-Tree`, `Group` or `Print Message` | Built-in names are reserved. |
| Copy and paste, also between applications | Between tabs, browsers and machines; not with the Qt widget. |
| Blackboard tab, types, Resets, order | The same, plus sources. |
| Object entries | Exist in the executor only; read-only in the browser. |
| Blackboard file written at every change, at most every 250 ms | The same, by the gateway; values of fed entries are not written. |
| Execute, Pause, Stop, Reset, Configure, Loop Execution | The same, plus the STOPPING phase. |
| Statuses Ready, Running, Succeeded, Failed | Plus Cancelling. |
| Stop sets every node to Ready at once | A node whose work on another machine is still being cancelled shows Cancelling first. Leaves written for the widget go to Ready at once, as there. |
| Tick interval | The periodic tick; other ticks are driven by events, so a node may be ticked more often than once per interval. Engine-thread leaves written for the widget are still called once per interval. |
| Loading another tree interrupts a run | New and Load are refused while a tree runs. |
| Go Back offers to discard a sub-tree's unsaved changes | Go Back only changes the view; Revert discards, for everyone. |
| `THREAD_WAIT`, `TICK_WAIT_BUDGET` | Gone. A finished call triggers a tick at once. A quick thread leaf is logged as started and finished, not as executed. |
| The log: file, levels, line format | The same, written by the executor. Editor events (loaded, saved, zoomed) go to the gateway's log, not the tree's. |
| New, Load, Save with file dialogs; two files per tree | Server-side file lists, upload and download; the same two files, in format version 1 or 2 ([section 12](#12-files-and-compatibility)). |
| Prompts for unsaved changes when closing | Closing a tab loses nothing; prompts remain for New, Load and Go Back. |
| The structure is locked while executing | The same, enforced by the gateway. |
| Qt signals (`nodeStatusChanged`, `executionFinished`, …) | Protocol messages for browsers; ROS topics and the action result for programs. |
| Python methods (`AddNode`, `Connect`, `SetEntry`, …) | The document model of `bt_server_core` builds tree files from code; the tree API runs them. |
| Several widgets in one application | Several executors, one session each. |

**New in the server**

* The Parallel node ([5.3](#53-parallel)).
* Evaluation with operators and Wait ([6.3](#63-evaluation)).
* ROS to blackboard: entries fed from topics, with ages ([7.4](#74-entries-fed-from-ros-topics)),
  and outputs of actions and services ([6.4](#64-fields-bindings-and-outputs)).
* Several executors, and trees that run on another executor
  ([3.4](#34-several-executors), [6.8](#68-remote-sub-tree)).
* Call Action, Call Service and Publish, with field bindings ([6.5](#65-the-ros-leaves)).
* The Cancelling status, and starts that wait for the cancels before them
  ([5.6](#56-cancelling-is-a-state)).
* Validation before Execute; several people on one page; runs that outlive the page; recovery
  of unsaved changes.

---

## 20. Open questions and things to verify

### 20.1 Open questions

1. **Svelte Flow or a canvas layer of our own.** A two-day trial with groups, notes, order
   numbers and paste placement decides ([11.1](#111-tools)).
2. **One session per executor.** Simple, and the widget's model. If several people must edit
   different trees at once without an executor each, sessions need to be separated from
   executors.
3. **Logins.** Left out for now ([section 13](#13-security)). The place for them exists.
4. **Undo and redo.** The widget has none. The operation log would make it possible: each
   operation has an inverse. Not planned.
5. **The watchdog's default.** A Remote Sub-Tree stops its remote tree after 5 s of silence
   unless told otherwise. On a poor wireless link that may stop trees that should have run on;
   0 as the default would do the opposite harm.
6. **More Parallel policies.** Two are common elsewhere: "succeed when N of the children did",
   and "fail only when every child has failed", which lets one child carry on after another
   failed. All and Any, with py_trees's rule, cover the cases named so far.
7. **Sources while idle.** Entries are fed only during a run; the Test button shows one value
   on demand. Feeding them all the time would need the executor to hold a blackboard between
   runs.
8. **A change to the widget for child order.** If the widget used the written order of
   connections when a file says so, trees would keep their order in both directions
   ([section 12](#12-files-and-compatibility)). It is a small change, but it is a change to the
   widget, which this design otherwise leaves alone.
9. **Typed or JSON messages for the tree API.** The design uses JSON inside typed envelopes
   ([4.2](#42-rules-behind-the-api)). Fully typed blackboard values would serve other ROS tools
   better and cost a much larger interface package.
10. **The shape of the name.** `bt.<hostname>.local` has three parts, which Linux machines with
    the usual Avahi setup do not look up ([14.3](#143-the-address)). A two-part name such as
    `bt-<hostname>.local` would resolve on every system, with the same publishing unit. Which
    one to use depends on what the browsing machines run.

### 20.2 To verify on the server

These were assumed from documentation and announcements and not checked on a machine.

| Assumption | Check |
|---|---|
| Ubuntu 26.04's `python3` is 3.14 (one source said 3.13). | `python3 --version` |
| `rclpy.experimental.EventsExecutor` exists in Lyrical. | `python3 -c "from rclpy.experimental import EventsExecutor"` |
| An `async` action `execute_callback`, and an `async` service callback, that await an `rclpy.task.Future` work under the `EventsExecutor`. | A ten-line test node. |
| `python3-aiohttp` is in the Ubuntu archive and known to `rosdep`. | `apt-cache policy python3-aiohttp`; `rosdep resolve python3-aiohttp` |
| Whether Lyrical ships `AsyncNode` (rclpy's asyncio node), and whether it has an action client. If it has, the gateway needs no second thread. | The rclpy documentation for Lyrical. |
| `rclpy.init(signal_handler_options=SignalHandlerOptions.NO)` leaves SIGINT and SIGTERM to the program. | A ten-line test node. |
| A machine that vanishes is dropped from discovery after about 20 s with Fast DDS's defaults. | Unplug a test machine during an action and time "server lost". |
| ufw with the three rules of [14.4](#144-the-host) lets ROS discovery and data through. | `ros2 topic list` on the server with ufw on. |
| nginx, if it is used in front, needs `proxy_set_header Host $host` to pass the name on. | A request through it, with the gateway's log open. |
| `~/execute` expands to the node's private name for an action server. | `ros2 action list` with the executor running. |
| `avahi-publish` is installed (package `avahi-utils`). | `which avahi-publish` |
| Windows looks up a three-part `.local` name. | `ping bt.<hostname>.local` on a Windows machine, with the publishing unit running. |
| Every target browser fires the `copy` event on Ctrl+C when no text is selected. | Try it in Chrome, Firefox, Edge and Safari. |
| An official Lyrical container image exists for tests. | The registry. |
| Svelte Flow's current version covers what [11.1](#111-tools) asks of it. | The two-day trial. |

---

## Appendix A: interface definitions

```
# action/ExecuteTree.action
string  bundle_json           # the tree and its sub-trees (4.3)
string  blackboard_json       # entries in the form of the blackboard file; "" for none
string  label                 # the tree's name, for logs and observers
string  requested_by          # who asked: a browser's address, "command line", an executor's name
string  watchdog_topic        # "" for none; otherwise the TreeStatus topic of the run that started this one
unique_identifier_msgs/UUID watchdog_run_id   # the id of that run
float32 watchdog_timeout_s    # stop when the topic has not shown that run as going for this long
---
uint8 OUTCOME_SUCCEEDED = 1   # the Root succeeded                  (goal succeeded)
uint8 OUTCOME_FAILED    = 2   # the Root failed                     (goal aborted)
uint8 OUTCOME_STOPPED   = 3   # Stop                                (goal canceled)
                              # a Stop node, or a silent watchdog   (goal aborted)
uint8 OUTCOME_ERROR     = 4   # the tick failed                     (goal aborted)
uint8  outcome
string message
uint32 loops_completed
uint32 unconfirmed_cancels
string blackboard_json        # the final values
---
uint8  state                  # as TreeStatus.state
uint32 loop_count
```

```
# msg/RunInfo.msg
builtin_interfaces/Time stamp
unique_identifier_msgs/UUID run_id     # the goal id; all zero when nothing is loaded (sent at start-up)
string   label
string   requested_by
string   bundle_json
string[] node_paths                    # instance path of each node; index = position in node_status
```

```
# msg/TreeStatus.msg
uint8 STATE_IDLE       = 0
uint8 STATE_RUNNING    = 1
uint8 STATE_PAUSED     = 2
uint8 STATE_STOPPING   = 3
uint8 STATE_RESTARTING = 4

uint8 NODE_READY      = 0
uint8 NODE_RUNNING    = 1
uint8 NODE_SUCCEEDED  = 2
uint8 NODE_FAILED     = 3
uint8 NODE_CANCELLING = 4

builtin_interfaces/Time stamp
unique_identifier_msgs/UUID run_id
uint64  seq
uint8   state
uint32  loop_count
uint32  cancelling                     # leaves waiting for a cancel to be confirmed
uint32  unsettled                      # goals whose cancel was never confirmed (5.6)
uint8   outcome                        # of the last finished run, as in ExecuteTree; 0 while one runs or before the first
string  message
uint8[] node_status                    # one byte per node, in the order of RunInfo.node_paths
```

```
# msg/BlackboardUpdate.msg
unique_identifier_msgs/UUID run_id
uint64 seq
bool   full                            # true: every entry (at the start of a run)
string update_json                     # {"entries": [...], "removed": [...], "order": [...]}; entries
                                       # and removals are named by entry id, so a rename is an entry
                                       # with a known id and a new name; values are never shortened
```

```
# msg/NodeDetails.msg
unique_identifier_msgs/UUID run_id
uint64 seq
string details_json                    # [{"i": 12, "kind": "error" | "feedback" | "waiting" |
                                       #   "field" | "title" | "remote_run", "value": ..., "stamp": ...}, ...]
```

```
# msg/LogRecord.msg
builtin_interfaces/Time stamp
uint8  level                           # 1 ERROR, 2 WARNING, 3 DEBUG, 4 INFO
int32  node_index                      # -1: not about a node
string text

# msg/LogBatch.msg
unique_identifier_msgs/UUID run_id
uint64 seq
LogRecord[] records
```

```
# srv/GetSnapshot.srv
---
RunInfo    run
TreeStatus status
uint64     blackboard_seq
string     blackboard_json
uint64     details_seq
string     details_json
uint64     log_seq                     # the number of the latest log record; the records come from GetLog

# srv/GetLog.srv
unique_identifier_msgs/UUID run_id     # all zero: the current or last run
uint64 from_seq
uint32 max_records
---
LogRecord[] records
uint64 next_seq                        # equal to from_seq + the number returned; the end is reached when nothing comes back

# srv/Apply.srv
string ops_json                        # [{"op": "bb.set_value", ...}, {"op": "doc.set_config", ...},
                                       #  {"op": "node.set_field", "tree": "<key>", "id": "<node id>", ...}]
---
uint8 APPLIED  = 0
uint8 NO_RUN   = 1                     # no run is active: the caller holds the blackboard and applies the change itself
uint8 REJECTED = 2
uint8  result
string message

# srv/Describe.srv
string kind                            # "node_types" | "graph" | "interface" | "sample"
string arg_json                        # e.g. {"type": "nav2_msgs/action/NavigateToPose"}
---
bool   ok
string result_json

# srv/Validate.srv
string bundle_json
string blackboard_json
---
bool   ok                              # false: the bundle could not be read at all
string findings_json                   # [{"path": "...", "severity": "error" | "warning", "text": "..."}]
```

---

## Appendix B: test cases

Each case states what goes in and what must come out. The cases follow from the rules of this
document; where a case and a section disagree, one of them is wrong, and the disagreement is
to be settled before the code is. Section numbers in the group headings say which rules a
group exercises.

### B.1 How to read the cases

* **Time.** Times are seconds on a clock the test controls. A run starts at 0 with its first
  tick. The tick interval is 1 s, so periodic ticks come at 1, 2, 3 and so on.
* **Options** are at their defaults unless a case says otherwise: Memory on, run once, *Wait
  for cancelled actions* on, *Cancel timeout* 5 s, *When a cancel is not confirmed* stop the
  tree.
* **Trees** are written on one line. The outermost node is the Root.

  | Written | Meaning |
  |---|---|
  | `Seq[…]`, `Sel[…]` | Sequence, Selector, with Memory on |
  | `Seq~[…]`, `Sel~[…]` | the same with Memory off |
  | `Neg[…]` | Negation |
  | `All[…]`, `Any[…]` | Parallel with that policy |

* **Leaves** are test doubles:

  | Written | Meaning |
  |---|---|
  | `s`, `f` | an instant leaf that succeeds, that fails |
  | `n` | an instant leaf that succeeds and counts how often it is called |
  | `c` | an Evaluation of `go == true`, Wait off, on the Bool entry `go`. A case changes `go` from outside the tree, as a browser would, which requests a tick. |
  | `W` | an Evaluation of `battery >= 20.0` with Wait on; `battery` is a Double entry that starts at 17.3 |
  | `A`, `B`, `D`, `H` | started leaves. Unless a case says otherwise, a started leaf never finishes by itself and confirms a cancel 0.2 s after it is sent. "A: S after 1" means A succeeds 1 s after it starts; "F after 1" that it fails. |

* **Statuses** are one letter per node, in the order the tree is written: `-` Ready, `R`
  Running, `S` Succeeded, `F` Failed, `C` Cancelling. For `Seq[s, f, s]` the string `F S F -`
  says: Root Failed, first leaf Succeeded, second Failed, third Ready.
* **Timelines** read "time: statuses", and give every moment at which something changes.

### B.2 Composites with instant leaves (5.2, 5.3)

One tick at 0 decides each of these. They run anywhere. py_trees gives the same statuses for
E-01 to E-15, which was checked by running it; it cannot build E-09, a Negation without a
child.

| # | Tree | Statuses after the tick | Run |
|---|---|---|---|
| E-01 | `Seq[s, s, s]` | `S S S S` | Succeeded |
| E-02 | `Seq[s, f, s]` | `F S F -` | Failed |
| E-03 | `Sel[f, s, s]` | `S F S -` | Succeeded |
| E-04 | `Sel[f, f]` | `F F F` | Failed |
| E-05 | `Seq[]` | `S` | Succeeded |
| E-06 | `Sel[]` | `F` | Failed |
| E-07 | `Seq[Neg[f], s]` | `S S F S` | Succeeded |
| E-08 | `Seq[Neg[s], s]` | `F F S -` | Failed |
| E-09 | `Seq[Neg[], s]` | `F F -` | Failed; the Negation's details say that it has no child |
| E-10 | `Seq[All[s, s], s]` | `S S S S S` | Succeeded |
| E-11 | `Seq[All[s, f, s], s]` | `F F S F S -` | Failed; the third child of the Parallel was still ticked |
| E-12 | `Seq[Any[f, s, s], s]` | `F F F S S -` | Failed: a failure wins over the successes of the same tick |
| E-13 | `Seq[Any[f, f], s]` | `F F F F -` | Failed |
| E-14 | `Seq[All[], s]` | `S S S` | Succeeded |
| E-15 | `Seq[Any[], s]` | `S S S` | Succeeded |
| E-16 | `Seq[x, s]`, where the leaf `x` raises an exception | `F F -` | Failed, with the outcome *failed*, not *error*. The details of `x` and an ERROR line of the log hold the exception's text. |

### B.3 Started leaves and cancelling (5.4, 5.6)

These run anywhere, with scripted leaves.

| # | Tree and script | Timeline | Also check |
|---|---|---|---|
| E-20 | `Seq[A, s]`. A: S after 1.5. | 0: `R R -` · 1.5: `S S S` | The run succeeds at 1.5. The log has A "started: Running" at 0 and "finished: Succeeded" at 1.5, then s "executed: Succeeded". |
| E-21 | `Seq[n, A, s]`. A: S after 2.5. | 0: `R S R -` · 2.5: `S S S S` | `n` was called once: Memory resumes at A. |
| E-22 | `Seq~[n, A, s]`. A: S after 2.5. | 0: `R S R -` · 2.5: `S S S S` | `n` was called 4 times: at 0, 1, 2 and 2.5. |
| E-23 | `Seq~[c, A]`. `go` is true and is set to false at 2.3. | 0: `R S R` · 2.3: `F F C` · 2.5: `F F -` | The run fails at 2.5, not at 2.3: its end waits for the cancel. A got its cancel at 2.3. The log has A "interrupted" at 2.3. |
| E-24 | `Sel~[Seq[c, A], B]`. `go` is false and is set to true at 1.4. A: S after 1. B confirms a cancel after 0.5 s. | 0: `R F F - R` · 1.4: `R R S R C` · 1.9: `R R S R -` · 2.9: `S S S S -` | A's goal was sent at 1.9, when B's cancel was confirmed, not at 1.4. At 1.4 the details of A say that it waits for B to cancel. |
| E-25 | E-24 with *Wait for cancelled actions* off for A. | 1.4: `R R S R C` · 1.9: `R R S R -` · 2.4: `S S S S -` | A's goal was sent at 1.4. |
| E-26 | `Seq[All[Sel~[Seq[c, A], B], Seq[D, H]]]`. `go` is false and is set to true at 1.2. A: S after 1. B confirms a cancel after 3 s. D: S after 1.5. H: S after 1. | 0: `R R R F F - R R R -` · 1.2: `R R R R S R C R R -` · 1.5: `R R R R S R C R S R` · 2.5: `R R R R S R C S S S` · 4.2: `R R R R S R - S S S` · 5.2: `S S S S S S - S S S` | H's goal was sent at 1.5, while B was Cancelling: a branch of a Parallel does not wait for another branch. A's goal was sent at 4.2. |
| E-27 | `Seq[Any[A, B], D]`. A: S after 1. B confirms a cancel after 0.5 s. D: S after 1. | 0: `R R R R -` · 1: `R S S C R` · 1.5: `R S S - R` · 2.5: `S S S - S` | D's goal was sent at 1.5: what follows a Parallel waits for the children it halted. |
| E-28 | `Seq[Any[A, B]]`. A: F after 1. B: S after 2.5. | 0: `R R R R` · 1: `F F F C` · 1.2: `F F F -` | The first child to finish decides: A's failure ends the Any and halts B. The run fails at 1.2, and B's server got a cancel at 1. |
| E-29 | `Seq[All[A, B]]`. A: F after 1. | 0: `R R R R` · 1: `F F F C` · 1.2: `F F F -` | The run fails at 1.2. |
| E-30 | `Seq[A]`. A has a Timeout of 2 s. | 0: `R R` · 2: `R C` · 2.2: `F F` | The details of A say "timed out". The run fails at 2.2. |
| E-31 | E-24, where A never finishes and has a Timeout of 2 s. | A is `C` at 3.9 and `F` at 4.1. | The Timeout counts from 1.9, when A really started. |
| E-32 | `Seq~[c, A]`. `go` is true and is set to false at 1.5. A never confirms a cancel. | 0: `R S R` · 1.5: `F F C` · 6.5: `- - -` | At 6.5 an ERROR is logged and the tree stops: state IDLE, outcome *stopped*, goal aborted, `unconfirmed_cancels` 1. `~/status` shows `unsettled` 1, and the goal is still in the journal. |
| E-33 | E-32 with *When a cancel is not confirmed* set to continue. | 6.5: `F F -` | Outcome *failed*, `unconfirmed_cancels` 1, `unsettled` 1. |
| E-34 | `Sel~[Seq~[c, A], B]`. `go` is true, is set to false at 1.2 and to true again at 2.4. A confirms a cancel after 3 s. | 0: `R R S R -` · 1.2: `R F F C R` · 2.4: `R R S C -` · 4.2: `R R S R -` | A's server saw a goal at 0, its cancel at 1.2 and a second goal at 4.2: never two goals at once. B's server saw no goal at all. |

### B.4 Run states and commands (4.4, 5.5)

These run anywhere. The state is given where it matters.

| # | Tree and commands | Timeline | Also check |
|---|---|---|---|
| R-01 | `Seq[A, s]`. A: S after 1. Pause at 0.5, resume at 3.4. | 0.5: PAUSED `R R -` · 1: PAUSED `R R -` · 3.4: RUNNING `S S S` | A's result arrived at 1 and was kept. The run succeeds at 3.4. |
| R-02 | `Seq[A]`. A has a Timeout of 2 s. Pause at 1, resume at 5.4. | 2: PAUSED `R C` · 2.2: PAUSED `R F` · 5.4: `F F` | The deadline acted during the pause; the tree moved on only at the resume. |
| R-03 | `Seq[A, s]`. Stop at 0.5. | 0.5: STOPPING `- C -` · 0.7: IDLE `- - -` | Goal canceled, outcome *stopped*, `unconfirmed_cancels` 0. |
| R-04 | `Seq[A, s]`. A: S after 1. Reset at 0.5. | 0.5: RESTARTING `- C -` · 0.7: RESTARTING `- - -` · 1: RUNNING `R R -` · 2: `S S S` | A's server saw a goal at 0, its cancel at 0.5 and a goal at 1. The run succeeds at 2. |
| R-05 | `Seq[A, s]`. A: S after 1. Pause at 0.5, Reset at 0.8, resume at 3.4. | 0.8: PAUSED `- C -` · 1: PAUSED `- - -` · 3.4: RUNNING `R R -` · 4.4: `S S S` | |
| R-06 | `Seq[s, f]`, then Reset after the run. | after the run: IDLE `F S F` · after Reset: IDLE `- - -` | |
| R-07 | `Seq[A]`, and a second goal at 0.5. | | The second goal is rejected. The first run is unchanged. |
| R-08 | `Seq[s, Stop, n]`, with a Stop node. | 0: IDLE `- - - -` | `n` was never called. Outcome *stopped*, goal aborted: the tree stopped itself. |
| R-09 | `Seq[s, Pause, n]`, with a Pause node. Resume at 2.4. | 0: PAUSED `R S R -` · 2.4: `S S S S` | `n` was called once. |
| R-10 | `Seq[n, Restart]`, with a Restart node. Stop at 2.5. | | `n` was called 3 times, at 0, 1 and 2: one loop per tick interval. |
| R-11 | `Seq[A]`. A: S after 0.5. Loop Execution on, switched off at 1.2. | 0: `R R` · 0.5: `S S` · 1: `R R` · 1.5: `S S` | The run succeeds at 1.5 with `loops_completed` 2. |
| R-12 | `Seq[All[Stop, n]]`, with a Stop node. | 0: IDLE, every node Ready | `n` was never called: the tick ended at the Stop node. |
| R-13 | A goal whose bundle is not JSON. | | The goal is rejected. The state stays IDLE. |

### B.5 Evaluation (6.3)

These run anywhere. First the comparisons, with Wait off:

| # | Entry | Operator | Compared with | Result |
|---|---|---|---|---|
| V-01 | Integer `3` | `==` | literal `3` | Succeeded |
| V-02 | Integer `3` | `!=` | literal `3` | Failed |
| V-03 | Double `17.3` | `>=` | literal `20.0` | Failed |
| V-04 | Double `20.0` | `>=` | literal `20.0` | Succeeded |
| V-05 | Integer `5` | `<` | literal `7` | Succeeded |
| V-06 | String `"a"` | `<` | literal `"b"` | Failed, "cannot be ordered" |
| V-07 | Bool `true` | `==` | literal `true` | Succeeded |
| V-08 | List `[1, 2]` | `==` | literal `[1.0, 2.0]` | Succeeded: numbers compare by value |
| V-09 | List `[1, 0]` | `==` | literal `[True, False]` | Failed: a Bool never equals a number |
| V-10 | Double `nan` | `==` | literal `nan` | Failed |
| V-11 | Double `nan` | `!=` | literal `nan` | Succeeded |
| V-12 | Integer `4` | `==` | the entry `b`, which is `4` | Succeeded |
| V-13 | Integer `4` | `==` | the entry `b`, which does not exist | Failed; the reason names `b` |
| V-14 | an entry that does not exist | `==` | literal `1` | Failed; the reason names the entry |
| V-15 | no entry chosen | | | Failed, with the reason |

Then waiting:

| # | Tree and script | Timeline | Also check |
|---|---|---|---|
| V-20 | `Seq[W, s]`. `battery` is set to 21.0 from outside at 3.3. | 0: `R R -` · 3.3: `S S S` | At 0 the details of W read `waiting for battery >= 20.0 (now 17.3)`. |
| V-21 | `Seq[W, s]`. W has a Timeout of 5 s. | 0: `R R -` · 5: `F F -` | The details of W say "timed out". |
| V-22 | `Seq[W, s]`. There is no entry `battery`; it is created with 25.0 at 2.2. | 0: `R R -` · 2.2: `S S S` | |
| V-23 | `Seq~[c, W]`. `go` is true and is set to false at 1.5. | 0: `R S R` · 1.5: `F F -` | W never shows Cancelling. The run fails at 1.5. |
| V-24 | `Seq[Any[A, W]]`. `battery` is set to 21.0 from outside at 2.4. | 0: `R R R R` · 2.4: `S S C S` · 2.6: `S S - S` | The run succeeds at 2.6. |
| V-25 | `Seq[All[W, Seq[A, Set]]]`. A: S after 0.5. The Set node writes `battery` = 21.0. | 0: `R R R R R -` · 0.5: `R R R S S S` · 1: `S S S S S S` | W succeeds at 1, at the next periodic tick. In the tick at 0.5 it was ticked before the Set node ran, and a change made inside a tick requests no tick. |
| V-26 | `Seq[All[Seq[A, Set], W]]`: V-25 with the branches exchanged. | 0: `R R R R - R` · 0.5: `S S S S S S` | W is ticked after the Set node in the tick at 0.5 and sees the change. |

### B.6 The blackboard (7.2, 7.3, 7.6, 9.4)

These run anywhere.

| # | Setup | Action | Expected |
|---|---|---|---|
| K-01 | Entry `n`, Integer 0, Resets on. `Seq[Set(n = 5), A]`. A: S after 1. | Read `n` at 0.5 and after the run. | 5 at 0.5. After the run 0: in the result, in the gateway's copy and in the blackboard file. |
| K-02 | Entry `n` as in K-01. `Seq[Evaluation(n == 0), Set(n = 5)]`. Loop Execution on. | Three loops. | `S S S` in every loop. With Resets off, the second loop gives `F F -`. |
| K-03 | Entry `m`, Integer 1. `Seq[Set(m = 9), A]`. *Restore blackboard values on Stop / Reset* on. | Stop at 0.5. | `m` is 1 afterwards. With the option off it is 9. |
| K-04 | K-03, with an Evaluation elsewhere that names `m`. | A browser renames `m` to `k` at 0.3. Stop at 0.5. | The entry is named `m` again and has kept its id. The Evaluation names `m`. |
| K-05 | `Seq[s]`, which ends at 0. | A `bb.set_value` of `x` to 7 reaches the executor after the run has ended. | The executor answers that no run is active. The gateway applies the edit: `x` is 7 in its copy and in the file. |
| K-06 | Any tree. | A `bb.set_value` of `x` to 7 arrives between the click on Execute and the acceptance of the goal. | The edit is held and then sent to the executor. `x` is 7 on both sides. |
| K-07 | An entry with a source. | A Set node writes it. A browser edits it. | The node fails with the reason. The edit is answered `no`. The value is unchanged. |
| K-08 | Entry `count`, Integer. | A `bb.set_value` of `count` to `"abc"`. | Answered `no` with the reason. The value is unchanged. |
| K-09 | Entry `big`, List. | A leaf sets it to a list of 100,000 integers. | The blackboard file holds all 100,000. The browser gets a preview marked `truncated`, and all of it when it asks for the value. |
| K-10 | Any tree. | 100 changes of a value within 100 ms. | The blackboard file is written twice, at 0 and at 0.25 s, and then equals the final state. |
| K-11 | Any tree. | The gateway is killed in the middle of a file write. | The file is the old one or the new one, whole; never a part of one. |

### B.7 Sources (7.4)

These need ROS. Each uses one entry fed from a test topic.

| # | Setup | Action | Expected |
|---|---|---|---|
| S-01 | `std_msgs/msg/Float64`, field `data`, into a Double entry. | Publish 1.5. | The entry is 1.5 and is not stale. |
| S-02 | Field `pose.position.x` of a `geometry_msgs/msg/PoseStamped`; field `ranges[0]` of a `sensor_msgs/msg/LaserScan`. | Publish messages with known values. | The entry holds that one number. |
| S-03 | No field, `geometry_msgs/msg/Point`, into a Dictionary entry. | Publish x 1, y 2, z 3. | `{'x': 1.0, 'y': 2.0, 'z': 3.0}`. |
| S-04 | `std_msgs/msg/Bool`, Max rate 2. | Publish `false` at 0 and `true` at 0.1, then nothing. | The entry is `false` from 0 and `true` from 0.5. |
| S-05 | Any source. `Seq[W]`, with W waiting on the entry. | The first message comes at 1.3. | Until then the entry has no data and W is Running. W succeeds at 1.3. An Evaluation with Wait off fails at 0 with "no recent data". |
| S-06 | Max age 2. | One message at 0. | An Evaluation with Wait off succeeds when ticked at 1 and fails with "no recent data" when ticked at 3. A tick is requested at 2, when the entry goes stale. |
| S-07 | A publisher that keeps its last message for late joiners and published before the run. | Start the run. | The entry has the value within 1 s, without a new message. |
| S-08 | A best-effort publisher. | Publish. | The value arrives. |
| S-09 | No publisher at the start; one appears at 2. | Publish at 3. | The value arrives. |
| S-10 | `std_msgs/msg/UInt32` into an Integer entry that holds 1. | Publish 3,000,000,000, then 5. | After the first: still 1, and one WARNING in the log. After the second: 5. |
| S-11 | A topic with 10 messages a second, Max rate 2, for 10 s. | | The entry changes about 20 times. The blackboard file is not rewritten for it. |
| S-12 | A source whose type is `os/msg/Thing`. | Validate. | A finding: not an interface package. The module `os.msg` is not imported. |

### B.8 The ROS leaves (6.5)

These need ROS, with scripted action servers and services. Times count from the start of the
leaf.

| # | Script | Expected |
|---|---|---|
| L-01 | Call Action. The server accepts, sends two feedback messages and succeeds after 1 s. | Succeeded at 1. The outputs are written. While it ran, the details showed the latest feedback. |
| L-02 | Call Action. There is no server. | Failed at 2, "server not available". |
| L-03 | The server rejects the goal. | Failed, "goal rejected". |
| L-04 | The server does not answer the goal. | Failed at 5, "no reply to the goal". If the server accepts at 6, it gets a cancel for that goal. |
| L-05 | The server aborts after 1 s. | Failed at 1, "aborted". |
| L-06 | Timeout 2 s. The server never finishes and confirms a cancel after 0.3 s. | Cancelling at 2. Failed at 2.3, "timed out". |
| L-07 | Feedback timeout 1 s. The server sends no feedback. | A cancel is sent at 1. Failed, "no feedback". |
| L-08 | The leaf is halted at 0.1. The server accepts the goal at 0.3. | The cancel is sent at 0.3, not before. The leaf settles when the result says *canceled*. |
| L-09 | The leaf is halted at 1. The server refuses the cancel and finishes at 20. | Cancelling until 6. Then the cancel is unconfirmed, the tree stops and `unsettled` is 1. The cancel is sent again every few seconds. At 20 `unsettled` is 0 and the journal marks the goal done. |
| L-10 | Call Service. The reply has `success` true; or `success` false and `message` "busy"; or there is no reply. | Succeeded; Failed with "busy"; Failed at 5. |
| L-11 | Call Service, halted at 1. The reply comes at 2. | Ready at 1, never Cancelling. The reply is ignored. |
| L-12 | Publish with a field bound to an entry. One subscriber. | The subscriber receives exactly one message, with the entry's value. |
| L-13 | Publish with *Wait for subscribers* 1 and no subscriber. | Failed at 5. |
| L-14 | Call Action with a binding to a missing entry, or to a stale one. | The node fails when it starts, with the reason. No goal is sent. |
| L-15 | Call Action whose Type is `os/action/Thing`. | A finding from validation. In a run the node fails. The module `os.action` is not imported. |

### B.9 The executor and the tree API (section 4, section 8)

These need ROS.

| # | Action | Expected |
|---|---|---|
| X-01 | End a run in each way: the Root succeeds; the Root fails; Stop; a Stop node; the tick raises. | Goal succeeded, `outcome` 1; aborted, 2; canceled, 3; aborted, 3; aborted, 4. `~/status` shows the same `outcome` afterwards. |
| X-02 | A run in which nothing changes for 5 s. | `~/status` arrives about every 0.5 s: 9 to 11 messages. |
| X-03 | 1,000 status changes within 1 s. | At most 31 `~/status` messages. The last holds the final statuses. |
| X-04 | Subscribe to `~/run` and `~/status` in the middle of a run. Subscribe to `~/run` of an executor that has never run. | Both deliver their last message at once. The second delivers an empty `RunInfo`. |
| X-05 | A client misses three `~/details` messages. | It sees the gap in the numbers. After `~/get_snapshot` its state equals the executor's. |
| X-06 | A run with 5,000 log records and a client that read none. | `~/get_log` from 0 returns all 5,000, in order, over several calls. |
| X-07 | Kill the executor (SIGKILL) during a Call Action. Start it again. | The action server gets a cancel for that goal id. |
| X-08 | Two executors on one host. Kill and restart one during an action of the other. | The other's goal is not cancelled. |
| X-09 | Send SIGINT to the executor during a Call Action. | The action server gets the cancel before the process ends. The process ends within the cancel timeout. |
| X-10 | A goal with a watchdog of 2 s. The watched topic stops at 3. | The run stops at 5: outcome *stopped*, goal aborted. |
| X-11 | The same, but from 3 on the watched topic carries another run id, or the state IDLE. | The same: such messages do not count. |
| X-12 | Stop a run that the gateway did not start, by its id through the cancel service. | The run stops; the goal ends canceled. |

### B.10 Two executors (6.8)

These need ROS and two executors, called local and remote here.

| # | Setup | Expected |
|---|---|---|
| M-01 | The local entry `a` is 5 and is bound to the remote entry `x`. The remote tree is `Seq[Set(y = x)]`. The output `y` goes to the local entry `b`. | The node succeeds. The local `b` is 5. |
| M-02 | The remote Root fails. | The node fails. Its outputs are written all the same. |
| M-03 | The remote executor is running a tree. | The node fails with "executor busy". The remote executor receives no goal. |
| M-04 | The node is halted at 1 while the remote tree runs an action. | The remote run goes to STOPPING and its action gets a cancel. The remote goal ends canceled. The node shows Cancelling until then. |
| M-05 | The local executor is killed at 1. Watchdog 5 s. | The remote run stops between 5.5 and 6: 5 s after the last heartbeat it counted. |
| M-06 | M-05 with Watchdog 0. | The remote run goes on. |
| M-07 | The remote executor is killed at 1. | The node fails with "executor lost" between 3.5 and 4. |
| M-08 | The remote tree's file has Loop Execution on. | The remote tree runs once, and the node finishes. |
| M-09 | Zoom In on the node during the run. | The page shows the remote tree with its live statuses, read-only. |

### B.11 The gateway and the browser protocol (section 9, section 10, section 13)

These run anywhere, against `LocalLink`.

| # | Action | Expected |
|---|---|---|
| G-01 | A `conn.add` that would make a cycle. | `no` with the reason. The document and its revision are unchanged. |
| G-02 | A `conn.add` of a second child to a Negation. | Applied. The connection of the first child is gone. |
| G-03 | A `node.remove` of the Root. | `no`. |
| G-04 | A `node.add` during a run; a `node.move` during a run. | `no`; applied. |
| G-05 | A `node.set_field` during a run. | The gateway's document changes only when the change comes back from the executor. A leaf that starts afterwards uses the new value. |
| G-06 | Browsers A and B set one field, A to 1 and B to 2. The gateway receives B's operation first. | The document holds 1. Both browsers show 1. The revision rose by 2. |
| G-07 | A and B each move a sibling at the same moment, each sending a child order. | The second order is dropped and asked for again. The final order matches the final positions. |
| G-08 | A clicks Execute. Before the goal is accepted, B sends a `node.remove`. | `no`. The tree that runs has the node. |
| G-09 | A browser reconnects in the middle of a run. | Its `snapshot` equals the gateway's state: documents, blackboard, statuses and the newest 500 log lines. |
| G-10 | A browser sends an operation and loses its connection before the answer. | After the reconnect it shows the server's state and names the change that was lost. |
| G-11 | A browser reads nothing for 5 s while statuses change 1,000 times. | The gateway's memory for that browser does not grow. Then it gets one `st` with the latest statuses. |
| G-12 | A browser falls 12,000 log lines behind. | The next `log` says that 2,000 were skipped. They can be fetched again. |
| G-13 | A WebSocket request with `Origin: http://evil.example`. | Refused with status 403. With the gateway's own origin it is accepted. |
| G-14 | A request with `Host: evil.example`. | Refused. With `Host: bt.<hostname>.local`, or the gateway's address, it is accepted. |
| G-15 | A `POST /upload` from another origin. | Refused. |
| G-16 | A `file load` of `../../etc/passwd`. | `no`. |
| G-17 | The literals `[1, 2.5, 'a']`; `__import__('os')`; 2 MB of text; 200 nested brackets. | A List; an error; refused; refused. |
| G-18 | Change a document without saving. Kill the gateway 3 s later and start it again. | The page offers to restore the document, with the change. |
| G-19 | Load a tree that another session has open. | `no`, with the name of that session's executor. |
| G-20 | A and B both show one sub-tree that has unsaved changes. A goes back. Then B sends Revert. | After Go Back nothing is discarded and B's view is unchanged. After Revert both show the file's state. |
| G-21 | A `file new` or a `file load` during a run. | `no`. |
| G-22 | The executor is down. | Its tree can be edited and saved, with the node types of the kept catalog. Execute is unavailable. |
| G-23 | A `cmd execute` for a tree whose validation has findings. | `no` with the findings. Sent again with `confirm`, the run starts. |

### B.12 Files and compatibility (section 12)

These run anywhere; F-05 needs the widget.

| # | Setup | Action | Expected |
|---|---|---|---|
| F-01 | Every tree file in the widget's `tests/data/`. | Load and save. | The saved file holds the same data, with version 1. |
| F-02 | A tree with only the widget's features. | Save. | Version 1. |
| F-03 | That tree with one addition: a Parallel; an Evaluation with `>=`; an Evaluation with Wait; a binding; a Cancel timeout of 3 s. | Save. | Version 2 in each case. |
| F-04 | A blackboard with one source, for a tree with only the widget's features. | Save. | The blackboard file has version 2. The tree file has version 1. |
| F-05 | A version 2 tree file. | Load it in the widget. | Refused, with the widget's message about a newer version. |
| F-06 | A file whose connections list `P → b` before `P → a`, with `a` drawn left of `b`. | Run. | `b` runs before `a`: the written order rules. |
| F-07 | A Sub-Tree whose File is `sub\patrol_BT.json`; one whose File is `C:\trees\patrol_BT.json`. | Load. | The first is read as `sub/patrol_BT.json`. The second is reported, with the offer to choose the file. |
| F-08 | A node of an unknown type that carries extra data. | Load, save, run. | It is shown as a placeholder. The saved node equals the loaded one. In a run it fails. |
| F-09 | A Double entry `1.0`, shown in a browser and saved unchanged; another edited to `nan`. | Save. | `1.0` in the file, not `1`; and `NaN`. |
| F-10 | A group whose nodes hang below a Selector with Memory off. | Promote to Sub-Tree. | The new file's Root is a Selector with Memory off. The tree runs as before. |
| F-11 | A version 1 file in which two siblings' centres are 10 pixels apart sideways. | Save. | A warning that names the two. |

### B.13 The page (section 11)

These are browser tests against a real gateway.

| # | Action | Expected |
|---|---|---|
| P-01 | Make a new tree. Add a Sequence and two leaves from the menu, connect them, and Execute. | Every node shows its status. The log pane shows a line for each node that ran. |
| P-02 | Drag the right child to the left of its sibling. | The numbers on the two lines swap. The gateway receives the new order. |
| P-03 | Select two connected nodes. Press Ctrl+C, then Ctrl+V. | Two new nodes, connected like the originals, at least 20 pixels from every other node, and selected. The Root is never copied. |
| P-04 | Turn the wheel with Ctrl held, over the canvas. | The canvas zooms, within 0.2× and 3×. The page itself does not zoom. |
| P-05 | Open the page in the middle of a run. | Statuses, blackboard and the newest log lines are there with the first picture. |
| P-06 | Press Stop while an action confirms its cancel slowly. | The toolbar shows the stopping state and the number of cancels outstanding. The leaf shows Cancelling. |
| P-07 | Drag a group's title bar. | The nodes completely inside it move with it. A node that only overlaps it stays. |
| P-08 | Type more text into a note. | The font shrinks so that all of the text fits. No word is split. |
| P-09 | Zoom In on a Sub-Tree node during a run. | Its nodes show the statuses of that instance. Go Back returns to the tree. |
| P-10 | Type `nan`, then `1,5`, into the editor of a Double. | Accepted as `nan` and as `1.5`. |
| P-11 | Two tabs on one session. Move a node in one. | The node follows in the other. |
| P-12 | Open `http://bt.<hostname>.local` from a machine that can look the name up, and the server's address from one that cannot. | The page loads in both. |

---

## References

Looked up on 2026-11-12 and 2026-11-13 for the platform facts above.

* [Lyrical Luth supported platforms](https://docs.ros.org/en/lyrical/Releases/lyrical/supported-platforms.html):
  Tier 1 platforms, default middleware.
* [ROS 2 Lyrical Luth released](https://discourse.openrobotics.org/t/ros-2-lyrical-luth-released/55021):
  long-term support until May 2031.
* [Faster rclpy executor now in Rolling](https://discourse.openrobotics.org/t/faster-rclpy-executor-now-in-rolling/42852):
  `rclpy.experimental.EventsExecutor`, its CPU use and its limits.
* [rclpy.experimental package](https://docs.ros.org/en/rolling/p/rclpy/rclpy.experimental.html)
  and [rclpy pull request 1620](https://github.com/ros2/rclpy/pull/1620): the asyncio node and
  its scope.
* [BehaviorTree.ROS2 pull request 53](https://github.com/BehaviorTree/BehaviorTree.ROS2/pull/53):
  a halt that arrives before the goal is accepted.
* [nss-mdns README](https://github.com/avahi/nss-mdns): the two-label rule of `mdns4_minimal`
  and `/etc/mdns.allow`.
* [avahi-publish manual](https://www.mankier.com/1/avahi-publish) and
  [several `.local` names for one address](https://pi3g.com/avahi-how-to-assign-several-local-names-to-same-ip/):
  `-a` and `-R`.
* [How to install Python on Ubuntu 26.04](https://linuxize.com/post/how-to-install-python-on-ubuntu-26-04/)
  and [the externally-managed-environment error](https://linuxize.com/post/fix-pip-externally-managed-environment/):
  the system Python and pip.
