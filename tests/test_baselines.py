"""Upstream rules must travel with data, independently of a virtual environment."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from fx_agent.baselines import baseline_lock, baseline_status, sync_baselines, validate_baselines
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


def test_import_updates_current_baseline_without_numbered_copies(tmp_path):
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
    assert not (a / "baselines/houdini-2").exists()
    assert json.loads((a / "baselines/houdini/manifest.json").read_text())["version"] == "fixture-version"
    assert import_data(b, a, apply=True)["baseline_snapshots"]["skipped"] == 2


def test_concurrent_sync_is_serialized_without_false_corruption(tmp_path):
    root = tmp_path / "data"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: sync_baselines(root), range(2)))
    assert results[0]["snapshots"] == results[1]["snapshots"]
    assert len(baseline_status(root)["snapshots"]) == 2
    assert not (root / ".baseline.lock").exists()


def test_runtime_lock_is_not_carried_to_another_device(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    Store(source)
    sync_baselines(source)
    with baseline_lock(source):
        migrate_data(source, target)
    assert not (target / ".baseline.lock").exists()
    assert sync_baselines(target)["snapshots"]


@pytest.mark.asyncio
async def test_baseline_sync_tool_is_available(tmp_path, monkeypatch):
    from fx_agent.server import get_server

    monkeypatch.setenv("FX_AGENT_DATA_ROOT", str(tmp_path / "data"))
    result = await get_server().call_tool("fx_sync_module_baselines", {})
    assert json.loads(result[0].text)["snapshots"]


def test_sync_updates_adds_and_removes_documents_in_same_directory(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import fx_agent.baselines as baselines

    package = tmp_path / "package"
    markdown = package / "fxhoudinimcp/prompts/markdown"
    markdown.mkdir(parents=True)
    (markdown / "keep.md").write_text("old", encoding="utf-8")
    (markdown / "remove.md").write_text("obsolete", encoding="utf-8")
    for name in ("LICENSE", "NOTICE"):
        path = package / "dist-info/licenses" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name, encoding="utf-8")
    monkeypatch.setattr(baselines, "distribution", lambda _: SimpleNamespace(
        version="fixture", locate_file=lambda p: package / p,
        files=["dist-info/licenses/LICENSE", "dist-info/licenses/NOTICE"],
    ))
    root = tmp_path / "data"
    sync_baselines(root)
    (markdown / "keep.md").write_text("updated", encoding="utf-8")
    (markdown / "remove.md").unlink()
    (markdown / "add.md").write_text("new", encoding="utf-8")
    sync_baselines(root)
    assert (root / "baselines/houdini/markdown/keep.md").read_text() == "updated"
    assert not (root / "baselines/houdini/markdown/remove.md").exists()
    assert (root / "baselines/houdini/markdown/add.md").read_text() == "new"
    assert {p.name for p in (root / "baselines").iterdir()} == {"houdini", "project", ".staging"}
    assert validate_baselines(root)["valid"]


def test_sync_consolidates_legacy_numbered_copies(tmp_path):
    import shutil

    root = tmp_path / "data"
    sync_baselines(root)
    shutil.copytree(root / "baselines/project", root / "baselines/project-2")
    shutil.copytree(root / "baselines/project", root / "baselines/project-3")
    sync_baselines(root)
    assert not (root / "baselines/project-2").exists()
    assert not (root / "baselines/project-3").exists()
    assert len(baseline_status(root)["snapshots"]) == 2


def test_failed_replacement_keeps_previous_current(tmp_path, monkeypatch):
    import hashlib
    import shutil

    import fx_agent.baselines as baselines

    root = tmp_path / "data"
    sync_baselines(root)
    target = root / "baselines/project"
    before = {p.relative_to(target): p.read_bytes() for p in target.rglob("*") if p.is_file()}
    source = tmp_path / "incoming"
    shutil.copytree(target, source)
    manifest = json.loads((source / "manifest.json").read_text())
    first = manifest["files"][0]
    (source / first["path"]).write_text("updated workflow", encoding="utf-8")
    first["sha256"] = hashlib.sha256((source / first["path"]).read_bytes()).hexdigest()
    first["size_bytes"] = (source / first["path"]).stat().st_size
    removed = manifest["files"].pop(1)
    (source / removed["path"]).unlink()
    (source / "added.md").write_text("new", encoding="utf-8")
    manifest["files"].append({"path": "added.md", "size_bytes": 3,
                              "sha256": hashlib.sha256(b"new").hexdigest()})
    manifest["snapshot_id"] = baselines._identity(manifest)
    (source / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    original = Path.replace

    def fail_new_copy(source, destination):
        if destination == target / "manifest.json" and "restore" not in source.parts:
            raise OSError("publication failed")
        return original(source, destination)

    monkeypatch.setattr(Path, "replace", fail_new_copy)
    with pytest.raises(OSError, match="publication failed"):
        baselines.publish_baseline(source, target)
    assert {p.relative_to(target): p.read_bytes() for p in target.rglob("*") if p.is_file()} == before
    assert validate_baselines(root)["valid"]


def test_failed_rollback_preserves_recoverable_previous_copy(tmp_path, monkeypatch):
    import fx_agent.baselines as baselines

    root = tmp_path / "data"
    sync_baselines(root)
    target = root / "baselines/project"
    before = (target / "manifest.json").read_bytes()
    original = Path.replace

    def fail_publication_and_restore(source, destination):
        if destination == target / "manifest.json":
            raise OSError("locked destination")
        return original(source, destination)

    monkeypatch.setattr(Path, "replace", fail_publication_and_restore)
    with pytest.raises(OSError, match="locked destination"):
        baselines.publish_baseline(target, target)
    retained = list((root / "baselines/.staging").glob("*/previous/manifest.json"))
    assert len(retained) == 1
    assert retained[0].read_bytes() == before


def test_import_selects_highest_legacy_number_then_consolidates(tmp_path):
    import hashlib
    import shutil

    from fx_agent.store import compact

    source, target = tmp_path / "source", tmp_path / "target"
    Store(source)
    sync_baselines(source)
    current = source / "baselines/project"
    shutil.copytree(current, source / "baselines/project-2")
    shutil.copytree(current, source / "baselines/project-10")
    for folder in source.glob("baselines/project*"):
        manifest = json.loads((folder / "manifest.json").read_text())
        manifest.pop("storage_mode", None)
        manifest.pop("snapshot_id")
        if folder.name == "project-2":
            manifest["version"] = "intermediate"
        manifest["snapshot_id"] = hashlib.sha256(compact(manifest).encode()).hexdigest()
        (folder / "manifest.json").write_text(compact(manifest), encoding="utf-8")
    assert validate_baselines(source)["valid"]
    result = import_data(source, target, apply=True)
    assert result["baseline_snapshots"] == {"added": 2, "skipped": 0}
    assert {p.name for p in (target / "baselines").iterdir()} == {"houdini", "project", ".staging"}
    assert json.loads((target / "baselines/project/manifest.json").read_text())["version"] != "intermediate"
    assert import_data(source, target, apply=True)["baseline_snapshots"]["skipped"] == 2


def test_internal_directory_link_is_rejected_before_import_changes_target(tmp_path, monkeypatch):
    source, target = tmp_path / "source", tmp_path / "target"
    for root in (source, target):
        Store(root)
        sync_baselines(root)
    houdini = target / "baselines/houdini"
    project = target / "baselines/project"
    original = (project / "manifest.json").read_bytes()
    original_is_junction = Path.is_junction
    monkeypatch.setattr(Path, "is_junction", lambda p: p == houdini or original_is_junction(p))
    with pytest.raises(ValueError, match="baseline|Baseline"):
        import_data(source, target, apply=True)
    assert (project / "manifest.json").read_bytes() == original


def test_resolved_destination_cannot_redirect_to_another_source(tmp_path, monkeypatch):
    import fx_agent.baselines as baselines

    root = tmp_path / "data"
    sync_baselines(root)
    manifest = json.loads((root / "baselines/houdini/manifest.json").read_text())
    original = baselines.contained
    monkeypatch.setattr(baselines, "contained", lambda base, path: (
        root / "baselines/project" if base == root and path == "baselines/houdini"
        else original(base, path)
    ))
    with pytest.raises(ValueError, match="must not be a link"):
        baselines.baseline_destination(root, manifest, set())


def test_current_source_wins_over_ambiguous_legacy_hashes(tmp_path):
    import hashlib
    import shutil

    from fx_agent.baselines import current_baseline_sources
    from fx_agent.store import compact

    root = tmp_path / "data"
    sync_baselines(root)
    for version, name in enumerate(("0" * 64, "1" * 64)):
        folder = root / "baselines" / name
        shutil.copytree(root / "baselines/project", folder)
        manifest = json.loads((folder / "manifest.json").read_text())
        manifest.pop("storage_mode")
        manifest.pop("snapshot_id")
        manifest["version"] = str(version)
        manifest["snapshot_id"] = hashlib.sha256(compact(manifest).encode()).hexdigest()
        (folder / "manifest.json").write_text(compact(manifest), encoding="utf-8")
    assert {p.name for p in current_baseline_sources(root)} == {"houdini", "project"}
