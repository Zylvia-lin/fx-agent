"""Admit reusable knowledge with provenance, scope and traceable corrections."""

import importlib
import json
import sqlite3

import pytest


def memories(root):
    return importlib.import_module("fx_agent.memory").MemoryStore(root)


def experience(**changes):
    return {
        "project_id": "广告A",
        "shot_id": "sh010",
        "kind": "lighting",
        "title": "逆光烟尘的层次控制",
        "method": "先检查灯光方向与体积轮廓，再调整局部对比。",
        "reason": "避免整体抬亮导致背景和噪点同时变亮。",
        "applies_when": "低机位逆光烟尘草稿",
        "source_type": "inferred",
        "source_ref": "阶段检查记录：尚未验证",
        "project_dependent": False,
    } | changes


def test_unverified_ideas_stay_candidates_and_are_not_default_recall(tmp_path):
    store = memories(tmp_path)
    result = store.submit(**experience())
    assert result["decision"] == "candidate"
    assert store.search("广告A", "逆光")["items"] == []
    found = store.search("广告A", "逆光", include_candidates=True)
    assert found["items"][0]["memory_id"] == result["memory_id"]
    assert found["items"][0]["evidence_status"] == "unverified"


def test_explicit_user_preference_is_saved_and_duplicates_are_not_reinserted(tmp_path):
    store = memories(tmp_path)
    values = experience(
        kind="preference",
        title="节点排版",
        method="不要自动移动既有节点。",
        source_type="user",
        reason="",
        applies_when="",
        shot_id="",
    )
    first = store.submit(**values)
    duplicate = store.submit(**(values | {"source_ref": "用户再次强调"}))
    assert first["decision"] == "admitted"
    assert duplicate["decision"] == "duplicate"
    assert first["memory_id"] == duplicate["memory_id"]
    assert len(memories(tmp_path).search("广告A", "节点")["items"]) == 1
    assert store.search("其他项目", "节点")["items"] == []


def test_non_reusable_output_is_rejected_without_inserting(tmp_path):
    store = memories(tmp_path)
    result = store.submit(**experience(reason="", applies_when=""))
    assert result["decision"] == "rejected"
    assert store.search("广告A", "逆光", include_candidates=True)["items"] == []


def test_engineering_memory_requires_an_independent_project_archive(tmp_path):
    store = memories(tmp_path)
    candidate = store.submit(**experience(source_type="user", project_dependent=True))
    assert candidate["decision"] == "candidate"
    assert "archive" in candidate["reason"]
    with pytest.raises(ValueError):
        store.submit(**experience(case_id="../missing", project_dependent=True))


def test_archived_project_is_traceable_but_does_not_claim_visual_verification(tmp_path):
    archive = importlib.import_module("fx_agent.archive").CaseArchive(tmp_path)
    hip = tmp_path / "scene.hip"
    hip.write_bytes(b"test fixture")
    case = archive.create(
        project_id="广告A",
        shot_id="sh010",
        title="test",
        files=[{"path": str(hip), "role": "project"}],
    )
    store = memories(tmp_path)
    result = store.submit(
        **experience(source_type="user", project_dependent=True, case_id=case["case_id"])
    )
    assert result["decision"] == "admitted"
    record = store.get(result["memory_id"], "广告A")
    assert record["case_id"] == case["case_id"]
    assert record["evidence_status"] == "user_reported_with_archive"
    assert record["visual_verified"] is False
    assert record["reproduction_status"] == "not_tested"
    with pytest.raises(ValueError, match="project"):
        store.get(result["memory_id"], "其他项目")


def test_user_correction_supersedes_old_memory_and_retains_history(tmp_path):
    store = memories(tmp_path)
    first = store.submit(**experience(source_type="user"))
    replacement = store.submit(
        **experience(source_type="user", method="修正后的灯光方法。", supersedes=first["memory_id"])
    )
    assert replacement["decision"] == "admitted"
    assert store.get(first["memory_id"], "广告A")["status"] == "superseded"
    items = store.search("广告A", "逆光")["items"]
    assert [item["memory_id"] for item in items] == [replacement["memory_id"]]
    with pytest.raises(ValueError, match="user"):
        store.submit(**experience(supersedes=replacement["memory_id"]))


def test_search_is_bounded_literal_and_does_not_cross_shots(tmp_path):
    store = memories(tmp_path)
    for index in range(8):
        store.submit(
            **experience(title=f"逆光层次 {index}", source_type="user", method="烟尘层次" * 500)
        )
    assert store.search("广告A", "%")["items"] == []
    assert store.search("广告A", "")["items"] == []
    assert store.search("广告A", "逆光", shot_id="sh020")["items"] == []
    results = store.search("广告A", "逆光 烟尘", max_chars=900)
    assert results["items"]
    assert len(json.dumps(results, ensure_ascii=False, separators=(",", ":"))) <= 900
    with pytest.raises(ValueError):
        store.search("广告A", "逆光", limit=1000)


def test_database_upgrade_preserves_existing_checkpoint(tmp_path):
    module = importlib.import_module("fx_agent.store")
    store = module.Store(tmp_path)
    store.save_checkpoint(
        project_id="p",
        shot_id="s",
        summary="draft",
        scene_path="D:/scene.hip",
        next_steps=["render"],
        expected_revision=0,
    )
    with sqlite3.connect(store.path) as db:
        db.execute("PRAGMA user_version=1")
    memories(tmp_path)
    assert module.Store(tmp_path).get_checkpoint("p", "s")["summary"] == "draft"


@pytest.mark.asyncio
async def test_memory_tools_return_summary_then_expand_on_request(tmp_path, monkeypatch):
    monkeypatch.setenv("FX_AGENT_DATA_ROOT", str(tmp_path))
    server = importlib.import_module("fx_agent.server").get_server()
    result = await server.call_tool(
        "fx_submit_experience", {"experience": experience(source_type="user")}
    )
    memory_id = json.loads(result[0].text)["memory_id"]
    result = await server.call_tool("fx_search_memory", {"project_id": "广告A", "query": "逆光"})
    assert json.loads(result[0].text)["items"][0]["memory_id"] == memory_id
    result = await server.call_tool(
        "fx_get_memory", {"project_id": "广告A", "memory_id": memory_id}
    )
    assert json.loads(result[0].text)["reason"] == experience()["reason"]
