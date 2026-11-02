"""Installation must preserve unrelated client settings and pin shared storage."""

import importlib.util
import os
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("install_codex", ROOT / "scripts/install_codex.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


def test_config_replaces_old_entry_and_preserves_other_settings(tmp_path):
    before = """model = "existing-model"
[mcp_servers.fxhoudini]
command = "old-python"
[mcp_servers.fxhoudini.env]
HOUDINI_PORT = "8100"
[mcp_servers.other]
command = "keep-me"
[mcp_servers.other.env]
TOKEN = "untouched"
"""
    updated = installer.codex_config(before, ROOT, tmp_path, 8186)
    parsed = tomllib.loads(updated)
    assert parsed["model"] == "existing-model"
    assert parsed["mcp_servers"]["other"] == tomllib.loads(before)["mcp_servers"]["other"]
    assert "fxhoudini" not in parsed["mcp_servers"]
    agent = parsed["mcp_servers"]["fx-agent"]
    assert agent["env"]["HOUDINI_PORT"] == "8186"
    assert agent["env"]["FX_AGENT_DATA_ROOT"] == str(tmp_path)
    assert agent["args"] == ["-m", "fx_agent"]
    assert installer.codex_config(updated, ROOT, tmp_path, 8186) == updated


def test_quoted_old_server_header_is_removed(tmp_path):
    updated = installer.codex_config(
        '[mcp_servers."fxhoudini"]\ncommand="old"\n', ROOT, tmp_path, 8186
    )
    assert "fxhoudini" not in tomllib.loads(updated)["mcp_servers"]


def test_houdini_cannot_take_registered_nuke_port_even_when_not_listening(tmp_path):
    before = '[mcp_servers.fx-nuke]\nargs=["-m","fx_agent.nuke_server","--port","8187"]\n'
    with pytest.raises(ValueError, match="Nuke"):
        installer.codex_config(before, ROOT, tmp_path, 8187)


def test_unrecognized_config_layout_fails_instead_of_damaging_it(tmp_path):
    with pytest.raises(ValueError):
        installer.codex_config('[mcp_servers]\nfxhoudini={command="old"}\n', ROOT, tmp_path, 8186)


def test_headers_inside_multiline_instructions_never_silently_remove_text(tmp_path):
    original = '''developer_instructions = """
keep before
[mcp_servers.fxhoudini]
keep this instruction too
[other text]
keep after
"""
[mcp_servers.fxhoudini]
command = "old"
'''
    with pytest.raises(ValueError):
        installer.codex_config(original, ROOT, tmp_path, 8186)


def test_package_pins_current_bundled_bridge_and_port():
    import json

    package = json.loads(installer.houdini_package(8186))
    env = {key: value for item in package["env"] for key, value in item.items()}
    assert env["FXHOUDINIMCP_PORT"] == "8186"
    assert env["FXHOUDINIMCP_BIND"] == "127.0.0.1"
    assert Path(env["FXHOUDINIMCP"]).is_relative_to(ROOT)


def test_package_preserves_existing_limits_and_custom_environment(tmp_path):
    import json

    old = tmp_path / "existing.json"
    old.write_text(
        json.dumps(
            {
                "enable": True,
                "env": [
                    {"FXHOUDINIMCP_PROJECT_ROOT": "D:/production"},
                    {"FXHOUDINIMCP_TIMEOUT": "900"},
                    {"CUSTOM_SETTING": "keep"},
                    {"FXHOUDINIMCP": "old-path"},
                    {"FXHOUDINIMCP_PORT": "8100"},
                ],
                "path": "$FXHOUDINIMCP",
            }
        ),
        encoding="utf-8",
    )
    package = json.loads(installer.houdini_package(8186, old))
    env = {key: value for item in package["env"] for key, value in item.items()}
    assert env["FXHOUDINIMCP_PROJECT_ROOT"] == "D:/production"
    assert env["FXHOUDINIMCP_TIMEOUT"] == "900"
    assert env["CUSTOM_SETTING"] == "keep"
    assert env["FXHOUDINIMCP_PORT"] == "8186"
    assert env["FXHOUDINIMCP"] != "old-path"
    assert package["enable"] is True
    old.write_text('{"env": "invalid"}', encoding="utf-8")
    with pytest.raises(ValueError):
        installer.houdini_package(8186, old)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction installer")
@pytest.mark.parametrize("fail_write", [False, True, "rollback"])
def test_install_and_failure_restore_original_files(tmp_path, monkeypatch, fail_write):
    home = tmp_path / "home"
    config = home / ".codex/config.toml"
    config.parent.mkdir(parents=True)
    original = '[mcp_servers.fxhoudini]\ncommand="old"\n'
    config.write_text(original, encoding="utf-8")
    pref = home / "houdini22.0"
    packages = pref / "packages"
    packages.mkdir(parents=True)
    old = packages / "fxhoudinimcp.json"
    old.write_text('{"env": []}', encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(sys, "argv", ["install_codex.py", "--houdini-pref", str(pref)])
    installer.main()
    assert config.read_text(encoding="utf-8") == original
    assert not (home / ".agents").exists(), "Preview must not mutate the profile"
    monkeypatch.setattr(sys, "argv", [*sys.argv, "--apply"])
    write = Path.write_text

    def checked_write(path, *args, **kwargs):
        if fail_write and path == packages / "fx-agent.json":
            raise OSError("simulated package write failure")
        return write(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", checked_write)
    copy = installer.shutil.copy2

    def checked_copy(source, destination):
        if fail_write == "rollback" and Path(destination) == config:
            raise OSError("simulated locked config during restore")
        return copy(source, destination)

    monkeypatch.setattr(installer.shutil, "copy2", checked_copy)
    try:
        if fail_write:
            with pytest.raises(OSError, match="simulated") as failed:
                installer.main()
            if fail_write == "rollback":
                assert any("部分回退失败" in note for note in failed.value.__notes__)
            else:
                assert config.read_text(encoding="utf-8") == original
            assert old.read_text(encoding="utf-8") == '{"env": []}'
            assert not (packages / "fx-agent.json").exists()
            assert not os.path.lexists(home / ".agents/skills/fx-workflow")
        else:
            installer.main()
            assert not old.exists()
            assert (packages / "fx-agent.json").exists()
            assert (home / ".agents/skills/fx-workflow").resolve() == ROOT / "skills/fx-workflow"
            installer.main()  # Reinstall uses the same targets without duplicating tools.
    finally:
        for name in ("fx-workflow", "fx-learning", "nuke-workflow"):
            link = home / ".agents/skills" / name
            if os.path.lexists(link):
                os.rmdir(link)
