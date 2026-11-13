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


def test_empty_active_recall_points_to_scoped_references_without_promoting_them(tmp_path):
    store = memories(tmp_path)
    saved = store.submit(**experience(modules=["materials"], reuse_scope="global"))
    result = store.search("其他项目", "逆光", module="materials")
    assert result["items"] == []
    hint = result["reference_hint"]
    assert hint["has_matches"] is True
    assert hint["next_call"]["include_candidates"] is True
    assert hint["repeat_same_arguments"] is True
    assert len(json.dumps(store.search("其他项目", "逆光", module="materials", max_chars=512),
                          ensure_ascii=False, separators=(",", ":"))) <= 512
    assert store.get(saved["memory_id"])["status"] == "candidate"
    assert "reference_hint" not in store.search("其他项目", "不存在", module="materials")
    assert "reference_hint" not in store.search("其他项目", "逆光", module="particles")


def test_reference_hint_survives_long_candidate_with_minimum_budget(tmp_path):
    store = memories(tmp_path)
    store.submit(**experience(title="逆光" + "烟" * 180, method="烟" * 1000,
                              modules=["materials"], reuse_scope="global"))
    result = store.search("其他项目", "逆光", module="materials", max_chars=512)
    assert result["items"] == [] and result["reference_hint"]["has_matches"]
    assert len(json.dumps(result, ensure_ascii=False, separators=(",", ":"))) <= 512


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
    assert store.search("其他项目", "节点")["items"][0]["memory_id"] == first["memory_id"]


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
    assert store.get(result["memory_id"], "其他项目")["project_id"] == "广告A"


def test_observation_needs_archived_output_and_corrupt_evidence_is_rejected(tmp_path):
    archive = importlib.import_module("fx_agent.archive").CaseArchive(tmp_path)
    hip, preview = tmp_path / "scene.hip", tmp_path / "preview.png"
    hip.write_bytes(b"fixture hip")
    preview.write_bytes(b"fixture preview")
    case = archive.create(
        project_id="广告A",
        shot_id="sh010",
        title="draft",
        files=[{"path": str(hip), "role": "project"}, {"path": str(preview), "role": "preview"}],
    )
    store = memories(tmp_path)
    result = store.submit(
        **experience(
            case_id=case["case_id"],
            source_type="observed",
            project_dependent=True,
        )
    )
    assert result["decision"] == "admitted"
    assert result["evidence_status"] == "codex_observation_with_archive"
    with pytest.raises(ValueError, match="shot"):
        store.submit(**experience(case_id=case["case_id"], shot_id="sh020"))
    from pathlib import Path

    (Path(case["case_path"]) / case["files"][1]["archive_path"]).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="checksum"):
        store.submit(**experience(case_id=case["case_id"]))


def test_project_dependent_memory_cannot_use_a_reference_only_case(tmp_path):
    archive = importlib.import_module("fx_agent.archive").CaseArchive(tmp_path)
    image = tmp_path / "reference.png"
    image.write_bytes(b"fixture")
    case = archive.create(
        project_id="广告A",
        shot_id="sh010",
        title="reference",
        files=[{"path": str(image), "role": "reference"}],
    )
    with pytest.raises(ValueError, match="archived project"):
        memories(tmp_path).submit(**experience(case_id=case["case_id"], project_dependent=True))


def test_user_correction_supersedes_old_memory_and_retains_history(tmp_path):
    store = memories(tmp_path)
    first = store.submit(**experience(source_type="user"))
    replacement = store.submit(
        **experience(source_type="user", method="修正后的灯光方法。", supersedes=first["memory_id"])
    )
    assert replacement["decision"] == "admitted"
    repeated = store.submit(
        **experience(source_type="user", method="修正后的灯光方法。", supersedes=first["memory_id"])
    )
    assert repeated["decision"] == "duplicate"
    assert repeated["memory_id"] == replacement["memory_id"]
    assert store.get(first["memory_id"], "广告A")["status"] == "superseded"
    items = store.search("广告A", "逆光")["items"]
    assert [item["memory_id"] for item in items] == [replacement["memory_id"]]
    with pytest.raises(ValueError, match="user"):
        store.submit(**experience(supersedes=replacement["memory_id"]))
    with pytest.raises(ValueError, match="project"):
        store.submit(**experience(source_type="user", supersedes="missing-memory"))
    with pytest.raises(ValueError, match="current"):
        store.submit(
            **experience(source_type="user", method="过期纠正", supersedes=first["memory_id"])
        )
    with pytest.raises(ValueError, match="distinct"):
        store.submit(
            **experience(
                source_type="user", method="修正后的灯光方法。", supersedes=replacement["memory_id"]
            )
        )
    reverted = store.submit(**experience(source_type="user", supersedes=replacement["memory_id"]))
    assert reverted["decision"] == "admitted"
    assert store.get(replacement["memory_id"], "广告A")["status"] == "superseded"
    assert store.get(reverted["memory_id"], "广告A")["method"] == experience()["method"]


def test_correction_cannot_silently_move_memory_to_another_shot(tmp_path):
    store = memories(tmp_path)
    original = store.submit(**experience(source_type="user"))
    with pytest.raises(ValueError, match="shot"):
        store.submit(
            **experience(
                source_type="user",
                shot_id="sh020",
                method="另一镜头做法",
                supersedes=original["memory_id"],
            )
        )
    assert store.get(original["memory_id"], "广告A")["status"] == "active"


def test_competing_corrections_only_replace_the_current_revision_once(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    store = memories(tmp_path)
    first = store.submit(**experience(source_type="user"))

    def replace(method):
        try:
            return store.submit(
                **experience(source_type="user", method=method, supersedes=first["memory_id"])
            )["decision"]
        except ValueError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(replace, ["修正一", "修正二"]))
    assert sorted(results) == ["admitted", "conflict"]
    assert len(store.search("广告A", "逆光")["items"]) == 1


def test_search_is_bounded_literal_and_does_not_cross_shots(tmp_path):
    store = memories(tmp_path)
    for index in range(8):
        store.submit(
            **experience(title=f"逆光层次 {index}", source_type="user", method="烟尘层次" * 500, reuse_scope="shot")
        )
    assert store.search("广告A", "%")["items"] == []
    assert store.search("广告A", "")["items"] == []
    assert store.search("广告A", "逆光", shot_id="sh020")["items"] == []
    results = store.search("广告A", "逆光 烟尘", max_chars=900)
    assert results["items"]
    assert len(json.dumps(results, ensure_ascii=False, separators=(",", ":"))) <= 900
    with pytest.raises(ValueError):
        store.search("广告A", "逆光", limit=1000)


def test_search_uses_a_consistent_snapshot_when_correction_happens_mid_read(tmp_path, monkeypatch):
    from contextlib import contextmanager

    store = memories(tmp_path)
    first = store.submit(**experience(source_type="user"))
    original_connection = store.connection
    corrected = False

    class CursorAfterCorrection:
        def __init__(self, cursor):
            self.cursor = cursor

        def fetchall(self):
            nonlocal corrected
            rows = self.cursor.fetchall()
            if not corrected:
                corrected = True
                store.submit(
                    **experience(
                        source_type="user", method="并发修正版", supersedes=first["memory_id"]
                    )
                )
            return rows

    class Connection:
        def __init__(self, db):
            self.db = db

        def execute(self, sql, *args):
            cursor = self.db.execute(sql, *args)
            if sql.startswith("SELECT") and "status='active'" in sql:
                return CursorAfterCorrection(cursor)
            return cursor

    @contextmanager
    def connection():
        with original_connection() as db:
            yield Connection(db)

    monkeypatch.setattr(store, "connection", connection)
    result = store.search("广告A", "逆光")
    assert corrected
    assert all(item["status"] == "active" for item in result["items"])
    assert store.get(first["memory_id"], "广告A")["status"] == "superseded"


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
