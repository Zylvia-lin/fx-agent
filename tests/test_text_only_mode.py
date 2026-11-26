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

REMOVED_LEGACY_PROJECT_TOOLS = (
    "fx_archive_case",
    "fx_get_case",
    "fx_validate_case",
    "fx_plan_effect_study",
    "fx_get_effect_learning_queue",
    "fx_refine_effect",
    "fx_plan_template_import",
    "fx_learn_templates",
    "fx_search_templates",
    "fx_get_template",
    "fx_validate_template",
    "fx_export_template",
    "fx_learn_template_experience",
    "fx_annotate_template_effects",
    "fx_merge_effect_cards",
    "fx_search_effects",
    "fx_get_effect",
    "fx_review_template_effects",
    "fx_import_data",
    "fx_get_import_records",
    "fx_resolve_import_memory",
)


def test_default_tool_profile_is_text_only_and_hides_legacy_project_tools(monkeypatch):
    monkeypatch.delenv("FX_AGENT_TOOL_PROFILE", raising=False)
    assert resolve_tool_profile() == "text_only"

    get_server.cache_clear()
    server = get_server()
    tool_names = set(server._tool_manager._tools.keys())
    for legacy in set(TEXT_ONLY_HIDDEN_FX_TOOLS) | set(REMOVED_LEGACY_PROJECT_TOOLS):
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


def test_task_gate_handlers_are_async_for_upstream_dispatch(monkeypatch):
    import inspect

    monkeypatch.delenv("FX_AGENT_TOOL_PROFILE", raising=False)
    get_server.cache_clear()
    server = get_server()
    for name in ("fx_begin_task", "fx_set_task_plan", "fx_finish_task"):
        tool = server._tool_manager._tools[name]
        assert inspect.iscoroutinefunction(tool.fn)


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
        source_type="user",
        source_ref="现场核验通过",
    )
    assert admitted["decision"] == "admitted"
    assert admitted["status"] == "active"
    assert admitted["evidence_status"] == "user_reported"
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
async def test_text_only_production_task_and_learning_without_case_archive(tmp_path, monkeypatch, record_preview):
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
    for frame in (101, 125, 150):
        record_preview(gates, tid, frame)
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


@pytest.mark.asyncio
async def test_prepare_task_brief_small_budget_four_modules_and_content_trimming(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("FX_AGENT_DATA_ROOT", str(tmp_path))
    store = MemoryStore(tmp_path)
    for mod in ("sop", "particles", "pyro", "materials"):
        store.submit(
            software="houdini",
            modules=[mod],
            kind="workflow",
            title=f"{mod} 长篇通用经验",
            method="通用工艺步骤说明。" * 80,
            reason="保持结构清晰并验证多模块预算裁剪逻辑。",
            applies_when="通用多模块特效制作",
            source_type="observed",
            source_ref="live verification",
        )

    gates = TaskGates(tmp_path)
    task = gates.begin(
        TaskSpec(
            project_id="proj_b",
            shot_id="sh020",
            software="houdini",
            kind="production",
            scene_path=str(tmp_path / "multi.hip"),
            endpoint="127.0.0.1:18186",
            goal="多模块火焰粒子联合测试",
            acceptance=["无报错且画面达标"],
            budget="低预算验证",
            modules=["sop", "particles", "pyro", "materials"],
        )
    )
    from fx_agent.store import compact

    tid = task["task_id"]
    brief = await fx_prepare_task_brief(task_id=tid, max_chars=2048)
    assert len(compact(brief)) <= 2048
    assert "fx_get_module_knowledge" in brief["deferred_reads"]
    for mod in {"sop", "particles", "pyro", "materials"} - set(brief.get("module_knowledge", {})):
        assert f"module:{mod}" in brief["task_state"]["missing"]


@pytest.mark.asyncio
async def test_maya_mcp_exposes_published_template_search(tmp_path, monkeypatch):
    from fx_agent.maya.server import build_server

    monkeypatch.setenv("FX_AGENT_DATA_ROOT", str(tmp_path))
    monkeypatch.delenv("FX_AGENT_TOOL_PROFILE", raising=False)
    server = build_server()
    names = {tool.name for tool in await server.list_tools()}
    assert "fx_search_published_templates" in names
    assert not (set(REMOVED_LEGACY_PROJECT_TOOLS) & names)


def test_store_v11_uses_delete_journal_mode_and_drops_legacy_tables(tmp_path):
    import sqlite3
    from contextlib import closing

    from fx_agent.store import Store

    db_path = tmp_path / "memory.sqlite3"
    with closing(sqlite3.connect(db_path)) as db, db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE template_studies (study_id TEXT PRIMARY KEY)")
        db.execute("CREATE TABLE template_effects (card_id TEXT PRIMARY KEY)")
        db.execute("CREATE TABLE template_effect_reviews (review_id TEXT PRIMARY KEY)")
        db.execute("CREATE TABLE import_runs (import_id TEXT PRIMARY KEY)")
        db.execute("CREATE TABLE memory_conflicts (memory_id TEXT, import_id TEXT)")
        db.execute("PRAGMA user_version=10")

    Store(tmp_path)
    with closing(sqlite3.connect(db_path)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 11
        assert db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert not (
            {
                "template_studies",
                "template_effects",
                "template_effect_reviews",
                "import_runs",
                "memory_conflicts",
            }
            & tables
        )


def test_user_rule_rejects_local_drive_and_unc_paths(tmp_path):
    store = LearningStore(tmp_path)
    for bad in (r"输出统一放到 D:\Project\cache", r"从 \\NAS-01\share\assets 读取"):
        with pytest.raises(ValueError, match="absolute paths"):
            store.record_rule(
                rule_key="bad-path-rule",
                title="错误路径规范",
                statement=bad,
                user_quote="test",
                source_ref="test",
                scope="global",
                expected_revision=0,
            )


def test_production_rules_have_no_legacy_archive_requirements():
    from fx_agent.production_rules import production_rules

    for software in ("houdini", "nuke", "maya"):
        text = json.dumps(production_rules(software), ensure_ascii=False).lower()
        assert "independent archive" not in text


def test_embeddings_stdlib_http_client_parses_openai_response(monkeypatch):
    import io
    import urllib.request

    from fx_agent.embeddings import embed_texts, set_embedder

    class FakeResp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    class FakeOpener:
        def open(self, req, timeout=8.0):
            assert req.get_method() == "POST"
            assert req.headers.get("Authorization") == "Bearer test-key"
            body = json.dumps(
                {"data": [{"index": 1, "embedding": [0.0, 2.0]}, {"index": 0, "embedding": [3.0, 0.0]}]}
            ).encode("utf-8")
            return FakeResp(body)

    set_embedder(None)
    monkeypatch.setenv("FX_AGENT_EMBEDDING_URL", "http://127.0.0.1:9999/v1/embeddings")
    monkeypatch.setenv("FX_AGENT_EMBEDDING_API_KEY", "test-key")
    monkeypatch.setattr(urllib.request, "build_opener", lambda *args: FakeOpener())
    vectors = embed_texts(["first", "second"])
    assert vectors == [[1.0, 0.0], [0.0, 1.0]]
