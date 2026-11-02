"""Case packages hold independent copies and detect missing or changed evidence."""

import importlib
import json
from pathlib import Path

import pytest


def archive_at(root):
    return importlib.import_module("fx_agent.archive").CaseArchive(root)


def source_files(tmp_path):
    source = tmp_path / "work"
    source.mkdir()
    hip = source / "draft.hip"
    hip.write_bytes(b"test fixture: not a real Houdini project")
    image = source / "reference.png"
    image.write_bytes(b"test fixture: not an actual image")
    return hip, image


def create(archive, hip, image, **changes):
    values = {
        "project_id": "广告A",
        "shot_id": "sh010",
        "title": "逆光烟尘",
        "files": [
            {"path": str(hip), "role": "project"},
            {"path": str(image), "role": "reference"},
        ],
    }
    return archive.create(**(values | changes))


def test_archive_survives_original_change_and_removal(tmp_path):
    hip, image = source_files(tmp_path)
    archive = archive_at(tmp_path / "data")
    manifest = create(archive, hip, image)
    assert manifest["archive_state"] == "provided_files_verified"
    assert manifest["reproduction_status"] == "not_tested"
    assert manifest["dependency_scan"] == "not_performed"
    paths = [Path(manifest["case_path"]) / item["archive_path"] for item in manifest["files"]]
    assert paths[0].read_bytes() == hip.read_bytes()
    hip.write_bytes(b"changed original")
    image.unlink()
    assert paths[0].read_bytes() != hip.read_bytes()
    checked = archive.verify(manifest["case_id"], project_id="广告A")
    assert checked["valid"] is True
    assert archive.get(manifest["case_id"], project_id="广告A") == manifest


def test_corrupt_archive_is_detected_and_other_project_cannot_read_it(tmp_path):
    hip, image = source_files(tmp_path)
    archive = archive_at(tmp_path / "data")
    manifest = create(archive, hip, image)
    with pytest.raises(ValueError, match="project"):
        archive.get(manifest["case_id"], project_id="其他工程")
    copied = Path(manifest["case_path"]) / manifest["files"][0]["archive_path"]
    copied.write_bytes(b"corrupt")
    result = archive.verify(manifest["case_id"], project_id="广告A")
    assert result["valid"] is False
    assert result["issues"]
    copied.unlink()
    assert archive.verify(manifest["case_id"], project_id="广告A")["valid"] is False


def test_missing_file_and_size_limit_never_publish_a_case(tmp_path):
    hip, image = source_files(tmp_path)
    archive = archive_at(tmp_path / "data")
    with pytest.raises(ValueError, match="max_total_bytes"):
        create(archive, hip, image, max_total_bytes=1)
    with pytest.raises(FileNotFoundError):
        create(archive, hip, image.with_name("missing.png"))
    assert not list((tmp_path / "data" / "cases").glob("*/manifest.json"))


def test_case_ids_and_tampered_manifest_paths_cannot_escape_root(tmp_path):
    hip, image = source_files(tmp_path)
    archive = archive_at(tmp_path / "data")
    for invalid in ("../outside", "D:/outside", "not-an-id"):
        with pytest.raises(ValueError):
            archive.get(invalid, project_id="广告A")
    manifest = create(archive, hip, image)
    path = Path(manifest["case_path"]) / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["files"][0]["archive_path"] = "../../outside.hip"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="path"):
        archive.verify(manifest["case_id"], project_id="广告A")


def test_partial_copy_failure_does_not_publish_success(tmp_path, monkeypatch):
    module = importlib.import_module("fx_agent.archive")
    hip, image = source_files(tmp_path)
    archive = archive_at(tmp_path / "data")

    def broken_copy(source, destination, **kwargs):
        destination.write_bytes(b"partial")
        raise OSError("disk full")

    monkeypatch.setattr(module, "copy_verified", broken_copy)
    with pytest.raises(OSError, match="disk full"):
        create(archive, hip, image)
    assert not list((tmp_path / "data" / "cases").glob("*/manifest.json"))
    assert not list((tmp_path / "data" / "cases" / ".staging").glob("*"))


def test_source_growth_after_preflight_is_bounded(tmp_path, monkeypatch):
    module = importlib.import_module("fx_agent.archive")
    hip, image = source_files(tmp_path)
    archive = archive_at(tmp_path / "data")
    copy = module.copy_verified

    def grow_then_copy(source, destination, **kwargs):
        source.write_bytes(b"x" * 1024)
        return copy(source, destination, **kwargs)

    monkeypatch.setattr(module, "copy_verified", grow_then_copy)
    with pytest.raises(ValueError, match="max_total_bytes"):
        create(archive, hip, image, max_total_bytes=512)


def test_source_changed_during_copy_is_rejected(tmp_path, monkeypatch):
    module = importlib.import_module("fx_agent.archive")
    hip, image = source_files(tmp_path)
    original_fstat = module.os.fstat
    calls = 0

    def mutate_after_first_stat(fd):
        nonlocal calls
        result = original_fstat(fd)
        calls += 1
        if calls == 1:
            with hip.open("ab") as file:
                file.write(b"changed")
        return result

    monkeypatch.setattr(module.os, "fstat", mutate_after_first_stat)
    with pytest.raises(ValueError, match="changed during copy"):
        create(archive_at(tmp_path / "data"), hip, image)


def test_archive_rejects_invalid_source_roles_and_relative_paths(tmp_path):
    hip, image = source_files(tmp_path)
    archive = archive_at(tmp_path / "data")
    with pytest.raises(ValueError, match="absolute"):
        create(archive, hip, image, files=[{"path": "draft.hip", "role": "project"}])
    with pytest.raises(ValueError, match="Project must have"):
        create(archive, hip, image, files=[{"path": str(image), "role": "project"}])
    with pytest.raises(ValueError):
        create(archive, hip, image, files=[{"path": str(image), "role": "executable"}])


def test_empty_or_invalid_manifest_is_not_valid_evidence(tmp_path):
    hip, image = source_files(tmp_path)
    archive = archive_at(tmp_path / "data")
    case = create(archive, hip, image)
    path = Path(case["case_path"]) / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["files"] = []
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError):
        archive.verify(case["case_id"], project_id="广告A")


@pytest.mark.asyncio
@pytest.mark.parametrize("repeat", [False, True])
async def test_cancelled_mcp_archive_waits_for_cleanup_and_does_not_publish(
    tmp_path, monkeypatch, repeat
):
    import asyncio
    import threading

    module = importlib.import_module("fx_agent.archive")
    hip, image = source_files(tmp_path)
    monkeypatch.setenv("FX_AGENT_DATA_ROOT", str(tmp_path / "data"))
    server = importlib.import_module("fx_agent.server").get_server()
    entered, release = threading.Event(), threading.Event()
    original = module.copy_verified

    def slow_copy(source, destination, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return original(source, destination, **kwargs)

    monkeypatch.setattr(module, "copy_verified", slow_copy)
    task = asyncio.create_task(
        server.call_tool(
            "fx_archive_case",
            {
                "project_id": "p",
                "shot_id": "s",
                "title": "draft",
                "files": [{"path": str(hip), "role": "project"}],
            },
        )
    )
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    # Give the tool's cancellation handler time to signal its worker.
    await asyncio.sleep(0)
    if repeat:
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not list((tmp_path / "data" / "cases").glob("*/manifest.json"))
    assert not list((tmp_path / "data" / "cases" / ".staging").glob("*"))


@pytest.mark.asyncio
async def test_case_tools_archive_explicit_files_and_retrieve_summary(tmp_path, monkeypatch):
    hip, image = source_files(tmp_path)
    monkeypatch.setenv("FX_AGENT_DATA_ROOT", str(tmp_path / "data"))
    server = importlib.import_module("fx_agent.server").get_server()
    response = await server.call_tool(
        "fx_archive_case",
        {
            "project_id": "p",
            "shot_id": "s",
            "title": "draft",
            "files": [
                {"path": str(hip), "role": "project"},
                {"path": str(image), "role": "reference"},
            ],
        },
    )
    case = json.loads(response[0].text)
    result = await server.call_tool("fx_get_case", {"case_id": case["case_id"], "project_id": "p"})
    summary = json.loads(result[0].text)
    assert summary["file_count"] == 2
    assert "files" not in summary
    result = await server.call_tool(
        "fx_validate_case", {"case_id": case["case_id"], "project_id": "p"}
    )
    assert json.loads(result[0].text)["valid"] is True
