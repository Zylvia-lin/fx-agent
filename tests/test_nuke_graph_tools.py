"""Explicit orthogonal routing never rearranges unrelated production nodes."""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


class Knob:
    def __init__(self, value): self.stored = value
    def value(self): return self.stored


class Parent:
    def __init__(self): self.children = []
    def nodes(self): return list(self.children)
    def __enter__(self): return self
    def __exit__(self, *args): pass


class Node:
    def __init__(self, parent, name, cls="Grade", x=0, y=0):
        self.group, self.node_name, self.cls = parent, name, cls
        self.x, self.y, self.connections = x, y, []
        self.selected, self.children = False, []
        self.callbacks = {key: Knob("") for key in ("onCreate", "onDestroy", "knobChanged", "updateUI")}
        parent.children.append(self)
    def fullName(self): return self.node_name
    def name(self): return self.node_name
    def Class(self): return self.cls
    def parent(self): return self.group
    def nodes(self): return self.children
    def xpos(self): return self.x
    def ypos(self): return self.y
    def setXYpos(self, x, y): self.x, self.y = x, y
    def screenWidth(self): return 12 if self.cls == "Dot" else 80
    def screenHeight(self): return 12 if self.cls == "Dot" else 20
    def inputs(self): return len(self.connections)
    def input(self, i): return self.connections[i] if i < len(self.connections) else None
    def setInput(self, i, source):
        self.connections.extend([None] * max(0, i + 1 - len(self.connections)))
        self.connections[i] = source
    def isSelected(self): return self.selected
    def setSelected(self, value): self.selected = value
    def knobs(self): return self.callbacks
    def __getitem__(self, key): return self.callbacks[key]


@pytest.fixture
def graph(monkeypatch, tmp_path):
    path = Path(__file__).resolve().parents[1] / "integrations/nuke/fx_agent_nuke_graph.py"
    spec = importlib.util.spec_from_file_location("graph_tool_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.tempfile, "tempdir", str(tmp_path))
    parent = Parent()
    source = Node(parent, "Source", x=0, y=0)
    target = Node(parent, "Target", x=200, y=200)
    target.setInput(0, source)
    other = Node(parent, "Unrelated", x=500, y=500)
    copied = []

    def node_copy(path):
        assert path != "%clipboard%"
        copied[:] = [node for node in parent.nodes() if node.selected]
        Path(path).write_text("copied", encoding="utf8")

    def node_paste(path):
        assert Path(path).read_text(encoding="utf8") == "copied"
        clone = Node(parent, copied[0].name() + "1", copied[0].Class())
        clone.selected = True
        clone.setInput(0, copied[0])
        return clone

    host = SimpleNamespace(
        toNode=lambda name: next((node for node in parent.nodes() if node.fullName() == name), None),
        nodes=SimpleNamespace(Dot=lambda: Node(parent, f"Dot{len(parent.nodes())}", "Dot")),
        delete=lambda node: parent.children.remove(node), nodeCopy=node_copy, nodePaste=node_paste,
    )
    monkeypatch.setitem(sys.modules, "nuke", host)
    return module, host, parent, source, target, other


def route(**changes):
    return {"source_node": "Source", "target_node": "Target", "input_index": 0,
            "waypoints": [[40, 100], [240, 100]], **changes}


def test_dry_run_and_execute_touch_only_explicit_nodes(graph):
    module, _, parent, source, target, other = graph
    params = {"positions": [{"node_name": "Target", "x": 200, "y": 220}],
              "routes": [route()], "dry_run": True}
    result = module.layout_nodes(params)["result"]
    assert result["orthogonal"] is True
    assert result["positions"][0]["width"] == 80
    assert result["positions"][0]["input_ports"]["0"] == [240, 220]
    assert result["positions"][0]["output_port"] == [240, 240]
    assert len(parent.nodes()) == 3 and target.ypos() == 200
    params["dry_run"] = False
    result = module.layout_nodes(params)["result"]
    assert len(result["created_dots"]) == 2
    assert target.input(0).input(0).input(0) is source
    assert target.ypos() == 220
    assert (other.xpos(), other.ypos()) == (500, 500)


@pytest.mark.parametrize("change", [
    {"waypoints": [[45, 100], [240, 100]]},
    {"input_index": 1},
    {"source_node": "Missing"},
])
def test_invalid_route_is_rejected_before_changes(graph, change):
    module, _, parent, source, target, _ = graph
    with pytest.raises(ValueError):
        module.layout_nodes({"positions": [{"node_name": "Target", "x": 200, "y": 220}],
                             "routes": [route(**change)], "dry_run": False})
    assert len(parent.nodes()) == 3 and target.ypos() == 200 and target.input(0) is source


def test_duplicate_position_and_noninteger_are_rejected(graph):
    module, _, _, _, _, _ = graph
    with pytest.raises(ValueError):
        module.layout_nodes({"positions": [{"node_name": "Target", "x": 200, "y": 200}] * 2,
                             "routes": [], "dry_run": False})
    with pytest.raises(ValueError):
        module.layout_nodes({"positions": [{"node_name": "Target", "x": float("nan"), "y": 200}],
                             "routes": [], "dry_run": False})


def test_failed_execution_restores_positions_connections_and_removes_dots(graph):
    module, host, parent, source, target, _ = graph
    original = host.nodes.Dot
    count = 0

    def fail_second():
        nonlocal count
        count += 1
        if count == 2:
            raise RuntimeError("Dot creation failed")
        return original()

    host.nodes.Dot = fail_second
    with pytest.raises(RuntimeError, match="Dot creation failed"):
        module.layout_nodes({"positions": [{"node_name": "Target", "x": 200, "y": 220}],
                             "routes": [route()], "dry_run": False})
    assert len(parent.nodes()) == 3 and target.ypos() == 200 and target.input(0) is source


def test_duplicate_uses_temporary_file_and_restores_selection(graph):
    module, _, parent, source, _, other = graph
    source.cls = "Group"
    other.selected = True
    result = module.duplicate_node({"source_node": "Source", "position": [700, 800]})["result"]
    clone = next(node for node in parent.nodes() if node.name() == result["name"])
    assert clone is not source and clone.input(0) is None
    assert (clone.xpos(), clone.ypos()) == (700, 800)
    assert other.selected and not source.selected and not clone.selected


def test_duplicate_rejects_recursive_callbacks_without_copy(graph):
    module, host, parent, source, _, _ = graph
    source.cls = "Group"
    child_parent = Parent()
    child = Node(child_parent, "Source.Inner")
    child.callbacks["knobChanged"].stored = "untrusted_callback()"
    source.children.append(child)
    host.nodeCopy = lambda path: pytest.fail("copy must not occur")
    with pytest.raises(ValueError, match="callback"):
        module.duplicate_node({"source_node": "Source", "position": [700, 800]})
    assert len(parent.nodes()) == 3


@pytest.mark.parametrize("nested", [False, True])
def test_duplicate_rejects_autolabel_on_source_or_child(graph, nested):
    module, host, parent, source, _, other = graph
    other.selected = True
    target = source
    if nested:
        source.cls = "Group"
        target = Node(Parent(), "Source.Inner")
        source.children.append(target)
    target.callbacks["autolabel"] = Knob("untrusted_autolabel()")
    host.nodeCopy = lambda path: pytest.fail("autolabel must be rejected before copy")
    with pytest.raises(ValueError, match="autolabel"):
        module.duplicate_node({"source_node": "Source", "position": [700, 800]})
    assert len(parent.nodes()) == 3 and other.selected and not source.selected


def test_duplicate_failure_removes_partial_copy_and_temporary_file(graph):
    module, host, parent, source, _, other = graph
    source.selected = True
    paths = []
    original_copy = host.nodeCopy

    def copy(path):
        paths.append(path)
        original_copy(path)

    def paste(path):
        Node(parent, "Partial")
        raise RuntimeError("paste failed")

    host.nodeCopy, host.nodePaste = copy, paste
    with pytest.raises(RuntimeError, match="paste failed"):
        module.duplicate_node({"source_node": "Source", "position": [700, 800]})
    assert len(parent.nodes()) == 3 and source.selected and not other.selected
    assert len(paths) == 1 and not Path(paths[0]).exists()


def test_cross_group_and_duplicate_target_routes_are_rejected(graph):
    module, _, parent, source, target, _ = graph
    with pytest.raises(ValueError, match="Duplicate route"):
        module.layout_nodes({"positions": [], "routes": [route(), route()], "dry_run": False})
    source.group = Parent()
    with pytest.raises(ValueError, match="Group"):
        module.layout_nodes({"positions": [], "routes": [route()], "dry_run": False})
    assert len(parent.nodes()) == 3 and target.input(0) is source


def test_silent_connection_refusal_is_detected_and_rolled_back(graph):
    module, _, parent, source, target, _ = graph
    original = target.setInput

    def refuse_dot(index, node):
        if node is source:
            original(index, node)
        return False

    target.setInput = refuse_dot
    with pytest.raises(RuntimeError, match="refused"):
        module.layout_nodes({"positions": [], "routes": [route()], "dry_run": False})
    assert len(parent.nodes()) == 3 and target.input(0) is source
