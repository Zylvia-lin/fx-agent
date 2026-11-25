"""Load the pinned bridge in Nuke. This module has no external dependencies."""

import errno
import io
import json
import os
import socket
import sys
import time
import uuid
from importlib import import_module

from fx_agent_nuke_compat import (
    make_directory, reload_module, remove_file, replace_file, suppress, text_type,
)


def _dispatch_with_result(self, callback, *args):
    import fx_agent_nuke_addon

    nuke = fx_agent_nuke_addon._get_nuke()
    if not nuke.GUI:
        return self._fx_original_dispatch(callback, *args)

    def invoke():
        try:
            if not getattr(self, "_running", True):
                raise RuntimeError("Nuke MCP server has stopped")
            value = callback(*args)
            return {"ok": True, "value": json.loads(json.dumps(value, default=text_type))}
        except Exception as error:
            return {"ok": False, "error": '{}: {}'.format(type(error).__name__, error)}

    result = nuke.executeInMainThreadWithResult(invoke, args=())
    if not isinstance(result, dict) or "ok" not in result:
        raise RuntimeError("Nuke main-thread dispatch returned no result")
    if not result["ok"]:
        raise RuntimeError(result["error"])
    return result["value"]


def _write_console_log(message):
    stream = sys.__stdout__ or sys.__stderr__
    if stream is not None:
        stream.write('[FX Agent Nuke] {}\n'.format(message))
        stream.flush()


def _install_dispatch(addon):
    bridge = addon.NukeMCPServer
    if not hasattr(bridge, "_fx_original_dispatch"):
        bridge._fx_original_dispatch = bridge._run_in_nuke
    bridge._run_in_nuke = _dispatch_with_result


def _open_listener(port):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # Windows SO_REUSEADDR permits competing Nuke instances to share a port.
        if sys.platform == "win32":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.settimeout(1.0)
        listener.bind(("127.0.0.1", port))
        listener.listen(1)
        return listener
    except Exception:
        listener.close()
        raise


def _serve_exclusive(self):
    for port in range(self.port, min(self.port + (16 if getattr(self, "_fx_auto_port", False) else 1), 65536)):
        if port in (8100, 8186, 18186, 54321):
            continue
        try:
            listener = _open_listener(port)
            self.port = port
            break
        except (OSError, socket.error) as error:
            if error.errno not in (errno.EADDRINUSE, errno.EACCES, 10048, 10013):
                raise
    else:
        raise OSError("No free Nuke session port in the configured 16-port range")
    self._server_sock = listener
    self._emit_log('Listening exclusively on 127.0.0.1:{}'.format(self.port))
    while self._running:
        try:
            client, addr = listener.accept()
        except socket.timeout:
            continue
        except (OSError, socket.error):
            break
        self._emit_log('Client connected from {}:{}'.format(addr[0], addr[1]))
        self._handle_client(client)
        self._emit_log("Client disconnected")


def _serve_with_cleanup(self):
    try:
        return self._fx_original_serve()
    except Exception as error:
        if self._running:
            self._fx_error = True
            _write_console_log('Server error: {}: {}'.format(type(error).__name__, error))
    finally:
        self._running = False
        listener = self._server_sock
        if listener is not None:
            with suppress(OSError, socket.error):
                listener.close()
        self._server_sock = None


def _handle_client_with_cleanup(self, client):
    import fx_agent_nuke_addon as addon

    try:
        if self._running:
            with suppress(OSError, socket.error):
                client.settimeout(30.0)
            return self._fx_original_handle_client(client)
    finally:
        with suppress(OSError, socket.error):
            client.shutdown(socket.SHUT_RDWR)
        with suppress(OSError, socket.error):
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
    except (OSError, socket.error):
        return "Error"


def _refresh_menu():
    import fx_agent_nuke_addon as addon

    server = getattr(addon, "_fx_agent_server", None)
    state = _listener_state(server)
    if server is not None:
        addon._fx_agent_port = server.port
    port = addon._fx_agent_port
    _publish_session(addon, server, state)
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
    menu.addCommand('Status: {} | 127.0.0.1:{}'.format(state, port), lambda: None).setEnabled(False)
    menu.addSeparator()
    base = getattr(server, "_fx_base_port", port)
    auto = getattr(server, "_fx_auto_port", True)
    menu.addCommand("Start", lambda: start(base, auto_port=auto)).setEnabled(state in ("Stopped", "Error"))
    menu.addCommand("Stop", stop).setEnabled(state in ("Running", "Starting", "Error"))
    addon._fx_agent_menu_state = (state, port)


def _session_directory():
    root = os.getenv("FX_AGENT_LOCAL_ROOT") or os.getenv("FX_AGENT_DATA_ROOT")
    return os.path.join(root or os.path.expanduser('~/.fx-agent/cache'), 'nuke_sessions')


def _publish_session(addon, server, state):
    path = os.path.join(_session_directory(), '{}.json'.format(os.getpid()))
    try:
        if state != "Running":
            addon._fx_last_session_pub = None
            remove_file(path)
            return
        nuke = addon._get_nuke()
        script = nuke.root().name()
        now = time.time()
        last = getattr(addon, "_fx_last_session_pub", None)
        sig = (state, server.port, server._fx_session_id, script)
        # Avoid 1Hz disk churn: rewrite immediately on state/script change or every 5s heartbeat.
        if last is not None and last[0] == sig and (now - last[1]) < 5.0 and os.path.isfile(path):
            return
        record = {
            "pid": os.getpid(),
            "port": server.port,
            "session_id": server._fx_session_id,
            "script": script,
            "nuke_version": nuke.NUKE_VERSION_STRING,
            "updated_at": now,
        }
        make_directory(os.path.dirname(path))
        temporary = os.path.splitext(path)[0] + '.tmp'
        with io.open(temporary, 'w', encoding='utf-8') as stream:
            stream.write(text_type(json.dumps(record, ensure_ascii=True)))
        replace_file(temporary, path)
        addon._fx_last_session_pub = (sig, now)
    except (OSError, socket.error) as error:
        _write_console_log('Cannot publish Nuke session: {}'.format(error))


def _session_handshake():
    import fx_agent_nuke_addon as addon

    result = addon._fx_original_handshake()
    result["session_id"] = addon._fx_agent_server._fx_session_id
    return result


def _reload_tool_modules(params):
    import fx_agent_nuke_addon as addon

    for name in ("fx_agent_nuke_viewer", "fx_agent_nuke_graph"):
        reload_module(import_module(name)).install(addon)
    return {"status": "ok", "result": {
        "reloaded": ["fx_agent_nuke_viewer", "fx_agent_nuke_graph"],
        "pid": os.getpid(), "session_id": addon._fx_agent_server._fx_session_id,
    }}


def _install_timer(addon):
    timer = getattr(addon, "_fx_agent_timer", None)
    if timer is None:
        timer = import_module(addon.QObject.__module__).QTimer()
        timer.setInterval(1000)
        addon._fx_agent_timer = timer
    else:
        # Reload replaces function objects; the old callback lacks new registry logic.
        with suppress(RuntimeError):
            timer.timeout.disconnect()
    timer.timeout.connect(_refresh_menu)
    timer.start()


def _stop_bridge(addon, server):
    if server is not None:
        server.stop()
        server._fx_error = False
    client = getattr(addon, "_event_client_socket", None)
    if client is not None:
        with suppress(OSError, socket.error):
            client.shutdown(socket.SHUT_RDWR)
        with suppress(OSError, socket.error):
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


def start(port=8187, auto_port=True):
    """Auto-start the bridge, install menu controls, and never create a popup."""
    if not 1 <= port <= 65535 or port in (8100, 8186, 18186, 54321):
        raise ValueError("Choose a separate Nuke MCP port")
    vendor = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'vendor')
    if sys.version_info[0] == 2:
        vendor = os.path.join(vendor, 'python27')
    if vendor not in sys.path:
        sys.path.insert(0, vendor)
    import fx_agent_nuke_addon as addon

    if not hasattr(addon, "_fx_original_handshake"):
        addon._fx_original_handshake = addon._handshake_data
    addon._handshake_data = _session_handshake
    reload_module(import_module("fx_agent_nuke_viewer")).install(addon)
    reload_module(import_module("fx_agent_nuke_graph")).install(addon)
    # Remove disabled commands at the Nuke TCP bridge layer as well as MCP surface.
    addon.COMMANDS.pop("execute_python", None)
    addon.COMMANDS.pop("auto_layout", None)
    addon.COMMANDS["reload_nuke_tools"] = _reload_tool_modules
    _install_dispatch(addon)
    _install_lifecycle(addon)
    addon.NukeMCPServer._fx_original_serve = _serve_exclusive
    server = getattr(addon, "_fx_agent_server", None)
    panel = addon._panel  # Upgrade a running old bridge without restarting the scene.
    if panel is not None:
        if server is not None and panel._server is not None and server is not panel._server:
            raise RuntimeError("Multiple FX Nuke bridges exist; stop them before upgrading")
        server = server or panel._server
    if server is not None and getattr(server, "_fx_base_port", server.port) != port:
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
    if server is not None and not hasattr(server, "_fx_session_id"):
        server._fx_session_id = uuid.uuid4().hex
    state = _listener_state(server)
    if state == "Stopping":
        _write_console_log("Previous connection is stopping; wait before starting again")
    elif state not in ("Running", "Starting"):
        _stop_bridge(addon, server)
        server = addon.NukeMCPServer(port=port)
        server._fx_base_port = port
        server._fx_auto_port = auto_port
        server._fx_session_id = uuid.uuid4().hex
        addon._fx_agent_server = server
        _attach_console(addon, server)
        server.start()
        _write_console_log('Server starting on 127.0.0.1:{}'.format(port))
    elif server is not None:
        _attach_console(addon, server)
    _refresh_menu()
    _install_timer(addon)
