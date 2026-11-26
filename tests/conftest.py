"""Shared pytest fixtures and environment isolation for FX Agent tests."""

import importlib.util
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

_ENV_VARS = (
    "FX_AGENT_LOCAL_ROOT",
    "FX_AGENT_TOOL_PROFILE",
    "FX_AGENT_HOUDINI_HIDE_GROUPS",
    "FX_AGENT_EMBEDDING_URL",
    "FX_AGENT_EMBEDDING_MODEL",
    "FX_AGENT_EMBEDDING_API_KEY",
)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Prevent workstation optional env vars from leaking into unit tests."""
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)


class NukeKnob:
    def __init__(self, value):
        self.stored = value

    def value(self):
        return self.stored


class NukeParent:
    def __init__(self):
        self.children = []

    def nodes(self):
        return list(self.children)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class NukeNode:
    def __init__(self, parent, name, cls="Grade", x=0, y=0):
        self.group, self.node_name, self.cls = parent, name, cls
        self.x, self.y, self.connections = x, y, []
        self.selected, self.children = False, []
        self.callbacks = {
            key: NukeKnob("") for key in ("onCreate", "onDestroy", "knobChanged", "updateUI")
        }
        parent.children.append(self)

    def fullName(self):
        return self.node_name

    def name(self):
        return self.node_name

    def Class(self):
        return self.cls

    def parent(self):
        return self.group

    def nodes(self):
        return self.children

    def xpos(self):
        return self.x

    def ypos(self):
        return self.y

    def setXYpos(self, x, y):
        self.x, self.y = x, y

    def screenWidth(self):
        return 12 if self.cls == "Dot" else 80

    def screenHeight(self):
        return 12 if self.cls == "Dot" else 20

    def inputs(self):
        return len(self.connections)

    def input(self, i):
        return self.connections[i] if i < len(self.connections) else None

    def setInput(self, i, source):
        self.connections.extend([None] * max(0, i + 1 - len(self.connections)))
        self.connections[i] = source

    def isSelected(self):
        return self.selected

    def setSelected(self, value):
        self.selected = value

    def knobs(self):
        return self.callbacks

    def __getitem__(self, key):
        return self.callbacks[key]


@pytest.fixture
def graph(monkeypatch, tmp_path):
    """Synthetic Nuke graph fixture shared across Nuke graph and selection tests."""
    path = Path(__file__).resolve().parents[1] / "integrations/nuke/fx_agent_nuke_graph.py"
    spec = importlib.util.spec_from_file_location("graph_tool_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    parent = NukeParent()
    source = NukeNode(parent, "Source", x=0, y=0)
    target = NukeNode(parent, "Target", x=200, y=200)
    target.setInput(0, source)
    other = NukeNode(parent, "Unrelated", x=500, y=500)
    copied = []

    def node_copy(copy_path):
        assert copy_path != "%clipboard%"
        copied[:] = [node for node in parent.nodes() if node.selected]
        Path(copy_path).write_text("copied", encoding="utf8")

    def node_paste(paste_path):
        assert Path(paste_path).read_text(encoding="utf8") == "copied"
        clone = NukeNode(parent, copied[0].name() + "1", copied[0].Class())
        clone.selected = True
        clone.setInput(0, copied[0])
        return clone

    host = SimpleNamespace(
        toNode=lambda name: next(
            (node for node in parent.nodes() if node.fullName() == name), None
        ),
        nodes=SimpleNamespace(Dot=lambda: NukeNode(parent, f"Dot{len(parent.nodes())}", "Dot")),
        delete=lambda node: parent.children.remove(node),
        nodeCopy=node_copy,
        nodePaste=node_paste,
    )
    monkeypatch.setitem(sys.modules, "nuke", host)
    return module, host, parent, source, target, other
