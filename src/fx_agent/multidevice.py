"""Authenticated data routing; DCC dispatch always stays on the workstation."""

import asyncio
import hashlib
import inspect
import json
import os
import re
import ssl
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

from fx_agent import __version__
from fx_agent.data_context import current_device as current_device
from fx_agent.data_context import current_scope as current_scope
from fx_agent.data_context import json_value as json_value
from fx_agent.data_context import remote_enabled as remote_enabled
from fx_agent.data_context import service_scope as service_scope
from fx_agent.data_contracts import DATA_TOOLS as DATA_TOOLS
from fx_agent.data_contracts import TASK_METHODS as TASK_METHODS

_task = ContextVar("fx_remote_task", default="")


@contextmanager
def task_scope(task_id):
    token = _task.set(task_id)
    try:
        yield
    finally:
        _task.reset(token)


class ServiceUnavailable(RuntimeError):
    """No local fallback: a disconnected client must not fork the shared data."""


def invalid_response(response, request_id, reason):
    """Keep useful HTTP evidence without echoing private bodies or credentials."""
    content_type = response.headers.get("content-type", "unknown").split(";", 1)[0].strip()
    if not re.fullmatch(r"[A-Za-z0-9.+/-]{1,80}", content_type):
        content_type = "unknown"
    text = response.text[:2048].casefold()
    markers = (
        "bad gateway",
        "gateway timeout",
        "gateway time-out",
        "service unavailable",
        "internal server error",
        "not found",
        "nginx",
        "cloudflare",
        "envoy",
    )
    hint = ", ".join(marker for marker in markers if marker in text) or "unrecognized content"
    return ServiceUnavailable(
        f"FX data service {reason}; HTTP {response.status_code}; content_type={content_type}; "
        f"body_bytes={len(response.content)}; response_hint={hint}; request_id={request_id}"
    )


class RemoteClient:
    def __init__(self, config_path: Path | None = None):
        config_path = config_path or os.getenv("FX_AGENT_CLIENT_CONFIG")
        explicit = bool(config_path)
        config = json.loads(Path(config_path).read_text(encoding="utf-8")) if config_path else {}
        host = config.get("server_ip", "") if explicit else os.getenv("FX_AGENT_SERVER_IP", "")
        port = config.get("port", 18443) if explicit else os.getenv("FX_AGENT_SERVER_PORT", "18443")
        if host:
            if not 1 <= int(port) <= 65535 or any(c in host for c in "/?#@"):
                raise ValueError("Invalid server IP or port")
            config.setdefault("url", f"https://{host}:{port}")
        if explicit:
            config.setdefault("token", config.get("api_key", ""))
        self.url = (
            config.get("url", "")
            if explicit
            else os.getenv("FX_AGENT_SERVICE_URL", config.get("url", ""))
        ).rstrip("/")
        self.token = (
            config.get("token", "")
            if explicit
            else os.getenv(
                "FX_AGENT_API_KEY", os.getenv("FX_AGENT_SERVICE_TOKEN", config.get("token", ""))
            )
        )
        self.data_project = (
            config.get("data_project", "") if explicit else os.getenv("FX_AGENT_DATA_PROJECT", "")
        )
        import getpass
        import socket
        import uuid

        self.device_id = (
            config.get("device_id")
            or os.getenv("FX_AGENT_DEVICE_ID")
            or uuid.uuid5(
                uuid.NAMESPACE_OID, f"{socket.gethostname()}:{uuid.getnode()}:{getpass.getuser()}"
            ).hex
        )
        self.upload_files = config.get(
            "upload_files", os.getenv("FX_AGENT_UPLOAD_FILES", "1") != "0"
        )
        ca_file = config.get("ca_file") or os.getenv("FX_AGENT_CA_FILE")
        self.verify = ssl.create_default_context(cafile=str(ca_file)) if ca_file else True
        parsed = urlsplit(self.url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("FX data service requires a valid HTTP(S) URL without credentials")
        if (
            parsed.scheme == "http"
            and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            and not config.get("allow_insecure_http", False)
            and os.getenv("FX_AGENT_ALLOW_INSECURE_HTTP") != "1"
        ):
            raise ValueError("Use HTTPS or explicitly enable trusted-LAN insecure HTTP")
        if len(self.token) < 32:
            raise ValueError("FX data service token is missing or too short")

    def call(self, operation: str, arguments: dict | None = None, *, request_id: str = ""):
        import uuid

        arguments = arguments or {}
        if self.upload_files:
            self.prepare_files(operation, json_value(arguments))
        body = {
            "request_id": request_id or uuid.uuid4().hex,
            "task_id": _task.get(),
            "operation": operation,
            "arguments": json_value(arguments or {}),
        }
        # A transport failure retries the exact request ID. The service persists
        # pending IDs before dispatch and never executes an uncertain write twice.
        with httpx.Client(
            timeout=120, trust_env=False, follow_redirects=False, verify=self.verify
        ) as client:
            for attempt in range(2):
                try:
                    response = client.post(
                        self.url + "/v1/operations",
                        json=body,
                        headers=self.headers()
                        | {
                            "X-FX-Version": __version__,
                            "X-FX-Protocol": "1",
                            "X-FX-Device-ID": self.device_id,
                        },
                    )
                    break
                except httpx.TransportError:
                    if attempt:
                        raise ServiceUnavailable(
                            "FX data service unavailable; no local data was written. "
                            "A write may have completed remotely; inspect state before retrying."
                        ) from None
        try:
            value = response.json()
        except ValueError:
            raise invalid_response(
                response, body["request_id"], "returned a non-JSON response"
            ) from None
        if (
            not isinstance(value, dict)
            or response.status_code == 200
            and "result" not in value
            or response.status_code != 200
            and not isinstance(value.get("error"), str)
        ):
            raise invalid_response(
                response, body["request_id"], "returned an invalid JSON envelope"
            )
        if response.status_code == 403:
            raise PermissionError(value.get("error", "Data service access denied"))
        if response.status_code >= 500:
            raise ServiceUnavailable(value.get("error", "FX data service failed"))
        if response.status_code != 200:
            raise ValueError(value.get("error", "FX data service request rejected"))
        return value["result"]

    def headers(self):
        headers = {"Authorization": "Bearer " + self.token, "X-FX-Device-ID": self.device_id}
        if self.data_project:
            headers["X-FX-Data-Project"] = quote(self.data_project, safe="")
        return headers

    def prepare_files(self, operation, arguments):
        paths = []
        if operation in {"task.begin", "task.update"}:
            spec = arguments["spec"]
            paths = [spec["scene_path"], *spec.get("input_paths", [])]
        elif operation in {"task.authorize", "task.finish"}:
            task = self.call("task.get", {"task_id": arguments["task_id"], "detail": True})
            paths = [task["scene_path"], *task.get("input_paths", [])]
        elif operation == "tool.fx_archive_case":
            paths = [item["path"] for item in arguments["files"]]
        elif operation in {"tool.fx_plan_template_import", "tool.fx_learn_templates"}:
            paths = [arguments["source"], *[item["path"] for item in arguments.get("files", [])]]
        for path in dict.fromkeys(paths):
            self.upload_path(
                Path(path),
                allow_missing=operation in {"task.begin", "task.update", "task.authorize"},
            )

    def upload_path(self, path, *, allow_missing=False):
        if path.is_dir():
            raise ValueError(
                "Upload template directories to the server first; direct file upload accepts individual files"
            )
        headers = self.headers()
        try:
            with httpx.Client(timeout=120, verify=self.verify, trust_env=False) as client:
                if not path.exists():
                    if not allow_missing:
                        raise ValueError(
                            "The current local evidence file is missing; an old server copy cannot be used"
                        )
                    result = client.post(
                        self.url + "/v1/files/register", headers=headers, json={"path": str(path)}
                    )
                else:
                    before = path.stat()
                    if before.st_size > 512 * 1024 * 1024:
                        raise ValueError("Cloud archive files must not exceed 512 MiB")
                    with path.open("rb") as stream:
                        digest = hashlib.file_digest(stream, "sha256").hexdigest()
                        stream.seek(0)
                        result = client.put(
                            self.url + "/v1/files",
                            content=stream,
                            headers=headers
                            | {
                                "X-FX-Client-Path": quote(str(path), safe=""),
                                "X-FX-SHA256": digest,
                                "X-FX-Mtime-Ns": str(before.st_mtime_ns),
                            },
                        )
                    after = path.stat()
                    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                        raise ValueError("Local file changed during upload; inspect and retry")
                result.raise_for_status()
        except httpx.HTTPError:
            raise ServiceUnavailable(
                "Shared file transfer failed; no local fallback was used"
            ) from None

    def status(self):
        try:
            with httpx.Client(
                timeout=10, trust_env=False, follow_redirects=False, verify=self.verify
            ) as client:
                response = client.get(
                    self.url + "/v1/status",
                    headers=self.headers(),
                )
                response.raise_for_status()
                return response.json()
        except (httpx.HTTPError, ValueError):
            raise ServiceUnavailable(
                "FX data service status unavailable; check connection and device token"
            ) from None


class RemoteTaskGates:
    def __init__(self):
        configured = os.getenv("FX_AGENT_LOCAL_ROOT")
        self.root = Path(configured) if configured else Path.home() / ".fx-agent" / "local-state"
        if not self.root.is_absolute():
            raise ValueError("FX_AGENT_LOCAL_ROOT must be absolute")

    def __getattr__(self, name):
        if name not in TASK_METHODS:
            raise AttributeError(name)

        def call(*args, **kwargs):
            from fx_agent.task_gates import TaskGates

            bound = inspect.signature(getattr(TaskGates, name)).bind(None, *args, **kwargs)
            return RemoteClient().call(
                "task." + name, {k: v for k, v in bound.arguments.items() if k != "self"}
            )

        return call


def install_remote_tools(mcp):
    """Preserve SDK schemas and validation, replacing only shared tool bodies."""
    for tool in mcp._tool_manager.list_tools():
        if tool.name not in DATA_TOOLS | {"fx_import_data"}:
            continue
        if getattr(tool.fn, "_fx_remote", False):
            continue

        def wrap(fn, name):
            @wraps(fn)
            async def routed(*args, **kwargs):
                if not remote_enabled():
                    result = fn(*args, **kwargs)
                    return await result if inspect.isawaitable(result) else result
                if name == "fx_import_data":
                    raise ValueError("Remote import is admin-only; use the data service import CLI")
                bound = inspect.signature(fn).bind(*args, **kwargs)
                return await asyncio.to_thread(RemoteClient().call, "tool." + name, bound.arguments)

            routed._fx_remote = True
            return routed

        tool.fn = wrap(tool.fn, tool.name)
        tool.is_async = True

    if not mcp._tool_manager.get_tool("fx_get_data_service_status"):

        @mcp.tool()
        async def fx_get_data_service_status() -> dict:
            """Check shared service connectivity, own device identity and backup health."""
            if remote_enabled():
                return await asyncio.to_thread(RemoteClient().status)
            return {"mode": "local", "shared_service": False}
