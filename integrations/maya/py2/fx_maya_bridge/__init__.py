# -*- coding: utf-8 -*-
"""Loopback-only Maya bridge. Run start() from Maya's main thread.

Python 2.7/3, stdlib plus Maya only. No plugin installation or autoloading.
"""
from __future__ import print_function

import json
import os
import socket
import sys
import threading
import time
import traceback
import uuid

from .protocol import encode, receive, text_value

SERVICE = "fx-maya-bridge"
SESSION_ID = uuid.uuid4().hex
_bridge = None


def identity(port):
    from maya import cmds

    from .lighting import renderer_status
    return {"software": "maya", "service": SERVICE, "protocol": 1,
            "session_id": SESSION_ID, "pid": os.getpid(), "port": port,
            "maya_version": cmds.about(version=True), "python_version": sys.version,
            "scene_path": cmds.file(query=True, sceneName=True) or "",
            "renderers": renderer_status(),
            "menu_exists": not cmds.about(batch=True) and cmds.menu("fxMayaMCPMenu", exists=True),
            "user_pref_dir": cmds.internalVar(userPrefDir=True)}


class Bridge(object):
    def __init__(self, port, root):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # Deliberately no SO_REUSEADDR: a second Maya must never take an occupied port.
        try:
            self.socket.bind(("127.0.0.1", port))
            self.socket.listen(8)
            self.socket.settimeout(0.5)
        except Exception:
            self.socket.close()
            raise
        self.port = self.socket.getsockname()[1]
        self.main_thread = threading.current_thread().ident
        self.running = True
        self.lock = threading.Lock()
        self.seen = set()
        self.record = os.path.join(root, "maya_sessions", SESSION_ID + ".json")
        self.thread = threading.Thread(target=self.serve, name="fx-maya-listener")
        self.thread.daemon = True

    def dispatch(self, request):
        """Called exclusively on Maya main thread, including identity validation."""
        from .lighting import dispatch
        if threading.current_thread().ident != self.main_thread:
            raise RuntimeError("Maya failed to schedule on main thread; batch requires a main-thread event pump")
        if not self.running:
            raise RuntimeError("Maya bridge stopped")
        request_id = request.get("id")
        if not isinstance(request_id, type(u"")) or not request_id:
            raise ValueError("Missing request ID")
        if request_id in self.seen:
            raise ValueError("Duplicate request ID; no automatic replay permitted")
        if len(self.seen) >= 100000:
            raise RuntimeError("Bridge request budget exhausted; restart bridge explicitly")
        self.seen.add(request_id)
        if time.time() > request.get("deadline", 0):
            raise RuntimeError("Request expired before main-thread dispatch; not executed")
        info = identity(self.port)
        command = request.get("command")
        if command == "identity":
            return info
        expected = request.get("target", {})
        for key in ("software", "service", "session_id", "pid", "port", "scene_path"):
            if expected.get(key) != info[key]:
                raise ValueError("Maya target identity/scene changed: " + key)
        params = request.get("params", {})
        if not isinstance(params, dict):
            raise ValueError("params must be an object")
        return dispatch(command, params)

    def handle(self, peer):
        from maya import utils
        request = {}
        try:
            peer.settimeout(10)
            request = receive(peer)
            # Serialize capture, main-thread execution and responses across clients.
            with self.lock:
                response = utils.executeInMainThreadWithResult(lambda: self.run_request(request))
        except Exception as exc:
            response = {"id": request.get("id"), "status": "error",
                        "error": u"%s: %s" % (type(exc).__name__, text_value(exc)),
                        "traceback": traceback.format_exc()}
        try:
            peer.sendall(encode(response))
        except (IOError, ValueError):
            pass  # Client may have timed out; never execute its request again.
        finally:
            peer.close()

    def run_request(self, request):
        # Catch inside the scheduled callable: Maya 2020's C++ scheduler discards
        # the original Python traceback when it propagates an exception to a worker.
        try:
            return {"id": request.get("id"), "status": "success", "result": self.dispatch(request)}
        except Exception as exc:
            return {"id": request.get("id"), "status": "error",
                    "error": u"%s: %s" % (type(exc).__name__, text_value(exc)),
                    "traceback": traceback.format_exc()}

    def serve(self):
        while self.running:
            try:
                peer, _address = self.socket.accept()
            except socket.timeout:
                continue
            except socket.error:
                break
            worker = threading.Thread(target=self.handle, args=(peer,))
            worker.daemon = True
            worker.start()

    def stop(self):
        self.running = False
        self.socket.close()
        if os.path.isfile(self.record):
            os.remove(self.record)


def start(port=None, data_root=None):
    """Explicit port conflicts fail; default scans 18770..18789 for a free bind."""
    global _bridge
    if threading.current_thread().name != "MainThread":
        raise RuntimeError("Start Maya bridge on the main thread")
    if _bridge is not None and _bridge.running:
        requested = port if port is not None else os.environ.get("FX_MAYA_PORT")
        if requested is not None and int(requested) != _bridge.port:
            raise ValueError("Maya bridge already running on port %s; stop it before changing ports" % _bridge.port)
        return identity(_bridge.port)
    configured = os.environ.get("FX_MAYA_PORT") if port is None else port
    ports = [int(configured)] if configured is not None else range(18770, 18790)
    root = data_root or os.environ.get("FX_AGENT_DATA_ROOT") or os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "data"))
    if isinstance(root, bytes):
        root = root.decode(sys.getfilesystemencoding() or "utf-8")
    if not os.path.isabs(root):
        raise ValueError("FX_AGENT_DATA_ROOT must be absolute")
    candidate = None
    for candidate_port in ports:
        if not 1 <= candidate_port <= 65535 or candidate_port in (18186, 8187):
            raise ValueError("Invalid or reserved DCC port")
        try:
            candidate = Bridge(candidate_port, root)
            break
        except socket.error:
            if configured is not None:
                raise
    if candidate is None:
        raise RuntimeError("No free Maya bridge port in 18770..18789")
    try:
        info = identity(candidate.port)
        folder = os.path.dirname(candidate.record)
        if not os.path.isdir(folder):
            try:
                os.makedirs(folder)
            except OSError:
                if not os.path.isdir(folder):
                    raise
        with open(candidate.record, "wb") as stream:
            stream.write(json.dumps(info, ensure_ascii=True).encode("utf-8"))
        candidate.thread.start()
    except Exception:
        candidate.stop()
        raise
    _bridge = candidate
    return info


def stop():
    global _bridge
    if _bridge is not None:
        _bridge.stop()
        _bridge = None
