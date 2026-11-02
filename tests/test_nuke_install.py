"""Nuke setup must avoid ports in use and preserve existing user startup code."""

import importlib.util
import socket
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("install_nuke", ROOT / "scripts/install_nuke.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


def test_nuke_registration_keeps_houdini_and_other_settings():
    original = '[mcp_servers.fx-agent]\ncommand="houdini"\n[mcp_servers.other]\ncommand="other"\n'
    updated = installer.nuke_config(original, ROOT, 8187)
    parsed = tomllib.loads(updated)
    assert parsed["mcp_servers"]["fx-agent"]["command"] == "houdini"
    assert parsed["mcp_servers"]["other"]["command"] == "other"
    assert parsed["mcp_servers"]["fx-nuke"]["args"][-1] == "8187"
    assert installer.nuke_config(updated, ROOT, 8187) == updated


def test_nuke_startup_keeps_user_code_and_gates_old_versions():
    original = '# 用户已有菜单\nprint("existing startup")\n'
    result = installer.menu_content(original, ROOT, 8187)
    assert result.startswith(original)
    assert ">= (15, 2)" in result
    assert installer.menu_content(result, ROOT, 8187) == result
    with pytest.raises(ValueError):
        installer.menu_content(original + installer.BEGIN, ROOT, 8187)
    embedded = 'documentation = """\n# BEGIN FX AGENT NUKE\nexample\n# END FX AGENT NUKE\n"""\n'
    assert installer.menu_content(embedded, ROOT, 8187).startswith(embedded)


def test_occupied_port_is_not_silently_reused():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        assert not installer.port_available(listener.getsockname()[1])


def test_bad_vendor_checksum_is_never_published(tmp_path):
    from io import BytesIO
    from unittest.mock import patch

    with (
        patch.object(installer.urllib.request, "urlopen", return_value=BytesIO(b"untrusted bytes")),
        pytest.raises(ValueError, match="checksum"),
    ):
        installer.prepare_vendor(tmp_path / "vendor")
    assert not (tmp_path / "vendor").exists()


def test_nuke_only_setup_requires_the_current_shared_memory_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(sys, "argv", ["install_nuke.py", "--require-shared-agent"])
    monkeypatch.setattr(installer, "port_available", lambda port: True)
    with pytest.raises(SystemExit) as exited:
        installer.main()
    assert exited.value.code == 2
    config = tmp_path / ".codex/config.toml"
    config.parent.mkdir()
    config.write_text('[mcp_servers.fx-agent.env]\nFX_AGENT_DATA_ROOT="C:/other/data"\n')
    with pytest.raises(SystemExit):
        installer.main()
    import json

    env = "[mcp_servers.fx-agent.env]\nFX_AGENT_DATA_ROOT=" + json.dumps(str(ROOT / "data")) + "\n"
    valid_command = "command=" + json.dumps(str(ROOT / ".venv/Scripts/python.exe")) + "\n"
    for settings in (
        "",  # An env subtable alone is not a launchable server.
        valid_command + 'args=["-m","fx_agent"]\nenabled=false\n',
        'command="other-program"\nargs=["-m","fx_agent"]\n',
        'command=".venv/Scripts/python.exe"\nargs=["-m","fx_agent"]\n',
        valid_command + 'args=["-m","other_module"]\n',
    ):
        config.write_text("[mcp_servers.fx-agent]\n" + settings + env)
        with pytest.raises(SystemExit):
            installer.main()
    config.write_text("[mcp_servers.fx-agent]\n" + valid_command + 'args=["-m","fx_agent"]\n' + env)
    installer.main()  # Preview only; no user files changed.
    config.write_text(
        "[mcp_servers.fx-agent]\n"
        + valid_command
        + 'args=["-m","fx_agent"]\n'
        + '[mcp_servers.fx-agent.env]\nFX_AGENT_DATA_ROOT="data"\n'
    )
    with pytest.raises(SystemExit):
        installer.main()


@pytest.mark.parametrize("port", [8100, 8186, 54321])
def test_original_mcp_ports_are_rejected_before_configuration_changes(port, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["install_nuke.py", "--port", str(port)])
    with pytest.raises(SystemExit) as exited:
        installer.main()
    assert exited.value.code == 2
