"""End-to-end verification of the text-only reusable experience architecture."""

import json

import pytest

from fx_agent.data_api import fx_prepare_task_brief
from fx_agent.learning import LearningStore
from fx_agent.memory import MemoryStore
from fx_agent.module_guides import ModuleGuides
from fx_agent.server import get_server
from fx_agent.task_gates import TaskGates, TaskSpec, check_knowledge_gate
from fx_agent.tool_profiles import TEXT_ONLY_HIDDEN_FX_TOOLS, resolve_tool_profile


def test_default_tool_profile_is_text_only_and_hides_legacy_project_tools(monkeypatch):
    monkeypatch.delenv("FX_AGENT_TOOL_PROFILE", raising=False)
    assert resolve_tool_profile() == "text_only"

    get_server.cache_clear()
    server = get_server()
    tool_names = set(server._tool_manager._tools.keys())
    for legacy in TEXT_ONLY_HIDDEN_FX_TOOLS:
        assert legacy not in tool_names
    for core in (
        "fx_status",
        "fx_get_production_rules",
        "fx_record_user_rule",
        "fx_submit_experience",
        "fx_search_memory",
        "fx_get_memory",
        "fx_get_module_knowledge",
        "fx_prepare_task_brief",
        "fx_search_published_templates",
        "fx_begin_task",
        "fx_set_task_plan",
        "fx_finish_task",
    ):
        assert core in tool_names


def test_text_only_experience_admission_hygiene_and_auto_guide_sync(tmp_path):
    store = MemoryStore(tmp_path)

    # Local or UNC paths are rejected in reusable text-only experiences.
    for bad_path in (r"C:\Users\artist\shot01.hip", r"\\Desktop-abc\share\shot.nk"):
        with pytest.raises(ValueError, match="absolute paths"):
            store.submit(
                software="houdini",
                modules=["sop"],
                kind="workflow",
                title="错误夹带盘符路径",
                method=f"读取 {bad_path} 节点",
                reason="测试脱敏拦截",
                applies_when="通用SOP",
                source_type="observed",
                source_ref="live verification",
            )

    # Relative $HIP paths and Houdini node paths are allowed and admitted directly into active + module guide.
    admitted = store.submit(
        software="houdini",
        modules=["sop", "io"],
        kind="troubleshooting",
        title="HDA视口正常但ROP写出旧缓存排查",
        method="核对 /obj 内 HDA 的 displayNode 与 renderNode，将最终输出同时设双旗标并用 $HIP/geo/$OS/$OS.$F4.bgeo.sc 写盘验证。",
        reason="ROP 默认读取 Render 旗标而非 Display 旗标。",
        applies_when="SOP HDA 视口与缓存输出不一致时",
        source_type="observed",
        source_ref="现场核验通过",
    )
    assert admitted["decision"] == "admitted"
    assert admitted["status"] == "active"
    assert admitted["evidence_status"] == "live_verified_text"
    assert admitted["module_guides"]["state"] == "updated"

    # Module Markdown guide is automatically updated with clean source line (no empty feedback=/case=).
    guide = ModuleGuides(tmp_path).get(software="houdini", module="sop")
    assert guide["available"]
    assert "HDA视口正常但ROP写出旧缓存排查" in guide["content"]
    assert f"来源：memory={admitted['memory_id']}" in guide["content"]
    assert "case=" not in guide["content"]
    assert not (tmp_path / "cases").exists()

    # Inferred hypotheses remain non-default candidates.
    inferred = store.submit(
        software="houdini",
        modules=["sop"],
        kind="workflow",
        title="未验证猜想",
        method="猜测调整子步即可",
        reason="尚未验证",
        applies_when="任意模拟",
        source_type="inferred",
        source_ref="待验证",
    )
    assert inferred["decision"] == "candidate"
    assert inferred["status"] == "candidate"


@pytest.mark.asyncio
async def test_text_only_production_task_and_learning_without_case_archive(tmp_path, monkeypatch):
    monkeypatch.setenv("FX_AGENT_DATA_ROOT", str(tmp_path))
    scene = tmp_path / "demo_v01.hip"
    scene.write_text("synthetic hip", encoding="utf-8")

    gates = TaskGates(tmp_path)
    task = gates.begin(
        TaskSpec(
            project_id="proj_a",
            shot_id="sh010",
            software="houdini",
            kind="production",
            scene_path=str(scene),
            endpoint="127.0.0.1:18186",
            goal="程序化光丝效果制作",
            acceptance=["多帧视觉检查通过"],
            budget="轻量化快速验证",
            modules=["sop"],
        )
    )
    tid = task["task_id"]

    # Brief no longer requires fx_search_effects and provides external template library hint.
    brief = await fx_prepare_task_brief(task_id=tid, query="光丝")
    assert "effect_matches" not in brief
    assert "fx_search_published_templates" in brief["template_library_hint"]

    # Satisfy Houdini upstream guides.
    gates.observe(tid, "get_workflow_guide", {"topic": "discipline"}, {"ok": True})
    gates.observe(tid, "get_workflow_guide", {"topic": "model"}, {"ok": True})
    assert gates.get(tid)["missing"] == []

    # Plan, edit, save, verify, and finish WITHOUT any case_id / archive.
    planned = gates.plan(tid, 1, "使用管面双噪声制作光丝", "通用力场光丝", "搭建 SOP 网络", [], "当前无历史匹配")
    assert planned["state"] == "ready"
    gates.edit(tid)
    gates.observe(tid, "save_scene", {}, {"saved": True})
    gates.observe(tid, "verify_network", {}, {"healthy": True})
    finished = gates.finish(tid, planned["revision"], "", "已查看 101/125/150 三帧，光丝层次清晰")
    assert finished["state"] == "verified"
    assert finished["case_id"] == ""

    # Record accepted text-only feedback and finalize into reusable global text experience.
    store = LearningStore(tmp_path)
    fb_args = dict(
        project_id="proj_a",
        shot_id="sh010",
        software="houdini",
        modules=["sop"],
        effect_type="程序化光丝",
        attempt_id="v1",
        problem="初版光丝过于均匀",
        change="采用高低频双各向异性噪声相乘并独立控制 Alpha Ramp",
        reason="分离纹理明暗与边缘透明度",
        result="用户确认层次清晰",
        outcome="accepted",
        user_quote="这个版本层次很好，可以",
        source_ref="用户审片确认",
        expected_revision=0,
    )
    check_knowledge_gate(gates, tid, tool_name="fx_record_feedback", arguments={"feedback": fb_args})
    fb = store.record_feedback(**fb_args)
    check_knowledge_gate(
        gates,
        tid,
        tool_name="fx_finalize_learning",
        arguments={"feedback_id": fb["feedback_id"], "project_id": "proj_a"},
    )
    finalized = store.finalize(
        fb["feedback_id"],
        "proj_a",
        "管面叠加双尺度各向异性噪声写入Cd，用相对包围盒Ramp单独控制Alpha",
        "纹理与透明度解耦且无需解算器",
        "程序化能量光丝与气浪条带",
    )
    assert finalized["decision"] == "admitted"
    assert finalized["evidence_status"] == "live_verified_text"
    assert finalized["module_guides"]["state"] == "updated"

    # Zero project files or case archives were created.
    assert not (tmp_path / "cases").exists()
    recalled = store.search(query="各向异性噪声", software="houdini", module="sop")
    assert recalled["items"][0]["memory_id"] == finalized["memory_id"]

    # fx_status advertises text_only knowledge mode.
    get_server.cache_clear()
    status_resp = await get_server().call_tool("fx_status", {})
    status = json.loads(status_resp[0].text)
    assert status["knowledge_mode"] == "text_only"
    assert status["template_library_tool"] == "fx_search_published_templates"
