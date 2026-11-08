"""FX Agent port-safe variant of fxhoudinimcp 2.25.0 startup (MIT; see LICENSE).

Handles starting/stopping the hwebserver and loading handler modules.
"""

from __future__ import annotations

# Built-in
import json
import os
import socket
import threading
import time
import urllib.parse
import urllib.request

_server_started = False
_port = 8100

# True while an auto-start readiness check is still in flight on a worker
# thread, so a menu click during startup does not start a second server.
_starting = False

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
    return f"http://127.0.0.1:{port}/api"


def _health_body() -> bytes:
    return urllib.parse.urlencode({"json": json.dumps(["mcp.health", [], {}])}).encode("utf-8")


def _query_health(port: int, timeout: float = 0.5) -> dict | None:
    request = urllib.request.Request(
        _health_url(port),
        data=_health_body(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
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
    """Restrict the server to loopback before it starts listening.

    hwebserver binds the any-address (0.0.0.0) by default, which would put
    this bridge on the LAN. That matters more here than for a typical web
    endpoint: the bridge runs arbitrary Python inside Houdini (see
    handlers/code_handlers.py) and has no authentication, so anyone able to
    reach the port has the session.

    Set FXHOUDINIMCP_BIND to override, e.g. "0.0.0.0" to accept remote
    connections deliberately.
    """
    address = os.environ.get("FXHOUDINIMCP_BIND", "127.0.0.1")
    try:
        # Note the argument order: (settings, port_name). Passing the port
        # number first raises AttributeError on 'int'.
        hwebserver.setSettingsForPort({"ADDRESS": address}, "main")
    except Exception as exc:
        print(
            f"[fxhoudinimcp] Warning: could not restrict bind address to "
            f"{address}: {exc}. The port may be reachable from the network."
        )


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
    global _server_started, _port, _starting

    if _server_started:
        print("[fxhoudinimcp] Server already running")
        return
    if _starting:
        print("[fxhoudinimcp] Server is still starting")
        return

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

    if background is None:
        # hwebserver.run() already defaults in_background to isUIAvailable(),
        # so this matches its behaviour; it is passed explicitly so the choice
        # is visible here and does not silently change under us. Blocking in a
        # UI session would wedge Houdini's main thread; blocking under hython
        # is what keeps the process alive to serve.
        background = hou.isUIAvailable()

    _bind_localhost_only(hwebserver)

    run_error = _run_on_available_port(hwebserver, base, background)

    if not background:
        # run() blocks until shutdown when serving in the foreground, so
        # reaching this point means it either finished or never started.
        _server_started = False
        if run_error is not None:
            raise RuntimeError(f"hwebserver failed to start on port {_port}: {run_error}")
        return

    if wait:
        _confirm_ready(run_error)
        return

    # Auto-start: nobody is waiting on a return value, so do not make Houdini's
    # main thread sit through the poll. The worker only does urllib and
    # os.getpid(), never hou.*, which is safe off the main thread and is exactly
    # why mcp.health had to become HOM-free.
    _starting = True
    worker = threading.Thread(target=_confirm_ready_async, args=(run_error,), daemon=True)
    try:
        worker.start()
    except Exception:
        # The thread never ran, so nothing else will clear this.
        _starting = False
        raise


def _confirm_ready(run_error: Exception | None) -> None:
    """Poll until the server answers as this process, then mark it running.

    Raises on failure, so an explicit Start Server can report why.
    """
    global _server_started

    health = _wait_for_current_process_health(_port)
    if health is None:
        _server_started = False
        detail = f": {run_error}" if run_error is not None else ""
        raise RuntimeError(f"hwebserver did not answer mcp.health on port {_port}{detail}")

    health_pid = health.get("pid")
    if health_pid != os.getpid():
        _server_started = False
        raise RuntimeError(
            f"hwebserver port {_port} is owned by another Houdini process "
            f"(pid {health_pid}), current pid {os.getpid()}"
        )

    _server_started = True
    print(
        "[fxhoudinimcp] Server ready on port {} (Houdini {}, pid {})".format(
            _port,
            health.get("houdini_version", "unknown"),
            health.get("pid", "unknown"),
        )
    )


def _confirm_ready_async(run_error: Exception | None) -> None:
    """_confirm_ready for a daemon thread: reports instead of raising.

    An exception here would die unheard in the worker, so the failure is printed
    in the same shape auto-start used to raise. _starting is always cleared, or
    a failed start would leave the server permanently un-startable from the menu.
    """
    global _starting

    try:
        _confirm_ready(run_error)
    except Exception as exc:
        print(f"[fxhoudinimcp] Auto-start failed: {exc}")
    finally:
        _starting = False


def stop() -> None:
    """Stop the FXHoudini-MCP server."""
    global _server_started
    if not _server_started:
        return

    # Note: we don't call hwebserver.requestShutdown() because that would
    # kill Houdini's built-in web server too. We just mark ourselves as stopped.
    _server_started = False
    print("[fxhoudinimcp] Server stopped")


def is_running() -> bool:
    """Check if the server is currently running."""
    return _server_started


def get_port() -> int:
    """Get the port the server is running on."""
    return _port


def is_starting() -> bool:
    """True while an auto-start readiness check is still in flight."""
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
