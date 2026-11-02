"""Resume projects without trusting stale state or overwriting newer progress."""

import importlib
import json
import sqlite3

import pytest


def store_at(path):
    return importlib.import_module("fx_agent.store").Store(path)


def save(store, **changes):
    values = {
        "project_id": "广告A",
        "shot_id": "sh010",
        "summary": "烟尘草稿已完成，待检查逆光轮廓",
        "scene_path": "D:/shots/sh010/draft.hip",
        "next_steps": ["检查关键帧", "调整灯光"],
        "expected_revision": 0,
    }
    return store.save_checkpoint(**(values | changes))


def test_checkpoint_survives_new_store_and_keeps_scopes_separate(tmp_path):
    store = store_at(tmp_path)
    first = save(store)
    save(store, project_id="广告B", summary="另一工程")
    restored = store_at(tmp_path).get_checkpoint("广告A", "sh010")
    assert restored == first
    assert restored["revision"] == 1
    assert restored["next_steps"] == ["检查关键帧", "调整灯光"]
    assert "+00:00" in restored["updated_at"]
    assert store.get_checkpoint("广告A", "sh020") is None


def test_stale_write_does_not_erase_newer_checkpoint(tmp_path):
    store = store_at(tmp_path)
    save(store)
    second = save(store, expected_revision=1, summary="已验证第二版")
    with pytest.raises(ValueError, match="revision"):
        save(store, expected_revision=1, summary="旧聊天覆盖")
    assert store.get_checkpoint("广告A", "sh010") == second


def test_competing_writes_cannot_both_claim_the_same_revision(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    store = store_at(tmp_path)
    save(store)

    def attempt(summary):
        try:
            return save(store, expected_revision=1, summary=summary)["revision"]
        except ValueError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, ["版本一", "版本二"]))
    assert sorted(outcomes, key=str) == [2, "conflict"]


def test_empty_context_has_scope_specific_hash_and_checks_budget(tmp_path):
    store = store_at(tmp_path)
    context = store.get_context("p", "s")
    assert context["checkpoint"] is None
    assert context["content_hash"] != store.get_context("p", "other")["content_hash"]
    with pytest.raises(ValueError, match="max_chars"):
        store.get_context("p", "s", max_chars=10)
    with pytest.raises(ValueError):
        store.get_context(" ", "s")


def test_long_paths_are_omitted_instead_of_returned_as_broken_paths(tmp_path):
    store = store_at(tmp_path)
    save(store, scene_path="D:/" + "folder/" * 500 + "scene.hip")
    context = store.get_context("广告A", "sh010", max_chars=768)
    assert context["truncated"] is True
    assert "scene_path" not in context["checkpoint"]
    assert len(json.dumps(context, ensure_ascii=False, separators=(",", ":"))) <= 768


def test_escaped_identifiers_never_break_budget_or_truncation(tmp_path):
    store = store_at(tmp_path)
    escaped = '"' * 128
    save(store, project_id=escaped, shot_id=escaped)
    with pytest.raises(ValueError, match="max_chars"):
        store.get_context(escaped, escaped, max_chars=768)
    with pytest.raises(ValueError):
        store.get_context("x" + "\x00" * 126 + "x", "s", max_chars=768)
    context = store.get_context(escaped, escaped)
    same = store.get_context(escaped, escaped, since_hash=context["content_hash"], max_chars=768)
    assert len(json.dumps(same, ensure_ascii=False, separators=(",", ":"))) <= 768


@pytest.mark.parametrize("changes", [{"project_id": " "}, {"summary": ""}, {"next_steps": []}])
def test_empty_required_checkpoint_content_is_rejected(tmp_path, changes):
    with pytest.raises(ValueError):
        save(store_at(tmp_path), **changes)


def test_unknown_database_version_is_not_silently_reset(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    with sqlite3.connect(db_path) as db:
        db.execute("PRAGMA user_version=999")
    with pytest.raises(ValueError, match="schema"):
        store_at(tmp_path)
    with sqlite3.connect(db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 999


def test_context_is_bounded_and_can_return_only_changes(tmp_path):
    store = store_at(tmp_path)
    save(store, summary="烟尘" * 2000, next_steps=["灯光" * 300] * 10)
    context = store.get_context("广告A", "sh010", max_chars=800)
    assert len(json.dumps(context, ensure_ascii=False, separators=(",", ":"))) <= 800
    assert context["requires_live_verification"] is True
    assert context["truncated"] is True
    assert context["checkpoint"]["revision"] == 1
    same = store.get_context("广告A", "sh010", since_hash=context["content_hash"], max_chars=800)
    assert same["unchanged"] is True
    assert "checkpoint" not in same
    save(store, expected_revision=1, summary="新一版")
    changed = store.get_context("广告A", "sh010", since_hash=context["content_hash"])
    assert changed["unchanged"] is False


@pytest.mark.asyncio
async def test_checkpoint_tools_use_configured_persistent_root(tmp_path, monkeypatch):
    monkeypatch.setenv("FX_AGENT_DATA_ROOT", str(tmp_path))
    server = importlib.import_module("fx_agent.server").get_server()
    result = await server.call_tool(
        "fx_save_checkpoint",
        {
            "project_id": "p",
            "shot_id": "s",
            "summary": "draft",
            "scene_path": "D:/draft.hip",
            "next_steps": ["lookdev"],
            "expected_revision": 0,
        },
    )
    assert json.loads(result[0].text)["revision"] == 1
    result = await server.call_tool("fx_get_context", {"project_id": "p", "shot_id": "s"})
    assert json.loads(result[0].text)["checkpoint"]["summary"] == "draft"
