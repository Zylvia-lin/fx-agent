"""A project owns a complete dataset; production project_id is only a label."""

import json
import sqlite3
import uuid
from pathlib import Path
from urllib.parse import quote

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


def auth(key, space=None):
    headers = {"Authorization": "Bearer " + key}
    if space is not None:
        headers["X-FX-Data-Project"] = quote(space, safe="")
    return headers


async def login(client, settings):
    response = await client.post("/admin/api/login", json={"password": settings.admin_password})
    assert response.status_code == 200
    return {"X-FX-Admin-Request": "1", "Origin": "https://test"}


async def op(client, headers, name, arguments, request_id=None):
    return await client.post(
        "/v1/operations",
        headers=headers,
        json={
            "request_id": request_id or uuid.uuid4().hex,
            "operation": "tool." + name,
            "arguments": arguments,
        },
    )


def checkpoint(label, text):
    return dict(
        project_id=label,
        shot_id="s1",
        summary=text,
        scene_path="scene.hip",
        next_steps=["next"],
        expected_revision=0,
    )


async def test_existing_entire_dataset_becomes_one_default_project(settings):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(settings)), base_url="https://test"
    ) as client:
        headers = auth(settings.api_key)
        for label in ("Film-A", "Film-B"):
            assert (
                await op(client, headers, "fx_save_checkpoint", checkpoint(label, label))
            ).status_code == 200
        admin = await login(client, settings)
        projects = (await client.get("/admin/api/projects")).json()["projects"]
        assert [p["id"] for p in projects] == ["default"]
        assert (await client.get("/v1/status", headers=headers)).json()["data_project"] == "default"
        listing = (await client.get("/admin/api/keys")).json()["keys"]
        assert listing[0]["project_ids"] == ["default"]
        assert (
            await client.post("/admin/api/projects", headers=admin, json={"id": "Film-C"})
        ).status_code == 201
        assert (
            await client.get("/v1/status", headers=auth(settings.api_key, "Film-C"))
        ).status_code == 403


async def test_spaces_isolate_global_knowledge_labels_files_and_cached_responses(settings):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(settings)), base_url="https://test"
    ) as client:
        admin = await login(client, settings)
        await client.post("/admin/api/projects", headers=admin, json={"id": "第二个数据空间"})
        key = (
            await client.post(
                "/admin/api/keys",
                headers=admin,
                json={"name": "team", "project_ids": ["default", "第二个数据空间"]},
            )
        ).json()
        assert (await client.get("/v1/status", headers=auth(key["api_key"]))).status_code == 422
        a, b = auth(key["api_key"], "default"), auth(key["api_key"], "第二个数据空间")
        rid = uuid.uuid4().hex
        for headers, text in ((a, "first"), (b, "second")):
            written = await op(
                client,
                headers,
                "fx_save_checkpoint",
                checkpoint("same-production-label", text),
                rid,
            )
            assert written.status_code == 200, written.text
            rule = dict(
                rule_key="shared-rule",
                title=text,
                statement=text,
                user_quote="test",
                source_ref="test",
                expected_revision=0,
            )
            written = await op(client, headers, "fx_record_user_rule", {"rule": rule})
            assert written.status_code == 200, written.text
        for headers, text in ((a, "first"), (b, "second")):
            read = await op(client, headers, "fx_get_user_rule", {"rule_key": "shared-rule"})
            assert read.json()["result"]["statement"] == text
            read = await op(
                client,
                headers,
                "fx_get_checkpoint",
                {"project_id": "same-production-label", "shot_id": "s1"},
            )
            assert read.json()["result"]["checkpoint"]["summary"] == text
        # Files are contained by the selected dataset, even if a path is guessed.
        settings.data_root.joinpath("default-only.txt").write_text("private")
        assert (await client.get("/v1/files/default-only.txt", headers=a)).status_code == 200
        assert (await client.get("/v1/files/default-only.txt", headers=b)).status_code == 404
        saved = await client.post("/v1/files/register", headers=b, json={"path": "T:/scene.hip"})
        assert saved.status_code == 200
        space_root = Path(saved.json()["path"]).parents[3]
        space_root.joinpath("default-only.txt").write_text("other space")
        assert (await client.get("/v1/files/default-only.txt", headers=b)).text == "other space"
        assert (await client.get("/v1/files/default-only.txt", headers=a)).text == "private"
        denied = await op(client, a, "fx_plan_template_import", {"source": str(space_root)})
        assert denied.status_code == 403
        relative = str(saved.json()["path"]).replace("\\", "/").split("/data/", 1)[-1]
        assert (await client.get("/v1/files/" + relative, headers=a)).status_code != 200
        # Removing a DATA space denies replay regardless of the internal label.
        await client.patch(
            "/admin/api/keys/" + key["id"], headers=admin, json={"project_ids": ["第二个数据空间"]}
        )
        assert (
            await op(
                client, a, "fx_save_checkpoint", checkpoint("same-production-label", "first"), rid
            )
        ).status_code == 403
        assert (
            await op(client, b, "fx_get_user_rule", {"rule_key": "shared-rule"})
        ).status_code == 200
        await client.delete("/admin/api/keys/" + key["id"], headers=admin)
        assert (await client.get("/v1/status", headers=b)).status_code == 401
        replacement = (
            await client.post(
                "/admin/api/keys",
                headers=admin,
                json={"name": "new", "project_ids": ["第二个数据空间"]},
            )
        ).json()
        read = await op(
            client, auth(replacement["api_key"]), "fx_get_user_rule", {"rule_key": "shared-rule"}
        )
        assert read.json()["result"]["statement"] == "second"
        assert {p["id"] for p in (await client.get("/admin/api/projects")).json()["projects"]} == {
            "default",
            "第二个数据空间",
        }
        for invalid in (["missing"], "default", [123], None):
            assert (
                await client.post(
                    "/admin/api/keys", headers=admin, json={"name": "bad", "project_ids": invalid}
                )
            ).status_code == 422
        empty = (
            await client.post(
                "/admin/api/keys", headers=admin, json={"name": "empty", "project_ids": []}
            )
        ).json()
        assert (await client.get("/v1/status", headers=auth(empty["api_key"]))).status_code == 403
        assert (await client.post("/admin/api/projects", json={"id": "csrf"})).status_code == 403


async def test_wrong_old_engineering_memberships_migrate_once_and_restart_preserves_spaces(
    settings,
):
    settings.database_root.mkdir(parents=True)
    with sqlite3.connect(settings.database_root / "service.sqlite3") as db:
        db.execute(
            "CREATE TABLE key_project_access (key_id TEXT PRIMARY KEY,project_ids TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO key_project_access VALUES (?,?)",
            ("environment", json.dumps(["Film-A", "Film-B"])),
        )
        db.execute("CREATE TABLE projects (id TEXT PRIMARY KEY,created REAL)")
        db.execute("INSERT INTO projects VALUES ('Film-A',1)")
    app = create_app(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test"
    ) as client:
        admin = await login(client, settings)
        assert [p["id"] for p in (await client.get("/admin/api/projects")).json()["projects"]] == [
            "default"
        ]
        await client.post("/admin/api/projects", headers=admin, json={"id": "new-space"})
        await client.patch(
            "/admin/api/keys/environment", headers=admin, json={"project_ids": ["new-space"]}
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(settings)), base_url="https://test"
    ) as client:
        status = await client.get("/v1/status", headers=auth(settings.api_key))
        assert status.json()["data_project"] == "new-space"
        assert status.json()["project_ids"] == ["new-space"]


def test_control_device_keys_cannot_overlap_external_device_ids():
    from fx_data_service.projects import DataProjects

    assert DataProjects.device_key("default", "workstation") == "workstation"
    assert "\x00" in DataProjects.device_key("other", "workstation")
    assert DataProjects.device_key("other", "workstation") != DataProjects.device_key(
        "another", "workstation"
    )
