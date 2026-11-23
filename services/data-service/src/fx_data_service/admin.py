"""Separate administrator login and one-time API key issuance."""

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager, suppress
from pathlib import Path
from urllib.parse import urlsplit

from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

ADMIN_SESSION_SECONDS = 8 * 60 * 60
LOG = logging.getLogger(__name__)
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


@contextmanager
def open_database(database):
    if callable(database):
        with database() as db:
            yield db
    else:
        with closing(sqlite3.connect(database, timeout=10)) as db:
            db.execute("PRAGMA busy_timeout=10000")
            with suppress(sqlite3.OperationalError):
                db.execute("PRAGMA journal_mode=WAL")
            with db:
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


def authenticate_key(database, supplied):
    """Single-query key authentication returning (key_id, project_ids) or None."""
    digest = hashlib.sha256(supplied.encode()).hexdigest()
    with open_database(database) as db:
        row = db.execute(
            "SELECT k.id,a.project_ids FROM api_keys k LEFT JOIN key_project_access a ON a.key_id=k.id WHERE k.token_hash=? AND k.enabled=1",
            (digest,),
        ).fetchone()
    if row is None:
        return None
    return row[0], (json.loads(row[1]) if row[1] is not None else ["default"])


def matches_key(database, supplied):
    return authenticate_key(database, supplied) is not None


def key_projects(database, supplied):
    """Credentials grant complete data spaces, never production labels."""
    info = authenticate_key(database, supplied)
    return info[1] if info is not None else ["default"]


class AdminConsole:
    def __init__(self, database, password):
        self.database, self.password = database, password
        self.attempts = {}
        self.revoked_sessions: dict[str, float] = {}
        self._signing_key = (
            hashlib.sha256(b"fx-admin-session-v2:" + password.encode()).digest()
            if password
            else b""
        )

    @staticmethod
    def _client_ip(request) -> str:
        peer = request.client.host if request.client else "unknown"
        if peer in {"127.0.0.1", "::1"}:
            real_ip = request.headers.get("x-real-ip", "").strip()
            if real_ip:
                return real_ip[:64]
            forwarded = request.headers.get("x-forwarded-for", "").strip()
            if forwarded:
                return forwarded.split(",", 1)[0].strip()[:64]
        return peer

    def _evict_expired(self, now_mono: float, now_wall: float) -> None:
        if len(self.attempts) > 1000:
            self.attempts = {
                host: state for host, state in self.attempts.items() if now_mono - state[0] <= 60
            }
        if self.revoked_sessions:
            self.revoked_sessions = {
                sid: exp for sid, exp in self.revoked_sessions.items() if exp > now_wall
            }

    def access_error(self, request):
        if not self.authorized(request):
            return JSONResponse({"error": "请先登录管理员"}, status_code=401, headers=_SECURITY_HEADERS)
        if request.method != "GET":
            origin = urlsplit(request.headers.get("origin", ""))
            if request.headers.get(
                "x-fx-admin-request"
            ) != "1" or origin.netloc != request.headers.get("host"):
                return JSONResponse({"error": "管理请求来源无效"}, status_code=403, headers=_SECURITY_HEADERS)
        return None

    async def projects(self, request):
        error = self.access_error(request)
        if error is not None:
            return error
        if request.method == "GET":
            def _list():
                with open_database(self.database) as db:
                    return [
                        {
                            "id": row[0],
                            "name": "默认项目（现有全部数据）" if row[0] == "default" else row[0],
                        }
                        for row in db.execute("SELECT id FROM data_projects ORDER BY id")
                    ]

            items = await asyncio.to_thread(_list)
            return JSONResponse({"projects": items}, headers=_SECURITY_HEADERS)
        from fx_agent.store import scope_id

        try:
            project = scope_id.validate_python((await self.body(request)).get("id"))
        except ValueError:
            return JSONResponse(
                {"error": "请输入有效的项目 ID（1–128 字符）"}, status_code=422, headers=_SECURITY_HEADERS
            )

        def _create():
            with open_database(self.database) as db:
                if db.execute("SELECT 1 FROM data_projects WHERE id=?", (project,)).fetchone():
                    return False
                db.execute("INSERT INTO data_projects VALUES (?,?)", (project, time.time()))
                return True

        created = await asyncio.to_thread(_create)
        if not created:
            return JSONResponse({"error": "项目已存在"}, status_code=409, headers=_SECURITY_HEADERS)
        LOG.info("admin_audit action=create_project ip=%s project=%s", self._client_ip(request), project)
        return JSONResponse({"id": project}, status_code=201, headers=_SECURITY_HEADERS)

    def _parse_session(self, cookie: str) -> tuple[str, float] | None:
        if not self.password or not cookie or len(cookie) > 256:
            return None
        try:
            payload, signature = cookie.rsplit(".", 1)
            ts_text, sid = payload.split(":", 1)
            timestamp = float(ts_text)
            expected = hmac.new(self._signing_key, payload.encode(), hashlib.sha256).hexdigest()
            if 0 <= time.time() - timestamp < ADMIN_SESSION_SECONDS and hmac.compare_digest(
                signature, expected
            ):
                return sid, timestamp + ADMIN_SESSION_SECONDS
        except (ValueError, TypeError):
            return None
        return None

    def authorized(self, request):
        parsed = self._parse_session(request.cookies.get("fx_admin", ""))
        if parsed is None:
            return False
        sid, _ = parsed
        return sid not in self.revoked_sessions

    async def body(self, request):
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise ValueError("Content-Type must be application/json")
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
            return JSONResponse({"error": "Web administration is disabled"}, status_code=404, headers=_SECURITY_HEADERS)
        nonce = secrets.token_urlsafe(16)
        html = Path(__file__).with_name("admin.html").read_text(encoding="utf-8")
        html = html.replace("<style>", f'<style nonce="{nonce}">', 1).replace(
            "<script>", f'<script nonce="{nonce}">', 1
        )
        return HTMLResponse(
            html,
            headers={
                **_SECURITY_HEADERS,
                "X-Frame-Options": "DENY",
                "Content-Security-Policy": (
                    f"default-src 'self'; script-src 'self' 'nonce-{nonce}'; "
                    f"style-src 'self' 'nonce-{nonce}'; frame-ancestors 'none'"
                ),
            },
        )

    async def login(self, request):
        if not self.password:
            return JSONResponse({"error": "Administration disabled"}, status_code=404, headers=_SECURITY_HEADERS)
        raw_origin = request.headers.get("origin", "")
        if raw_origin and urlsplit(raw_origin).netloc != request.headers.get("host"):
            return JSONResponse({"error": "管理请求来源无效"}, status_code=403, headers=_SECURITY_HEADERS)
        now_mono, now_wall = time.monotonic(), time.time()
        self._evict_expired(now_mono, now_wall)
        host = self._client_ip(request)
        start, count = self.attempts.get(host, (now_mono, 0))
        if now_mono - start > 60:
            start, count = now_mono, 0
        self.attempts[host] = (start, count + 1)
        if count >= 10:
            LOG.warning("admin_audit action=login_throttled ip=%s", host)
            return JSONResponse(
                {"error": "登录尝试过多，请一分钟后重试"}, status_code=429, headers=_SECURITY_HEADERS
            )
        try:
            supplied = (await self.body(request)).get("password", "")
            valid = isinstance(supplied, str) and hmac.compare_digest(
                supplied.encode(), self.password.encode()
            )
        except ValueError:
            valid = False
        if not valid:
            LOG.warning("admin_audit action=login_failed ip=%s", host)
            return JSONResponse({"error": "管理员密码错误"}, status_code=401, headers=_SECURITY_HEADERS)
        sid = secrets.token_hex(16)
        payload = f"{now_wall}:{sid}"
        signature = hmac.new(self._signing_key, payload.encode(), hashlib.sha256).hexdigest()
        LOG.info("admin_audit action=login_ok ip=%s", host)
        response = JSONResponse(
            {"ok": True, "expires_in": ADMIN_SESSION_SECONDS}, headers=_SECURITY_HEADERS
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
            return JSONResponse({"error": "管理请求来源无效"}, status_code=403, headers=_SECURITY_HEADERS)
        parsed = self._parse_session(request.cookies.get("fx_admin", ""))
        if parsed is not None:
            self.revoked_sessions[parsed[0]] = parsed[1]
        LOG.info("admin_audit action=logout ip=%s", self._client_ip(request))
        response = JSONResponse({"ok": True}, headers=_SECURITY_HEADERS)
        response.delete_cookie(
            "fx_admin", path="/admin", secure=True, httponly=True, samesite="strict"
        )
        return response

    async def keys(self, request):
        error = self.access_error(request)
        if error is not None:
            return error
        if request.method == "GET":
            def _list_keys():
                with open_database(self.database) as db:
                    return [
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

            result = await asyncio.to_thread(_list_keys)
            return JSONResponse({"keys": result}, headers=_SECURITY_HEADERS)
        if request.method == "DELETE":
            key_id = request.path_params["key_id"]

            def _revoke():
                with open_database(self.database) as db:
                    return db.execute("UPDATE api_keys SET enabled=0 WHERE id=?", (key_id,)).rowcount

            changed = await asyncio.to_thread(_revoke)
            LOG.info("admin_audit action=revoke_key ip=%s key_id=%s changed=%s", self._client_ip(request), key_id, bool(changed))
            return JSONResponse({"ok": bool(changed)}, headers=_SECURITY_HEADERS)
        try:
            body = await self.body(request)
            projects = body.get("project_ids", ["default"])
            if not isinstance(projects, list) or len(projects) > 100:
                raise ValueError("项目列表无效")
            from fx_agent.store import scope_id

            projects = sorted({scope_id.validate_python(value) for value in projects})
        except ValueError:
            return JSONResponse(
                {"error": "请选择已存在的项目（最多 100 个）"}, status_code=422, headers=_SECURITY_HEADERS
            )

        def _mutate():
            with open_database(self.database) as db:
                for project in projects:
                    if not db.execute("SELECT 1 FROM data_projects WHERE id=?", (project,)).fetchone():
                        return ("bad_project", None)
                if request.method == "PATCH":
                    key_id = request.path_params["key_id"]
                    if "project_ids" not in body:
                        return ("missing_projects", None)
                    if not db.execute("SELECT 1 FROM api_keys WHERE id=?", (key_id,)).fetchone():
                        return ("not_found", None)
                    db.execute(
                        "INSERT INTO key_project_access VALUES (?,?) ON CONFLICT(key_id) DO UPDATE SET project_ids=excluded.project_ids",
                        (key_id, json.dumps(projects)),
                    )
                    return ("patched", {"id": key_id, "project_ids": projects})
                name = body.get("name", "")
                name = name.strip() if isinstance(name, str) else ""
                if not 1 <= len(name) <= 128:
                    return ("bad_name", None)
                key, key_id = secrets.token_urlsafe(32), uuid.uuid4().hex
                db.execute(
                    "INSERT INTO api_keys VALUES (?,?,?,1,?)",
                    (key_id, name, hashlib.sha256(key.encode()).hexdigest(), time.time()),
                )
                db.execute(
                    "INSERT INTO key_project_access VALUES (?,?)", (key_id, json.dumps(projects))
                )
                return ("created", {"id": key_id, "name": name, "api_key": key, "project_ids": projects})

        outcome, payload = await asyncio.to_thread(_mutate)
        if outcome == "bad_project":
            return JSONResponse({"error": "请选择已存在的项目（最多 100 个）"}, status_code=422, headers=_SECURITY_HEADERS)
        if outcome == "missing_projects":
            return JSONResponse({"error": "请提供 project_ids"}, status_code=422, headers=_SECURITY_HEADERS)
        if outcome == "not_found":
            return JSONResponse({"error": "Key 不存在"}, status_code=404, headers=_SECURITY_HEADERS)
        if outcome == "bad_name":
            return JSONResponse({"error": "请输入 1–128 字符的名称"}, status_code=422, headers=_SECURITY_HEADERS)
        if outcome == "patched":
            LOG.info("admin_audit action=patch_key ip=%s key_id=%s", self._client_ip(request), payload["id"])
            return JSONResponse(payload, headers=_SECURITY_HEADERS)
        LOG.info("admin_audit action=create_key ip=%s key_id=%s", self._client_ip(request), payload["id"])
        return JSONResponse(payload, status_code=201, headers=_SECURITY_HEADERS)

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
