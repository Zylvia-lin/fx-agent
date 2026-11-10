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

from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from fx_agent import __version__
from fx_agent.data_context import json_value, service_scope
from fx_agent.data_contracts import DATA_TOOLS, TASK_METHODS

LOG = logging.getLogger(__name__)
MAX_BODY = 2 * 1024 * 1024
BACKUP_INTERVAL_SECONDS = 24 * 60 * 60


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
        normalized = value.replace("\\", "/")
        for source, target in sorted(mappings.items(), key=lambda item: -len(item[0])):
            prefix = source.replace("\\", "/").rstrip("/")
            if normalized.casefold() == prefix.casefold() or normalized.casefold().startswith(
                prefix.casefold() + "/"
            ):
                return target.replace("\\", "/").rstrip("/") + normalized[len(prefix) :]
    return value


def check_file_access(operation, arguments, settings):
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
        paths.extend(item.get("path", "") for item in arguments.get("files", []))
    if operation in {"task.begin", "task.update"}:
        spec = arguments.get("spec", {})
        paths.extend([spec.get("scene_path", ""), *spec.get("input_paths", [])])
    if operation == "task.authorize":
        paths.append(arguments.get("scene_path", ""))
    roots = [settings.data_root.resolve(), *(p.resolve() for p in settings.allowed_roots)]
    for value in paths:
        path = Path(value)
        if not path.is_absolute() or not any(path.resolve().is_relative_to(root) for root in roots):
            raise PermissionError(
                "File path is outside the service's allowed shared roots; configure a path mapping"
            )


class Operation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    operation: str = Field(min_length=1, max_length=100)
    arguments: dict = Field(default_factory=dict)
    task_id: str = Field(default="", max_length=128)


def check_knowledge_gate(name, arguments, task_id, root):
    """The HTTP API enforces the same admission rules as workstation MCPs."""
    from fx_agent.data_contracts import KNOWLEDGE_WRITES
    from fx_agent.task_gates import TaskGates

    acceptance = (
        name == "fx_record_feedback" and arguments.get("feedback", {}).get("outcome") == "accepted"
    )
    knowledge_write = name in KNOWLEDGE_WRITES or (
        name == "fx_review_template_effects" and arguments.get("reviewed_node_paths") is not None
    )
    if not (acceptance or knowledge_write or name == "fx_finalize_learning"):
        return
    if not task_id:
        raise ValueError("Knowledge admission requires an owned task")
    gates = TaskGates(root)
    task = gates.get(task_id, True)
    if knowledge_write and (not task["plan"] or gates.get(task_id)["missing"]):
        raise ValueError("Learning write requires current knowledge and a plan")
    if acceptance or name == "fx_finalize_learning":
        if task["state"] != "verified" or task["kind"] != "production":
            raise ValueError("Accepted learning requires a verified production task")
        gates.authorize(task_id, task["software"], task["endpoint"], task["scene_path"])
        if acceptance:
            feedback = arguments["feedback"]
        else:
            from fx_agent.learning import LearningStore

            feedback = LearningStore(root).get_feedback(
                arguments["feedback_id"], arguments["project_id"]
            )
        if any(
            feedback.get(key, "houdini" if key == "software" else "") != task[key]
            for key in ("project_id", "shot_id", "software", "case_id")
        ):
            raise ValueError("Accepted feedback must belong to this verified task")


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
    lock = asyncio.Lock()
    rate = {}
    backup_state = {"last_backup": None, "backup_error": None}
    from fx_data_service.admin import AdminConsole, initialize_keys, key_projects, matches_key

    initialize_keys(control_connection, settings.api_key)

    def discover_projects():
        projects = set()
        with (
            service_scope(settings.data_root, settings.database_root, "", backend),
            Store(settings.data_root).connection() as db,
        ):
            # Checkpoint keys are device hashes; the payload keeps the real project ID.
            for table in (
                "checkpoints",
                "memories",
                "learning_feedback",
                "task_gates",
                "render_issues",
                "template_studies",
                "dream_runs",
            ):
                for row in db.execute(f"SELECT payload FROM {table}"):
                    value = json.loads(row[0]).get("project_id")
                    if value:
                        projects.add(value)
            for row in db.execute("SELECT DISTINCT scope FROM user_rules"):
                if row[0].startswith("project:"):
                    projects.add(row[0][len("project:") :])
        for manifest in (settings.data_root / "cases").glob("*/manifest.json"):
            value = json.loads(manifest.read_text(encoding="utf-8")).get("project_id")
            if value:
                projects.add(value)
        return projects

    admin = AdminConsole(control_connection, settings.admin_password, discover_projects)

    def backup():
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
        destination = settings.backup_root or settings.data_root / "backups"
        if backend:
            destination.mkdir(parents=True, exist_ok=True)
            backend.backup(destination / (stamp + "-mysql.json.gz"))
            backup_state.update(last_backup=stamp, backup_error=None)
            return
        snapshot_database(
            settings.database_root / "memory.sqlite3", destination / (stamp + "-memory.sqlite3")
        )
        snapshot_database(control_path, destination / (stamp + "-service.sqlite3"))
        backup_state.update(last_backup=stamp, backup_error=None)

    async def periodic_backup():
        while True:
            await asyncio.sleep(BACKUP_INTERVAL_SECONDS)
            try:
                async with lock:
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

    def authenticate(request):
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer "):
            return None
        supplied = header.removeprefix("Bearer ")
        if matches_key(control_connection, supplied):
            request.state.project_ids = key_projects(control_connection, supplied)
            device_id = request.headers.get("x-fx-device-id", "shared")
            if (
                not device_id
                or len(device_id) > 128
                or not all(c.isascii() and (c.isalnum() or c in "-_") for c in device_id)
            ):
                return None
            return device_id
        for device_id, device in settings.devices.items():
            if hmac.compare_digest(supplied.encode(), device.token.encode()):
                request.state.project_ids = None
                return device_id
        return None

    def failure(message, status):
        return JSONResponse(
            {"error": message}, status_code=status, headers={"Cache-Control": "no-store"}
        )

    def file_target(device_id, original):
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
        target = settings.data_root / "workspaces" / device_id / key / basename
        if not target.resolve().is_relative_to(settings.data_root):
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
        device_id = authenticate(request)
        if not device_id:
            return failure("Invalid API key", 401)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 8192:
                return failure("Path request too large", 413)
        try:
            original = json.loads(body)["path"]
            target = file_target(device_id, original)
        except (ValueError, KeyError, TypeError):
            return failure("Invalid client path", 422)
        async with lock:
            target.unlink(missing_ok=True)
            remember_file(device_id, original, target)
        return JSONResponse({"path": str(target)})

    async def upload_file(request):
        device_id = authenticate(request)
        if not device_id:
            return failure("Invalid API key", 401)
        try:
            original = unquote(request.headers.get("x-fx-client-path", ""))
            target = file_target(device_id, original)
            expected = request.headers.get("x-fx-sha256", "")
            mtime = int(request.headers.get("x-fx-mtime-ns", "0"))
            if not re.fullmatch(r"[a-f0-9]{64}", expected) or not 0 <= mtime < 2**63:
                raise ValueError("Invalid file metadata")
        except ValueError:
            return failure("Invalid file metadata", 422)
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = target.with_name(".upload-" + uuid.uuid4().hex)
        digest, size = hashlib.sha256(), 0
        try:
            with staging.open("xb") as stream:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > 512 * 1024 * 1024:
                        return failure("File exceeds 512 MiB", 413)
                    stream.write(chunk)
                    digest.update(chunk)
            if digest.hexdigest() != expected:
                return failure("File checksum mismatch", 422)
            async with lock:
                os.replace(staging, target)
                os.utime(target, ns=(mtime, mtime))
                remember_file(device_id, original, target)
            return JSONResponse({"path": str(target), "sha256": expected, "size": size})
        finally:
            staging.unlink(missing_ok=True)

    async def download_file(request):
        if not authenticate(request):
            return failure("Invalid API key", 401)
        from fx_agent.archive import contained

        try:
            path = contained(settings.data_root, request.path_params["path"])
        except ValueError:
            return failure("Invalid shared file path", 403)
        if (
            not path.is_file()
            or path.suffix in {".sqlite3", ".pem", ".key"}
            or any(
                part in {"backups", ".stversions", "workspaces", "task-locks"}
                for part in path.relative_to(settings.data_root).parts
            )
        ):
            return failure("File is not available", 404)
        return FileResponse(path, headers={"Cache-Control": "no-store"})

    async def status(request):
        device_id = authenticate(request)
        if not device_id:
            return failure("Invalid device token", 401)
        return JSONResponse(
            {
                "mode": "shared",
                "protocol": 1,
                "version": __version__,
                "device_id": device_id,
                "device_name": settings.devices[device_id].name
                if device_id in settings.devices
                else device_id,
                "offline_policy": "fail_closed",
                "project_ids": request.state.project_ids,
                **backup_state,
            },
            headers={"Cache-Control": "no-store"},
        )

    async def health(request):
        supplied = request.headers.get("authorization", "").removeprefix("Bearer ")
        if not settings.api_key or not hmac.compare_digest(
            supplied.encode(), settings.api_key.encode()
        ):
            return failure("Invalid health key", 401)
        return JSONResponse({"ready": True})

    async def devices(request):
        if not authenticate(request):
            return failure("Invalid device token", 401)
        with control_connection() as db:
            seen = {
                row[0]: row[1:]
                for row in db.execute("SELECT device_id,last_seen,version FROM devices")
            }
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
            }
        )

    async def operation(request: Request):
        device_id = authenticate(request)
        if not device_id:
            return failure("Invalid device token", 401)
        if request.headers.get("x-fx-protocol", "1") != "1":
            return failure("Unsupported data protocol; update the workstation plugin", 409)
        now = time.monotonic()
        start, count = rate.get(device_id, (now, 0))
        if now - start >= 60:
            start, count = now, 0
        rate[device_id] = (start, count + 1)
        if count >= 600:
            return failure("Device request limit exceeded; retry later", 429)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_BODY:
                return failure("Request exceeds 2 MiB", 413)
        try:
            item = Operation.model_validate_json(bytes(body))
        except ValueError:
            return failure("Invalid operation request", 422)
        kind, _, name = item.operation.partition(".")
        if not (kind == "tool" and name in tools or kind == "task" and name in TASK_METHODS):
            return failure("Operation is not allowed", 403)
        payload_hash = hashlib.sha256(
            json.dumps([item.operation, item.arguments, item.task_id], sort_keys=True).encode()
        ).hexdigest()
        async with lock:
            # Recheck after waiting, before returning any previously cached result.
            if not authenticate(request):
                return failure("Invalid device token", 401)
            allowed = request.state.project_ids
            if allowed is not None:
                from fx_agent.store import scope_id
                from fx_agent.task_gates import TaskGates

                def check_projects(value):
                    if isinstance(value, dict):
                        for field, child in value.items():
                            if field in {"project_id", "source_project"} and child:
                                if scope_id.validate_python(child) not in allowed:
                                    raise PermissionError(
                                        "Project is not associated with this API key"
                                    )
                            elif isinstance(child, (dict, list)):
                                check_projects(child)
                    elif isinstance(value, list):
                        for child in value:
                            check_projects(child)

                try:
                    check_projects(item.arguments)
                    with service_scope(
                        settings.data_root, settings.database_root, device_id, backend
                    ):
                        for task_id in {item.task_id, item.arguments.get("task_id", "")} - {""}:
                            task = TaskGates(settings.data_root).get(task_id, True)
                            check_projects({"project_id": task["project_id"]})
                except PermissionError:
                    return failure("Project is not associated with this API key", 403)
                except (ValueError, TypeError):
                    return failure("Invalid project or task", 422)
            with control_connection() as db:
                row = db.execute(
                    "SELECT payload_hash,response,status FROM requests WHERE device_id=? AND request_id=?",
                    (device_id, item.request_id),
                ).fetchone()
                if row:
                    if row[0] != payload_hash:
                        return failure("Request ID conflict: arguments changed", 409)
                    if row[1] is None:
                        return failure(
                            "Previous operation outcome unknown; inspect data before retrying", 409
                        )
                    return JSONResponse(json.loads(row[1]), status_code=row[2])
                db.execute(
                    "INSERT INTO requests VALUES (?,?,?,?,NULL,NULL,?)",
                    (device_id, item.request_id, item.operation, payload_hash, time.time()),
                )
                db.execute(
                    "INSERT INTO devices VALUES (?,?,?) ON CONFLICT(device_id) DO UPDATE SET last_seen=excluded.last_seen,version=excluded.version",
                    (device_id, time.time(), request.headers.get("x-fx-version", "unknown")[:64]),
                )
            device = settings.devices.get(device_id)
            mappings = dict(device.path_maps if device else settings.path_maps)
            with control_connection() as db:
                mappings.update(
                    {
                        row[0]: row[1]
                        for row in db.execute(
                            "SELECT original,stored FROM files WHERE device_id=?", (device_id,)
                        )
                    }
                )
            arguments = map_value(item.arguments, mappings)
            status = 200
            try:
                check_file_access(item.operation, arguments, settings)
                with service_scope(settings.data_root, settings.database_root, device_id, backend):
                    if kind == "tool":
                        check_knowledge_gate(name, arguments, item.task_id, settings.data_root)
                        result = await tools[name](**arguments)
                    else:
                        from fx_agent.task_gates import TaskGates, TaskSpec

                        if name in {"begin", "update"}:
                            arguments["spec"] = TaskSpec.model_validate(arguments.get("spec"))
                        result = await asyncio.to_thread(
                            getattr(TaskGates(settings.data_root), name), **arguments
                        )
                # The pinned Houdini adapter compact-serializes dict results.
                if isinstance(result, str) and result.lstrip().startswith(("{", "[")):
                    with suppress(json.JSONDecodeError):
                        result = json.loads(result)
                response = {
                    "result": map_value(json_value(result), {v: k for k, v in mappings.items()})
                }
            except PermissionError:
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
                    if any(
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
            except Exception:
                LOG.exception(
                    "Data operation failed: %s device=%s request=%s",
                    item.operation,
                    device_id,
                    item.request_id,
                )
                status, response = (
                    500,
                    {"error": "Data service operation failed; inspect server logs"},
                )
            with control_connection() as db:
                db.execute(
                    "UPDATE requests SET response=?,status=? WHERE device_id=? AND request_id=?",
                    (json.dumps(response, ensure_ascii=False), status, device_id, item.request_id),
                )
            return JSONResponse(response, status_code=status, headers={"Cache-Control": "no-store"})

    return Starlette(
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
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
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
