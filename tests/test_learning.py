"""Dialogue requirements and correction histories must improve subsequent recall."""

import importlib
import json

import pytest


def learning(root):
    return importlib.import_module("fx_agent.learning").LearningStore(root)


def rule(**changes):
    return {
        "rule_key": "cache-layout",
        "title": "缓存目录",
        "statement": "$HIP/geo/$OS/$OS.$F4.bgeo.sc",
        "user_quote": "filcache节点的存储为$HIP/geo/$OS/$OS.$F4.bgeo.sc",
        "source_ref": "用户对话 2026-11-02",
        "scope": "global",
        "expected_revision": 0,
    } | changes


def feedback(**changes):
    return {
        "project_id": "p",
        "shot_id": "s",
        "effect_type": "逆光烟尘",
        "attempt_id": "lighting-1",
        "problem": "边缘缺少层次",
        "change": "调整逆光方向并降低整体提亮",
        "reason": "避免背景一起变亮",
        "result": "轮廓更清楚，等待最终确认",
        "outcome": "improved",
        "user_quote": "轮廓再亮一些，背景不要一起亮",
        "source_ref": "对话中的纠错",
        "expected_revision": 0,
    } | changes


def case_at(root, name="draft"):
    from fx_agent.archive import CaseArchive

    hip, image = root / f"{name}.hip", root / f"{name}.png"
    hip.write_bytes(b"test HIP fixture " + name.encode())
    image.write_bytes(b"test preview fixture")
    return CaseArchive(root).create(
        project_id="p",
        shot_id="s",
        title=name,
        files=[{"path": str(hip), "role": "project"}, {"path": str(image), "role": "preview"}],
    )["case_id"]


def test_user_rules_are_revisioned_scoped_and_keep_original_quotes(tmp_path):
    store = learning(tmp_path)
    first = store.record_rule(**rule())
    assert first["revision"] == 1
    assert first["user_quote"] == rule()["user_quote"]
    with pytest.raises(ValueError, match="revision"):
        store.record_rule(**rule(statement="过期聊天改写"))
    store.record_rule(
        **rule(rule_key="preview", scope="project", project_id="p", statement="先做低清预览")
    )
    assert len(store.get_rules("p")["user_rules"]) == 2
    assert len(store.get_rules("other")["user_rules"]) == 1
    assert learning(tmp_path).get_rules("p")["user_rules"][0]["user_quote"]
    small = store.get_rules("p", max_chars=1024)
    assert len(json.dumps(small, ensure_ascii=False, separators=(",", ":"))) <= 1024


def test_feedback_history_does_not_become_a_success_before_acceptance(tmp_path):
    store = learning(tmp_path)
    saved = store.record_feedback(**feedback())
    history = store.history("p", "s", "逆光烟尘")
    assert history["items"][0]["change"] == feedback()["change"]
    assert store.search("p", "逆光")["items"] == []
    with pytest.raises(ValueError, match="accepted"):
        store.finalize(saved["feedback_id"], "p", "最终方案", "原因", "适用条件")
    with pytest.raises(ValueError, match="revision"):
        store.record_feedback(**feedback())


def test_accepted_result_learns_a_reusable_method_with_archived_evidence(tmp_path):
    store = learning(tmp_path)
    saved = store.record_feedback(
        **feedback(
            outcome="accepted",
            case_id=case_at(tmp_path),
            result="达到最终效果",
            user_quote="这个版本可以，保留这个方案",
        )
    )
    learned = store.finalize(
        saved["feedback_id"], "p", "先分离轮廓灯，再调整局部对比", "不抬高背景", "逆光烟尘"
    )
    assert learned["decision"] == "admitted"
    assert (
        store.finalize(
            saved["feedback_id"], "p", "先分离轮廓灯，再调整局部对比", "不抬高背景", "逆光烟尘"
        )["memory_id"]
        == learned["memory_id"]
    )
    recalled = store.search("p", "逆光烟尘", shot_id="s")
    assert recalled["items"][0]["memory_id"] == learned["memory_id"]
    assert (
        store.search("p", "逆光烟尘", shot_id="another-shot")["items"][0]["memory_id"]
        == learned["memory_id"]
    )
    expanded = store.get(learned["memory_id"], "p")
    assert saved["feedback_id"] in expanded["source_ref"]
    assert expanded["case_id"] == saved["case_id"]
    assert not expanded["visual_verified"]
    with pytest.raises(ValueError, match="different method"):
        store.finalize(saved["feedback_id"], "p", "未确认的另一个方法", "原因", "条件")
    assert len(store.search("p", "逆光烟尘")["items"]) == 1


def test_revised_feedback_invalidates_derived_method_and_blocks_stale_finalize(tmp_path):
    store = learning(tmp_path)
    accepted = store.record_feedback(**feedback(outcome="accepted", case_id=case_at(tmp_path)))
    memory = store.finalize(accepted["feedback_id"], "p", "旧方法", "旧原因", "旧条件")
    store.record_feedback(
        **feedback(expected_revision=1, outcome="failed", result="换镜头后暴露问题")
    )
    assert store.get(memory["memory_id"], "p")["status"] == "disabled"
    assert store.search("p", "逆光")["items"] == []
    with pytest.raises(ValueError, match="current"):
        store.finalize(accepted["feedback_id"], "p", "过期方法", "原因", "条件")
    assert len(store.history("p", "s", "逆光烟尘")["items"]) == 2


def test_accepted_feedback_needs_project_preview_and_matching_scope(tmp_path):
    store = learning(tmp_path)
    with pytest.raises(ValueError, match="case"):
        store.record_feedback(**feedback(outcome="accepted"))
    case = case_at(tmp_path)
    with pytest.raises(ValueError, match="shot"):
        store.record_feedback(**feedback(outcome="accepted", case_id=case, shot_id="other"))


def test_dream_reduces_duplicate_recall_without_losing_sources_or_upgrading_candidates(tmp_path):
    store = learning(tmp_path)
    from test_memory import experience

    ids = []
    for name in ("draft1", "draft2"):
        case = case_at(tmp_path, name)
        entry = experience(project_id="p", shot_id="s", source_type="user", case_id=case)
        ids.append(store.submit(**entry)["memory_id"])
    store.submit(**experience(project_id="p", shot_id="s", title="未验证的新想法"))
    before = store.search("p", "逆光")
    plan = store.dream("p")
    assert len(before["items"]) == 2
    assert len(plan["actions"]) == 1
    assert len(store.search("p", "逆光")["items"]) == 2
    result = store.dream("p", mode="apply", expected_snapshot=plan["snapshot"])
    assert result["applied"] == 1
    after = store.search("p", "逆光")
    assert len(after["items"]) == 1
    assert len(json.dumps(after, ensure_ascii=False)) < len(json.dumps(before, ensure_ascii=False))
    for memory_id in ids:
        assert store.get(memory_id, "p")["status"] == "active"
    assert store.search("p", "未验证")["items"] == []
    canonical = store.get(after["items"][0]["memory_id"], "p")
    assert set(canonical["related_memory_ids"]) | {canonical["memory_id"]} == set(ids)
    reset = store.dream("p")
    store.dream("p", mode="reset", expected_snapshot=reset["snapshot"])
    assert len(store.search("p", "逆光")["items"]) == 2


def test_dream_refuses_a_stale_snapshot_and_never_mutates_another_project(tmp_path):
    store = learning(tmp_path)
    from test_memory import experience

    plan = store.dream("p")
    store.submit(**experience(project_id="p", source_type="user"))
    with pytest.raises(ValueError, match="snapshot"):
        store.dream("p", mode="apply", expected_snapshot=plan["snapshot"])
    assert store.dream("other")["actions"] == []


@pytest.mark.asyncio
async def test_learning_tools_preserve_quotes_and_produce_retrievable_success(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("FX_AGENT_DATA_ROOT", str(tmp_path))
    server = importlib.import_module("fx_agent.server").get_server()
    result = await server.call_tool("fx_record_user_rule", {"rule": rule()})
    assert json.loads(result[0].text)["revision"] == 1
    result = await server.call_tool("fx_get_production_rules", {"project_id": "p"})
    assert json.loads(result[0].text)["user_rules"][0]["user_quote"] == rule()["user_quote"]
    full_rule = await server.call_tool("fx_get_user_rule", {"rule_key": "cache-layout"})
    assert json.loads(full_rule[0].text)["user_quote"] == rule()["user_quote"]
    result = await server.call_tool(
        "fx_record_feedback",
        {
            "feedback": feedback(
                outcome="accepted",
                case_id=case_at(tmp_path),
                user_quote="这个版本可以，保存该方案",
            )
        },
    )
    feedback_id = json.loads(result[0].text)["feedback_id"]
    expanded = await server.call_tool(
        "fx_get_feedback", {"project_id": "p", "feedback_id": feedback_id}
    )
    assert json.loads(expanded[0].text)["outcome"] == "accepted"
    result = await server.call_tool("fx_get_learning_history", {"project_id": "p", "shot_id": "s"})
    assert json.loads(result[0].text)["items"][0]["feedback_id"] == feedback_id
    result = await server.call_tool(
        "fx_finalize_learning",
        {
            "feedback_id": feedback_id,
            "project_id": "p",
            "method": "调整逆光",
            "reason": "保留层次",
            "applies_when": "逆光烟尘",
        },
    )
    assert json.loads(result[0].text)["decision"] == "admitted"
    result = await server.call_tool("fx_dream_memory", {"project_id": "p"})
    assert json.loads(result[0].text)["active_memories"] == 1


def test_independent_acceptances_do_not_share_invalidation_and_reacceptance_is_new_revision(
    tmp_path,
):
    store = learning(tmp_path)
    case = case_at(tmp_path)
    a = store.record_feedback(**feedback(outcome="accepted", case_id=case))
    b = store.record_feedback(**feedback(outcome="accepted", case_id=case, attempt_id="lighting-2"))
    ma = store.finalize(a["feedback_id"], "p", "方法", "原因", "条件")
    mb = store.finalize(b["feedback_id"], "p", "方法", "原因", "条件")
    assert ma["memory_id"] != mb["memory_id"]
    store.record_feedback(**feedback(expected_revision=1, outcome="failed"))
    assert store.get(mb["memory_id"], "p")["status"] == "active"
    again = store.record_feedback(**feedback(expected_revision=2, outcome="accepted", case_id=case))
    revised = store.finalize(again["feedback_id"], "p", "方法", "原因", "条件")
    assert revised["status"] == "active"
    assert revised["memory_id"] != ma["memory_id"]
    assert store.get(revised["memory_id"], "p")["origin_feedback_id"] == again["feedback_id"]
    with pytest.raises(ValueError, match="non-empty"):
        store.finalize(again["feedback_id"], "p", "方法", "", "条件")


def test_large_escaped_feedback_and_rules_are_expandable_without_blocking_pagination(tmp_path):
    store = learning(tmp_path)
    small = store.record_feedback(**feedback(attempt_id="small"))
    large = store.record_feedback(
        **feedback(
            attempt_id="large",
            problem='"' * 1000,
            change='"' * 3000,
            reason='"' * 1500,
            result='"' * 1500,
            user_quote='"' * 2000,
        )
    )
    history = store.history("p", "s", max_chars=1024)
    assert history["items"]
    assert history["next_cursor"] > 0
    assert len(json.dumps(history, ensure_ascii=False, separators=(",", ":"))) <= 1024
    assert store.get_feedback(large["feedback_id"], "p")["change"] == '"' * 3000
    for number in range(3):
        store.record_rule(
            **rule(
                rule_key=f"large-{number}",
                statement='"' * 3000,
                user_quote='"' * 2000,
                source_ref="r" * 512,
            )
        )
    rules = store.get_rules("p", max_chars=1024)
    assert rules["next_offset"] > 0
    assert store.get_rule("large-2")["statement"] == '"' * 3000
    assert len(json.dumps(rules, ensure_ascii=False, separators=(",", ":"))) <= 1024
    assert store.get_feedback(small["feedback_id"], "p")["attempt_id"] == "small"


def test_dream_index_can_be_reset_after_the_project_grows_past_plan_limit(tmp_path):
    store = learning(tmp_path)
    from test_memory import experience

    for number in range(501):
        store.submit(**experience(project_id="p", source_type="user", title=f"经验 {number}"))
    plan = store.dream("p")
    assert plan["capacity_limited"]
    with pytest.raises(ValueError, match="limit"):
        store.dream("p", mode="apply", expected_snapshot=plan["snapshot"])
    assert store.dream("p", mode="reset", expected_snapshot=plan["snapshot"])["applied"] == 0


@pytest.mark.asyncio
async def test_rules_fast_path_enforces_same_budget_and_scope_validation(tmp_path, monkeypatch):
    from mcp.server.fastmcp.exceptions import ToolError

    monkeypatch.setenv("FX_AGENT_DATA_ROOT", str(tmp_path))
    server = importlib.import_module("fx_agent.server").get_server()
    for args in ({"max_chars": 1}, {"project_id": "\x00"}, {"offset": -1}):
        with pytest.raises(ToolError):
            await server.call_tool("fx_get_production_rules", args)
    assert not (tmp_path / "memory.sqlite3").exists()


def test_history_budgets_include_multi_digit_cursors_and_expand_only_on_request(tmp_path):
    store = learning(tmp_path)
    for number in range(20):
        store.record_feedback(**feedback(attempt_id=f"attempt-{number}", result="r" * 600))
    history = store.history("p", "s", max_chars=1024)
    assert history["next_cursor"] >= 10
    assert len(json.dumps(history, ensure_ascii=False, separators=(",", ":"))) <= 1024
    next_page = store.history("p", "s", max_chars=1024, before_cursor=history["next_cursor"])
    assert next_page["items"][0]["feedback_id"] != history["items"][0]["feedback_id"]


def test_schema_two_database_is_upgraded_without_losing_checkpoint(tmp_path):
    import sqlite3

    checkpoint = {
        "project_id": "p",
        "shot_id": "s",
        "summary": "旧工程进度",
        "revision": 1,
        "scene_path": "D:/scene.hip",
        "next_steps": ["继续灯光"],
        "updated_at": "2026-11-02T00:00:00+00:00",
    }
    with sqlite3.connect(tmp_path / "memory.sqlite3") as db:
        db.execute(
            "CREATE TABLE checkpoints (project_id TEXT NOT NULL,shot_id TEXT NOT NULL,revision INTEGER NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(project_id,shot_id))"
        )
        db.execute(
            "CREATE TABLE memories (memory_id TEXT PRIMARY KEY,project_id TEXT NOT NULL,shot_id TEXT NOT NULL,status TEXT NOT NULL,content_hash TEXT NOT NULL,payload TEXT NOT NULL,search_text TEXT NOT NULL,UNIQUE(project_id,content_hash))"
        )
        db.execute(
            "INSERT INTO checkpoints VALUES (?,?,?,?)", ("p", "s", 1, json.dumps(checkpoint))
        )
        db.execute("PRAGMA user_version=2")
    store = learning(tmp_path)
    assert store.get_checkpoint("p", "s") == checkpoint
    assert store.record_rule(**rule())["revision"] == 1
    with sqlite3.connect(store.path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
