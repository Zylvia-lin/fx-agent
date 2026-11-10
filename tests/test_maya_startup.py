"""Simulate shared module startup; no Maya process is launched."""

import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("batch,loaded", [(False, False), (False, True), (True, False)])
def test_module_startup_defers_once_skips_batch_and_existing_plugin(monkeypatch, batch, loaded):
    cmds = SimpleNamespace(about=Mock(return_value=batch), pluginInfo=Mock(return_value=loaded),
                           loadPlugin=Mock(), warning=Mock())
    deferred = Mock()
    monkeypatch.setitem(sys.modules, "maya", SimpleNamespace(cmds=cmds, utils=SimpleNamespace(executeDeferred=deferred)))
    path = ROOT / f"integrations/maya/{os.environ.get('FX_MAYA_TEST_RUNTIME', 'py2')}/fx_maya_bridge/startup.py"
    spec = importlib.util.spec_from_file_location("fx_startup_test", path)
    startup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(startup)
    startup.schedule()
    startup.schedule()
    if batch:
        deferred.assert_not_called()
        cmds.loadPlugin.assert_not_called()
        return
    deferred.assert_called_once_with(startup.load_bridge)
    cmds.loadPlugin.assert_not_called()
    deferred.call_args.args[0]()
    if loaded:
        cmds.loadPlugin.assert_not_called()
    else:
        cmds.loadPlugin.assert_called_once_with("fx_maya_plugin.py", quiet=True)
        cmds.loadPlugin.side_effect = RuntimeError("fixture load failure")
        startup.load_bridge()
        cmds.warning.assert_called_once()


def test_shared_mod_routes_versions_to_separate_runtimes():
    spec = importlib.util.spec_from_file_location("fx_module_installer_test", ROOT / "scripts/install_maya.py")
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    for content in [(ROOT / "integrations/maya/fx_maya.mod").read_text(), installer.module_content(ROOT, ROOT / "data")]:
        assert "scripts: scripts" in content
        assert "PYTHONPATH +:= scripts" in content
        assert "+ MAYAVERSION:2020 PLATFORM:win64 fx_maya any " in content
        assert "+ MAYAVERSION:2024 PLATFORM:win64 fx_maya any " in content
        for line in content.splitlines():
            if line.startswith("+ MAYAVERSION:2020 "):
                assert line.endswith("py2")
            elif line.startswith("+ MAYAVERSION:2024 "):
                assert line.endswith("py3")
    for runtime in ['py2', 'py3']:
        hook = (ROOT / 'integrations/maya' / runtime / 'scripts/userSetup.py').read_text()
        assert "fx_maya_bridge.startup" in hook
