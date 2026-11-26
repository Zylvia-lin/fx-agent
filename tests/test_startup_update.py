"""The MCP startup update preserves the local checkout and checks once per launch."""

import subprocess

import pytest

from fx_agent import startup_update


@pytest.mark.parametrize("mode", ["clean", "current", "dirty", "diverged", "offline", "changed"])
def test_update_fast_forwards_only_a_clean_ancestor(tmp_path, monkeypatch, mode):
    commands = []

    def run(command, **kwargs):
        args = tuple(command[1:])
        commands.append(args)
        assert kwargs["cwd"] == tmp_path
        assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
        assert kwargs["capture_output"]
        outputs = {
            ("symbolic-ref", "--quiet", "--short", "HEAD"): "main",
            ("rev-parse", "--abbrev-ref", "@{upstream}"): "origin/main",
            ("remote", "get-url", "origin"): "https://github.com/Zylvia-lin/fx-agent.git",
            ("rev-parse", "HEAD"): "a" * 40,
            ("rev-parse", "refs/remotes/origin/main"): ("a" if mode == "current" else "b") * 40,
        }
        dirty = mode == "dirty" or (mode == "changed" and any(cmd[0] == "fetch" for cmd in commands))
        if (args[0], mode) in {("fetch", "offline"), ("merge-base", "diverged")}:
            raise subprocess.CalledProcessError(1, command)
        if args[0] == "status" and dirty:
            outputs[args] = " M local.py"
        return subprocess.CompletedProcess(command, 0, stdout=outputs.get(args, ""))

    monkeypatch.setattr(startup_update.subprocess, "run", run)
    assert startup_update.update_checkout(tmp_path) is (mode == "clean")
    assert ("merge", "--ff-only", "--no-edit", "b" * 40) in commands if mode == "clean" else all(
        args[0] != "merge" for args in commands
    )
    if mode == "dirty":
        assert all(args[0] != "fetch" for args in commands)


def test_updated_child_skips_github_and_runs_with_locked_dependencies(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "uv.lock").touch()
    monkeypatch.delenv("FX_AGENT_AUTO_UPDATE", raising=False)
    monkeypatch.delenv(startup_update._RESTARTED, raising=False)
    monkeypatch.setattr(startup_update.shutil, "which", lambda _: "uv")
    updates = []
    monkeypatch.setattr(startup_update, "update_checkout", lambda root: updates.append(root) or True)

    def run(command, *, env):
        assert command == ["uv", "run", "--directory", str(tmp_path), "--frozen", "--no-dev", "fx-agent"]
        assert env[startup_update._RESTARTED] == "1"
        monkeypatch.setenv(startup_update._RESTARTED, "1")
        startup_update.restart_if_updated(tmp_path)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(startup_update.subprocess, "run", run)
    with pytest.raises(SystemExit) as error:
        startup_update.restart_if_updated(tmp_path)
    assert error.value.code == 0
    assert updates == [tmp_path]
