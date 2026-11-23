"""Single-host SQLite ownership and authenticated shared knowledge operations."""

import argparse
import asyncio
import hashlib
import hmac
import json
import logging
import ntpath
import os
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager, closing, suppress
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, unquote

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from fx_agent import __version__
from fx_agent.data_context import json_value, service_scope
from fx_agent.data_contracts import DATA_TOOLS, TASK_METHODS
from fx_data_service.diagnostics import (
    RequestLoggingMiddleware,
    error_details,
    log_rejection,
    safe_reason,
)

LOG = logging.getLogger(__name__)
MAX_BODY = 2 * 1024 * 1024
BACKUP_INTERVAL_SECONDS = 24 * 60 * 60
REQUEST_RETENTION_SECONDS = 7 * 24 * 60 * 60
TRANSFER_RETENTION_SECONDS = 24 * 60 * 60
MAX_RETAINED_BACKUPS = 14
_BLOCKED_DOWNLOAD_SUFFIXES = frozenset(
    {".sqlite3", ".sqlite3-wal", ".sqlite3-shm", ".sqlite3-journal", ".db", ".pem", ".key", ".env"}
)
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


class DeviceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128)
    token: str = Field(min_length=32, max_length=256, repr=False)
    path_maps: dict[str, str] = Field(default_factory=dict)


class ServiceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data_root: Path
    database_root: Path
    allowed_roots: list[Path] = Field(default_factory=list)
    devices: dict[str, DeviceConfig] = Field(default_factory=dict, max_length=100)
    api_key: str = Field(default="", repr=False)
    admin_password: str = Field(default="", repr=False)
    backup_root: Path | None = None
    path_maps: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_paths(self):
        if not self.data_root.is_absolute() or not self.database_root.is_absolute():
            raise ValueError("Data and database roots must be absolute")
        self.data_root = self.data_root.resolve()
        self.database_root = require_local_directory(self.database_root)
        if self.data_root == self.database_root:
            raise ValueError("Use separate shared artifacts and local database directories")
        if any(not p.is_absolute() for p in self.allowed_roots):
            raise ValueError("Allowed roots must be absolute")
        self.allowed_roots = [p.resolve() for p in self.allowed_roots]
        tokens = [d.token for d in self.devices.values()]
        if not tokens and len(self.api_key) < 32:
            raise ValueError("An API key of at least 32 characters is required")
        if self.admin_password and len(self.admin_password) < 12:
            raise ValueError("Admin password must be at least 12 characters")
        if len(set(tokens)) != len(tokens):
            raise ValueError("Each device must have a unique token")
        for device_id, device in self.devices.items():
            if (
                not device_id
                or len(device_id) > 128
                or not all(c.isalnum() or c in "-_" for c in device_id)
            ):
                raise ValueError("Invalid device ID")
            for source, target in device.path_maps.items():
                import ntpath

                if (
                    not (Path(source).is_absolute() or ntpath.isabs(source))
                    or not Path(target).is_absolute()
                ):
                    raise ValueError("Path mapping prefixes must be absolute")
        return self


def require_local_directory(path: Path) -> Path:
    path = path.resolve()
    if str(path).startswith(("\\\\", "//")):
        raise ValueError("The service database must be on a local disk, not a network share")
    if os.name == "nt":
        import ctypes

        if ctypes.windll.kernel32.GetDriveTypeW(str(path.anchor)) == 4:
            raise ValueError("The service database must not use a mapped network drive")
    return path


def snapshot_database(source: Path, target: Path):
    """Consistent snapshot; exclusively reserve target and never overwrite data."""
    source, target = Path(source).resolve(strict=True), Path(target).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb"):
        pass
    try:
        # SQLite rejects non-local URI authorities. An empty authority with a
        # double-slash path preserves Windows UNC paths without enabling the
        # optional SQLITE_ALLOW_URI_AUTHORITY build flag.
        uri = (
            "file:////" + quote(source.as_posix().lstrip("/"), safe="/:")
            if source.as_posix().startswith("//")
            else source.as_uri()
        )
        with (
            closing(sqlite3.connect(uri + "?mode=ro", uri=True)) as reader,
            closing(sqlite3.connect(target)) as writer,
        ):
            reader.backup(writer)
            if writer.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Database snapshot failed integrity check")
    except BaseException:
        target.unlink(missing_ok=True)
        raise


PATH_FIELDS = frozenset(
    {
        "path",
        "source",
        "destination",
        "media_source",
        "scene_path",
        "previous_scene_path",
        "input_paths",
        "source_path",
        "archive_path",
        "data_root",
        "snapshot_path",
        "resolved_path",
        "sample_paths",
        "manifest_path",
        "export_manifest_path",
        "export_path",
        "file_path",
        "case_path",
        "archive_root",
        "markdown_path",
        "patterns_path",
    }
)
TEXT_FIELDS = frozenset(
    {
        "user_quote",
        "statement",
        "method",
        "reason",
        "applies_when",
        "source_ref",
        "text",
        "parameters",
        "code",
        "script",
        "snippet",
    }
)


def map_value(value, mappings: dict[str, str], field=""):
    """Only translate explicit path fields; preserve knowledge and raw parameters."""
    if isinstance(value, dict):
        return {
            key: item if key in TEXT_FIELDS else map_value(item, mappings, key)
            for key, item in value.items()
        }
    if isinstance(value, list):
        if field == "assets":
            return [[map_value(item[0], mappings, "path"), *item[1:]] for item in value]
        return [map_value(item, mappings, field) for item in value]
    if isinstance(value, str) and field in PATH_FIELDS:
        windows = bool(ntpath.splitdrive(value)[0])
        normalized = ntpath.normpath(value).replace("\\", "/") if windows else value
        for source, target in sorted(mappings.items(), key=lambda item: -len(item[0])):
            prefix = (ntpath.normpath(source).replace("\\", "/")
                      if ntpath.splitdrive(source)[0] else source).rstrip("/")
            compared, match = ((normalized.casefold(), prefix.casefold())
                               if windows else (normalized, prefix))
            if compared == match or compared.startswith(match + "/"):
                return target.replace("\\", "/").rstrip("/") + normalized[len(prefix) :]
    return value


def check_file_access(operation, arguments, settings, forbidden_roots=()):
    paths = []
    name = operation.removeprefix("tool.")
    if name in {"fx_plan_template_import", "fx_learn_templates"}:
        paths.append(arguments.get("source", ""))
    if name == "fx_export_template":
        paths.append(arguments.get("destination", ""))
    if name in {"fx_plan_effect_study", "fx_review_template_effects"} and arguments.get(
        "media_source"
    ):
        paths.append(arguments["media_source"])
    if name in {"fx_archive_case", "fx_learn_templates"}:
        files = arguments.get("files", [])
        if files is not None:
            if not isinstance(files, list) or any(not isinstance(item, dict) for item in files):
                raise ValueError("files must be a list of objects")
            paths.extend(item.get("path", "") for item in files)
    if operation in {"task.begin", "task.update"}:
        spec = arguments.get("spec", {})
        if not isinstance(spec, dict):
            raise ValueError("spec must be an object")
        input_paths = spec.get("input_paths", [])
        if not isinstance(input_paths, list):
            raise ValueError("spec.input_paths must be a list")
        paths.extend([spec.get("scene_path", ""), *input_paths])
    if operation == "task.authorize":
        paths.append(arguments.get("scene_path", ""))
    roots = [settings.data_root.resolve(), *(p.resolve() for p in settings.allowed_roots)]
    for value in paths:
        if not isinstance(value, str):
            raise ValueError("File path must be a string")
        path = Path(value)
        if (
            not path.is_absolute()
            or not any(path.resolve().is_relative_to(root) for root in roots)
            or any(path.resolve().is_relative_to(root.resolve()) for root in forbidden_roots)
        ):
            raise PermissionError(
                "File path is outside the service's allowed shared roots; configure a path mapping"
            )


class Operation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    operation: str = Field(pattern=r"^(tool|task)\.[A-Za-z_][A-Za-z0-9_]{0,94}$", max_length=100)
    arguments: dict = Field(default_factory=dict)
    task_id: str = Field(default="", max_length=128)


def check_knowledge_gate(name, arguments, task_id, root):
    """Delegate HTTP knowledge admission to the single shared TaskGates policy."""
    from fx_agent.task_gates import TaskGates
    from fx_agent.task_gates import check_knowledge_gate as shared_check

    shared_check(TaskGates(root), task_id, tool_name=name, arguments=arguments)


def create_app(settings: ServiceConfig, backend=None):
    from pydantic import validate_call

    from fx_agent.data_api import shared_functions
    from fx_agent.store import Store

    settings.database_root.mkdir(parents=True, exist_ok=True)
    control_path = settings.database_root / "service.sqlite3"
    from fx_data_service.admin import open_database

    def control_connection():
        return backend.connection() if backend else open_database(control_path)

    with control_connection() as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS requests (device_id TEXT, request_id TEXT, operation TEXT, payload_hash TEXT, response TEXT, status INTEGER, created REAL, PRIMARY KEY(device_id,request_id))"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS devices (device_id TEXT PRIMARY KEY, last_seen REAL, version TEXT)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS files (device_id TEXT, original TEXT, stored TEXT, PRIMARY KEY(device_id,original))"
        )
    with service_scope(settings.data_root, settings.database_root, "", backend):
        existing = settings.database_root / "memory.sqlite3"
        if existing.exists() and not backend:
            snapshot_database(
                existing,
                settings.data_root / "backups" / ("before-start-" + uuid.uuid4().hex + ".sqlite3"),
            )
        Store(settings.data_root)
        tools = {
            fn.__name__: validate_call(fn) for fn in shared_functions() if fn.__name__ in DATA_TOOLS
        }
    project_locks: dict[str, asyncio.Lock] = {}

    def project_lock(project_name: str) -> asyncio.Lock:
        return project_locks.setdefault(project_name, asyncio.Lock())

    rate = {}
    backup_state = {"last_backup": None, "backup_error": None}
    from fx_data_service.admin import AdminConsole, authenticate_key, initialize_keys, matches_key

    initialize_keys(control_connection, settings.api_key)

    from fx_data_service.projects import DataProjects

    projects = DataProjects(settings, backend)
    admin = AdminConsole(control_connection, settings.admin_password)

    def _prune_maintenance():
        """Enforce bounded retention on old backups, transfers, and cached request bodies."""
        now = time.time()
        with suppress(Exception), control_connection() as db:
            db.execute("DELETE FROM requests WHERE created < ?", (now - REQUEST_RETENTION_SECONDS,))
        destination = settings.backup_root or settings.data_root / "backups"
        if destination.is_dir():
            backups = sorted(
                (p for p in destination.iterdir() if p.is_file() and not p.name.startswith("before-start-")),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for old_backup in backups[MAX_RETAINED_BACKUPS * 3 :]:
                with suppress(OSError):
                    old_backup.unlink()
        transfers_dir = settings.data_root / "transfers"
        if transfers_dir.is_dir():
            for entry in transfers_dir.iterdir():
                if entry.is_file() and (now - entry.stat().st_mtime) > TRANSFER_RETENTION_SECONDS:
                    with suppress(OSError):
                        entry.unlink()

    def backup():
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
        destination = settings.backup_root or settings.data_root / "backups"
        if backend:
            destination.mkdir(parents=True, exist_ok=True)
            backend.backup(destination / (stamp + "-mysql.json.gz"))
            with control_connection() as db:
                ids = [
                    row[0]
                    for row in db.execute("SELECT id FROM data_projects")
                    if row[0] != "default"
                ]
            for project in ids:
                _, project_backend = projects.get(project)
                project_backend.backup(
                    destination / (stamp + "-" + project_backend.prefix + "mysql.json.gz")
                )
            _prune_maintenance()
            backup_state.update(last_backup=stamp, backup_error=None)
            return
        snapshot_database(
            settings.database_root / "memory.sqlite3", destination / (stamp + "-memory.sqlite3")
        )
        snapshot_database(control_path, destination / (stamp + "-service.sqlite3"))
        with control_connection() as db:
            ids = [
                row[0] for row in db.execute("SELECT id FROM data_projects") if row[0] != "default"
            ]
        for project in ids:
            project_settings, _ = projects.get(project)
            snapshot_database(
                project_settings.database_root / "memory.sqlite3",
                destination / (stamp + "-" + project_settings.database_root.name + ".sqlite3"),
            )
        _prune_maintenance()
        backup_state.update(last_backup=stamp, backup_error=None)

    async def periodic_backup():
        while True:
            await asyncio.sleep(BACKUP_INTERVAL_SECONDS)
            try:
                # Online SQLite .backup() and MySQL REPEATABLE READ dumps do not need
                # to stall live operations across all projects.
                await asyncio.to_thread(backup)
            except Exception:
                backup_state["backup_error"] = "Automatic backup failed; inspect server logs"
                LOG.exception("Automatic data backup failed")

    @asynccontextmanager
    async def lifespan(app):
        from fx_agent.task_gates import endpoint_lock

        with endpoint_lock(settings.database_root, "data-service-owner"):
            await asyncio.to_thread(backup)
            worker = asyncio.create_task(periodic_backup())
            try:
                yield
            finally:
                worker.cancel()
                with suppress(asyncio.CancelledError):
                    await worker

    def _authenticate_sync(request):
        request.state.auth_status = 401
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer "):
            return None
        supplied = header.removeprefix("Bearer ")
        auth_info = authenticate_key(control_connection, supplied)
        if auth_info is not None:
            key_id, project_ids = auth_info
            request.state.auth_key_id = key_id
            request.state.project_ids = project_ids
            device_id = request.headers.get("x-fx-device-id", "shared")
            if (
                not device_id
                or len(device_id) > 128
                or not all(c.isascii() and (c.isalnum() or c in "-_") for c in device_id)
            ):
                return None
        else:
            device_id = next(
                (
                    key
                    for key, device in settings.devices.items()
                    if hmac.compare_digest(supplied.encode(), device.token.encode())
                ),
                None,
            )
            if device_id is None:
                return None
            request.state.auth_key_id = "device:" + device_id
            request.state.project_ids = ["default"]
        allowed = request.state.project_ids
        selected = unquote(request.headers.get("x-fx-data-project", "")) or getattr(
            request.state, "data_project", ""
        )
        if not selected and len(allowed) == 1:
            selected = allowed[0]
        if not selected:
            request.state.auth_status = 422 if allowed else 403
            return None
        if selected not in allowed:
            request.state.auth_status = 403
            return None
        request.state.data_project = selected
        request.state.log_device = device_id
        request.state.project_settings, request.state.project_backend = projects.get(selected)
        request.state.device_key = projects.device_key(selected, device_id)
        return device_id

    async def authenticate(request):
        return await asyncio.to_thread(_authenticate_sync, request)

    def check_rate_limit(request, device_id: str, limit: int = 600) -> bool:
        now = time.monotonic()
        if len(rate) > 2000:
            expired = [k for k, (ts, _) in rate.items() if now - ts >= 60]
            for k in expired:
                rate.pop(k, None)
        key_id = getattr(request.state, "auth_key_id", device_id)
        # Bound both per-(key, device) and per-key aggregate rate so rotating X-FX-Device-ID
        # cannot bypass throttling or leak unbounded memory.
        for bucket, bucket_limit in (((key_id, device_id), limit), (("key", key_id), limit * 4)):
            start, count = rate.get(bucket, (now, 0))
            if now - start >= 60:
                start, count = now, 0
            rate[bucket] = (start, count + 1)
            if count >= bucket_limit:
                return False
        return True

    def failure(message, status, request=None):
        payload = {"error": message}
        if request is not None:
            payload["request_id"] = request.state.request_id
            LOG.warning("request_rejected request_id=%s project=%s device=%s reason=%s",
                        request.state.request_id, getattr(request.state, "data_project", "-"),
                        getattr(request.state, "log_device", "-"), message)
        return JSONResponse(payload, status_code=status, headers=_SECURITY_HEADERS)

    def auth_failure(request):
        code = getattr(request.state, "auth_status", 401)
        return failure(
            "Select an authorized data project using X-FX-Data-Project"
            if code != 401
            else "Invalid API key",
            code,
            request,
        )

    def file_target(request, device_id, original):
        if (
            not isinstance(original, str)
            or len(original) > 4096
            or not original
            or not (Path(original).is_absolute() or ntpath.isabs(original))
        ):
            raise ValueError("An absolute client file path is required")
        basename = ntpath.basename(original.replace("/", "\\"))
        if basename in {"", ".", ".."} or any(c in basename for c in "\x00\r\n"):
            raise ValueError("Invalid file name")
        canonical = (
            ntpath.normcase(ntpath.normpath(original))
            if ntpath.splitdrive(original)[0]
            else os.path.normpath(original)
        )
        if ntpath.splitdrive(original)[0]:
            basename = ntpath.basename(canonical)
        key = hashlib.sha256(canonical.encode()).hexdigest()
        root = request.state.project_settings.data_root
        target = root / "workspaces" / device_id / key / basename
        if not target.resolve().is_relative_to(root):
            raise ValueError("Invalid workspace path")
        return target

    def remember_file(device_id, original, target):
        original = (
            ntpath.normcase(ntpath.normpath(original))
            if ntpath.splitdrive(original)[0]
            else os.path.normpath(original)
        )
        with control_connection() as db:
            db.execute(
                "INSERT INTO files VALUES (?,?,?) ON CONFLICT(device_id,original) DO UPDATE SET stored=excluded.stored",
                (device_id, original, str(target)),
            )

    async def register_file(request):
        device_id = await authenticate(request)
        if not device_id:
            return auth_failure(request)
        if not check_rate_limit(request, device_id, limit=300):
            return failure("File request limit exceeded; retry later", 429, request)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 8192:
                return failure("Path request too large", 413, request)
        try:
            original = json.loads(body)["path"]
            target = file_target(request, device_id, original)
        except (ValueError, KeyError, TypeError):
            return failure("Invalid client path", 422, request)
        async with project_lock(request.state.data_project):
            if not await authenticate(request):
                return auth_failure(request)
            def _commit_register():
                target.unlink(missing_ok=True)
                remember_file(request.state.device_key, original, target)
            await asyncio.to_thread(_commit_register)
        return JSONResponse({"path": str(target)}, headers=_SECURITY_HEADERS)

    async def upload_file(request):
        device_id = await authenticate(request)
        if not device_id:
            return auth_failure(request)
        if not check_rate_limit(request, device_id, limit=300):
            return failure("File upload limit exceeded; retry later", 429, request)
        try:
            original = unquote(request.headers.get("x-fx-client-path", ""))
            target = file_target(request, device_id, original)
            expected = request.headers.get("x-fx-sha256", "")
            mtime = int(request.headers.get("x-fx-mtime-ns", "0"))
            if not re.fullmatch(r"[a-f0-9]{64}", expected) or not 0 <= mtime < 2**63:
                raise ValueError("Invalid file metadata")
        except ValueError:
            return failure("Invalid file metadata", 422, request)
        staging = target.parent / (".upload-" + uuid.uuid4().hex)
        digest, size = hashlib.sha256(), 0
        created_dir = False
        try:
            if not target.parent.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                created_dir = True
            stream = await asyncio.to_thread(staging.open, "xb")
            try:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > 512 * 1024 * 1024:
                        return failure("File exceeds 512 MiB", 413, request)
                    await asyncio.to_thread(stream.write, chunk)
                    digest.update(chunk)
            finally:
                await asyncio.to_thread(stream.close)
            if digest.hexdigest() != expected:
                return failure("File checksum mismatch", 422, request)
            async with project_lock(request.state.data_project):
                if not await authenticate(request):
                    return auth_failure(request)
                def _publish_upload():
                    os.replace(staging, target)
                    os.utime(target, ns=(mtime, mtime))
                    remember_file(request.state.device_key, original, target)
                await asyncio.to_thread(_publish_upload)
            created_dir = False
            LOG.info("file_uploaded request_id=%s project=%s device=%s size=%s sha256=%s file_id=%s",
                     request.state.request_id, request.state.data_project, device_id,
                     size, expected, target.parent.name)
            return JSONResponse(
                {
                    "path": str(target),
                    "sha256": expected,
                    "size": size,
                    "download_path": target.relative_to(request.state.project_settings.data_root).as_posix(),
                },
                headers=_SECURITY_HEADERS,
            )
        finally:
            staging.unlink(missing_ok=True)
            if created_dir:
                with suppress(OSError):
                    target.parent.rmdir()

    async def download_file(request):
        device_id = await authenticate(request)
        if not device_id:
            return auth_failure(request)
        if not check_rate_limit(request, device_id, limit=600):
            return failure("File download limit exceeded; retry later", 429, request)
        from fx_agent.archive import contained

        try:
            path = contained(request.state.project_settings.data_root, request.path_params["path"])
        except ValueError:
            return failure("Invalid shared file path", 403, request)
        rel_parts = path.relative_to(request.state.project_settings.data_root).parts
        lower_name = path.name.lower()
        if (
            not path.is_file()
            or path.suffix.lower() in _BLOCKED_DOWNLOAD_SUFFIXES
            or any(lower_name.endswith(ext) for ext in _BLOCKED_DOWNLOAD_SUFFIXES)
            or any(part.startswith(".") and part not in {".fx-template"} for part in rel_parts if not part.startswith(".fx-template-"))
            or any(
                part in {"backups", ".stversions", "task-locks", ".projects"}
                for part in rel_parts
            )
        ):
            return failure("File is not available", 404, request)
        relative = path.relative_to(request.state.project_settings.data_root)
        if relative.parts[0] == "workspaces" and (
            len(relative.parts) < 3 or relative.parts[1] != request.state.log_device
        ):
            return failure("File is not available", 404, request)
        return FileResponse(
            path,
            filename=path.name,
            headers={**_SECURITY_HEADERS, "Content-Disposition": f'attachment; filename="{path.name}"'},
        )

    async def status(request):
        device_id = await authenticate(request)
        if not device_id:
            return auth_failure(request)
        return JSONResponse(
            {
                "mode": "shared",
                "protocol": 1,
                "version": __version__,
                "service_revision": "cloud-repair-20261113",
                "file_protocol": 1,
                "task_source_adaptations": True,
                "device_id": device_id,
                "device_name": settings.devices[device_id].name
                if device_id in settings.devices
                else device_id,
                "offline_policy": "fail_closed",
                "project_ids": request.state.project_ids,
                "data_project": request.state.data_project,
                **backup_state,
            },
            headers=_SECURITY_HEADERS,
        )

    async def health(request):
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer "):
            return failure("Invalid health key", 401, request)
        supplied = header.removeprefix("Bearer ")
        if not settings.api_key or not hmac.compare_digest(
            supplied.encode(), settings.api_key.encode()
        ):
            return failure("Invalid health key", 401, request)
        def _check_db():
            if not matches_key(control_connection, supplied):
                return "disabled"
            with control_connection() as db:
                db.execute("SELECT 1").fetchone()
            return "ok"
        try:
            db_state = await asyncio.to_thread(_check_db)
        except Exception as exc:
            LOG.error("healthcheck_db_failed error=%s", type(exc).__name__)
            return failure("Database health check failed", 503, request)
        if db_state != "ok":
            return failure("Invalid health key", 401, request)
        return JSONResponse({"ready": True}, headers=_SECURITY_HEADERS)

    async def devices(request):
        if not await authenticate(request):
            return auth_failure(request)
        def _read_devices():
            with control_connection() as db:
                return {
                    row[0]: row[1:]
                    for row in db.execute("SELECT device_id,last_seen,version FROM devices")
                }
        seen = await asyncio.to_thread(_read_devices)
        now = time.time()
        return JSONResponse(
            {
                "devices": [
                    {
                        "device_id": key,
                        "name": device.name,
                        "last_seen": seen.get(key, (None, ""))[0],
                        "online": now - seen.get(key, (0, ""))[0] < 120 if key in seen else False,
                        "version": seen.get(key, (0, ""))[1],
                    }
                    for key, device in settings.devices.items()
                ]
            },
            headers=_SECURITY_HEADERS,
        )

    async def operation(request: Request):
        device_id = await authenticate(request)
        if not device_id:
            return auth_failure(request)
        if request.headers.get("x-fx-protocol", "1") != "1":
            return failure("Unsupported data protocol; update the workstation plugin", 409, request)
        if not check_rate_limit(request, device_id, limit=600):
            return failure("Device request limit exceeded; retry later", 429, request)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_BODY:
                return failure("Request exceeds 2 MiB", 413, request)
        try:
            item = Operation.model_validate_json(bytes(body))
        except ValueError as exc:
            log_rejection(request, exc, "invalid_request")
            return JSONResponse({"error": "Invalid operation request", "error_code": "invalid_request",
                                 "request_id": request.state.request_id, "details": error_details(exc)},
                                status_code=422, headers=_SECURITY_HEADERS)
        request.state.request_id = item.request_id
        request.state.operation = item.operation
        kind, _, name = item.operation.partition(".")
        if not (kind == "tool" and name in tools or kind == "task" and name in TASK_METHODS):
            return failure("Operation is not allowed", 403, request)
        payload_hash = hashlib.sha256(
            json.dumps([item.operation, item.arguments, item.task_id], sort_keys=True).encode()
        ).hexdigest()
        async with project_lock(request.state.data_project):
            # Recheck after waiting, before returning any previously cached result.
            if not await authenticate(request):
                return auth_failure(request)
            scoped = request.state.project_settings
            scoped_backend = request.state.project_backend
            control_device = request.state.device_key

            def _begin_request():
                with control_connection() as db:
                    row = db.execute(
                        "SELECT payload_hash,response,status FROM requests WHERE device_id=? AND request_id=?",
                        (control_device, item.request_id),
                    ).fetchone()
                    if row:
                        return ("cached", (row[0], row[1], row[2]), None)
                    db.execute(
                        "INSERT INTO requests VALUES (?,?,?,?,NULL,NULL,?)",
                        (control_device, item.request_id, item.operation, payload_hash, time.time()),
                    )
                    db.execute(
                        "INSERT INTO devices VALUES (?,?,?) ON CONFLICT(device_id) DO UPDATE SET last_seen=excluded.last_seen,version=excluded.version",
                        (
                            control_device,
                            time.time(),
                            request.headers.get("x-fx-version", "unknown")[:64],
                        ),
                    )
                    file_maps = {
                        r[0]: r[1]
                        for r in db.execute(
                            "SELECT original,stored FROM files WHERE device_id=?", (control_device,)
                        )
                    }
                    return ("new", None, file_maps)

            req_state, cached_row, file_maps = await asyncio.to_thread(_begin_request)
            if req_state == "cached":
                if cached_row[0] != payload_hash:
                    return failure("Request ID conflict: arguments changed", 409, request)
                if cached_row[1] is None:
                    return failure(
                        "Previous operation outcome unknown; inspect data before retrying", 409, request
                    )
                return JSONResponse(json.loads(cached_row[1]), status_code=cached_row[2], headers=_SECURITY_HEADERS)
            device = settings.devices.get(device_id)
            mappings = dict(device.path_maps if device else settings.path_maps)
            mappings.update(file_maps)
            arguments = map_value(item.arguments, mappings)
            status = 200
            try:
                if item.operation != "tool.fx_export_template":
                    check_file_access(
                        item.operation,
                        arguments,
                        scoped,
                        [settings.data_root / ".projects"]
                        if request.state.data_project == "default"
                        else [],
                    )
                with service_scope(
                    scoped.data_root, scoped.database_root, device_id, scoped_backend
                ):
                    if kind == "tool":
                        await asyncio.to_thread(
                            check_knowledge_gate, name, arguments, item.task_id, scoped.data_root
                        )
                        if name == "fx_export_template":
                            from fx_data_service.transfers import export_bundle
                            if request.headers.get("x-fx-file-protocol") != "1":
                                raise ValueError("Client update required for verified export downloads")
                            result = await asyncio.to_thread(validate_call(export_bundle),
                                                            scoped.data_root, **arguments)
                        else:
                            result = await tools[name](**arguments)
                    else:
                        from fx_agent.task_gates import TaskGates, TaskSpec

                        if name in {"begin", "update"}:
                            arguments["spec"] = TaskSpec.model_validate(arguments.get("spec"))
                        method = validate_call(getattr(TaskGates(scoped.data_root), name))
                        result = await asyncio.to_thread(method, **arguments)
                # The pinned Houdini adapter compact-serializes dict results.
                if isinstance(result, str) and result.lstrip().startswith(("{", "[")):
                    with suppress(json.JSONDecodeError):
                        result = json.loads(result)
                response = {
                    "result": map_value(json_value(result), {v: k for k, v in mappings.items()})
                }
            except PermissionError as exc:
                log_rejection(request, exc, "access_denied")
                status, response = (
                    403,
                    {
                        "error": "Device does not own this task or file path is outside allowed shared roots"
                    },
                )
            except (ValueError, TypeError, FileNotFoundError) as exc:
                message = str(exc)
                status = (
                    409
                    if not isinstance(exc, ValidationError) and any(
                        word in message.lower() for word in ("conflict", "stale", "busy", "changed")
                    )
                    else 422
                )
                # Validation errors can contain complete request values; do not
                # echo those or server-local paths across the network.
                response = {
                    "error": "Operation conflict; read current revision and retry"
                    if status == 409
                    else "Invalid operation arguments or unavailable shared file"
                }
                response["error_code"] = ("invalid_arguments" if isinstance(exc, ValidationError)
                                          else "file_not_found" if isinstance(exc, FileNotFoundError)
                                          else "operation_conflict" if status == 409
                                          else "operation_rejected")
                response["details"] = error_details(exc)
                response["reason"] = safe_reason(exc)
                log_rejection(request, exc, response["error_code"])
                if message == "Unknown module or too many modules; read fx_get_module_catalog":
                    response = {
                        "error": "Unknown module; read fx_get_module_catalog for supported module IDs",
                        "error_code": "unknown_module",
                    }
                from fx_agent.data_api import RULES_PAGINATION_ERROR

                if name == "fx_get_production_rules" and (
                    message == RULES_PAGINATION_ERROR
                    or isinstance(exc, ValidationError)
                    and any(error["loc"] in {("max_chars",), ("offset",)} for error in exc.errors())
                ):
                    status = 422
                    response = {
                        "error": RULES_PAGINATION_ERROR,
                        "error_code": "invalid_pagination",
                        "limits": {
                            "max_chars": {"minimum": 1024, "maximum": 12000},
                            "offset": {"minimum": 0},
                        },
                    }
            except Exception as exc:
                log_rejection(request, exc, "internal_error")
                status, response = (
                    500,
                    {"error": "Data service operation failed; inspect server logs"},
                )
            response["request_id"] = item.request_id

            def _finish_request():
                with control_connection() as db:
                    db.execute(
                        "UPDATE requests SET response=?,status=? WHERE device_id=? AND request_id=?",
                        (
                            json.dumps(response, ensure_ascii=False),
                            status,
                            control_device,
                            item.request_id,
                        ),
                    )

            await asyncio.to_thread(_finish_request)
            return JSONResponse(response, status_code=status, headers=_SECURITY_HEADERS)

    return Starlette(
        middleware=[Middleware(RequestLoggingMiddleware)],
        routes=[
            Route("/v1/operations", operation, methods=["POST"]),
            Route("/v1/devices", devices),
            Route("/v1/status", status),
            Route("/v1/files/register", register_file, methods=["POST"]),
            Route("/v1/files", upload_file, methods=["PUT"]),
            Route("/v1/files/{path:path}", download_file, methods=["GET"]),
            Route("/internal/health", health),
            *admin.routes(),
        ],
        lifespan=lifespan,
    )


def save_private_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    with suppress(OSError):
        path.chmod(0o600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Create a private service and first-device config")
    init.add_argument("--config", type=Path, required=True)
    init.add_argument("--data-root", type=Path, required=True)
    init.add_argument("--database-root", type=Path, required=True)
    init.add_argument("--client-config", type=Path, required=True)
    init.add_argument("--name", required=True)
    init.add_argument("--url", required=True)
    init.add_argument("--allow-root", action="append", type=Path, default=[])
    init.add_argument("--allow-insecure-http", action="store_true")
    for name in ("serve", "backup", "import", "add-device"):
        command = commands.add_parser(name)
        command.add_argument("--config", type=Path, required=True)
        if name == "serve":
            command.add_argument("--host", default="127.0.0.1")
            command.add_argument("--port", type=int, default=18790)
        elif name == "backup":
            command.add_argument("--output", type=Path, required=True)
        elif name == "import":
            command.add_argument("--source", type=Path, required=True)
            command.add_argument("--apply", action="store_true")
        else:
            command.add_argument("--name", required=True)
            command.add_argument("--client-config", type=Path, required=True)
            command.add_argument("--url", required=True)
            command.add_argument("--allow-insecure-http", action="store_true")
    args = parser.parse_args()
    if args.command == "init":
        token, device_id = secrets.token_urlsafe(32), uuid.uuid4().hex
        maps = {str(path): str(path.resolve()) for path in [args.data_root, *args.allow_root]}
        settings = ServiceConfig(
            data_root=args.data_root,
            database_root=args.database_root,
            allowed_roots=args.allow_root,
            devices={device_id: DeviceConfig(name=args.name, token=token, path_maps=maps)},
        )
        if args.config.exists() or args.client_config.exists():
            parser.error("Config already exists; refusing to overwrite")
        # Snapshot the existing database before the service upgrades any schema.
        source = settings.data_root / "memory.sqlite3"
        target = settings.database_root / "memory.sqlite3"
        if source.exists() and not target.exists():
            snapshot_database(source, target)
        save_private_json(args.config, settings.model_dump(mode="json"))
        save_private_json(
            args.client_config,
            {
                "url": args.url,
                "token": token,
                "device_id": device_id,
                "allow_insecure_http": args.allow_insecure_http,
            },
        )
        print("Created private configuration; existing source data preserved.")
        return
    settings = ServiceConfig.model_validate_json(args.config.read_text(encoding="utf-8"))
    if args.command == "serve":
        import uvicorn

        # One process owns the DB, serialization and request deduplication.
        uvicorn.run(create_app(settings), host=args.host, port=args.port, workers=1)
    elif args.command == "backup":
        snapshot_database(settings.database_root / "memory.sqlite3", args.output)
        print("Database backup verified; back up shared artifacts separately.")
    elif args.command == "add-device":
        if args.client_config.exists():
            parser.error("Client config already exists")
        token, device_id = secrets.token_urlsafe(32), uuid.uuid4().hex
        maps = next(iter(settings.devices.values())).path_maps.copy()
        settings.devices[device_id] = DeviceConfig(name=args.name, token=token, path_maps=maps)
        save_private_json(
            args.client_config,
            {
                "url": args.url,
                "token": token,
                "device_id": device_id,
                "allow_insecure_http": args.allow_insecure_http,
            },
        )
        temporary = args.config.with_name(args.config.name + "." + uuid.uuid4().hex + ".tmp")
        save_private_json(temporary, settings.model_dump(mode="json"))
        temporary.replace(args.config)
        print("Device registered; restart the data service to load the updated config.")
    else:
        from fx_agent.data_import import import_data
        from fx_agent.task_gates import endpoint_lock

        # Admin import requires a stopped service; a shared instance lock is
        # acquired by this CLI and by the serving process (see service lease).
        with (
            service_scope(settings.data_root, settings.database_root, ""),
            endpoint_lock(settings.database_root, "data-service-owner"),
        ):
            print(
                json.dumps(
                    import_data(args.source, settings.data_root, apply=args.apply),
                    ensure_ascii=False,
                    indent=2,
                )
            )


if __name__ == "__main__":
    main()
