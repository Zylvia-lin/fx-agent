"""Explicit, per-client targets. One request per socket, never retried."""

import json
import socket
import threading
import time
import uuid
from pathlib import Path

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
    return info


def request(port, command, params=None, target=None, timeout=10.0):
    validate_port(port)
    if not 0 < timeout <= 3600:
        raise ValueError("timeout must be >0 and <=3600 seconds")
    request_id = uuid.uuid4().hex
    message = {"id": request_id, "command": command, "params": params or {},
               "target": target, "deadline": time.time() + timeout}
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


def probe(port, timeout=0.5):
    return validate_identity(request(port, "identity", timeout=timeout), port)


def discover(root: Path, ports=DEFAULT_PORTS):
    """Registry files suggest ports only. Every returned identity is read live."""
    candidates = set(ports)
    for path in (Path(root) / "maya_sessions").glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            port = record["port"]
            validate_port(port)
            candidates.add(port)
        except (OSError, ValueError, TypeError, KeyError):
            continue
    sessions, unavailable = [], []
    for port in sorted(candidates):
        try:
            validate_port(port)
            sessions.append(probe(port) | {"identity_verified": True})
        except (OSError, ValueError, RuntimeError, KeyError) as exc:
            unavailable.append({"port": port, "error": str(exc)})
    return {"sessions": sessions, "unavailable": unavailable}


class MayaConnection:
    def __init__(self):
        self.target = None
        self.failed = False
        self.lock = threading.RLock()

    def select(self, port, pid, session_id, expected_scene):
        with self.lock:
            candidate = probe(port, timeout=5)
            if any(candidate[key] != value for key, value in (
                ("pid", pid), ("session_id", session_id), ("scene_path", expected_scene)
            )):
                raise ValueError("Maya session/scene changed; refresh list_maya_sessions")
            self.target = candidate
            self.failed = False
            return dict(candidate)

    def send(self, command, params=None, timeout=10.0):
        with self.lock:
            if self.target is None:
                raise RuntimeError("Explicitly select a target with connect_maya first")
            if self.failed:
                raise RuntimeError("Previous request outcome unknown; inspect Maya then explicitly reconnect. Never replay edits automatically.")
            try:
                return request(self.target["port"], command, params, self.target, timeout)
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
