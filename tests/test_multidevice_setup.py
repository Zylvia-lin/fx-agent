"""Client setup changes only FX paths and private-config references."""

import json
import tomllib
from pathlib import Path

import pytest

from fx_agent.client_setup import configure_codex, configure_maya
from fx_agent.data_service import main


def test_client_setup_preserves_unrelated_configuration():
    source = '[other]\nkeep = 1\n[mcp_servers.unrelated.env]\nA = "B"\n'
    for name in ("fx-agent", "fx-maya", "fx-nuke"):
        source += f'[mcp_servers.{name}]\ncommand="python"\n[mcp_servers.{name}.env]\nFX_AGENT_DATA_ROOT="old"\nPYTHONUTF8="1"\n'
    changed = configure_codex(source, Path("C:/private/client.json"), Path("C:/local"), Path("T:/FX-AGNET/data"))
    config = tomllib.loads(changed)
    assert config["other"] == {"keep": 1}
    assert config["mcp_servers"]["unrelated"]["env"] == {"A": "B"}
    for name in ("fx-agent", "fx-maya", "fx-nuke"):
        assert config["mcp_servers"][name]["env"]["FX_AGENT_CLIENT_CONFIG"] == str(Path("C:/private/client.json"))
        assert config["mcp_servers"][name]["env"]["PYTHONUTF8"] == "1"
    assert configure_codex(changed, Path("C:/private/client.json"), Path("C:/local"), Path("T:/FX-AGNET/data")) == changed
    module = '+ fx_maya any C:/maya\nFX_AGENT_DATA_ROOT = T:/FX-AGNET/data\n'
    assert "FX_AGENT_LOCAL_ROOT = C:/local" in configure_maya(module, Path("C:/local"))


def test_init_and_device_registration_do_not_print_tokens(tmp_path, monkeypatch, capsys):
    config = tmp_path / "private/service.json"
    client = tmp_path / "private/client-a.json"
    shared = tmp_path / "shared"
    local = tmp_path / "local"
    monkeypatch.setattr("sys.argv", ["fx-data-service", "init", "--config", str(config),
                        "--client-config", str(client), "--data-root", str(shared),
                        "--database-root", str(local), "--name", "A", "--url", "http://127.0.0.1:18790"])
    main()
    token = json.loads(client.read_text())["token"]
    assert token not in capsys.readouterr().out
    monkeypatch.setattr("sys.argv", ["fx-data-service", "add-device", "--config", str(config),
                        "--client-config", str(tmp_path / "client-b.json"), "--name", "B", "--url", "https://server.example"])
    main()
    assert len(json.loads(config.read_text())["devices"]) == 2
    with pytest.raises(SystemExit):
        main()
