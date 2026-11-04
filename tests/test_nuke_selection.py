"""Selection inspection includes nested Groups without changing the graph."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest_plugins = ["test_nuke_graph_tools"]


@pytest.mark.parametrize("selected", [False, True])
def test_selection_snapshot_is_complete_and_read_only(graph, selected):
    module, host, parent, source, target, other = graph
    parent.name = lambda: "shot.nk"
    parent.fullName = lambda: "root"
    host.root = lambda: parent
    source.node_name = "Group1.Read1"
    source.selected = selected
    target.selected = selected
    other.setSelected = Mock(side_effect=AssertionError("Must not change selection"))
    source.setSelected = target.setSelected = other.setSelected
    before = [(n.fullName(), n.xpos(), n.ypos(), list(n.connections), n.selected)
              for n in parent.nodes()]
    result = module.get_selected_nodes({})["result"]
    assert result["script"] == "shot.nk"
    assert result["count"] == (2 if selected else 0)
    assert [n["name"] for n in result["nodes"]] == (
        ["Group1.Read1", "Target"] if selected else []
    )
    if selected:
        assert result["nodes"][1] == {
            "name": "Target", "class": "Grade", "group": "root",
            "xpos": 200, "ypos": 200, "inputs": ["Group1.Read1"],
        }
    assert before == [(n.fullName(), n.xpos(), n.ypos(), list(n.connections), n.selected)
                      for n in parent.nodes()]


def test_selection_includes_nested_groups_with_duplicate_short_names(graph):
    from test_nuke_graph_tools import Node

    module, host, parent, source, target, other = graph
    parent.name = lambda: "shot.nk"
    parent.fullName = lambda: "root"
    host.root = lambda: parent
    group = Node(parent, "Group1", "Group")
    child = Node(group, "Group1.Read1", "Read")
    nested = Node(group, "Group1.Group2", "Group")
    grandchild = Node(nested, "Group1.Group2.Read1", "Read")
    child.selected = grandchild.selected = True
    source.node_name, source.selected = "Read1", True
    result = module.get_selected_nodes({})["result"]
    assert result["count"] == 3
    assert [n["name"] for n in result["nodes"]] == [
        "Group1.Group2.Read1", "Group1.Read1", "Read1",
    ]
    assert [n["group"] for n in result["nodes"]] == ["Group1.Group2", "Group1", "root"]


@pytest.mark.asyncio
async def test_mcp_selection_is_registered_read_only_without_task_gate(tmp_path, monkeypatch):
    from fx_agent.nuke_server import build_server

    monkeypatch.setenv("FX_AGENT_DATA_ROOT", str(tmp_path))
    response = {"script": "shot.nk", "count": 0, "nodes": []}
    connection = SimpleNamespace(send_command=Mock(return_value=response))
    server = build_server(connection)
    tool = next(t for t in await server.list_tools() if t.name == "get_selected_nodes")
    assert tool.annotations.readOnlyHint is True
    assert "fx_task_id" not in tool.inputSchema.get("required", [])
    result = await server.call_tool("get_selected_nodes", {})
    assert "shot.nk" in str(result)
    connection.send_command.assert_called_once_with("get_selected_nodes")


def test_selection_command_is_installed(graph):
    module, *_ = graph
    addon = SimpleNamespace(COMMANDS={})
    module.install(addon)
    assert addon.COMMANDS["get_selected_nodes"] is module.get_selected_nodes
