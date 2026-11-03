"""Module-specific recipes remain separate from immutable upstream guidelines."""

import json

import pytest

from fx_agent.learning import LearningStore
from fx_agent.modules import module_catalog


def experience(**changes):
    return (
        dict(
            project_id="p",
            kind="workflow",
            title="层次调整",
            method="先检查尺度再调整",
            reason="避免参数套错场景",
            applies_when="相同输入条件",
            source_type="user",
            source_ref="explicit user",
            project_dependent=False,
        )
        | changes
    )


def test_module_filter_and_multi_module_classification(tmp_path):
    store = LearningStore(tmp_path)
    pyro = store.submit(**experience(modules=["pyro", "sop"]))
    store.submit(**experience(modules=["rbd"]))
    assert [item["memory_id"] for item in store.search("p", "", module="pyro")["items"]] == [
        pyro["memory_id"]
    ]
    assert store.search("p", "尺度", module="sop")["items"][0]["modules"] == ["pyro", "sop"]
    assert store.search("p", "尺度", module="flip")["items"] == []
    assert store.submit(**experience(modules=["sop", "smoke", "pyro"]))["decision"] == "duplicate"
    with pytest.raises(ValueError):
        store.submit(**experience(software="nuke", modules=["pyro"]))
    with pytest.raises(ValueError):
        store.search("p", "尺度", module="nonexistent")


def test_legacy_records_are_unclassified_and_not_silently_mixed(tmp_path):
    store = LearningStore(tmp_path)
    old = store.submit(**experience())
    with store.connection() as db:
        row = db.execute(
            "SELECT payload FROM memories WHERE memory_id=?", (old["memory_id"],)
        ).fetchone()
        payload = json.loads(row[0])
        payload.pop("modules", None)
        db.execute(
            "UPDATE memories SET payload=? WHERE memory_id=?",
            (json.dumps(payload), old["memory_id"]),
        )
    assert store.get(old["memory_id"], "p")["modules"] == ["unclassified"]
    assert store.search("p", "尺度", module="pyro")["items"] == []
    assert len(store.search("p", "", module="unclassified")["items"]) == 1
    assert store.submit(**experience())["decision"] == "duplicate", (
        "Keep old default content hashes"
    )
    corrected = store.submit(**experience(modules=["sop"], supersedes=old["memory_id"]))
    assert store.get(corrected["memory_id"], "p")["modules"] == ["sop"]


def test_dream_never_merges_recipes_from_different_modules(tmp_path):
    store = LearningStore(tmp_path)
    store.submit(**experience(modules=["pyro"]))
    store.submit(**experience(modules=["rbd"]))
    assert store.dream("p")["actions"] == []


def test_finalized_learning_inherits_feedback_modules(tmp_path):
    from fx_agent.archive import CaseArchive

    hip, preview = tmp_path / "final.hip", tmp_path / "preview.png"
    hip.write_bytes(b"synthetic HIP")
    preview.write_bytes(b"synthetic image")
    store = LearningStore(tmp_path / "data")
    case = CaseArchive(store.root).create(
        project_id="p",
        shot_id="s",
        title="test",
        files=[{"path": str(hip), "role": "project"}, {"path": str(preview), "role": "preview"}],
    )
    feedback = store.record_feedback(
        project_id="p",
        shot_id="s",
        effect_type="卷浪",
        attempt_id="a",
        modules=["sop", "ocean"],
        problem="卷曲不正确",
        change="用控制曲线与Ramp",
        reason="便于调整",
        result="用户认可",
        outcome="accepted",
        user_quote="这个版本可以",
        source_ref="user",
        case_id=case["case_id"],
        expected_revision=0,
    )
    assert store.history("p", "s", module="pyro")["items"] == []
    assert len(store.history("p", "s", module="ocean")["items"]) == 1
    learned = store.finalize(feedback["feedback_id"], "p", "用curveu控制卷曲", "可调", "局部卷浪")
    assert store.get(learned["memory_id"], "p")["modules"] == ["ocean", "sop"]


def test_module_guides_are_available_without_live_houdini():
    from fxhoudinimcp.prompts.workflows import workflow_guide_text

    catalog = module_catalog("houdini")
    entries = {item["module"]: item for item in catalog["items"]}
    for key, topic in {
        "sop": "model",
        "particles": "dopparticles",
        "pyro": "pyro",
        "flip": "fluid",
        "rbd": "destruction",
    }.items():
        assert entries[key]["guide_topic"] == topic
    for item in catalog["items"]:
        if item["guide_topic"]:
            assert workflow_guide_text(item["guide_topic"]).strip()
    assert catalog["guidelines_are_learned_experience"] is False


@pytest.mark.asyncio
async def test_module_tools_publish_schemas_and_forward_filters(tmp_path, monkeypatch):
    from fx_agent.server import get_server

    monkeypatch.setenv("FX_AGENT_DATA_ROOT", str(tmp_path))
    store = LearningStore(tmp_path)
    item = store.submit(**experience(modules=["sop"]))
    server = get_server()
    tools = {tool.name: tool for tool in await server.list_tools()}
    assert len(tools) == 253
    assert "module" in tools["fx_search_memory"].inputSchema["properties"]
    assert "module" in tools["fx_get_learning_history"].inputSchema["properties"]
    assert "get_workflow_guide" in tools

    async def call(name, arguments):
        result = await server.call_tool(name, arguments)
        return json.loads(result[0].text)

    feedback = await call(
        "fx_record_feedback",
        {
            "feedback": dict(
                project_id="p",
                shot_id="s",
                effect_type="测试变形",
                attempt_id="first",
                modules=["sop"],
                problem="范围错误",
                change="待修改遮罩",
                reason="限定区域",
                result="待验证",
                outcome="pending",
                user_quote="其他区域不应变形",
                source_ref="synthetic test",
                expected_revision=0,
            )
        },
    )
    assert (await call("fx_status", {}))["module_taxonomy_version"] == 1
    catalog = await call("fx_get_module_catalog", {"software": "nuke"})
    assert "pyro" not in {entry["module"] for entry in catalog["items"]}
    found = await call("fx_search_memory", {"project_id": "p", "query": "", "module": "sop"})
    assert found["items"][0]["memory_id"] == item["memory_id"]
    assert (await call("fx_search_memory", {"project_id": "p", "query": "", "module": "pyro"}))[
        "items"
    ] == []
    assert (
        await call("fx_get_learning_history", {"project_id": "p", "shot_id": "s", "module": "sop"})
    )["items"][0]["feedback_id"] == feedback["feedback_id"]
    assert (
        await call("fx_get_learning_history", {"project_id": "p", "shot_id": "s", "module": "pyro"})
    )["items"] == []
    guide = await call("fx_get_module_knowledge", {"project_id": "p", "module": "sop"})
    assert guide["guide_topic"] == "model"
    assert not guide["available"], "Direct submissions are not finalized module workflows"
    assert (await call("fx_refresh_module_guides", {"project_id": "p"}))["state"] == "current"
