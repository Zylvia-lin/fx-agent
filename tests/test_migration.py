"""Data relocation must preserve the SQLite snapshot, guides and baseline manifests."""

import json
import shutil
from pathlib import Path

import pytest

from fx_agent.baselines import sync_baselines, validate_baselines
from fx_agent.learning import LearningStore
from fx_agent.migration import migrate_data


def test_migration_preserves_rules_memories_and_baselines_after_move(tmp_path):
    source, target = tmp_path / "old", tmp_path / "agent/data"
    store = LearningStore(source)
    store.record_rule(
        rule_key="test",
        title="规范",
        statement="保存工程",
        user_quote="记住保存工程",
        source_ref="test",
        scope="global",
        expected_revision=0,
    )
    saved = store.submit(
        project_id="p",
        software="houdini",
        modules=["sop"],
        kind="workflow",
        title="通用SOP经验",
        method="使用相对路径 $HIP/geo",
        reason="便于跨机复用",
        applies_when="常规资产缓存",
        source_type="user",
        source_ref="test",
    )
    sync_baselines(source)
    migrate_data(source, target)
    assert LearningStore(target).get_rules()["user_rules"][0]["statement"] == "保存工程"
    assert LearningStore(target).get(saved["memory_id"], "p")["title"] == "通用SOP经验"
    assert validate_baselines(target)["valid"]
    assert (source / "memory.sqlite3").exists(), "Keep the original as a rollback source"
    relocated = tmp_path / "another-device/data"
    shutil.copytree(target, relocated)
    assert validate_baselines(relocated)["valid"]
    assert LearningStore(relocated).get(saved["memory_id"], "p")["title"] == "通用SOP经验"
    with pytest.raises(FileExistsError):
        migrate_data(source, target)


def test_migration_rejects_overlapping_roots_and_links(tmp_path):
    source = tmp_path / "source"
    LearningStore(source)
    with pytest.raises(ValueError):
        migrate_data(source, source / "nested")
    with pytest.raises(ValueError):
        migrate_data(source, source.parent)


def test_checkout_default_is_portable_and_not_cwd(tmp_path, monkeypatch):
    from fx_agent.config import data_root

    monkeypatch.delenv("FX_AGENT_DATA_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    assert data_root() == Path(__file__).resolve().parents[1] / "data"


@pytest.mark.parametrize("damage", ["corrupt_file", "missing_manifest"])
def test_corrupted_baseline_does_not_publish_destination(tmp_path, damage):
    source, target = tmp_path / "source", tmp_path / "target"
    LearningStore(source)
    synced = sync_baselines(source)
    snap_dir = source / synced["snapshots"][0]["relative_path"]
    manifest_path = snap_dir / "manifest.json"
    if damage == "missing_manifest":
        manifest_path.unlink()
    else:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        first_file = snap_dir / manifest["files"][0]["path"]
        first_file.write_bytes(b"corrupted baseline content")
    with pytest.raises(ValueError, match="baseline"):
        migrate_data(source, target)
    assert not target.exists()
    assert not list(tmp_path.glob(".fx-data-*"))
