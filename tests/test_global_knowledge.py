"""Global recall, host-authored Dream and repeatable cross-device imports."""

import pytest
from test_module_learning import experience

from fx_agent.learning import LearningStore


def test_global_recall_retains_origin_and_checkpoint_isolation(tmp_path):
    store = LearningStore(tmp_path)
    item = store.submit(**experience(project_id="A", modules=["pyro"]))
    assert store.search("B", "尺度")["items"][0]["memory_id"] == item["memory_id"]
    assert store.get(item["memory_id"], "B")["project_id"] == "A"
    assert store.search(query="尺度")["items"]
    assert store.get_checkpoint("B", "s") is None


def test_host_dream_merge_is_traceable_stale_protected_and_reversible(tmp_path):
    store = LearningStore(tmp_path)
    a = store.submit(**experience(project_id="A", modules=["pyro"]))["memory_id"]
    b = store.submit(**experience(project_id="B", modules=["pyro"], method="先核对场景尺寸"))[
        "memory_id"
    ]
    plan = store.dream()
    actions = [
        {
            "operation": "merge",
            "source_ids": [a, b],
            "title": "尺度检查",
            "method": "求解前统一场景单位，再调整参数",
            "reason": "参数受尺度影响",
            "applies_when": "Pyro 求解",
            "rationale": "两条来源描述同一前置检查",
        }
    ]
    result = store.dream(mode="apply", expected_snapshot=plan["snapshot"], actions=actions)
    found = store.search(query="统一")["items"][0]
    detail = store.get(found["memory_id"])
    assert set(detail["source_memory_ids"]) == {a, b}
    assert store.get(a)["status"] == "active"
    with pytest.raises(ValueError, match="snapshot"):
        store.dream(mode="apply", expected_snapshot=plan["snapshot"], actions=actions)
    store.undo_dream(result["run_id"])
    assert store.search(query="统一")["items"] == []
    assert len(store.search(query="尺度")["items"]) == 1


def test_dream_does_not_promote_candidates_or_mix_software(tmp_path):
    store = LearningStore(tmp_path)
    a = store.submit(**experience())["memory_id"]
    b = store.submit(**experience(title="候选", source_type="inferred"))["memory_id"]
    action = {
        "operation": "merge",
        "source_ids": [a, b],
        "title": "合并",
        "method": "方法",
        "reason": "原因",
        "applies_when": "条件",
        "rationale": "说明",
    }
    with pytest.raises(ValueError, match="active"):
        store.dream(mode="apply", expected_snapshot=store.dream()["snapshot"], actions=[action])


def test_dream_source_invalidation_restores_other_sources(tmp_path):
    store = LearningStore(tmp_path)
    a = store.submit(**experience(project_id="A"))["memory_id"]
    b = store.submit(**experience(project_id="B", method="另一个尺度检查方法"))["memory_id"]
    action = {
        "operation": "merge",
        "source_ids": [a, b],
        "title": "合并",
        "method": "合并方法",
        "reason": "原因",
        "applies_when": "条件",
        "rationale": "说明",
    }
    result = store.dream(
        mode="apply", expected_snapshot=store.dream()["snapshot"], actions=[action]
    )
    with store.connection() as db:
        db.execute("UPDATE memories SET status='disabled' WHERE memory_id=?", (a,))
    assert store.search(query="合并方法")["items"] == []
    assert store.search(query="另一个")["items"][0]["memory_id"] == b
    with pytest.raises(ValueError, match="changed"):
        store.undo_dream(result["run_id"])


def test_import_is_previewable_idempotent_and_preserves_divergence(tmp_path):
    from fx_agent.data_import import import_data, import_records

    source, target = tmp_path / "source", tmp_path / "target"
    store = LearningStore(source)
    item = store.submit(**experience(project_id="A"))
    preview = import_data(source, target)
    assert preview["counts"]["memories"]["added"] == 1
    assert not (target / "memory.sqlite3").exists()
    imported = import_data(source, target, apply=True)
    assert imported["applied"]
    assert LearningStore(target).search(query="尺度")["items"]
    assert import_data(source, target, apply=True)["counts"]["memories"]["added"] == 0
    with store.connection() as db:
        db.execute("UPDATE memories SET status='disabled' WHERE memory_id=?", (item["memory_id"],))
    conflict = import_data(source, target, apply=True)
    assert conflict["counts"]["memories"]["conflicts"] == 1
    records = import_records(target, conflict["import_id"], table="memories")
    assert records["items"][0]["incoming"]["status"] == "disabled"
    assert LearningStore(target).get(item["memory_id"])["status"] == "needs_review"


def test_exact_dream_cannot_hide_both_a_refinement_and_its_source(tmp_path):
    store = LearningStore(tmp_path)
    value = experience()
    source = store.submit(**value)["memory_id"]
    action = {k: value[k] for k in ("title", "method", "reason", "applies_when")}
    action.update(operation="refine", source_ids=[source], rationale="保留原结论并建立知识修订")
    store.dream(mode="apply", expected_snapshot=store.dream()["snapshot"], actions=[action])
    plan = store.dream()
    store.dream(mode="apply", expected_snapshot=plan["snapshot"])
    assert len(store.search(query="尺度")["items"]) == 1


def test_undo_preserves_unrelated_new_experience(tmp_path):
    store = LearningStore(tmp_path)
    source = store.submit(**experience())["memory_id"]
    action = {
        "operation": "refine",
        "source_ids": [source],
        "title": "总结",
        "method": "总结方法",
        "reason": "原因",
        "applies_when": "条件",
        "rationale": "说明",
    }
    result = store.dream(
        mode="apply", expected_snapshot=store.dream()["snapshot"], actions=[action]
    )
    extra = store.submit(**experience(title="新的经验"))["memory_id"]
    store.undo_dream(result["run_id"])
    assert store.get(extra)["status"] == "active"


def test_import_failed_feedback_invalidates_old_local_success(tmp_path):
    from test_learning import feedback as feedback_values
    from test_module_guides import accepted

    from fx_agent.data_import import import_data

    source, entry = accepted(tmp_path)
    source.finalize(entry["feedback_id"], "p", "以前的成功方法", "原因", "条件")
    target = tmp_path / "target"
    import_data(source.root, target, apply=True)
    assert LearningStore(target).search(query="以前的成功方法")["items"]
    source.record_feedback(
        **feedback_values(
            project_id="p", shot_id="s", attempt_id="a", expected_revision=1, outcome="failed"
        )
    )
    import_data(source.root, target, apply=True)
    assert LearningStore(target).search(query="以前的成功方法")["items"] == []


def test_import_snapshot_tracks_manual_notes_without_recursive_history(tmp_path):
    from fx_agent.data_import import import_data

    source = tmp_path / "source"
    LearningStore(source).submit(**experience())
    notes = source / "notes.md"
    notes.write_text("第一版", encoding="utf-8")
    history = source / "imports" / "old"
    history.mkdir(parents=True)
    (history / "large-history.txt").write_text("old", encoding="utf-8")
    target = tmp_path / "target"
    first = import_data(source, target, apply=True)
    notes.write_text("第二版", encoding="utf-8")
    second = import_data(source, target, apply=True)
    assert first["import_id"] != second["import_id"]
    from pathlib import Path

    assert not (Path(second["snapshot_path"]) / "imports").exists()
    assert (Path(second["snapshot_path"]) / "notes.md").read_text(encoding="utf-8") == "第二版"


def test_import_memory_resolution_retains_versions_and_is_not_repeated(tmp_path):
    import json

    from fx_agent.data_import import import_data, import_records, resolve_memory
    from fx_agent.store import compact

    source, target = tmp_path / "source", tmp_path / "target"
    store = LearningStore(source)
    original = store.submit(**experience())["memory_id"]
    import_data(source, target, apply=True)
    with store.connection() as db:
        row = db.execute("SELECT payload FROM memories WHERE memory_id=?", (original,)).fetchone()
        value = json.loads(row[0])
        value["method"] = "来自其他设备的不同方法"
        db.execute(
            "UPDATE memories SET payload=?,search_text=? WHERE memory_id=?",
            (compact(value), value["method"], original),
        )
    batch = import_data(source, target, apply=True)
    local = LearningStore(target)
    resolved = resolve_memory(
        target,
        batch["import_id"],
        original,
        "keep_both",
        local.dream()["snapshot"],
        "两套方法均保留供条件对照",
    )
    assert local.get(resolved["branch_id"])["method"] == value["method"]
    assert local.get(original)["method"] == experience()["method"]
    import_data(source, target, apply=True)
    assert (
        import_records(target, batch["import_id"], table="memories")["items"][0]["resolution"][
            "branch_id"
        ]
        == resolved["branch_id"]
    )


def test_global_hash_remains_compatible_with_a_real_legacy_payload(tmp_path):
    import hashlib

    from fx_agent.store import compact

    store = LearningStore(tmp_path)
    value = experience()
    memory = store.submit(**value)["memory_id"]
    from fx_agent.memory import Experience

    old = Experience(**value).model_dump(exclude={"source_ref", "reuse_scope"})
    old.pop("software")
    old.pop("modules")
    with store.connection() as db:
        db.execute(
            "UPDATE memories SET content_hash=? WHERE memory_id=?",
            (hashlib.sha256(compact(old).encode()).hexdigest(), memory),
        )
    assert store.submit(**value)["decision"] == "duplicate"


def test_independent_same_named_attempts_do_not_extend_each_others_history(tmp_path):
    from test_learning import feedback

    from fx_agent.data_import import import_data

    source, target = LearningStore(tmp_path / "source"), LearningStore(tmp_path / "target")
    target.record_feedback(**feedback())
    source.record_feedback(**feedback())
    source.record_feedback(**feedback(expected_revision=1, outcome="failed"))
    result = import_data(source.root, target.root, apply=True)
    assert result["counts"]["learning_feedback"]["conflicts"] == 2
    assert len(target.history("p", "s")["items"]) == 1


@pytest.mark.asyncio
async def test_mcp_global_dream_and_import_tools_work_without_a_project(tmp_path, monkeypatch):
    import json

    from fx_agent.server import get_server

    source, target = tmp_path / "source", tmp_path / "target"
    item = LearningStore(source).submit(**experience(modules=["pyro"]))
    monkeypatch.setenv("FX_AGENT_DATA_ROOT", str(target))
    server = get_server()

    async def call(name, arguments):
        result = await server.call_tool(name, arguments)
        return json.loads(result[0].text)

    preview = await call("fx_import_data", {"source": str(source)})
    assert not preview["applied"]
    batch = await call("fx_import_data", {"source": str(source), "apply": True})
    assert batch["applied"]
    assert (await call("fx_search_memory", {"query": "尺度"}))["items"]
    assert (await call("fx_get_memory", {"memory_id": item["memory_id"]}))["project_id"] == "p"
    plan = await call("fx_dream_memory", {"software": "houdini", "module": "pyro"})
    result = await call(
        "fx_dream_memory",
        {
            "mode": "apply",
            "software": "houdini",
            "module": "pyro",
            "expected_snapshot": plan["snapshot"],
            "actions": [
                {
                    "operation": "refine",
                    "source_ids": [item["memory_id"]],
                    "title": "尺度方法",
                    "method": "统一尺度后调整参数",
                    "reason": "场景尺度影响参数",
                    "applies_when": "相同求解器",
                    "rationale": "从来源总结",
                }
            ],
        },
    )
    assert (await call("fx_get_dream_runs", {"run_id": result["run_id"]}))["created_ids"]
    assert (await call("fx_get_dream_runs", {}))["items"]
    assert (await call("fx_get_import_records", {}))["items"]
    assert (await call("fx_get_import_records", {"import_id": batch["import_id"]}))["items"] == []
    assert (await call("fx_undo_dream", {"run_id": result["run_id"]}))["undone"]


def test_upstream_conflict_does_not_reactivate_on_a_third_device(tmp_path):
    from fx_agent.data_import import import_data

    source, target, third = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    store = LearningStore(source)
    item = store.submit(**experience())
    import_data(source, target, apply=True)
    with store.connection() as db:
        db.execute("UPDATE memories SET status='disabled' WHERE memory_id=?", (item["memory_id"],))
    import_data(source, target, apply=True)
    assert LearningStore(target).search(query="尺度")["items"] == []
    import_data(target, third, apply=True)
    assert LearningStore(third).search(query="尺度")["items"] == []
    assert LearningStore(third).get(item["memory_id"])["status"] == "needs_review"


def test_two_semantic_runs_can_be_undone_in_reverse_order(tmp_path):
    store = LearningStore(tmp_path)
    source = store.submit(**experience())["memory_id"]

    def refine(memory_id, method):
        return store.dream(
            mode="apply",
            expected_snapshot=store.dream()["snapshot"],
            actions=[
                {
                    "operation": "refine",
                    "source_ids": [memory_id],
                    "title": "总结",
                    "method": method,
                    "reason": "原因",
                    "applies_when": "条件",
                    "rationale": "说明",
                }
            ],
        )

    first = refine(source, "第一轮方法")
    second = refine(first["created_ids"][0], "第二轮方法")
    store.undo_dream(second["run_id"])
    store.undo_dream(first["run_id"])
    assert store.get(source)["status"] == "active"
    assert store.search(query="第一轮方法")["items"] == []
    assert store.search(query="第二轮方法")["items"] == []
