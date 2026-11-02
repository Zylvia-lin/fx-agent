"""Real Nuke main-thread dispatch differs from the upstream offline mock."""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_bridge_returns_main_thread_values_and_retains_errors(monkeypatch):
    monkeypatch.setattr(sys, "path", sys.path.copy())

    class OriginalBridge:
        def _run_in_nuke(self, callback, *args):
            return None  # Nuke executeInMainThread is fire-and-forget.

    host = SimpleNamespace(
        GUI=True, executeInMainThreadWithResult=lambda callback, args: callback(*args)
    )
    vendor = SimpleNamespace(
        NukeMCPServer=OriginalBridge,
        _get_nuke=lambda: host,
    )
    monkeypatch.setitem(sys.modules, "fx_agent_nuke_addon", vendor)
    path = Path(__file__).resolve().parents[1] / "integrations/nuke/fx_agent_nuke.py"
    spec = importlib.util.spec_from_file_location("fx_agent_nuke_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    bridge = OriginalBridge()
    module._install_dispatch(vendor)
    assert bridge._run_in_nuke(lambda: {"type": "handshake"}) == {"type": "handshake"}
    module._install_dispatch(vendor)  # Hot reload does not wrap itself recursively.
    assert bridge._run_in_nuke(lambda x: x + 1, 5) == 6

    def fail():
        raise ValueError("node failure")

    with pytest.raises(RuntimeError, match="node failure"):
        bridge._run_in_nuke(fail)
