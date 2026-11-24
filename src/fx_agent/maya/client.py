"""Explicit, per-client targets. One request per socket, never retried."""

import json
import re
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fx_agent.config import session_root
from fx_agent.maya.protocol import encode, receive

SERVICE = "fx-maya-bridge"
DEFAULT_PORTS = tuple(range(18770, 18790))


def validate_port(port):
    if type(port) is not int or not 1 <= port <= 65535 or port in (18186, 8187):
        raise ValueError("Invalid or reserved DCC port")


def validate_identity(info, port):
    if (
        not isinstance(info, dict)
        or info.get("software") != "maya"
        or info.get("service") != SERVICE
        or info.get("protocol") != 1
        or info.get("port") != port
        or type(info.get("pid")) is not int
        or info["pid"] <= 0
        or not isinstance(info.get("session_id"), str)
        or not info["session_id"]
        or not isinstance(info.get("scene_path"), str)
        or not isinstance(info.get("maya_version"), str)
        or not isinstance(info.get("python_version"), str)
        or not isinstance(info.get("renderers"), dict)
    ):
        raise ValueError("Not an FX Maya bridge or invalid session identity")
    return {key: value for key, value in info.items() if key != "token"}


def request(port, command, params=None, target=None, timeout=10.0, *, token):
    validate_port(port)
    if not 0 < timeout <= 3600:
        raise ValueError("timeout must be >0 and <=3600 seconds")
    request_id = uuid.uuid4().hex
    message = {"id": request_id, "command": command, "params": params or {},
               "target": target, "deadline": time.time() + timeout, "token": token}
    # No retry/reconnect loop, even when the reply is lost after a successful edit.
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as peer:
        peer.settimeout(timeout)
        peer.sendall(encode(message))
        response = receive(peer)
    if response.get("id") != request_id:
        raise OSError("Maya response ID mismatch; execution outcome unknown")
    if response.get("status") != "success":
        raise RuntimeError(response.get("error", "Maya bridge returned an invalid response")
                           + "\n" + response.get("traceback", ""))
    return response["result"]


def probe(port, token, timeout=0.5):
    return validate_identity(request(port, "identity", timeout=timeout, token=token), port)


def registry(root: Path):
    records = {}
    for path in (Path(root) / "maya_sessions").glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            port = record["port"]
            validate_port(port)
            session_id = record["session_id"]
            token = record["token"]
            if not isinstance(session_id, str) or not re.fullmatch(r"[0-9a-f]{64}", token):
                continue
            records.setdefault(port, {})[session_id] = token
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return records


def discover(root: Path, ports=DEFAULT_PORTS):
    """Authenticate using local registry credentials, then read the identity live."""
    records = registry(root)
    sessions, unavailable = [], []
    ordered = sorted(set(ports) | records.keys())

    def _check(port):
        error = "No Maya session credentials; start or restart the FX Maya bridge"
        for session_id, token in records.get(port, {}).items():
            try:
                info = probe(port, token)
                if info["session_id"] == session_id:
                    return port, info | {"identity_verified": True}, None
                error = "Registered Maya session changed"
            except (OSError, ValueError, RuntimeError, KeyError) as exc:
                error = str(exc)
        return port, None, error

    with ThreadPoolExecutor(max_workers=min(20, max(1, len(ordered)))) as pool:
        for port, info, error in pool.map(_check, ordered):
            if info is not None:
                sessions.append(info)
            else:
                unavailable.append({"port": port, "error": error})
    return {"sessions": sessions, "unavailable": unavailable}


class MayaConnection:
    def __init__(self, root=None):
        self.root = root
        self.target = None
        self._token = None
        self.failed = False
        self.lock = threading.RLock()

    def select(self, port, pid, session_id, expected_scene):
        with self.lock:
            token = registry(self.root or session_root()).get(port, {}).get(session_id)
            if token is None:
                raise ValueError("Maya session changed or has no credentials; refresh list_maya_sessions")
            candidate = probe(port, token, timeout=5)
            if any(candidate[key] != value for key, value in (
                ("pid", pid), ("session_id", session_id), ("scene_path", expected_scene)
            )):
                raise ValueError("Maya session/scene changed; refresh list_maya_sessions")
            self.target = candidate
            self._token = token
            self.failed = False
            return dict(candidate)

    def send(self, command, params=None, timeout=10.0):
        with self.lock:
            if self.target is None:
                raise RuntimeError("Explicitly select a target with connect_maya first")
            if self.failed:
                raise RuntimeError("Previous request outcome unknown; inspect Maya then explicitly reconnect. Never replay edits automatically.")
            try:
                return request(self.target["port"], command, params, self.target, timeout, token=self._token)
            except (OSError, ValueError, KeyError):
                self.failed = True
                raise

    def info(self):
        with self.lock:
            if self.target is None:
                raise RuntimeError("No Maya target selected")
            # identity is unguarded only for discovery; scene_info checks the selected
            # PID/session/scene on Maya's main thread before returning any scene data.
            return self.send("scene_info") | {key: self.target[key] for key in ("pid", "session_id", "port")}
