"""Load the pinned bridge in Nuke 15.2. This module has no external dependencies."""

import socket
import sys
from contextlib import suppress
from importlib import import_module
from pathlib import Path


def _dispatch_with_result(self, callback, *args):
    import fx_agent_nuke_addon

    nuke = fx_agent_nuke_addon._get_nuke()
    if not nuke.GUI:
        return self._fx_original_dispatch(callback, *args)

    def invoke():
        try:
            if not getattr(self, "_running", True):
                raise RuntimeError("Nuke MCP server has stopped")
            return {"ok": True, "value": callback(*args)}
        except Exception as error:
            return {"ok": False, "error": f"{type(error).__name__}: {error}"}

    result = nuke.executeInMainThreadWithResult(invoke, args=())
    if not isinstance(result, dict) or "ok" not in result:
        raise RuntimeError("Nuke main-thread dispatch returned no result")
    if not result["ok"]:
        raise RuntimeError(result["error"])
    return result["value"]


def _write_console_log(message):
    stream = sys.__stdout__ or sys.__stderr__
    if stream is not None:
        print(f"[FX Agent Nuke] {message}", file=stream, flush=True)


def _install_dispatch(addon):
    bridge = addon.NukeMCPServer
    if not hasattr(bridge, "_fx_original_dispatch"):
        bridge._fx_original_dispatch = bridge._run_in_nuke
    bridge._run_in_nuke = _dispatch_with_result


def _serve_with_cleanup(self):
    try:
        return self._fx_original_serve()
    except Exception as error:
        if self._running:
            self._fx_error = True
            _write_console_log(f"Server error: {type(error).__name__}: {error}")
    finally:
        self._running = False
        listener = self._server_sock
        if listener is not None:
            with suppress(OSError):
                listener.close()
        self._server_sock = None


def _handle_client_with_cleanup(self, client):
    import fx_agent_nuke_addon as addon

    try:
        if self._running:
            return self._fx_original_handle_client(client)
    finally:
        with suppress(OSError):
            client.shutdown(socket.SHUT_RDWR)
        with suppress(OSError):
            client.close()
        if getattr(addon, "_event_client_socket", None) is client:
            addon._event_client_socket = None


def _install_lifecycle(addon):
    bridge = addon.NukeMCPServer
    for name, replacement in (
        ("_serve", _serve_with_cleanup),
        ("_handle_client", _handle_client_with_cleanup),
    ):
        original = "_fx_original" + name
        if not hasattr(bridge, original):
            setattr(bridge, original, getattr(bridge, name))
        setattr(bridge, name, replacement)


def _listener_state(server):
    if server is None:
        return "Stopped"
    alive = bool(server._thread and server._thread.is_alive())
    if not alive and getattr(server, "_fx_error", False):
        return "Error"
    if not server._running:
        return "Stopping" if alive else "Stopped"
    if not alive:
        return "Error"
    listener = server._server_sock
    if listener is None:
        return "Starting"
    try:
        return "Running" if listener.getsockname()[1] == server.port else "Starting"
    except OSError:
        return "Error"


def _refresh_menu():
    import fx_agent_nuke_addon as addon

    state = _listener_state(getattr(addon, "_fx_agent_server", None))
    port = addon._fx_agent_port
    if not getattr(addon, "_fx_agent_menu", None):
        root = addon._get_nuke().menu("Nuke")
        existing = root.findItem("MCP Server")
        if existing and hasattr(existing, "addMenu"):
            addon._fx_agent_menu = existing.addMenu("FX Agent")
        else:
            addon._fx_agent_menu = root.addMenu(
                "MCP Server" if not existing else "FX Agent MCP Server"
            )
    if getattr(addon, "_fx_agent_menu_state", None) == (state, port):
        return
    menu = addon._fx_agent_menu
    menu.clearMenu()
    menu.addCommand(f"Status: {state} | 127.0.0.1:{port}", lambda: None).setEnabled(False)
    menu.addSeparator()
    menu.addCommand("Start", lambda: start(port)).setEnabled(state in ("Stopped", "Error"))
    menu.addCommand("Stop", stop).setEnabled(state in ("Running", "Starting", "Error"))
    addon._fx_agent_menu_state = (state, port)


def _install_timer(addon):
    if not getattr(addon, "_fx_agent_timer", None):
        timer = import_module(addon.QObject.__module__).QTimer()
        timer.setInterval(1000)
        timer.timeout.connect(_refresh_menu)
        addon._fx_agent_timer = timer
        timer.start()


def _stop_bridge(addon, server):
    if server is not None:
        server.stop()
        server._fx_error = False
    client = getattr(addon, "_event_client_socket", None)
    if client is not None:
        with suppress(OSError):
            client.shutdown(socket.SHUT_RDWR)
        with suppress(OSError):
            client.close()
        addon._event_client_socket = None


def _attach_console(addon, server):
    previous = getattr(server, "_fx_console_callback", None)
    if previous is _write_console_log:
        return
    if previous is not None:
        with suppress(RuntimeError, TypeError):
            server.on_log.disconnect(previous)
    server.on_log.connect(_write_console_log, addon.Qt.QueuedConnection)
    server._fx_console_callback = _write_console_log


def stop():
    """Stop listening and close the active MCP client, preserving the Nuke script."""
    import fx_agent_nuke_addon as addon

    _stop_bridge(addon, getattr(addon, "_fx_agent_server", None))
    _write_console_log("Server stopped")
    _refresh_menu()


def start(port=8187):
    """Auto-start the bridge, install menu controls, and never create a popup."""
    if not 1 <= port <= 65535 or port in (8100, 8186, 54321):
        raise ValueError("Choose a separate Nuke MCP port")
    vendor = str(Path(__file__).resolve().parent / "vendor")
    if vendor not in sys.path:
        sys.path.insert(0, vendor)
    import fx_agent_nuke_addon as addon

    _install_dispatch(addon)
    _install_lifecycle(addon)
    server = getattr(addon, "_fx_agent_server", None)
    panel = addon._panel  # Upgrade a running old bridge without restarting the scene.
    if panel is not None:
        if server is not None and panel._server is not None and server is not panel._server:
            raise RuntimeError("Multiple FX Nuke bridges exist; stop them before upgrading")
        server = server or panel._server
    if server is not None and server.port != port:
        raise ValueError("Restart Nuke before changing its bridge port")
    addon._fx_agent_port = port
    if panel is not None:
        if panel._server is not None:
            with suppress(RuntimeError, TypeError):
                panel._server.on_log.disconnect(panel._append_log)
        panel._server = None
        panel.hide()
        panel.deleteLater()
        addon._panel = None
    addon._fx_agent_server = server
    state = _listener_state(server)
    if state == "Stopping":
        _write_console_log("Previous connection is stopping; wait before starting again")
    elif state not in ("Running", "Starting"):
        _stop_bridge(addon, server)
        server = addon.NukeMCPServer(port=port)
        addon._fx_agent_server = server
        _attach_console(addon, server)
        server.start()
        _write_console_log(f"Server starting on 127.0.0.1:{port}")
    elif server is not None:
        _attach_console(addon, server)
    _refresh_menu()
    _install_timer(addon)
