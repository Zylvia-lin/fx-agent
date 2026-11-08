"""Exercise port selection with real sockets and startup races without a DCC."""

import importlib.util
import os
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from fx_agent.houdini_runtime import STARTUP, prepare_runtime, runtime_path

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def startup():
    spec = importlib.util.spec_from_file_location(
        "fx_startup_test", ROOT / "integrations/houdini/startup/startup.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_real_non_houdini_listener_is_not_free(startup, monkeypatch):
    monkeypatch.setenv("FXHOUDINIMCP_BIND", "127.0.0.1")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        assert not startup._can_bind_port(port)
        with pytest.raises(RuntimeError, match="occupied or unavailable"):
            startup._pick_free_port(port, probe=lambda p: None, max_tries=1)
    assert startup._can_bind_port(port)


def test_skip_houdini_then_nuke_then_choose_free_port(startup):
    def health(port):
        return {"pid": -1} if port == 8186 else None

    assert startup._pick_free_port(8186, probe=health, can_bind=lambda p: p >= 8188) == 8188


def test_current_process_keeps_its_existing_server(startup):
    def must_not_bind(port):
        pytest.fail("An existing server must not be rebound")

    assert (
        startup._pick_free_port(
            8186, probe=lambda p: {"pid": 42}, my_pid=42, can_bind=must_not_bind
        )
        == 8186
    )


def test_search_is_bounded_and_does_not_overflow(startup):
    tried = []
    with pytest.raises(RuntimeError, match="65534-65535"):
        startup._pick_free_port(65534, probe=lambda p: tried.append(p), can_bind=lambda p: False)
    assert tried == [65534, 65535]


@pytest.mark.parametrize("port", [0, -1, 65536])
def test_invalid_base_is_rejected(startup, port):
    with pytest.raises(ValueError, match="Invalid port"):
        startup._pick_free_port(port)


def test_probe_honors_configured_address(startup, monkeypatch):
    monkeypatch.setenv("FXHOUDINIMCP_BIND", "invalid address !")
    assert not startup._can_bind_port(8186)


def test_bind_race_continues_to_next_port(startup, monkeypatch):
    attempts = []
    monkeypatch.setattr(startup, "_pick_free_port", lambda base, **kw: base)
    monkeypatch.setattr(startup, "_query_health", lambda port: None)
    monkeypatch.setattr(startup, "_can_bind_port", lambda port: False)

    def run(port, **kwargs):
        attempts.append((port, kwargs))
        if port == 8186:
            raise OSError("port taken after preflight")

    assert startup._run_on_available_port(SimpleNamespace(run=run), 8186, True) is None
    assert [p for p, _ in attempts] == [8186, 8187]
    assert startup.get_port() == 8187
    assert attempts[-1][1] == {"debug": False, "in_background": True}


def test_unrelated_start_failure_is_not_hidden_by_port_retry(startup, monkeypatch):
    error = RuntimeError("handler initialization failed")
    attempts = []
    monkeypatch.setattr(startup, "_pick_free_port", lambda base, **kw: base)
    monkeypatch.setattr(startup, "_query_health", lambda port: None)
    monkeypatch.setattr(startup, "_can_bind_port", lambda port: True)

    def run(port, **kwargs):
        attempts.append(port)
        raise error

    assert startup._run_on_available_port(SimpleNamespace(run=run), 8186, True) is error
    assert attempts == [8186]


def test_existing_hwebserver_in_this_process_is_preserved(startup, monkeypatch):
    error = RuntimeError("already running")
    monkeypatch.setattr(startup, "_pick_free_port", lambda base, **kw: base)
    monkeypatch.setattr(startup, "_query_health", lambda port: {"pid": os.getpid()})

    def run(*args, **kwargs):
        raise error

    assert startup._run_on_available_port(SimpleNamespace(run=run), 8186, True) is error
    assert startup.get_port() == 8186


def test_all_bind_races_stop_at_range_limit(startup, monkeypatch):
    attempts = []
    monkeypatch.setattr(startup, "_pick_free_port", lambda base, **kw: base)
    monkeypatch.setattr(startup, "_query_health", lambda port: None)
    monkeypatch.setattr(startup, "_can_bind_port", lambda port: False)

    def run(port, **kwargs):
        attempts.append(port)
        raise OSError("occupied")

    with pytest.raises(RuntimeError, match="8186-8201"):
        startup._run_on_available_port(SimpleNamespace(run=run), 8186, True)
    assert attempts == list(range(8186, 8202))
    assert not startup.is_running()


@pytest.mark.parametrize("background", [True, False])
def test_start_uses_retry_and_only_confirms_actual_port(startup, monkeypatch, background):
    monkeypatch.setitem(sys.modules, "hou", SimpleNamespace(isUIAvailable=lambda: True))
    fake_web = SimpleNamespace(setSettingsForPort=lambda *args: None)
    monkeypatch.setitem(sys.modules, "hwebserver", fake_web)
    monkeypatch.setitem(
        sys.modules, "fxhoudinimcp_server", SimpleNamespace(handlers=None, hwebserver_app=None)
    )
    confirmed = []

    def launch(web, base, bg):
        assert web is fake_web and base == 8186 and bg is background
        startup._port = 8188

    monkeypatch.setattr(startup, "_run_on_available_port", launch)
    monkeypatch.setattr(startup, "_confirm_ready", lambda error: confirmed.append(startup._port))
    startup.start(port=8186, background=background)
    assert startup.get_port() == 8188
    assert confirmed == ([8188] if background else [])


def test_runtime_preview_is_read_only_and_apply_preserves_upstream(tmp_path):
    source = tmp_path / "upstream"
    (source / STARTUP).parent.mkdir(parents=True)
    (source / STARTUP).write_text("original startup", encoding="utf8")
    (source / "MainMenuCommon.xml").write_text("menu", encoding="utf8")
    root = tmp_path / "checkout"
    patch = root / "integrations/houdini/startup"
    patch.mkdir(parents=True)
    for name in ("startup.py", "LICENSE", "NOTICE"):
        (patch / name).write_text(name, encoding="utf8")
    destination = runtime_path(root, source)
    assert not destination.parent.exists()
    assert prepare_runtime(root, source) == destination
    assert (destination / STARTUP).read_text() == "startup.py"
    assert (source / STARTUP).read_text() == "original startup"
    assert (destination / "MainMenuCommon.xml").read_text() == "menu"
    assert prepare_runtime(root, source) == destination
    (patch / "startup.py").write_text("new fix", encoding="utf8")
    updated = prepare_runtime(root, source)
    assert updated != destination
    assert (destination / STARTUP).read_text() == "startup.py"
    assert (updated / STARTUP).read_text() == "new fix"
