"""Separate administrator login and one-time API key issuance."""

import hashlib
import hmac
import json
import secrets
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

ADMIN_SESSION_SECONDS = 8 * 60 * 60


@contextmanager
def open_database(database):
    if callable(database):
        with database() as db:
            yield db
    else:
        with closing(sqlite3.connect(database)) as db, db:
            yield db


def initialize_keys(database, bootstrap):
    with open_database(database) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS api_keys (id TEXT PRIMARY KEY,name TEXT,token_hash TEXT UNIQUE,enabled INTEGER,created REAL)"
        )
        db.execute("CREATE TABLE IF NOT EXISTS data_projects (id TEXT PRIMARY KEY,created REAL)")
        db.execute(
            "CREATE TABLE IF NOT EXISTS data_project_state (name TEXT PRIMARY KEY,value TEXT)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS key_project_access (key_id TEXT PRIMARY KEY,project_ids TEXT NOT NULL)"
        )
        if bootstrap:
            digest = hashlib.sha256(bootstrap.encode()).hexdigest()
            row = db.execute("SELECT token_hash FROM api_keys WHERE id='environment'").fetchone()
            if row is None:
                db.execute(
                    "INSERT INTO api_keys VALUES ('environment','环境变量主 Key',?,1,?)",
                    (digest, time.time()),
                )
            elif row[0] != digest:
                db.execute(
                    "UPDATE api_keys SET token_hash=?,enabled=1,created=? WHERE id='environment'",
                    (digest, time.time()),
                )
        # Earlier builds confused production labels with complete data spaces.
        # Keep those labels/data intact; migrate credentials exactly once.
        if not db.execute("SELECT 1 FROM data_project_state WHERE name='dataset-v1'").fetchone():
            db.execute("INSERT OR IGNORE INTO data_projects VALUES ('default',?)", (time.time(),))
            for row in db.execute("SELECT id FROM api_keys").fetchall():
                db.execute(
                    "INSERT INTO key_project_access VALUES (?,?) ON CONFLICT(key_id) DO UPDATE SET project_ids=excluded.project_ids",
                    (row[0], json.dumps(["default"])),
                )
            db.execute("INSERT INTO data_project_state VALUES ('dataset-v1','done')")
        # A newly provisioned bootstrap credential starts with the legacy data.
        db.execute(
            "INSERT OR IGNORE INTO key_project_access (key_id,project_ids) SELECT id,? FROM api_keys WHERE id='environment'",
            (json.dumps(["default"]),),
        )


def matches_key(database, supplied):
    digest = hashlib.sha256(supplied.encode()).hexdigest()
    with open_database(database) as db:
        return (
            db.execute(
                "SELECT 1 FROM api_keys WHERE token_hash=? AND enabled=1", (digest,)
            ).fetchone()
            is not None
        )


def key_projects(database, supplied):
    """Credentials grant complete data spaces, never production labels."""
    digest = hashlib.sha256(supplied.encode()).hexdigest()
    with open_database(database) as db:
        row = db.execute(
            "SELECT a.project_ids FROM api_keys k LEFT JOIN key_project_access a ON a.key_id=k.id WHERE k.token_hash=? AND k.enabled=1",
            (digest,),
        ).fetchone()
    return json.loads(row[0]) if row and row[0] is not None else ["default"]


class AdminConsole:
    def __init__(self, database, password):
        self.database, self.password = database, password
        self.attempts = {}

    def access_error(self, request):
        if not self.authorized(request):
            return JSONResponse({"error": "请先登录管理员"}, status_code=401)
        if request.method != "GET":
            origin = urlsplit(request.headers.get("origin", ""))
            if request.headers.get(
                "x-fx-admin-request"
            ) != "1" or origin.netloc != request.headers.get("host"):
                return JSONResponse({"error": "管理请求来源无效"}, status_code=403)
        return None

    async def projects(self, request):
        error = self.access_error(request)
        if error is not None:
            return error
        with open_database(self.database) as db:
            if request.method == "GET":
                return JSONResponse(
                    {
                        "projects": [
                            {
                                "id": row[0],
                                "name": "默认项目（现有全部数据）"
                                if row[0] == "default"
                                else row[0],
                            }
                            for row in db.execute("SELECT id FROM data_projects ORDER BY id")
                        ]
                    },
                    headers={"Cache-Control": "no-store"},
                )
            from fx_agent.store import scope_id

            try:
                project = scope_id.validate_python((await self.body(request)).get("id"))
            except ValueError:
                return JSONResponse({"error": "请输入有效的项目 ID（1–128 字符）"}, status_code=422)
            if db.execute("SELECT 1 FROM data_projects WHERE id=?", (project,)).fetchone():
                return JSONResponse({"error": "项目已存在"}, status_code=409)
            db.execute("INSERT INTO data_projects VALUES (?,?)", (project, time.time()))
        return JSONResponse({"id": project}, status_code=201)

    def authorized(self, request):
        cookie = request.cookies.get("fx_admin", "")
        if not self.password or len(cookie) > 256:
            return False
        try:
            payload, signature = cookie.rsplit(".", 1)
            timestamp = float(payload.split(":", 1)[0])
            expected = hmac.new(
                self.password.encode(), payload.encode(), hashlib.sha256
            ).hexdigest()
            return 0 <= time.time() - timestamp < ADMIN_SESSION_SECONDS and hmac.compare_digest(
                signature, expected
            )
        except (ValueError, TypeError):
            return False

    async def body(self, request):
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > 8192:
                raise ValueError("Body too large")
        value = json.loads(content)
        if not isinstance(value, dict):
            raise ValueError("Object required")
        return value

    async def page(self, request):
        if not self.password:
            return JSONResponse({"error": "Web administration is disabled"}, status_code=404)
        return HTMLResponse(
            Path(__file__).with_name("admin.html").read_text(encoding="utf-8"),
            headers={
                "Cache-Control": "no-store",
                "X-Frame-Options": "DENY",
                "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'",
            },
        )

    async def login(self, request):
        if not self.password:
            return JSONResponse({"error": "Administration disabled"}, status_code=404)
        host = request.client.host if request.client else "unknown"
        start, count = self.attempts.get(host, (time.monotonic(), 0))
        if time.monotonic() - start > 60:
            start, count = time.monotonic(), 0
        self.attempts[host] = (start, count + 1)
        if count >= 10:
            return JSONResponse({"error": "登录尝试过多，请一分钟后重试"}, status_code=429)
        try:
            supplied = (await self.body(request)).get("password", "")
            valid = isinstance(supplied, str) and hmac.compare_digest(
                supplied.encode(), self.password.encode()
            )
        except ValueError:
            valid = False
        if not valid:
            return JSONResponse({"error": "管理员密码错误"}, status_code=401)
        payload = f"{time.time()}:{secrets.token_hex(16)}"
        signature = hmac.new(self.password.encode(), payload.encode(), hashlib.sha256).hexdigest()
        response = JSONResponse(
            {"ok": True, "expires_in": ADMIN_SESSION_SECONDS}, headers={"Cache-Control": "no-store"}
        )
        response.set_cookie(
            "fx_admin",
            payload + "." + signature,
            max_age=ADMIN_SESSION_SECONDS,
            httponly=True,
            secure=True,
            samesite="strict",
            path="/admin",
        )
        return response

    async def logout(self, request):
        origin = urlsplit(request.headers.get("origin", ""))
        if request.headers.get("x-fx-admin-request") != "1" or origin.netloc != request.headers.get(
            "host"
        ):
            return JSONResponse({"error": "管理请求来源无效"}, status_code=403)
        response = JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})
        response.delete_cookie(
            "fx_admin", path="/admin", secure=True, httponly=True, samesite="strict"
        )
        return response

    async def keys(self, request):
        error = self.access_error(request)
        if error is not None:
            return error
        with open_database(self.database) as db:
            if request.method == "GET":
                result = [
                    {
                        "id": row[0],
                        "name": row[1],
                        "enabled": bool(row[2]),
                        "created": row[3],
                        "project_ids": json.loads(row[4]) if row[4] is not None else ["default"],
                    }
                    for row in db.execute(
                        "SELECT k.id,k.name,k.enabled,k.created,a.project_ids FROM api_keys k LEFT JOIN key_project_access a ON a.key_id=k.id ORDER BY k.created DESC"
                    )
                ]
                return JSONResponse({"keys": result}, headers={"Cache-Control": "no-store"})
            if request.method == "DELETE":
                changed = db.execute(
                    "UPDATE api_keys SET enabled=0 WHERE id=?", (request.path_params["key_id"],)
                ).rowcount
                return JSONResponse({"ok": bool(changed)})
            try:
                body = await self.body(request)
                projects = body.get("project_ids", ["default"])
                if not isinstance(projects, list) or len(projects) > 100:
                    raise ValueError("项目列表无效")
                from fx_agent.store import scope_id

                projects = sorted({scope_id.validate_python(value) for value in projects})
                for project in projects:
                    if not db.execute(
                        "SELECT 1 FROM data_projects WHERE id=?", (project,)
                    ).fetchone():
                        raise ValueError("项目不存在")
            except ValueError:
                return JSONResponse({"error": "请选择已存在的项目（最多 100 个）"}, status_code=422)
            if request.method == "PATCH":
                key_id = request.path_params["key_id"]
                if "project_ids" not in body:
                    return JSONResponse({"error": "请提供 project_ids"}, status_code=422)
                if not db.execute("SELECT 1 FROM api_keys WHERE id=?", (key_id,)).fetchone():
                    return JSONResponse({"error": "Key 不存在"}, status_code=404)
                db.execute(
                    "INSERT INTO key_project_access VALUES (?,?) ON CONFLICT(key_id) DO UPDATE SET project_ids=excluded.project_ids",
                    (key_id, json.dumps(projects)),
                )
                return JSONResponse({"id": key_id, "project_ids": projects})
            name = body.get("name", "")
            name = name.strip() if isinstance(name, str) else ""
            if not 1 <= len(name) <= 128:
                return JSONResponse({"error": "请输入 1–128 字符的名称"}, status_code=422)
            key, key_id = secrets.token_urlsafe(32), uuid.uuid4().hex
            db.execute(
                "INSERT INTO api_keys VALUES (?,?,?,1,?)",
                (key_id, name, hashlib.sha256(key.encode()).hexdigest(), time.time()),
            )
            db.execute(
                "INSERT INTO key_project_access VALUES (?,?)", (key_id, json.dumps(projects))
            )
        return JSONResponse(
            {"id": key_id, "name": name, "api_key": key, "project_ids": projects},
            status_code=201,
            headers={"Cache-Control": "no-store"},
        )

    def routes(self):
        return [
            Route("/admin", self.page),
            Route("/admin/", self.page),
            Route("/admin/api/login", self.login, methods=["POST"]),
            Route("/admin/api/logout", self.logout, methods=["POST"]),
            Route("/admin/api/keys", self.keys, methods=["GET", "POST"]),
            Route("/admin/api/keys/{key_id}", self.keys, methods=["DELETE", "PATCH"]),
            Route("/admin/api/projects", self.projects, methods=["GET", "POST"]),
        ]
