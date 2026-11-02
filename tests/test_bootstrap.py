"""Public entry points must work without the original MCP installation."""

import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_version_does_not_start_houdini_or_write_protocol_noise():
    result = subprocess.run(
        [sys.executable, "-m", "fx_agent", "--version"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0.1.0"
    assert result.stderr == ""


def test_unknown_command_never_starts_server():
    result = subprocess.run(
        [sys.executable, "-m", "fx_agent", "not-a-command"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 2
    assert result.stdout == ""


@pytest.mark.parametrize("argument,code", [("--version", 0), ("--help", 0), ("bad", 2)])
def test_cli_options_exit_before_server_initialization(argument, code, monkeypatch):
    from fx_agent.__main__ import main

    monkeypatch.setattr(sys, "argv", ["fx-agent", argument])
    with pytest.raises(SystemExit) as exited:
        main()
    assert exited.value.code == code


def test_cli_dispatches_only_stdio(monkeypatch):
    from unittest.mock import Mock

    from fx_agent.__main__ import main

    server = Mock()
    monkeypatch.setattr("fx_agent.server.get_server", lambda: server)
    monkeypatch.setattr(sys, "argv", ["fx-agent"])
    main()
    server.run.assert_called_once_with(transport="stdio")


def test_plugin_uses_local_project_and_a_bundled_skill():
    manifest = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
    config = json.loads((ROOT / "mcp.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "fx-agent"
    server = config["mcpServers"]["fx-agent"]
    assert server["type"] == "stdio"
    assert "${PLUGIN_ROOT}" in server["args"]
    assert "--frozen" in server["args"]
    assert (ROOT / "skills/fx-workflow/SKILL.md").is_file()


def test_data_root_is_explicit_and_independent_of_working_directory(tmp_path, monkeypatch):
    config = importlib.import_module("fx_agent.config")
    data = tmp_path / "persistent"
    monkeypatch.setenv("FX_AGENT_DATA_ROOT", str(data))
    monkeypatch.chdir(tmp_path)
    assert config.data_root() == data.resolve()
    assert not data.exists(), "Reading configuration must not create runtime data"
    monkeypatch.setenv("FX_AGENT_DATA_ROOT", "relative/data")
    with pytest.raises(ValueError, match="absolute"):
        config.data_root()


def test_default_data_root_is_stable_when_localappdata_is_sandboxed(tmp_path, monkeypatch):
    config = importlib.import_module("fx_agent.config")
    monkeypatch.delenv("FX_AGENT_DATA_ROOT", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "sandbox-one"))
    original = config.data_root()
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "sandbox-two"))
    assert config.data_root() == original


@pytest.mark.asyncio
async def test_server_preserves_upstream_tools_and_rejects_unknown_arguments():
    server = importlib.import_module("fx_agent.server").get_server()
    tools = {tool.name: tool for tool in await server.list_tools()}
    for name in (
        "get_scene_info",
        "build_network",
        "verify_network",
        "capture_screenshot",
        "write_cache",
        "start_render",
        "get_workflow_guide",
        "fx_status",
    ):
        assert name in tools
    assert tools["build_network"].inputSchema["additionalProperties"] is False
    assert tools["fx_status"].inputSchema["additionalProperties"] is False
    assert server is importlib.import_module("fx_agent.server").get_server()
    status = await server.call_tool("fx_status", {})
    payload = json.loads(status[0].text)
    assert payload["upstream_version"] == "2.25.0"
    assert payload["version"] == "0.1.0"
