"""Maya plugin loader may omit __file__; moduleInfo is authoritative."""

import importlib.util
import json
import os
import sys
import threading
import time
import types
import uuid
from pathlib import Path
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_plugin_without_file_uses_mod_root_and_manages_bridge(monkeypatch):
    bridge = types.ModuleType("fx_maya_bridge")
    bridge.start, bridge.stop = Mock(), Mock()
    bridge.ui = types.SimpleNamespace(enabled=False, install=Mock(), uninstall=Mock())
    maya = types.ModuleType("maya")
    plugin = Mock()
    maya.cmds = types.SimpleNamespace(moduleInfo=Mock(return_value=str(ROOT / "integrations/maya")))
    maya.OpenMayaMPx = types.SimpleNamespace(MPxCommand=object, MFnPlugin=Mock(return_value=plugin),
                                            asMPxPtr=lambda value: value)
    maya.utils = types.SimpleNamespace(executeDeferred=Mock())
    monkeypatch.setitem(sys.modules, "maya", maya)
    monkeypatch.setitem(sys.modules, "fx_maya_bridge", bridge)
    namespace = {"__name__": "test_plugin_without_file"}
    path = ROOT / f"integrations/maya/{os.environ.get('FX_MAYA_TEST_RUNTIME', 'py2')}/plug-ins/fx_maya_plugin.py"
    original = list(sys.path)
    try:
        exec(compile(path.read_bytes(), str(path), "exec"), namespace)
        namespace["refresh_bridge"] = Mock()
        namespace["initializePlugin"](object())
        bridge.start.assert_called_once_with()
        maya.utils.executeDeferred.assert_called_once_with(bridge.ui.install)
        namespace["uninitializePlugin"](object())
        bridge.stop.assert_called_once_with()
        bridge.ui.uninstall.assert_called_once_with()
        bridge.start.side_effect = RuntimeError("occupied port")
        with pytest.raises(RuntimeError, match="occupied"):
            namespace["initializePlugin"](object())
        assert plugin.deregisterCommand.call_count == 2
    finally:
        sys.path[:] = original


def test_installer_mod_config_preserves_dccs_and_rejects_conflicts(tmp_path):
    spec = importlib.util.spec_from_file_location("maya_installer_test", ROOT / "scripts/install_maya.py")
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    data = tmp_path / "共享数据"
    import tomllib

    original = '[mcp_servers.fx-agent]\ncommand="old"\n[mcp_servers.fx-agent.env]\nFX_AGENT_DATA_ROOT=' + json.dumps(str(data)) + '\n[mcp_servers.fx-nuke]\ncommand="nuke"\nargs=["--port","8187"]\n'
    changed = installer.maya_config(original, ROOT, data, (18770, 18771))
    config = tomllib.loads(changed)
    assert config["mcp_servers"]["fx-nuke"] == tomllib.loads(original)["mcp_servers"]["fx-nuke"]
    assert config["mcp_servers"]["fx-agent"] == tomllib.loads(original)["mcp_servers"]["fx-agent"]
    assert "fx-maya" in config["mcp_servers"]
    assert installer.maya_config(changed, ROOT, data, (18770, 18771)) == changed
    module = installer.module_content(ROOT, data)
    assert "plug-ins" in module and "FX_MAYA_PORT" not in module
    assert str(data.as_posix()) in module
    with pytest.raises(ValueError, match="share"):
        installer.maya_config(original, ROOT, tmp_path / "other", (18770,))
    with pytest.raises(ValueError, match="conflicts"):
        installer.maya_config(original.replace('"8187"', '"18770"'), ROOT, data, (18770,))


@pytest.fixture
def bridge_module(monkeypatch):
    path = ROOT / f"integrations/maya/{os.environ.get('FX_MAYA_TEST_RUNTIME', 'py2')}/fx_maya_bridge/__init__.py"
    spec = importlib.util.spec_from_file_location("maya_bridge_test", path, submodule_search_locations=[str(path.parent)])
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "maya_bridge_test", module)
    spec.loader.exec_module(module)
    lighting = types.ModuleType("maya_bridge_test.lighting")
    lighting.dispatch = Mock(return_value={"success": True})
    monkeypatch.setitem(sys.modules, "maya_bridge_test.lighting", lighting)
    info = {"software": "maya", "service": "fx-maya-bridge", "session_id": "real-id",
            "pid": 1, "port": 0, "scene_path": "D:/灯光.ma"}
    monkeypatch.setattr(module, "identity", lambda port: info | {"port": port})
    yield module, lighting


def test_bridge_conflict_identity_deadline_duplicate_and_main_thread(bridge_module, tmp_path):
    module, lighting = bridge_module
    bridge = module.Bridge(0, str(tmp_path))
    info = module.identity(bridge.port)
    def request(**changes):
        return {"id": uuid.uuid4().hex, "command": "create_light", "target": info,
                "deadline": time.time() + 5, "params": {}} | changes
    try:
        with pytest.raises(OSError):
            module.Bridge(bridge.port, str(tmp_path))
        with pytest.raises(ValueError, match="identity"):
            bridge.dispatch(request(target=info | {"pid": 2}))
        with pytest.raises(RuntimeError, match="expired"):
            bridge.dispatch(request(deadline=time.time() - 1))
        lighting.dispatch.assert_not_called()
        value = request()
        assert bridge.dispatch(value)["success"]
        with pytest.raises(ValueError, match="Duplicate"):
            bridge.dispatch(value)
        errors = []
        def worker():
            try:
                bridge.dispatch(request())
            except RuntimeError as exc:
                errors.append(str(exc))
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        assert "main thread" in errors[0]
        assert lighting.dispatch.call_count == 1
    finally:
        bridge.stop()
