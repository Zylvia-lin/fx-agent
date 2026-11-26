"""FX Agent port-safe variant of fxhoudinimcp 2.25.0 startup (MIT; see LICENSE).

Handles starting/stopping the hwebserver and loading handler modules.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import urllib.parse
import urllib.request

# Built-in
from contextlib import suppress

_server_started = False
_port = 8100
_state_lock = threading.Lock()

# True while an auto-start readiness check is still in flight on a worker
# thread, so a menu click during startup does not start a second server.
_starting = False
_stop_requested = False

# Ceiling for the readiness poll. A healthy start answers in well under a
# second, since mcp.health needs nothing from the main thread; the old 3s was
# tight only because the health endpoint used to deadlock against this very
# loop. Generous now that auto-start no longer waits on the main thread.
_READINESS_TIMEOUT = 15.0

# How many ports to try from the configured base. A second Houdini used to fail
# outright with "port 8100 is owned by another Houdini process", leaving that
# session with no MCP at all. Sixteen covers more concurrent sessions than
# anyone runs while keeping the failed-probe cost bounded.
_PORT_SEARCH_RANGE = 16


def _health_url(port: int) -> str:
    bind = (os.environ.get("FXHOUDINIMCP_BIND") or "127.0.0.1").strip()
    if bind in ("", "0.0.0.0", "::", "*"):
        host = "127.0.0.1"
    elif ":" in bind and not bind.startswith("["):
        host = f"[{bind}]"
    else:
        host = bind
    return f"http://{host}:{port}/api"


def _health_body() -> bytes:
    return urllib.parse.urlencode({"json": json.dumps(["mcp.health", [], {}])}).encode("utf-8")


def _query_health(port: int, timeout: float = 0.5) -> dict | None:
    headers = {"Content-Type": "application/x-www-form-urlencoded", "X-FX-Client": "fx-agent"}
    token = os.environ.get("FX_AGENT_HOUDINI_TOKEN")
    if token:
        headers["X-FX-Houdini-Token"] = token
    request = urllib.request.Request(
        _health_url(port),
        data=_health_body(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8")
    except Exception:
        return None

    try:
        data = json.loads(payload)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _wait_for_current_process_health(
    port: int,
    timeout_seconds: float = _READINESS_TIMEOUT,
) -> dict | None:
    deadline = time.time() + max(0.0, timeout_seconds)
    current_pid = os.getpid()
    last_health = None
    while time.time() < deadline:
        health = _query_health(port)
        if health is not None:
            last_health = health
            if health.get("pid") == current_pid:
                return health
        time.sleep(0.1)
    return last_health


def _pick_free_port(
    base: int,
    probe=None,
    my_pid: int | None = None,
    max_tries: int = _PORT_SEARCH_RANGE,
    can_bind=None,
) -> int:
    """Return the first port at or after *base* this process can serve on.

    A port is free only when the OS allows an exclusive bind. A port answering
    as *this* process is returned as-is, so restarting the server in a session
    that already has one is idempotent rather than a move to the next port. A
    port owned by a different pid is another Houdini and is skipped.

    Health distinguishes this process from other listeners; unrelated servers
    (including Nuke) must never be mistaken for unused sockets.
    """
    if probe is None:
        probe = _query_health
    if my_pid is None:
        my_pid = os.getpid()
    if can_bind is None:
        can_bind = _can_bind_port
    if not 1 <= base <= 65535 or max_tries < 1:
        raise ValueError("Invalid port search range")

    for port in range(base, min(base + max_tries, 65536)):
        health = probe(port)
        if health is not None and health.get("pid") == my_pid:
            return port
        if health is None and can_bind(port):
            return port
    raise RuntimeError(
        f"No available port in {base}-{min(base + max_tries - 1, 65535)}: "
        "ports are occupied or unavailable for binding."
    )


def _can_bind_port(port: int) -> bool:
    """Check the configured listen address without reusing another listener.

    Close the probe immediately; start() must still handle a process taking the
    port between this check and hwebserver.run(). No listener is stopped.
    """
    address = os.environ.get("FXHOUDINIMCP_BIND", "127.0.0.1")
    try:
        addresses = socket.getaddrinfo(address, port, type=socket.SOCK_STREAM)
        for family, kind, protocol, _, endpoint in addresses:
            with socket.socket(family, kind, protocol) as candidate:
                if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    candidate.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                candidate.bind(endpoint)
    except OSError:
        return False
    return True


def _run_on_available_port(hwebserver, base: int, background: bool):
    """Retry bind races within the original bounded range, on the calling thread."""
    global _port
    end = min(base + _PORT_SEARCH_RANGE, 65536)
    candidate = base
    while candidate < end:
        _port = _pick_free_port(candidate, max_tries=end - candidate)
        if _port != base:
            print(f"[fxhoudinimcp] Port {base} unavailable; trying port {_port}.")
        try:
            hwebserver.run(_port, debug=False, in_background=background)
            return None
        except Exception as exc:
            health = _query_health(_port)
            if health is not None and health.get("pid") == os.getpid():
                return exc  # Existing server in this process; confirm readiness normally.
            if _can_bind_port(_port):
                return exc  # Not a bind conflict; preserve the original failure report.
            print(f"[fxhoudinimcp] Port {_port} became unavailable; trying the next port.")
            candidate = _port + 1
    raise RuntimeError(f"No available port in {base}-{end - 1}; server not started.")


def _bind_localhost_only(hwebserver) -> None:
    """Restrict the server to loopback before it starts listening (fail-closed).

    hwebserver binds the any-address (0.0.0.0) by default, which would put
    this bridge on the LAN. Failing to restrict the bind address aborts startup
    instead of exposing arbitrary Python execution on the network.

    Set FXHOUDINIMCP_BIND to override deliberately.
    """
    address = os.environ.get("FXHOUDINIMCP_BIND", "127.0.0.1")
    try:
        # Note the argument order: (settings, port_name). Passing the port
        # number first raises AttributeError on 'int'.
        hwebserver.setSettingsForPort({"ADDRESS": address}, "main")
    except Exception as exc:
        raise RuntimeError(
            f"Refusing to start Houdini bridge: failed to restrict bind address to {address}: {exc}"
        ) from exc


def _install_csrf_guard(hwebserver_app) -> None:
    """Reject browser cross-origin CSRF POSTs (e.g. <form> simple requests) to /api."""
    if hwebserver_app is None or getattr(hwebserver_app, "_fx_csrf_guarded", False):
        return
    import hmac

    def _reject_browser_request(request) -> str | None:
        headers = {}
        raw_headers = getattr(request, "headers", None)
        if callable(raw_headers):
            raw_headers = raw_headers()
        if isinstance(raw_headers, dict):
            headers = {str(k).lower(): str(v) for k, v in raw_headers.items()}
        # Browsers always send Origin or Sec-Fetch-Site on cross-origin POST/form submissions.
        if headers.get("origin") or headers.get("sec-fetch-site", "").lower() in {
            "cross-site",
            "same-site",
        }:
            return "Cross-origin browser requests to Houdini MCP bridge are forbidden"
        expected_token = os.environ.get("FX_AGENT_HOUDINI_TOKEN")
        if expected_token:
            supplied = headers.get("x-fx-houdini-token", "")
            if not hmac.compare_digest(supplied, expected_token):
                return "Missing or invalid X-FX-Houdini-Token"
        return None

    orig_foreign = getattr(hwebserver_app, "_foreign_request_reason", None)
    if callable(orig_foreign):
        def _guarded_foreign(request, _orig=orig_foreign):
            reason = _orig(request)
            if reason is not None:
                return reason
            return _reject_browser_request(request)

        with suppress(Exception):
            hwebserver_app._foreign_request_reason = _guarded_foreign

    reregister = getattr(hwebserver_app, "_api_function", None)
    forbidden = getattr(hwebserver_app, "_forbidden", None)
    for endpoint_name in ("session_info", "list_commands"):
        fn = getattr(hwebserver_app, endpoint_name, None)
        if callable(fn):
            def _wrap_endpoint(original):
                def _guarded(request, *args, **kwargs):
                    reason = _reject_browser_request(request)
                    if reason:
                        if callable(forbidden):
                            return forbidden(reason)
                        raise PermissionError(reason)
                    return original(request, *args, **kwargs)

                _guarded.__name__ = original.__name__
                return _guarded

            wrapped = _wrap_endpoint(fn)
            with suppress(Exception):
                setattr(hwebserver_app, endpoint_name, wrapped)
                if callable(reregister):
                    reregister("mcp")(wrapped)

    for attr in dir(hwebserver_app):
        fn = getattr(hwebserver_app, attr, None)
        if callable(fn) and (attr in {"api", "handle_api", "api_handler"} or attr.endswith("_handler")):
            def _wrap(original):
                def _guarded(request, *args, **kwargs):
                    reason = _reject_browser_request(request)
                    if reason:
                        raise PermissionError(reason)
                    return original(request, *args, **kwargs)
                return _guarded
            with suppress(Exception):
                setattr(hwebserver_app, attr, _wrap(fn))
    hwebserver_app._fx_csrf_guarded = True


def start(
    port: int | None = None,
    background: bool | None = None,
    wait: bool = True,
) -> None:
    """Start the FXHoudini-MCP server.

    Registers all command handlers and ensures hwebserver is running.

    Must be called from the thread that will own the server. hwebserver keeps
    its ``Server`` object in a ``threading.local()``, so API functions
    registered on one thread are invisible to ``run()`` on another -- calling
    ``run()`` from a fresh thread fails outright with "No URL handlers have
    been added to the server."

    Args:
        port: Port for hwebserver. Defaults to FXHOUDINIMCP_PORT env var or 8100.
        background: Serve on a background thread instead of blocking. Defaults
            to Houdini's own choice, which is True in a UI session and False
            under hython. Pass True from a headless script that needs start()
            to return while the server keeps serving.
        wait: Block until the server answers, and raise if it does not. Pass
            False for auto-start, where nothing reads the result and blocking
            would stall Houdini's UI; readiness is then confirmed on a worker
            thread and failure is printed rather than raised.
    """
    global _server_started, _port, _starting, _stop_requested

    with _state_lock:
        if _server_started:
            print("[fxhoudinimcp] Server already running")
            return
        if _starting:
            print("[fxhoudinimcp] Server is still starting")
            return
        _stop_requested = False

    base = port or int(os.environ.get("FXHOUDINIMCP_PORT", "8100"))

    # Import handlers to trigger registration via register_handler() calls
    # Start hwebserver if not already running. In Houdini 20.5+ it may already
    # be running for built-in features; in that case registering the functions
    # above is enough. Either way, prove the HTTP endpoint is reachable before
    # advertising readiness.
    import hou
    import hwebserver

    # Import hwebserver_app to register the API functions
    from fxhoudinimcp_server import (
        handlers,  # noqa: F401
        hwebserver_app,  # noqa: F401
    )

    _install_csrf_guard(hwebserver_app)

    if background is None:
        background = hou.isUIAvailable()

    _bind_localhost_only(hwebserver)

    run_error = _run_on_available_port(hwebserver, base, background)

    if not background:
        with _state_lock:
            _server_started = False
        if run_error is not None:
            raise RuntimeError(f"hwebserver failed to start on port {_port}: {run_error}")
        return

    if wait:
        _confirm_ready(run_error)
        return

    with _state_lock:
        _starting = True
    worker = threading.Thread(target=_confirm_ready_async, args=(run_error,), daemon=True)
    try:
        worker.start()
    except Exception:
        with _state_lock:
            _starting = False
        raise


def _confirm_ready(run_error: Exception | None) -> None:
    """Poll until the server answers as this process, then mark it running.

    Raises on failure, so an explicit Start Server can report why.
    """
    global _server_started

    health = _wait_for_current_process_health(_port)
    if health is None:
        with _state_lock:
            _server_started = False
        detail = f": {run_error}" if run_error is not None else ""
        raise RuntimeError(f"hwebserver did not answer mcp.health on port {_port}{detail}")

    health_pid = health.get("pid")
    if health_pid != os.getpid():
        with _state_lock:
            _server_started = False
        raise RuntimeError(
            f"hwebserver port {_port} is owned by another Houdini process "
            f"(pid {health_pid}), current pid {os.getpid()}"
        )

    with _state_lock:
        if _stop_requested:
            _server_started = False
            return
        _server_started = True
    print(
        "[fxhoudinimcp] Server ready on port {} (Houdini {}, pid {})".format(
            _port,
            health.get("houdini_version", "unknown"),
            health.get("pid", "unknown"),
        )
    )


def _confirm_ready_async(run_error: Exception | None) -> None:
    """_confirm_ready for a daemon thread: reports instead of raising."""
    global _starting

    try:
        _confirm_ready(run_error)
    except Exception as exc:
        print(f"[fxhoudinimcp] Auto-start failed: {exc}")
    finally:
        with _state_lock:
            _starting = False


def stop() -> None:
    """Stop the FXHoudini-MCP server."""
    global _server_started, _stop_requested
    with _state_lock:
        _stop_requested = True
        if not _server_started and not _starting:
            return
        _server_started = False
    with suppress(Exception):
        import hwebserver

        shutdown = getattr(hwebserver, "requestShutdown", None)
        if callable(shutdown):
            shutdown()
    print("[fxhoudinimcp] Server stopped")


def is_running() -> bool:
    """Check if the server is currently running."""
    with _state_lock:
        return _server_started


def get_port() -> int:
    """Get the port the server is running on."""
    with _state_lock:
        return _port


def is_starting() -> bool:
    """True while an auto-start readiness check is still in flight."""
    with _state_lock:
        return _starting


def ensure_running(wait: bool = True) -> None:
    """Start the server if it's not already running.

    Args:
        wait: Passed through to start(). Auto-start uses False so Houdini's UI
            is never blocked by the readiness poll.
    """
    global _server_started
    if _starting:
        return
    if _server_started:
        health = _wait_for_current_process_health(_port, timeout_seconds=0.5)
        if health is not None and health.get("pid") == os.getpid():
            return
        _server_started = False
    start(wait=wait)
