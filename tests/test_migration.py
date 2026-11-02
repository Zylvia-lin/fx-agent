"""Data relocation must preserve the SQLite snapshot and archived evidence."""

import shutil
from pathlib import Path

import pytest

from fx_agent.archive import CaseArchive
from fx_agent.learning import LearningStore
from fx_agent.migration import migrate_data


def test_migration_preserves_rules_and_case_after_move(tmp_path):
    source, target = tmp_path / "old", tmp_path / "agent/data"
    LearningStore(source).record_rule(
        rule_key="test",
        title="规范",
        statement="保存工程",
        user_quote="记住保存工程",
        source_ref="test",
        scope="global",
        expected_revision=0,
    )
    hip = tmp_path / "sample.hip"
    hip.write_bytes(b"fixture only")
    case = CaseArchive(source).create(
        project_id="p", shot_id="s", title="case", files=[{"path": str(hip), "role": "project"}]
    )
    migrate_data(source, target)
    assert LearningStore(target).get_rules()["user_rules"][0]["statement"] == "保存工程"
    assert CaseArchive(target).verify(case["case_id"], "p")["valid"]
    assert (source / "memory.sqlite3").exists(), "Keep the original as a rollback source"
    relocated = tmp_path / "another-device/data"
    shutil.copytree(target, relocated)
    assert CaseArchive(relocated).verify(case["case_id"], "p")["valid"]
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


def test_corrupted_case_does_not_publish_destination(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    LearningStore(source)
    hip = tmp_path / "sample.hip"
    hip.write_bytes(b"fixture")
    case = CaseArchive(source).create(
        project_id="p", shot_id="s", title="case", files=[{"path": str(hip), "role": "project"}]
    )
    (Path(case["case_path"]) / case["files"][0]["archive_path"]).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="checksum"):
        migrate_data(source, target)
    assert not target.exists()
    assert not list(tmp_path.glob(".fx-data-*"))


@pytest.mark.parametrize("damage", ["missing_manifest", "wrong_identity"])
def test_missing_manifest_or_forged_case_identity_prevents_migration(tmp_path, damage):
    import json

    source, target = tmp_path / "source", tmp_path / "target"
    LearningStore(source)
    hip = tmp_path / "sample.hip"
    hip.write_bytes(b"fixture")
    archive = CaseArchive(source)
    cases = [
        archive.create(
            project_id="p", shot_id="s", title="case", files=[{"path": str(hip), "role": "project"}]
        )
        for _ in range(2)
    ]
    path = Path(cases[0]["case_path"]) / "manifest.json"
    if damage == "missing_manifest":
        path.unlink()
    else:
        manifest = json.loads(path.read_text())
        manifest["case_id"] = cases[1]["case_id"]
        path.write_text(json.dumps(manifest))
    with pytest.raises((ValueError, FileNotFoundError)):
        migrate_data(source, target)
    assert not target.exists()
