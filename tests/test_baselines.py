"""Upstream rules must travel with data, independently of a virtual environment."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from fx_agent.baselines import baseline_status, sync_baselines, validate_baselines
from fx_agent.data_import import import_data
from fx_agent.migration import migrate_data
from fx_agent.store import Store


def test_rules_are_copied_to_data_with_version_hashes_and_licenses(tmp_path):
    root = tmp_path / "data"
    result = sync_baselines(root)
    snapshots = result["snapshots"]
    houdini = next(s for s in snapshots if s["source"] == "fxhoudinimcp")
    assert houdini["workflow_count"] == 31
    folder = root / houdini["relative_path"]
    assert folder.name == "houdini"
    assert (folder / "markdown/workflows/pyro.md").is_file()
    assert (folder / "licenses/LICENSE").is_file()
    assert (folder / "licenses/NOTICE").is_file()
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == "2.25.0"
    assert all(len(f["sha256"]) == 64 for f in manifest["files"])
    local = next(s for s in snapshots if s["source"] == "fx-agent")
    assert (root / local["relative_path"] / "skills/nuke-workflow/SKILL.md").is_file()
    assert "学习补充" in (root / "modules/global/houdini/README.md").read_text(encoding="utf-8")
    assert validate_baselines(root)["valid"]
    assert not (root / "memory.sqlite3").exists(), "Snapshotting rules does not upgrade user memory"
    assert sync_baselines(root)["snapshots"] == snapshots


def test_baselines_migrate_and_merge_without_regenerating_from_venv(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    Store(source)
    sync_baselines(source)
    moved = tmp_path / "moved"
    migrate_data(source, moved)
    assert validate_baselines(moved)["valid"]
    preview = import_data(moved, target)
    assert preview["baseline_snapshots"]["added"] == 2
    assert not target.exists()
    merged = import_data(moved, target, apply=True)
    assert merged["baseline_snapshots"]["added"] == 2
    assert len(baseline_status(target)["snapshots"]) == 2
    assert (target / "modules/global/nuke/README.md").is_file()
    assert import_data(moved, target, apply=True)["baseline_snapshots"]["skipped"] == 2


def test_corrupt_baseline_prevents_migration_and_is_not_overwritten(tmp_path):
    root = tmp_path / "data"
    Store(root)
    snapshot = sync_baselines(root)["snapshots"][0]
    folder = root / snapshot["relative_path"]
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    path = folder / manifest["files"][0]["path"]
    path.write_text("changed", encoding="utf-8")
    assert not validate_baselines(root)["valid"]
    with pytest.raises(ValueError, match="baseline"):
        migrate_data(root, tmp_path / "moved")
    with pytest.raises(ValueError, match="baseline"):
        sync_baselines(root)
    assert path.read_text() == "changed"


def test_legacy_hash_paths_are_renamed_and_links_are_short(tmp_path):
    root = tmp_path / "data"
    first = sync_baselines(root)
    for snapshot in first["snapshots"]:
        (root / snapshot["relative_path"]).rename(root / "baselines" / snapshot["snapshot_id"])
    result = sync_baselines(root)
    assert {Path(s["relative_path"]).name for s in result["snapshots"]} == {"houdini", "project"}
    index = (root / "modules/global/houdini/README.md").read_text(encoding="utf-8")
    assert "baselines/houdini/" in index
    assert not any(s["snapshot_id"] in index for s in result["snapshots"])
    assert validate_baselines(root)["valid"]


def test_same_short_name_different_snapshots_merge_without_overwriting(tmp_path):
    import hashlib

    from fx_agent.store import compact

    a, b = tmp_path / "A", tmp_path / "B"
    for root in (a, b):
        Store(root)
        sync_baselines(root)
    folder = b / "baselines/houdini"
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    path = folder / manifest["files"][0]["path"]
    path.write_text("a different valid rule revision", encoding="utf-8")
    manifest["files"][0]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest["files"][0]["size_bytes"] = path.stat().st_size
    manifest["version"] = "fixture-version"
    manifest.pop("snapshot_id")
    manifest["snapshot_id"] = hashlib.sha256(compact(manifest).encode()).hexdigest()
    (folder / "manifest.json").write_text(compact(manifest), encoding="utf-8")
    assert validate_baselines(b)["valid"]
    result = import_data(b, a, apply=True)
    assert result["baseline_snapshots"] == {"added": 1, "skipped": 1}
    assert (a / "baselines/houdini-2").is_dir()
    assert json.loads((a / "baselines/houdini/manifest.json").read_text())["version"] == "2.25.0"
    assert import_data(b, a, apply=True)["baseline_snapshots"]["skipped"] == 2


def test_concurrent_sync_is_serialized_without_false_corruption(tmp_path):
    root = tmp_path / "data"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: sync_baselines(root), range(2)))
    assert results[0]["snapshots"] == results[1]["snapshots"]
    assert len(baseline_status(root)["snapshots"]) == 2
    assert not (root / ".baseline.lock").exists()


@pytest.mark.asyncio
async def test_baseline_sync_tool_is_available(tmp_path, monkeypatch):
    from fx_agent.server import get_server

    monkeypatch.setenv("FX_AGENT_DATA_ROOT", str(tmp_path / "data"))
    result = await get_server().call_tool("fx_sync_module_baselines", {})
    assert json.loads(result[0].text)["snapshots"]
