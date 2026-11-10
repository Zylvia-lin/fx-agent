"""Normal Maya launch is mocked; tests never open applications."""

from types import SimpleNamespace

import pytest

from fx_agent.maya import launcher


def test_normal_launch_passes_no_flags_or_environment_overrides(tmp_path, monkeypatch):
    executable = tmp_path / "Maya 中文" / "maya.exe"
    executable.parent.mkdir()
    executable.touch()
    calls = []

    def popen(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(pid=123)

    monkeypatch.setattr(launcher.subprocess, "Popen", popen)
    result = launcher.start_maya(str(executable))
    assert calls == [([str(executable)], {"shell": False})]
    assert result["pid"] == 123 and result["bridge_ready"] is False


@pytest.mark.parametrize("path", ["maya.exe", "missing/maya.exe", __file__])
def test_invalid_executable_never_spawns(path, monkeypatch):
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *a, **k: pytest.fail("spawned"))
    with pytest.raises(ValueError):
        launcher.start_maya(path)


