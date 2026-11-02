"""No popup: menu state and lifecycle controls operate the existing bridge."""

import importlib.util
import io
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


class Menu:
    def __init__(self):
        self.menus, self.commands = {}, {}
        self.clear_count = 0

    def findItem(self, name):
        return self.menus.get(name)

    def addMenu(self, name):
        self.menus[name] = Menu()
        return self.menus[name]

    def clearMenu(self):
        self.clear_count += 1
        self.commands.clear()

    def addCommand(self, label, callback):
        item = SimpleNamespace(callback=callback, setEnabled=Mock())
        self.commands[label] = item
        return item

    def addSeparator(self):
        pass


@pytest.fixture
def environment(monkeypatch):
    monkeypatch.setattr(sys, "path", sys.path.copy())
    terminal = io.StringIO()
    monkeypatch.setattr(sys, "__stdout__", terminal)
    root_menu = Menu()

    class Bridge:
        instances = []

        def __init__(self, port):
            self.port, self._running = port, False
            self.on_log = Mock()
            self._thread = SimpleNamespace(alive=False, is_alive=lambda: self._thread.alive)
            self._server_sock = None
            Bridge.instances.append(self)

        def _run_in_nuke(self, callback, *args):
            return None

        def _serve(self):
            pass

        def _handle_client(self, client):
            pass

        def start(self):
            self._running, self._thread.alive = True, True
            self._server_sock = Mock()
            self._server_sock.fileno.return_value = 1
            self._server_sock.getsockname.return_value = ("127.0.0.1", self.port)

        def stop(self):
            self._running, self._thread.alive = False, False

    host = SimpleNamespace(GUI=True, menu=lambda name: root_menu, tprint=Mock())
    addon = SimpleNamespace(
        NukeMCPServer=Bridge,
        _get_nuke=lambda: host,
        _panel=None,
        _fx_agent_timer=object(),
        Qt=SimpleNamespace(QueuedConnection=2),
        NukeMCPPanel=Mock(side_effect=AssertionError("Popup must never be created")),
    )
    monkeypatch.setitem(sys.modules, "fx_agent_nuke_addon", addon)
    path = Path(__file__).resolve().parents[1] / "integrations/nuke/fx_agent_nuke.py"
    spec = importlib.util.spec_from_file_location("fx_nuke_menu_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, addon, host, root_menu, terminal


def test_auto_start_menu_controls_and_original_terminal_logging(environment, monkeypatch):
    module, addon, host, root, terminal = environment
    redirected = io.StringIO()
    monkeypatch.setattr(sys, "stdout", redirected)
    module.start()
    menu = root.menus["MCP Server"]
    server = addon._fx_agent_server
    assert any("Running" in label and "8187" in label for label in menu.commands)
    assert menu.commands["Start"].setEnabled.call_args.args == (False,)
    addon.NukeMCPPanel.assert_not_called()
    module.start()
    assert len(addon.NukeMCPServer.instances) == 1
    server.on_log.connect.assert_called_once()
    server.on_log.connect.call_args.args[0]("Client connected")
    assert "[FX Agent Nuke] Client connected" in terminal.getvalue()
    assert redirected.getvalue() == ""
    host.tprint.assert_not_called()
    client = Mock()
    addon._event_client_socket = client
    menu.commands["Stop"].callback()
    client.shutdown.assert_called_once()
    client.close.assert_called_once()
    assert any("Stopped" in label for label in menu.commands)
    assert menu.commands["Stop"].setEnabled.call_args.args == (False,)
    menu.commands["Start"].callback()
    assert addon._fx_agent_server is not server
    assert any("Running" in label for label in menu.commands)


def test_menu_preserves_other_plugin_entries_and_reports_listener_failure(environment):
    module, addon, _, root, _ = environment
    existing = root.addMenu("MCP Server")
    existing.addCommand("Other MCP", lambda: None)
    module.start()
    assert "Other MCP" in existing.commands
    assert existing.clear_count == 0
    own = existing.menus["FX Agent"]
    addon._fx_agent_server._thread.alive = False
    module._refresh_menu()
    assert any("Error" in label for label in own.commands)
    assert own.commands["Start"].setEnabled.call_args.args == (True,)


def test_hot_reload_adopts_legacy_panel_without_stopping_its_server(environment):
    module, addon, _, _, _ = environment
    service = addon.NukeMCPServer(8187)
    service.start()
    panel = SimpleNamespace(
        _port=8187, _server=service, _append_log=lambda msg: None, hide=Mock(), deleteLater=Mock()
    )
    addon._panel = panel
    module.start()
    assert addon._panel is None
    assert addon._fx_agent_server is service and service._running
    assert panel._server is None
    panel.hide.assert_called_once()
    panel.deleteLater.assert_called_once()


def test_early_stop_always_closes_a_late_created_listener(environment):
    import socket
    import threading

    module, addon, _, _, _ = environment
    entered, proceed = threading.Event(), threading.Event()

    class RacingBridge:
        _running = True
        _server_sock = None

        def _serve(self):
            entered.set()
            assert proceed.wait(2)
            self._server_sock = socket.socket()
            self._server_sock.bind(("127.0.0.1", 0))
            self._server_sock.listen()
            self.listener = self._server_sock
            # Upstream exits without closing when Stop won the startup race.

        def _handle_client(self, client):
            pass

    addon.NukeMCPServer = RacingBridge
    module._install_lifecycle(addon)
    server = RacingBridge()
    thread = threading.Thread(target=server._serve)
    thread.start()
    assert entered.wait(2)
    server._running = False
    proceed.set()
    thread.join(2)
    assert not thread.is_alive()
    assert server.listener.fileno() == -1
    assert server._server_sock is None


def test_handshake_error_closes_client_and_clears_only_its_own_reference(environment):
    module, addon, _, _, _ = environment
    client = Mock()

    class FailingBridge:
        _running = True

        def _serve(self):
            pass

        def _handle_client(self, sock):
            addon._event_client_socket = sock
            raise RuntimeError("handshake failed after stop")

    addon.NukeMCPServer = FailingBridge
    module._install_lifecycle(addon)
    with pytest.raises(RuntimeError):
        FailingBridge()._handle_client(client)
    client.close.assert_called_once()
    assert addon._event_client_socket is None


def test_stopped_server_rejects_queued_main_thread_operations(environment):
    module, addon, host, _, _ = environment
    host.executeInMainThreadWithResult = lambda callback, args: callback(*args)
    module.start()
    server = addon._fx_agent_server
    operation = Mock()
    module.stop()
    with pytest.raises(RuntimeError, match="stopped"):
        server._run_in_nuke(operation)
    operation.assert_not_called()
