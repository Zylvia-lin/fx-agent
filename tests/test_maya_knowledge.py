"""Maya uses the existing database, archive, task gate and learned module views."""

import pytest

from fx_agent.archive import CaseArchive
from fx_agent.memory import MemoryStore
from fx_agent.modules import module_catalog
from fx_agent.store import Store
from fx_agent.task_gates import TaskGates, TaskSpec
from fx_agent.template_extract import extract_project


def test_maya_archive_memories_and_checkpoints_keep_software_scopes(tmp_path):
    scene = tmp_path / "灯光.ma"
    scene.write_text("// Maya fixture", encoding="utf-8")
    archive = CaseArchive(tmp_path)
    case = archive.create(project_id="p", shot_id="s", title="Maya fixture",
                          files=[{"path": str(scene), "role": "project"}])
    assert case["files"][0]["archive_path"].startswith("projects/maya/")
    memory = MemoryStore(tmp_path)
    base = dict(project_id="p", shot_id="s", kind="lighting", title="Maya灯光",
                method="按实际节点属性修改曝光", reason="插件版本属性不同", applies_when="灯光",
                source_type="user", source_ref="test fixture", project_dependent=True,
                case_id=case["case_id"], modules=["lighting"], software="maya")
    record = memory.submit(**base)
    assert record["decision"] == "admitted"
    assert memory.search("p", "", software="maya", module="lighting")["items"]
    assert memory.search("p", "", software="houdini", module="lighting")["items"] == []
    assert memory.search("p", "", software="nuke")["items"] == []
    store = Store(tmp_path)
    store.save_checkpoint(project_id="p", shot_id="s", software="maya", summary="灯光已创建",
                          scene_path=str(scene), next_steps=["检查渲染"], expected_revision=0)
    assert store.get_checkpoint("p", "s", software="maya")["software"] == "maya"
    assert store.get_checkpoint("p", "s", software="houdini") is None
    assert archive.verify(case["case_id"], "p")["valid"]


def test_maya_catalog_task_and_unsupported_parser(tmp_path):
    assert module_catalog("maya")["workflow_skill"] == "maya-workflow"
    spec = TaskSpec(project_id="p", shot_id="s", software="maya", scene_path="D:/灯光.ma",
                    endpoint="127.0.0.1:18770", goal="布光", acceptance=["实际渲染"],
                    budget="64x64", modules=["lighting"])
    gates = TaskGates(tmp_path)
    task = gates.begin(spec)
    assert task["software"] == "maya"
    with pytest.raises(ValueError, match="knowledge"):
        gates.plan(task["task_id"], 1, "方法", "条件", "修改", [], "无适用记录")
    with pytest.raises(ValueError, match="not implemented"):
        extract_project(b"//Maya", "maya")


@pytest.mark.asyncio
async def test_maya_mcp_registers_shared_tools_and_blocks_ungated_writes(tmp_path, monkeypatch):
    from fx_agent.maya.server import build_server

    monkeypatch.setenv("FX_AGENT_DATA_ROOT", str(tmp_path))

    class Connection:
        target = None

        def send(self, *args):
            raise AssertionError("Ungated operation must not reach Maya")

    server = build_server(Connection())
    tools = {tool.name: tool for tool in await server.list_tools()}
    for name in ("create_maya_light", "update_maya_light", "set_maya_hdri", "set_maya_light_link",
                 "execute_maya_python", "render_maya_frame", "save_scene", "load_scene"):
        assert "fx_task_id" in tools[name].inputSchema["required"]
        with pytest.raises(ValueError, match="Task gate"):
            await server._tool_manager.call_tool(name, {})
    assert "fx_task_id" not in tools["connect_maya"].inputSchema.get("required", [])
    assert "start_maya" in tools
    assert "fx_task_id" not in tools["start_maya"].inputSchema.get("required", [])
    assert "fx_search_memory" in tools and "fx_finish_task" in tools
    assert "create_node" not in tools and "fx_render_view_start" not in tools
    schema = tools["fx_search_memory"].inputSchema
    assert "maya" in schema["properties"]["software"]["enum"]
