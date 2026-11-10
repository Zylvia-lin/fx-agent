"""Optional service configuration, shared API keys and cross-machine files."""

import hashlib
import json
import uuid
from urllib.parse import quote

import httpx
from fx_data_service.app import ServiceConfig, create_app

from fx_agent.multidevice import RemoteClient, remote_enabled


async def test_shared_api_key_authenticates_but_device_tasks_stay_separate(tmp_path):
    settings = ServiceConfig(
        data_root=tmp_path / "data", database_root=tmp_path / "db", api_key="k" * 43
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(settings)), base_url="http://test"
    ) as client:
        for endpoint in ("/v1/status", "/v1/devices"):
            assert (await client.get(endpoint)).status_code == 401
        ids = []
        for device in ("a", "b"):
            headers = {"Authorization": "Bearer " + "k" * 43, "X-FX-Device-ID": device}
            status = await client.get("/v1/status", headers=headers)
            assert status.json()["device_id"] == device
            register = await client.post(
                "/v1/files/register", headers=headers, json={"path": "T:/work/scene.hip"}
            )
            assert register.status_code == 200, register.text
            scene = register.json()["path"]
            ids.append(scene)
            content = b"synthetic HIP"
            response = await client.put(
                "/v1/files",
                headers=headers
                | {
                    "X-FX-Client-Path": quote("T:/work/scene.hip", safe=""),
                    "X-FX-SHA256": hashlib.sha256(content).hexdigest(),
                    "X-FX-Mtime-Ns": "1000000000",
                },
                content=content,
            )
            assert response.status_code == 200, response.text
            assert response.json()["sha256"] == hashlib.sha256(content).hexdigest()
        assert ids[0] != ids[1]
        bad = await client.put("/v1/files", headers={"Authorization": "Bearer wrong"}, content=b"x")
        assert bad.status_code == 401


def test_service_mode_accepts_ip_port_key_and_local_override(monkeypatch):
    monkeypatch.setenv("FX_AGENT_DATA_MODE", "service")
    monkeypatch.setenv("FX_AGENT_SERVER_IP", "127.0.0.1")
    monkeypatch.setenv("FX_AGENT_SERVER_PORT", "18443")
    monkeypatch.setenv("FX_AGENT_API_KEY", "k" * 43)
    connection = RemoteClient()
    assert remote_enabled()
    assert connection.url == "https://127.0.0.1:18443"
    assert connection.token == "k" * 43
    monkeypatch.setenv("FX_AGENT_DATA_MODE", "local")
    assert not remote_enabled()


def test_config_file_wins_over_old_environment(tmp_path, monkeypatch):
    path = tmp_path / "client.json"
    path.write_text(
        json.dumps(
            {
                "server_ip": "127.0.0.1",
                "port": 18443,
                "api_key": "k" * 43,
                "device_id": uuid.uuid4().hex,
            }
        )
    )
    monkeypatch.setenv("FX_AGENT_CLIENT_CONFIG", str(path))
    monkeypatch.setenv("FX_AGENT_SERVICE_URL", "https://wrong.example")
    monkeypatch.setenv("FX_AGENT_SERVICE_TOKEN", "x" * 43)
    assert RemoteClient().url == "https://127.0.0.1:18443"
    assert RemoteClient().token == "k" * 43


async def test_admin_login_create_revoke_and_do_not_list_plain_keys(tmp_path):
    settings = ServiceConfig(
        data_root=tmp_path / "data",
        database_root=tmp_path / "db",
        api_key="k" * 43,
        admin_password="admin-" + "p" * 32,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(settings)), base_url="https://test"
    ) as client:
        assert (await client.get("/admin")).status_code == 200
        assert (await client.get("/admin/api/keys")).status_code == 401
        assert (
            await client.post("/admin/api/login", json={"password": "wrong"})
        ).status_code == 401
        login = await client.post("/admin/api/login", json={"password": settings.admin_password})
        assert login.status_code == 200, login.text
        headers = {"X-FX-Admin-Request": "1", "Origin": "https://test"}
        created = await client.post("/admin/api/keys", headers=headers, json={"name": "team"})
        assert created.status_code == 201, created.text
        key = created.json()["api_key"]
        listing = await client.get("/admin/api/keys")
        assert key not in listing.text and settings.api_key not in listing.text
        assert (
            await client.get("/v1/status", headers={"Authorization": "Bearer " + key})
        ).status_code == 200
        revoke = await client.delete("/admin/api/keys/" + created.json()["id"], headers=headers)
        assert revoke.status_code == 200
        assert (
            await client.get("/v1/status", headers={"Authorization": "Bearer " + key})
        ).status_code == 401
        assert (await client.post("/admin/api/keys", json={"name": "csrf"})).status_code == 403
