"""Two authenticated workstations share knowledge, never local production state."""

import asyncio
import sqlite3
import uuid

import httpx
import pytest
from fx_data_service.app import DeviceConfig, ServiceConfig, create_app, snapshot_database

from fx_agent.multidevice import RemoteClient, ServiceUnavailable, service_scope
from fx_agent.store import Store


@pytest.fixture
def settings(tmp_path):
    return ServiceConfig(
        data_root=tmp_path / "shared",
        database_root=tmp_path / "local",
        allowed_roots=[tmp_path / "shared"],
        devices={
            "device-a": DeviceConfig(
                name="A", token="a" * 40, path_maps={"T:/project": str(tmp_path / "shared/project")}
            ),
            "device-b": DeviceConfig(
                name="B", token="b" * 40, path_maps={"T:/project": str(tmp_path / "shared/project")}
            ),
        },
    )


async def call(client, operation, arguments=None, *, device="a", request_id=None):
    return await client.post(
        "/v1/operations",
        headers={"Authorization": "Bearer " + device * 40},
        json={
            "request_id": request_id or uuid.uuid4().hex,
            "operation": operation,
            "arguments": arguments or {},
        },
    )


@pytest.fixture
async def client(settings):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(settings)), base_url="http://test"
    ) as connection:
        yield connection


def checkpoint(**changes):
    return (
        dict(
            project_id="p",
            shot_id="s",
            software="houdini",
            summary="saved",
            scene_path="T:/project/scene.hip",
            next_steps=["check"],
            expected_revision=0,
        )
        | changes
    )


async def test_authentication_allowlist_and_size_limit(client):
    assert (await client.post("/v1/operations", json={})).status_code == 401
    assert (await call(client, "tool.execute_python", {"code": "print(1)"})).status_code == 403
    assert (await call(client, "task._read")).status_code == 403
    response = await client.post(
        "/v1/operations",
        content=b"x" * (2 * 1024 * 1024 + 1),
        headers={"Authorization": "Bearer " + "a" * 40},
    )
    assert response.status_code == 413


@pytest.mark.parametrize(
    "arguments", [{"max_chars": 14000}, {"max_chars": 30000}, {"max_chars": 1023}, {"offset": -1}]
)
async def test_rule_pagination_returns_actionable_limits(client, arguments):
    response = await call(client, "tool.fx_get_production_rules", arguments)
    assert response.status_code == 422
    assert response.json()["error_code"] == "invalid_pagination"
    assert "12000" in response.json()["error"]
    assert response.json()["limits"]["max_chars"] == {"minimum": 1024, "maximum": 12000}
    assert (
        await call(client, "tool.fx_get_production_rules", {"max_chars": 12000, "offset": 16})
    ).status_code == 200


def test_rules_schema_advertises_budget_and_offset_bounds():
    from mcp.server.fastmcp import FastMCP

    from fx_agent.data_api import fx_get_production_rules

    mcp = FastMCP("schema-test")
    mcp.add_tool(fx_get_production_rules)
    schema = mcp._tool_manager.get_tool("fx_get_production_rules").parameters["properties"]
    assert schema["max_chars"]["minimum"] == 1024
    assert schema["max_chars"]["maximum"] == 12000
    assert schema["offset"]["minimum"] == 0


@pytest.mark.parametrize(
    "status,payload",
    [
        (502, b"<html><title>502 Bad Gateway</title>private data</html>"),
        (200, b"null"),
        (200, b"[]"),
        (200, b'{"ok":true}'),
        (502, b'{"ok":true}'),
        (502, b'{"error":{"detail":"private data"}}'),
    ],
)
def test_invalid_remote_envelope_has_safe_http_diagnostics(monkeypatch, status, payload):
    monkeypatch.delenv("FX_AGENT_CLIENT_CONFIG", raising=False)
    monkeypatch.setenv("FX_AGENT_SERVER_IP", "127.0.0.1")
    monkeypatch.setenv("FX_AGENT_API_KEY", "k" * 43)
    original = httpx.Client
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            status,
            content=payload,
            headers={
                "Content-Type": "text/html" if payload.startswith(b"<") else "application/json"
            },
        )
    )
    monkeypatch.setattr(
        "fx_agent.multidevice.httpx.Client",
        lambda **kwargs: original(transport=transport, **kwargs),
    )
    with pytest.raises(ServiceUnavailable) as error:
        RemoteClient().call("tool.fx_get_production_rules", request_id="a" * 32)
    text = str(error.value)
    assert f"HTTP {status}" in text
    assert "request_id=" + "a" * 32 in text
    assert "private data" not in text and "k" * 43 not in text


async def test_shared_knowledge_but_isolated_checkpoint_and_revisions(client, settings):
    rule = dict(
        rule_key="layout",
        scope="global",
        title="布局",
        statement="保留节点布局",
        user_quote="保留节点布局",
        source_ref="user",
        expected_revision=0,
    )
    response = await call(client, "tool.fx_record_user_rule", {"rule": rule})
    assert response.status_code == 200, response.text
    response = await call(client, "tool.fx_get_production_rules", device="b")
    assert response.status_code == 200, response.text
    assert "保留节点布局" in response.text
    conflict = await call(client, "tool.fx_record_user_rule", {"rule": rule}, device="b")
    assert conflict.status_code == 409
    for device in ("a", "b"):
        saved = await call(
            client, "tool.fx_save_checkpoint", checkpoint(summary=device), device=device
        )
        assert saved.status_code == 200, saved.text
    for device in ("a", "b"):
        read = await call(
            client, "tool.fx_get_checkpoint", {"project_id": "p", "shot_id": "s"}, device=device
        )
        assert read.json()["result"]["checkpoint"]["summary"] == device
    assert not (settings.data_root / "memory.sqlite3").exists()
    assert (settings.database_root / "memory.sqlite3").exists()


def task_spec(scene="T:/project/scene.hip"):
    return dict(
        project_id="p",
        shot_id="s",
        software="houdini",
        scene_path=scene,
        endpoint="127.0.0.1:18186",
        goal="test",
        acceptance=["preview"],
        budget="draft",
        modules=["particles"],
    )


async def test_task_ownership_and_device_endpoint_isolation(client):
    ids = []
    for device in ("a", "b"):
        response = await call(client, "task.begin", {"spec": task_spec()}, device=device)
        assert response.status_code == 200, response.text
        ids.append(response.json()["result"]["task_id"])
    assert (await call(client, "task.get", {"task_id": ids[0]}, device="b")).status_code == 403
    assert (await call(client, "task.edit", {"task_id": ids[0]})).status_code == 200
    other = await call(client, "task.get", {"task_id": ids[1], "detail": True}, device="b")
    assert other.json()["result"]["edit_seq"] == 0


async def test_idempotency_concurrency_and_audit(client, settings):
    request_id = uuid.uuid4().hex
    replies = await asyncio.gather(
        *[
            call(client, "tool.fx_save_checkpoint", checkpoint(), request_id=request_id)
            for _ in range(2)
        ]
    )
    assert [r.status_code for r in replies] == [200, 200]
    assert replies[0].json() == replies[1].json()
    mismatch = await call(
        client, "tool.fx_save_checkpoint", checkpoint(summary="different"), request_id=request_id
    )
    assert mismatch.status_code == 409
    with sqlite3.connect(settings.database_root / "service.sqlite3") as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM requests WHERE request_id=?", (request_id,)
            ).fetchone()[0]
            == 1
        )
        assert (
            db.execute(
                "SELECT device_id FROM requests WHERE request_id=?", (request_id,)
            ).fetchone()[0]
            == "device-a"
        )
    devices = await client.get("/v1/devices", headers={"Authorization": "Bearer " + "a" * 40})
    assert devices.status_code == 200
    assert "token" not in devices.text
    assert any(d["device_id"] == "device-a" and d["online"] for d in devices.json()["devices"])


async def test_path_mapping_and_file_access_boundary(settings, tmp_path):
    shared = settings.data_root
    shared.mkdir()
    scene = shared / "scene.hip"
    scene.write_bytes(b"HIP")
    settings.devices["device-a"].path_maps = {"T:/FX-AGNET/data": str(shared)}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(settings)), base_url="http://test"
    ) as c:
        response = await call(c, "task.begin", {"spec": task_spec("T:/FX-AGNET/data/scene.hip")})
        assert response.status_code == 200, response.text
        read = await call(
            c, "task.get", {"task_id": response.json()["result"]["task_id"], "detail": True}
        )
        assert read.json()["result"]["scene_path"] == "T:/FX-AGNET/data/scene.hip"
        assert read.json()["result"]["assets"] == []
        blocked = await call(c, "tool.fx_plan_template_import", {"source": str(tmp_path.parent)})
        assert blocked.status_code == 403
        traversal = await call(
            c, "tool.fx_plan_template_import", {"source": "T:/FX-AGNET/data/../../outside"}
        )
        assert traversal.status_code == 403
        extras = await call(
            c,
            "tool.fx_learn_templates",
            {
                "source": str(shared / "input.hip"),
                "files": [{"path": str(tmp_path / "private.txt"), "role": "reference"}],
            },
        )
        assert extras.status_code == 403
        media = await call(
            c, "tool.fx_review_template_effects", {"study_id": "x", "media_source": str(tmp_path)}
        )
        assert media.status_code == 403


async def test_mapping_preserves_user_quote_across_devices(client):
    quote = "T:/project/scene.hip"
    rule = dict(
        rule_key="verbatim",
        title="path quote",
        statement=quote,
        user_quote=quote,
        source_ref=quote,
        expected_revision=0,
    )
    response = await call(client, "tool.fx_record_user_rule", {"rule": rule})
    assert response.status_code == 200
    response = await call(client, "tool.fx_get_user_rule", {"rule_key": "verbatim"}, device="b")
    assert response.json()["result"]["user_quote"] == quote
    assert response.json()["result"]["statement"] == quote
    assert response.json()["result"]["source_ref"] == quote


async def test_feedback_attempts_are_isolated_but_history_shared(client):
    feedback = dict(
        project_id="p",
        shot_id="s",
        effect_type="particles",
        attempt_id="same-name",
        problem="draft",
        change="node",
        reason="test",
        result="preview",
        outcome="improved",
        user_quote="test",
        source_ref="test",
        expected_revision=0,
    )
    ids = []
    for device in ("a", "b"):
        response = await call(
            client, "tool.fx_record_feedback", {"feedback": feedback}, device=device
        )
        assert response.status_code == 200, response.text
        assert response.json()["result"]["revision"] == 1
        ids.append(response.json()["result"]["feedback_id"])
    assert ids[0] != ids[1]
    response = await call(
        client, "tool.fx_get_learning_history", {"project_id": "p", "shot_id": "s"}, device="b"
    )
    assert all(value in response.text for value in ids)


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("fx_record_feedback", {"feedback": {"outcome": "accepted"}}),
        ("fx_finalize_learning", {"feedback_id": "x", "project_id": "p"}),
        ("fx_refine_effect", {"card_id": "x"}),
    ],
)
async def test_http_cannot_bypass_knowledge_admission(client, name, arguments):
    response = await call(client, "tool." + name, arguments)
    assert response.status_code == 422


def test_snapshot_keeps_source_and_refuses_overwrite(tmp_path):
    source = tmp_path / "old.sqlite3"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE evidence(value)")
        db.execute("INSERT INTO evidence VALUES ('keep')")
    target = tmp_path / "backup.sqlite3"
    snapshot_database(source, target)
    with sqlite3.connect(target) as db:
        assert db.execute("SELECT value FROM evidence").fetchone()[0] == "keep"
    assert source.exists()
    with pytest.raises(FileExistsError):
        snapshot_database(source, target)


def test_offline_does_not_create_local_database(monkeypatch, tmp_path):
    monkeypatch.setenv("FX_AGENT_SERVICE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("FX_AGENT_SERVICE_TOKEN", "a" * 40)
    with pytest.raises(ServiceUnavailable):
        RemoteClient().call("task.begin", {"spec": task_spec(str(tmp_path / "missing.hip"))})
    assert not (tmp_path / "memory.sqlite3").exists()


def test_direct_store_access_fails_closed_in_client_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("FX_AGENT_SERVICE_URL", "http://127.0.0.1:1")
    with pytest.raises(RuntimeError, match="remote|service"):
        Store(tmp_path)


def test_device_scope_does_not_change_single_machine_checkpoints(tmp_path):
    store = Store(tmp_path)
    store.save_checkpoint(**checkpoint(summary="legacy"))
    with service_scope(tmp_path, tmp_path, "device-a"):
        assert Store(tmp_path).get_checkpoint("p", "s") is None
        Store(tmp_path).save_checkpoint(**checkpoint(summary="device"))
    assert store.get_checkpoint("p", "s")["summary"] == "legacy"
