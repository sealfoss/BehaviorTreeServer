"""Write the example trees of examples/trees with the document model, so they are exactly in the
format the gateway writes (and load without problems)."""
import sys
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "bt_server_core"))

from bt_server_core import files  # noqa: E402
from bt_server_core.document import TreeDocument  # noqa: E402
from bt_server_core.registry import Registry  # noqa: E402

OUT = REPO / "examples" / "trees"
OUT.mkdir(parents=True, exist_ok=True)
catalog = Registry(["bt_server_core.demo_leaves"]).catalog()


def nid() -> str:
    return uuid.uuid4().hex


def tree(config, nodes, connections, view_center=(0.0, 200.0)):
    return {"format": "behavior_tree_widget", "version": 1, "config": config,
            "view": {"zoom": 1.0, "center": list(view_center)}, "nodes": nodes, "connections": connections}


def leaf(type_name, title, x, y, fields=None, selections=None):
    node = {"id": nid(), "type": type_name, "title": title, "x": float(x), "y": float(y), "fields": fields or {}}
    if selections:
        node["field_selections"] = selections
    return node


def save(key, data, blackboard=None):
    doc = TreeDocument(data, catalog, key=key)
    assert not doc.problems, (key, doc.problems)
    out = doc.to_file()
    files.write_tree_file(str(OUT / f"{key}_BT.json"), out)
    if blackboard is not None:
        files.write_blackboard_file(str(OUT / f"{key}_BB.json"), blackboard)
    print(key, "version", doc.file_version(), "warnings", doc.save_warnings())


CONFIG = {"tick_interval_ms": 100, "repeat": False, "restore_blackboard": False, "default_memory": True}

# --- hello: only the widget's features (version 1) ------------------------------------------
root = {"id": nid(), "type": "Root", "title": "Root", "x": 0.0, "y": 0.0, "composite": "Sequence", "memory": True}
hello = leaf("LogMessage", "Say Hello", -450, 200, {"message": "Hello from the Behavior Tree Server!",
                                                    "level": ["INFO", "WARNING", "ERROR"]}, {"level": 0})
wait = leaf("Wait", "Wait", -150, 200, {"seconds": 1.0})
inc = leaf("IncrementCounter", "Increment Counter", 150, 200, {"entry": "counter", "step": 1})
show = leaf("Print Message", "Print Counter", 450, 200, {"Level": 4, "Source": "counter", "Message": ""})
note = {"id": nid(), "type": "Notes", "title": "Notes", "x": 300.0, "y": -150.0,
        "text": "A first tree: it says hello, waits a second and counts.\nPress Execute; switch on Loop Execution "
                "in Configure to repeat.", "width": 360, "height": 110}
save("hello", tree(CONFIG, [root, hello, wait, inc, show, note],
                   [{"parent": root["id"], "child": n["id"]} for n in (hello, wait, inc, show)]),
     [{"name": "counter", "type": "Integer", "value": 0}])

# --- patrol: a sub-tree (version 1) -----------------------------------------------------------
proot = {"id": nid(), "type": "Root", "title": "Root", "x": 0.0, "y": 0.0, "composite": "Selector", "memory": False}
done = {"id": nid(), "type": "Sequence", "title": "Done?", "x": -300.0, "y": 200.0, "memory": True}
enough = leaf("CounterAtLeast", "Counter At Least", -450, 400, {"entry": "counter", "threshold": 3})
report = leaf("LogMessage", "Report", -150, 400, {"message": "Patrol complete", "level": ["INFO", "WARNING", "ERROR"]},
              {"level": 0})
step = {"id": nid(), "type": "Sequence", "title": "Patrol Step", "x": 300.0, "y": 200.0, "memory": True}
move = leaf("MoveTo", "Move To", 150, 400, {"x": 5.0, "y": 2.5, "speed": 5, "mode": ["walk", "run", "crawl"],
                                          "avoid_obstacles": True, "label": "waypoint"}, {"mode": 0})
count = leaf("IncrementCounter", "Increment Counter", 450, 400, {"entry": "counter", "step": 1})
save("patrol", tree(CONFIG, [proot, done, enough, report, step, move, count],
                    [{"parent": proot["id"], "child": done["id"]}, {"parent": proot["id"], "child": step["id"]},
                     {"parent": done["id"], "child": enough["id"]}, {"parent": done["id"], "child": report["id"]},
                     {"parent": step["id"], "child": move["id"]}, {"parent": step["id"], "child": count["id"]}]))

# --- demo: the server's new features (version 2) ----------------------------------------------
droot = {"id": nid(), "type": "Root", "title": "Root", "x": 0.0, "y": 0.0, "composite": "Sequence", "memory": True}
start = leaf("LogMessage", "Start", -900, 200, {"message": "Demo started", "level": ["INFO", "WARNING", "ERROR"]},
             {"level": 0})
par = {"id": nid(), "type": "Parallel", "title": "Charge and Count", "x": -450.0, "y": 200.0, "policy": "all"}
charge = leaf("Wait", "Charge", -800, 600, {"seconds": 2.0})
setbat = leaf("Set", "Battery Full", -500, 600, {"ValueName": "battery", "CompareTo": None, "LiteralValue": 100.0})
chargeseq = {"id": nid(), "type": "Sequence", "title": "Charging", "x": -650.0, "y": 400.0, "memory": True}
count2 = leaf("IncrementCounter", "Increment Counter", -250, 400, {"entry": "counter", "step": 1})
waitbat = leaf("Evaluation", "Battery Charged?", 150, 200,
               {"ValueName": "battery", "CompareTo": None, "LiteralValue": 90.0, "Operator": ">=", "Wait": True,
                "WaitTimeout": 10.0})
sub = leaf("Sub-Tree", "patrol", 450, 200, {"File": "patrol_BT.json"})
final = leaf("Print Message", "Print Counter", 750, 200, {"Level": 4, "Source": "counter", "Message": ""})
dnote = {"id": nid(), "type": "Notes", "title": "Notes", "x": -900.0, "y": -150.0,
         "text": "New in the server: a Parallel (both branches at once), an Evaluation that waits until "
                 "battery >= 90, and a Sub-Tree.", "width": 380, "height": 110}
nodes = [droot, start, par, chargeseq, charge, setbat, count2, waitbat, sub, final, dnote]
conns = [{"parent": droot["id"], "child": n["id"]} for n in (start, par, waitbat, sub, final)]
conns += [{"parent": par["id"], "child": chargeseq["id"]}, {"parent": par["id"], "child": count2["id"]},
          {"parent": chargeseq["id"], "child": charge["id"]}, {"parent": chargeseq["id"], "child": setbat["id"]}]
save("demo", tree(dict(CONFIG), nodes, conns),
     [{"name": "counter", "type": "Integer", "value": 0},
      {"name": "battery", "type": "Double", "value": 20.0, "reset": True},
      {"name": "position", "type": "List", "value": [0.0, 0.0]},
      {"name": "last_message", "type": "String", "value": ""}])
