"""Cross-platform cloud regressions: actionable errors and verified file transfers."""

import hashlib
import io
import uuid
import zipfile
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest
from fx_data_service.app import ServiceConfig, create_app, map_value

from fx_agent.multidevice import RemoteClient


@pytest.fixture
async def cloud(tmp_path):
    settings = ServiceConfig(data_root=tmp_path / "data", database_root=tmp_path / "db",
                             api_key="k" * 43)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(settings)),
                                base_url="http://test",
                                headers={"Authorization": "Bearer " + settings.api_key,
                                         "X-FX-Device-ID": "test-device"}) as client:
        yield client, settings


async def post(client, operation, arguments):
    return await client.post("/v1/operations", json={"request_id": uuid.uuid4().hex,
                                                    "operation": operation,
                                                    "arguments": arguments})


async def test_validation_diagnostics_are_correlated_and_do_not_log_values(cloud, caplog):
    client, _ = cloud
    private = "private-token-never-log-this"
    response = await post(client, "tool.fx_get_module_catalog", {"unexpected": private})
    assert response.status_code == 422
    body = response.json()
    assert body["error_code"] == "invalid_arguments"
    assert body["request_id"] == response.headers["x-fx-request-id"]
    assert body["details"][0]["type"] == "unexpected_keyword_argument"
    assert private not in response.text + caplog.text
    assert "tool.fx_get_module_catalog" in caplog.text
    assert body["request_id"] in caplog.text


async def test_malformed_request_is_logged_without_private_json(cloud, caplog):
    client, _ = cloud
    response = await client.post("/v1/operations", json={"request_id": "secret-input"})
    assert response.status_code == 422
    assert response.headers.get("x-fx-request-id")
    assert "invalid_request" in caplog.text
    assert "secret-input" not in caplog.text + response.text


async def test_unknown_field_names_and_operation_control_characters_are_not_logged(cloud, caplog):
    client, _ = cloud
    secret_key = "secret-credential-in-key-" + "x" * 1000
    response = await post(client, "tool.fx_get_module_catalog", {secret_key: "value"})
    assert response.status_code == 422
    assert response.json()["details"][0]["field"] == ["<unknown>"]
    assert secret_key not in response.text + caplog.text
    injected = await post(client, "task.get\nforged-log", {})
    assert injected.status_code == 422
    assert "forged-log" not in caplog.text


def test_mapping_is_case_sensitive_on_posix_and_preserves_windows_semantics():
    maps = {"/data/Case": "/local/Case", "T:/Shots": "/shared/shots"}
    assert map_value("/data/case/file.nk", maps, "path") == "/data/case/file.nk"
    assert map_value(r"t:\SHOTS\a\..\scene.ma", maps, "scene_path") == "/shared/shots/scene.ma"
    assert map_value({"user_quote": "T:/Shots/a", "path": "T:/Shots/a"}, maps) == {
        "user_quote": "T:/Shots/a", "path": "/shared/shots/a"}


@pytest.mark.parametrize("software,suffix", [("houdini", ".hip"), ("maya", ".ma"), ("nuke", ".nk")])
async def test_upload_mapping_plan_and_private_download(cloud, software, suffix):
    client, settings = cloud
    original = "T:/镜头/" + software + suffix
    content = b"synthetic scene"
    upload = await client.put("/v1/files", content=content, headers={
        "X-FX-Client-Path": quote(original, safe=""),
        "X-FX-SHA256": hashlib.sha256(content).hexdigest(), "X-FX-Mtime-Ns": "1000000000"})
    assert upload.status_code == 200, upload.text
    # Uploaded workspace evidence must have a usable authenticated download URL.
    assert upload.json()["download_path"]
    downloaded = await client.get("/v1/files/" + upload.json()["download_path"])
    assert downloaded.status_code == 200 and downloaded.content == content
    other = await client.get("/v1/files/" + upload.json()["download_path"],
                             headers={"X-FX-Device-ID": "other-device"})
    assert other.status_code == 404
    spec = dict(project_id="p", shot_id="s", software=software, scene_path=original,
                endpoint="127.0.0.1:18186", modules=["general"], goal="test",
                acceptance=["synthetic verification"], budget="draft")
    begun = await post(client, "task.begin", {"spec": spec})
    assert begun.status_code == 200, begun.text
    task_id = begun.json()["result"]["task_id"]
    from fx_agent.data_context import service_scope
    from fx_agent.task_gates import TaskGates
    with service_scope(settings.data_root, settings.database_root, "test-device"):
        gates = TaskGates(settings.data_root)
        for tool, arguments, result in [
            ("get_workflow_guide", {"topic": "discipline"}, "guide"),
            ("fx_get_production_rules", {"project_id": "p"}, {"user_rules": [], "omitted_rules": 0, "next_offset": 0}),
            ("fx_get_module_knowledge", {"module": "general"}, {"truncated": False}),
            ("fx_search_effects", {"module": "general"}, {"items": []}),
            ("fx_search_memory", {"module": "general"}, {"items": []}),
        ]:
            gates.observe(task_id, tool, arguments | {"software": software}, result)
    planned = await post(client, "task.plan", dict(task_id=task_id, expected_revision=1,
        method="minimal native nodes", applies_when="test", changes="material",
        selected_sources=[], source_adaptations=[], no_match_reason="Actual search returned zero"))
    assert planned.status_code == 200, planned.text
    assert planned.json()["result"]["state"] == "ready"


def client_config(monkeypatch):
    monkeypatch.delenv("FX_AGENT_CLIENT_CONFIG", raising=False)
    monkeypatch.setenv("FX_AGENT_SERVICE_URL", "http://127.0.0.1:18790")
    monkeypatch.setenv("FX_AGENT_SERVICE_TOKEN", "k" * 43)


def test_upload_failure_includes_http_and_request_id(monkeypatch, tmp_path):
    client_config(monkeypatch)
    original = httpx.Client
    transport = httpx.MockTransport(lambda request: httpx.Response(422,
        json={"error": "File checksum mismatch", "error_code": "checksum_mismatch"},
        headers={"x-fx-request-id": "a" * 32}))
    monkeypatch.setattr("fx_agent.multidevice.httpx.Client",
                        lambda **kwargs: original(transport=transport, **kwargs))
    path = tmp_path / "scene.ma"
    path.write_bytes(b"scene")
    with pytest.raises(Exception, match="HTTP 422.*request_id=" + "a" * 32):
        RemoteClient().upload_path(path)


def test_download_checks_hash_and_refuses_overwrite(monkeypatch, tmp_path):
    client_config(monkeypatch)
    original = httpx.Client
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"scene"))
    monkeypatch.setattr("fx_agent.multidevice.httpx.Client",
                        lambda **kwargs: original(transport=transport, **kwargs))
    connection = RemoteClient()
    target = tmp_path / "scene.nk"
    with pytest.raises(ValueError, match="checksum"):
        connection.download_file("transfers/test.zip", target, "0" * 64)
    assert not target.exists()
    connection.download_file("transfers/test.zip", target, hashlib.sha256(b"scene").hexdigest())
    assert target.read_bytes() == b"scene"
    with pytest.raises(FileExistsError):
        connection.download_file("transfers/test.zip", target, hashlib.sha256(b"scene").hexdigest())
    monkeypatch.setattr("fx_agent.multidevice.MAX_TRANSFER_BYTES", 4)
    oversize = tmp_path / "oversize.nk"
    with pytest.raises(ValueError, match="exceeds"):
        connection.download_file("transfers/test.zip", oversize, hashlib.sha256(b"scene").hexdigest())
    assert not oversize.exists()


def test_export_download_rejects_traversal(monkeypatch, tmp_path):
    client_config(monkeypatch)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../escape.nk", b"private")
    connection = RemoteClient()
    def fake_download(relative, target, sha256):
        Path(target).write_bytes(buffer.getvalue())
    monkeypatch.setattr(connection, "download_file", fake_download)
    with pytest.raises(ValueError, match="archive"):
        connection.receive_export({"download": {"path": "transfers/a.zip", "sha256": "0" * 64}}, tmp_path / "copy")
    assert not (tmp_path / "escape.nk").exists()
    assert not (tmp_path / "copy").exists()


@pytest.mark.parametrize("software,suffix", [("houdini", ".hip"), ("maya", ".ma"), ("nuke", ".nk")])
def test_template_bundle_roundtrip_all_software(monkeypatch, tmp_path, software, suffix):
    from fx_data_service.transfers import export_bundle

    from fx_agent.archive import CaseArchive
    from fx_agent.data_context import service_scope
    from fx_agent.store import Store, compact

    root, database = tmp_path / "data", tmp_path / "db"
    source = tmp_path / ("source" + suffix)
    content = b"synthetic original project, unchanged"
    source.write_bytes(content)
    with service_scope(root, database, "test-device"):
        case = CaseArchive(root).create(project_id="p", shot_id="s", title="sample",
            files=[{"path": str(source), "role": "project"}])
        study_id = uuid.uuid4().hex
        record = dict(study_id=study_id, case_id=case["case_id"], project_id="p", software=software,
            artifacts=[{"archive_path": case["files"][0]["archive_path"],
                        "export_path": "scenes/项目" + suffix,
                        "sha256": hashlib.sha256(content).hexdigest()}],
            export_manifest_path=".fx-template/manifest.json")
        with Store(root).connection() as db:
            db.execute("INSERT INTO template_studies VALUES (?,?,?,?,?,?)",
                       (study_id, "source", "fingerprint", "p", compact(record), "sample"))
        result = export_bundle(root, study_id, "T:/local/copy")
    client_config(monkeypatch)
    original = httpx.Client
    def respond(request):
        assert request.headers["authorization"] == "Bearer " + "k" * 43
        return httpx.Response(200, content=(root / result["download"]["path"]).read_bytes())
    monkeypatch.setattr("fx_agent.multidevice.httpx.Client",
                        lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    target = tmp_path / "local-copy"
    published = RemoteClient().receive_export(result, target)
    assert published["downloaded"] is True
    assert (target / ("scenes/项目" + suffix)).read_bytes() == content
    assert Path(published["manifest_path"]).is_file()
    assert source.read_bytes() == content
    with pytest.raises(FileExistsError):
        RemoteClient().receive_export(result, target)
    # A source that grows after stat must be stopped before a large ZIP is written.
    from fx_data_service import transfers
    monkeypatch.setattr(transfers, "MAX_TRANSFER_BYTES", 128)
    source_member = root / "cases" / case["case_id"] / case["files"][0]["archive_path"]
    actual_open = Path.open
    def growing_open(path, *args, **kwargs):
        if path == source_member and args and args[0] == "rb":
            return io.BytesIO(b"x" * 129)
        return actual_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", growing_open)
    monkeypatch.setattr(transfers.TemplateLibrary, "validate", lambda self, key: {"valid": True})
    with service_scope(root, database, "test-device"), pytest.raises(ValueError, match="transfer limits"):
        export_bundle(root, study_id, "T:/local/other")
    assert not list((root / "transfers").glob(".bundle-*"))


async def test_task_signature_mismatch_is_actionable(cloud, caplog):
    client, _ = cloud
    response = await post(client, "task.plan", {"unexpected_new_client_field": "private"})
    assert response.status_code == 422
    assert response.json()["error_code"] == "invalid_arguments"
    assert any(d["type"] == "unexpected_keyword_argument" for d in response.json()["details"])
    assert "private" not in caplog.text


async def test_invalid_upload_reports_diagnostics_and_keeps_existing_file(cloud, caplog):
    client, _ = cloud
    metadata = {"X-FX-Client-Path": quote("T:/scene.nk", safe=""),
                "X-FX-SHA256": hashlib.sha256(b"good").hexdigest(), "X-FX-Mtime-Ns": "0"}
    response = await client.put("/v1/files", content=b"good", headers=metadata)
    member = response.json()["download_path"]
    failed = await client.put("/v1/files", content=b"bad", headers=metadata)
    assert failed.status_code == 422
    assert failed.headers["x-fx-request-id"] in caplog.text
    assert (await client.get("/v1/files/" + member)).content == b"good"
