"""Project membership survives credential rotation and is checked before replay."""

import uuid

import httpx
import pytest
from fx_data_service.app import ServiceConfig, create_app


@pytest.fixture
def settings(tmp_path):
    return ServiceConfig(
        data_root=tmp_path / "data",
        database_root=tmp_path / "db",
        api_key="k" * 43,
        admin_password="admin-password",
    )


async def test_project_membership_rotation_and_cached_access(settings):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(settings)), base_url="https://test"
    ) as client:
        admin = {"X-FX-Admin-Request": "1", "Origin": "https://test"}
        assert (await client.get("/admin/api/projects")).status_code == 401
        await client.post("/admin/api/login", json={"password": settings.admin_password})
        for project in ("项目 A", "项目 B"):
            assert (
                await client.post("/admin/api/projects", headers=admin, json={"id": project})
            ).status_code == 201
        created = await client.post(
            "/admin/api/keys",
            headers=admin,
            json={"name": "team", "project_ids": ["项目 A", "项目 B"]},
        )
        assert created.status_code == 201
        key = created.json()
        headers = {"Authorization": "Bearer " + key["api_key"]}
        request = {
            "request_id": uuid.uuid4().hex,
            "operation": "tool.fx_save_checkpoint",
            "arguments": {
                "project_id": "项目 A",
                "shot_id": "s1",
                "summary": "保留数据",
                "scene_path": "scene.hip",
                "next_steps": ["继续"],
                "expected_revision": 0,
            },
        }
        saved = await client.post("/v1/operations", headers=headers, json=request)
        assert saved.status_code == 200, saved.text
        assert (
            await client.patch(
                "/admin/api/keys/" + key["id"], headers=admin, json={"project_ids": ["项目 B"]}
            )
        ).status_code == 200
        assert (
            await client.post("/v1/operations", headers=headers, json=request)
        ).status_code == 403
        assert (
            await client.delete("/admin/api/keys/" + key["id"], headers=admin)
        ).status_code == 200
        assert (await client.get("/v1/status", headers=headers)).status_code == 401
        projects = (await client.get("/admin/api/projects")).json()["projects"]
        assert {p["id"] for p in projects} == {"项目 A", "项目 B"}
        replacement = (
            await client.post(
                "/admin/api/keys",
                headers=admin,
                json={"name": "replacement", "project_ids": ["项目 A"]},
            )
        ).json()
        read = await client.post(
            "/v1/operations",
            headers={"Authorization": "Bearer " + replacement["api_key"]},
            json={
                "request_id": uuid.uuid4().hex,
                "operation": "tool.fx_get_checkpoint",
                "arguments": {"project_id": "项目 A", "shot_id": "s1"},
            },
        )
        assert read.status_code == 200, read.text
        assert "保留数据" in read.text


async def test_project_validation_legacy_discovery_and_empty_membership(settings):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(settings)), base_url="https://test"
    ) as client:
        admin = {"X-FX-Admin-Request": "1", "Origin": "https://test"}
        await client.post("/admin/api/login", json={"password": settings.admin_password})
        legacy_headers = {"Authorization": "Bearer " + settings.api_key}
        assert (
            await client.post(
                "/v1/operations",
                headers=legacy_headers,
                json={
                    "request_id": uuid.uuid4().hex,
                    "operation": "tool.fx_save_checkpoint",
                    "arguments": {
                        "project_id": "existing",
                        "shot_id": "s1",
                        "summary": "existing",
                        "scene_path": "a.hip",
                        "next_steps": ["next"],
                        "expected_revision": 0,
                    },
                },
            )
        ).status_code == 200
        assert {p["id"] for p in (await client.get("/admin/api/projects")).json()["projects"]} == {
            "existing"
        }
        for projects in (["missing"], "existing", [123], None):
            response = await client.post(
                "/admin/api/keys", headers=admin, json={"name": "bad", "project_ids": projects}
            )
            assert response.status_code == 422
        assert (await client.post("/admin/api/projects", json={"id": "csrf"})).status_code == 403
        assert (
            await client.post("/admin/api/projects", headers=admin, json={"id": "existing"})
        ).status_code == 409
        created = (
            await client.post("/admin/api/keys", headers=admin, json={"name": "empty"})
        ).json()
        response = await client.post(
            "/v1/operations",
            headers={"Authorization": "Bearer " + created["api_key"]},
            json={
                "request_id": uuid.uuid4().hex,
                "operation": "tool.fx_get_checkpoint",
                "arguments": {"project_id": "existing", "shot_id": "s1"},
            },
        )
        assert response.status_code == 403


async def test_task_access_nested_projects_and_restart(settings):
    app = create_app(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test"
    ) as client:
        admin = {"X-FX-Admin-Request": "1", "Origin": "https://test"}
        await client.post("/admin/api/login", json={"password": settings.admin_password})
        for project in ("a", "b"):
            await client.post("/admin/api/projects", headers=admin, json={"id": project})
        key = (
            await client.post(
                "/admin/api/keys", headers=admin, json={"name": "team", "project_ids": ["a", "b"]}
            )
        ).json()
        headers = {"Authorization": "Bearer " + key["api_key"]}

        async def operation(name, arguments, task_id=""):
            return await client.post(
                "/v1/operations",
                headers=headers,
                json={
                    "request_id": uuid.uuid4().hex,
                    "operation": name,
                    "arguments": arguments,
                    "task_id": task_id,
                },
            )

        task = await operation(
            "task.begin",
            {
                "spec": {
                    "project_id": "a",
                    "shot_id": "s1",
                    "software": "houdini",
                    "scene_path": str(settings.data_root / "scene.hip"),
                    "endpoint": "127.0.0.1:18811",
                    "goal": "test",
                    "acceptance": ["test"],
                    "budget": "test",
                    "modules": ["sop"],
                }
            },
        )
        assert task.status_code == 200, task.text
        task_id = task.json()["result"]["task_id"]
        await client.patch(
            "/admin/api/keys/" + key["id"], headers=admin, json={"project_ids": ["b"]}
        )
        assert (await operation("task.get", {"task_id": task_id})).status_code == 403
        assert (await operation("tool.fx_get_module_catalog", {}, task_id)).status_code == 403
        assert (
            await operation(
                "tool.fx_record_user_rule", {"rule": {"scope": "project", "project_id": " a "}}
            )
        ).status_code == 403
        assert (
            await client.patch("/admin/api/keys/" + key["id"], json={"project_ids": ["a"]})
        ).status_code == 403
        assert (
            await client.patch(
                "/admin/api/keys/missing", headers=admin, json={"project_ids": ["a"]}
            )
        ).status_code == 404
        listing = (await client.get("/admin/api/keys")).json()["keys"]
        assert next(k for k in listing if k["id"] == "environment")["project_ids"] is None

    # Startup must not reset edited memberships or remove independently stored projects.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(settings)), base_url="https://test"
    ) as client:
        assert (await client.get("/v1/status", headers=headers)).json()["project_ids"] == ["b"]
        await client.post("/admin/api/login", json={"password": settings.admin_password})
        assert {p["id"] for p in (await client.get("/admin/api/projects")).json()["projects"]} == {
            "a",
            "b",
        }
