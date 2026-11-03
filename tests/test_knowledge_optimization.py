"""Knowledge views retain module coverage, disputes and safe incremental reads."""

import importlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from test_module_guides import accepted
from test_module_learning import experience

from fx_agent.dream import snapshot
from fx_agent.learning import LearningStore
from fx_agent.module_guides import ModuleGuides
from fx_agent.store import Store


def revise(store, sources, *, operation="refine", modules=None):
    return store.dream(
        mode="apply",
        expected_snapshot=store.dream()["snapshot"],
        actions=[
            dict(
                operation=operation,
                source_ids=sources,
                title="提炼方法",
                method="统一场景单位",
                reason="参数受尺度影响",
                applies_when="相同输入",
                rationale="根据来源整理",
                modules=modules,
            )
        ],
    )


@pytest.mark.parametrize("operation", ["refine", "split"])
def test_narrower_synthesis_keeps_other_module_recall(tmp_path, operation):
    store = LearningStore(tmp_path)
    source = store.submit(**experience(modules=["pyro", "sop"]))["memory_id"]
    result = revise(store, [source], operation=operation, modules=["pyro"])
    assert store.search(module="sop")["items"][0]["memory_id"] == source
    assert store.search(module="pyro")["items"][0]["memory_id"] == result["created_ids"][0]


def test_narrower_synthesis_keeps_other_module_guides(tmp_path):
    store, feedback = accepted(tmp_path)
    source = store.finalize(feedback["feedback_id"], "p", "原始卷浪方法", "原因", "条件")
    revise(store, [source["memory_id"]], modules=["ocean"])
    sop = ModuleGuides(store.root).get(module="sop")
    assert sop["lesson_count"] == 1
    assert "原始卷浪方法" in sop["content"]
    assert "统一场景单位" not in sop["content"]


def test_conflicts_are_visible_in_guides_and_removed_on_undo(tmp_path):
    store, feedback = accepted(tmp_path)
    source = store.finalize(feedback["feedback_id"], "p", "原始卷浪方法", "原因", "条件")
    conflict = revise(store, [source["memory_id"]], operation="conflict")
    guide = ModuleGuides(store.root).get(module="sop")
    assert "存在争议" in guide["content"]
    assert conflict["created_ids"][0] in guide["content"]
    store.undo_dream(conflict["run_id"])
    assert "存在争议" not in ModuleGuides(store.root).get(module="sop")["content"]


def test_long_disputed_method_keeps_conflict_metadata_after_truncation(tmp_path):
    store, feedback = accepted(tmp_path)
    source = store.finalize(feedback["feedback_id"], "p", "长方法" * 1500, "原因", "条件")
    conflict = revise(store, [source["memory_id"]], operation="conflict")
    for _ in range(2):
        guide = ModuleGuides(store.root).get(module="sop", max_chars=1024)
        assert guide["truncated"]
        assert guide["has_conflict"]
        assert conflict["created_ids"][0] in guide["conflict_memory_ids"]


def test_cached_document_detects_manual_marker_damage(tmp_path):
    from fx_agent.module_guides import START

    store, feedback = accepted(tmp_path)
    store.finalize(feedback["feedback_id"], "p", "原始卷浪方法", "原因", "条件")
    guide = ModuleGuides(store.root)
    path = Path(guide.get(module="sop")["path"])
    malformed = path.read_text(encoding="utf-8") + "\n" + START
    path.write_text(malformed, encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid managed"):
        guide.get(module="sop")
    assert path.read_text(encoding="utf-8") == malformed


def test_store_construction_and_reads_do_not_wait_for_an_existing_writer(tmp_path):
    Store(tmp_path)
    with sqlite3.connect(tmp_path / "memory.sqlite3") as writer:
        writer.execute("BEGIN IMMEDIATE")
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(lambda: Store(tmp_path).get_checkpoint("p", "s"))
            try:
                assert future.result(timeout=1) is None
            finally:
                writer.rollback()


def test_unchanged_module_read_reuses_view_without_rebuilding(tmp_path, monkeypatch):
    store, feedback = accepted(tmp_path)
    store.finalize(feedback["feedback_id"], "p", "原始卷浪方法", "原因", "条件")
    guide = ModuleGuides(store.root)
    expected = guide.get(module="sop")

    def unexpected_write(*args, **kwargs):
        raise AssertionError("Unchanged reads must not regenerate Markdown")

    monkeypatch.setattr(ModuleGuides, "_write", unexpected_write)
    assert ModuleGuides(store.root).get(module="sop") == expected


def test_module_read_detects_external_database_change_and_deleted_document(tmp_path):
    store, feedback = accepted(tmp_path)
    result = store.finalize(feedback["feedback_id"], "p", "原始卷浪方法", "原因", "条件")
    guide = ModuleGuides(store.root)
    path = Path(guide.get(module="sop")["path"])
    path.unlink()
    assert "原始卷浪方法" in guide.get(module="sop")["content"]
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE memories SET status='disabled' WHERE memory_id=?", (result["memory_id"],))
    assert "原始卷浪方法" not in guide.get(module="sop")["content"]


def test_module_read_does_not_rebuild_unrelated_documents(tmp_path, monkeypatch):
    store, feedback = accepted(tmp_path)
    result = store.finalize(feedback["feedback_id"], "p", "原始卷浪方法", "原因", "条件")
    with store.connection() as db:
        db.execute("UPDATE memories SET status='disabled' WHERE memory_id=?", (result["memory_id"],))
    write = ModuleGuides._write
    touched = []

    def record_write(self, path, section, header):
        touched.append(path.stem)
        return write(self, path, section, header)

    monkeypatch.setattr(ModuleGuides, "_write", record_write)
    assert ModuleGuides(store.root).get(module="sop")["lesson_count"] == 0
    assert touched == ["sop"]


def test_knowledge_snapshot_is_constant_cost_and_tracks_external_writes(tmp_path):
    store = LearningStore(tmp_path)
    source = store.submit(**experience())["memory_id"]
    statements = []
    with store.connection() as db:
        before = snapshot(db)
        db.set_trace_callback(statements.append)
        assert snapshot(db) == before
    assert not any("SELECT * FROM" in sql.upper() for sql in statements)
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE memories SET status='disabled' WHERE memory_id=?", (source,))
    with store.connection() as db:
        assert snapshot(db) != before
        before = snapshot(db)
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE memories SET status='active' WHERE memory_id=?", (source,))
        assert snapshot(db) != before
        db.rollback()
        assert snapshot(db) == before


def test_direct_server_start_pins_new_port_and_preserves_explicit_port(monkeypatch):
    from fx_agent.server import get_server

    monkeypatch.delenv("HOUDINI_PORT", raising=False)
    get_server.cache_clear()
    get_server()
    import os

    assert os.environ["HOUDINI_PORT"] == "18186"
    monkeypatch.setenv("HOUDINI_PORT", "19000")
    get_server.cache_clear()
    get_server()
    assert os.environ["HOUDINI_PORT"] == "19000"


def test_plugin_default_port_matches_new_bridge():
    root = Path(__file__).resolve().parents[1]
    plugin = json.loads((root / "mcp.json").read_text(encoding="utf-8"))
    assert plugin["mcpServers"]["fx-agent"]["env"]["HOUDINI_PORT"] == "18186"


def test_schema_five_data_is_upgraded_without_losing_memory(tmp_path):
    store = LearningStore(tmp_path)
    source = store.submit(**experience())["memory_id"]
    with store.connection() as db:
        triggers = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")]
        for trigger in triggers:
            db.execute(f'DROP TRIGGER "{trigger}"')
        db.execute("DROP TABLE knowledge_state")
        db.execute("DROP TABLE module_guide_state")
        db.execute("PRAGMA user_version=5")
    assert LearningStore(tmp_path).get(source)["status"] == "active"
    with Store(tmp_path).connection() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == Store.SCHEMA_VERSION


def test_dream_exact_duplicates_are_found_across_page_boundaries(tmp_path):
    store = LearningStore(tmp_path)
    store.submit(**experience(project_id="A", modules=["pyro"]))
    for number in range(5):
        store.submit(**experience(title=f"unrelated-{number}", modules=["pyro"]))
    store.submit(**experience(project_id="B", modules=["pyro"]))
    plan = store.dream(limit=1)
    assert len(plan["actions"]) == 1
    store.dream(mode="apply", expected_snapshot=plan["snapshot"], limit=1)
    assert len(store.search(query="尺度", module="pyro", limit=20, max_chars=12000)["items"]) == 6


def test_feedback_changes_invalidate_dream_but_guide_reads_do_not(tmp_path):
    from test_learning import feedback

    store = LearningStore(tmp_path)
    plan = store.dream()
    ModuleGuides(store.root).get(module="pyro")
    assert store.dream()["snapshot"] == plan["snapshot"]
    store.record_feedback(**feedback())
    with pytest.raises(ValueError, match="snapshot"):
        store.dream(mode="apply", expected_snapshot=plan["snapshot"])


def test_check_script_and_installer_share_default_port(tmp_path, monkeypatch, capsys):
    from test_install_codex import installer

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr("sys.argv", ["install_codex.py", "--houdini-pref", str(tmp_path)])
    installer.main()
    assert json.loads(capsys.readouterr().out)["port"] == 18186
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("check_houdini_audit", root / "scripts/check_houdini.py")
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    monkeypatch.delenv("HOUDINI_PORT", raising=False)

    class InspectedParameters(Exception):
        pass

    def inspect_parameters(**values):
        assert values["env"]["HOUDINI_PORT"] == "18186"
        raise InspectedParameters

    monkeypatch.setattr(checker, "StdioServerParameters", inspect_parameters)
    import asyncio

    with pytest.raises(InspectedParameters):
        asyncio.run(checker.main(mcp_only=True))
