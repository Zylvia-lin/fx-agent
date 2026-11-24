"""Keep private state in SQLite and share only reviewed summary fields."""

import json
import runpy
from pathlib import Path

import pytest

from fx_agent import config, shared_knowledge
from fx_agent.learning import LearningStore
from fx_agent.memory import MemoryStore


def test_first_run_loads_shared_summaries_once_and_never_imports_quotes(tmp_path):
    root = tmp_path / "cache"
    shared_knowledge.load_shared_experiences(root)
    shared_knowledge.load_shared_experiences(root)
    store = MemoryStore(root)
    with store.connection() as db:
        rows = db.execute("SELECT payload FROM memories").fetchall()
        assert len(rows) == len(json.loads(shared_knowledge.SHARED_PATH.read_text(encoding="utf-8")))
        assert db.execute("SELECT count(*) FROM user_rules").fetchone()[0] == 0
        assert all(json.loads(row[0])["evidence_status"] == "text_distilled_reference" for row in rows)
    assert store.search("", "", software="nuke", module="compositing")["items"]


def test_removing_a_published_summary_withdraws_it_from_shared_search(tmp_path, monkeypatch):
    original = json.loads(shared_knowledge.SHARED_PATH.read_text(encoding="utf-8"))[:1]
    published = tmp_path / "shared.json"
    monkeypatch.setattr(shared_knowledge, "SHARED_PATH", published)
    cache = tmp_path / "cache"
    store = MemoryStore(cache)
    for entries, expected in ((original, 1), ([], 0), (original, 1)):
        published.write_text(json.dumps(entries), encoding="utf-8")
        shared_knowledge.load_shared_experiences(cache)
        with store.connection() as db:
            assert db.execute("SELECT count(*) FROM memories WHERE status='active'").fetchone()[0] == expected
            assert db.execute("SELECT count(*) FROM memories").fetchone()[0] == 1


def test_legacy_profile_cache_migrates_privately(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("FX_AGENT_DATA_ROOT", raising=False)
    monkeypatch.setattr(shared_knowledge, "CHECKOUT_DATA", tmp_path / "checkout/data")
    legacy = home / ".fx-agent"
    LearningStore(legacy).record_rule(
        rule_key="private", title="Private", statement="Private statement",
        user_quote="Private chat excerpt", source_ref="private chat reference", expected_revision=0,
    )
    (config.local_data_root() / "maya_sessions").mkdir(parents=True)
    root = shared_knowledge.prepare_local_data()
    assert root == config.local_data_root()
    assert (legacy / "memory.sqlite3").is_file()
    assert LearningStore(root).get_rule("private")["user_quote"] == "Private chat excerpt"


def test_export_only_admitted_global_summary_fields(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts/export_shared_experiences.py"
    export = runpy.run_path(str(script))["export_summaries"]
    store = MemoryStore(tmp_path)
    values = dict(kind="workflow", title="Portable method", method="Use a curve", reason="Editable",
                  applies_when="Procedural motion", source_type="observed", source_ref="private reference")
    active = store.submit(**values)
    candidate = store.submit(**(values | {"title": "Hypothesis", "source_type": "inferred"}))
    exported = export(store.path, [active["memory_id"]])
    assert exported[0].keys() == shared_knowledge.FIELDS
    assert "private reference" not in json.dumps(exported)
    with pytest.raises(ValueError, match="unsuitable"):
        export(store.path, [candidate["memory_id"]])
