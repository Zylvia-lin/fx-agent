"""Native menu lifecycle and enabled states, with no OS window automation."""

import importlib.util
import os
import sys
import types
from pathlib import Path
from unittest.mock import Mock


def test_native_menu_reflects_service_start_stop_and_cleans_up(monkeypatch):
    menus, items = {"unrelatedMenu"}, {}
    bridge = types.ModuleType("fx_maya_bridge")
    bridge._bridge = None
    bridge.identity = lambda port: {"port": port, "pid": 123, "session_id": "stable"}
    bridge.start = Mock(side_effect=lambda: setattr(bridge, "_bridge", types.SimpleNamespace(port=18770, running=True)))
    bridge.stop = Mock(side_effect=lambda: setattr(bridge, "_bridge", None))
    monkeypatch.setitem(sys.modules, "fx_maya_bridge", bridge)

    def menu(name, **kwargs):
        if kwargs.get("exists"):
            return name in menus
        menus.add(name)

    def item(name=None, **kwargs):
        if name:
            items.setdefault(name, {}).update(kwargs)

    maya = types.ModuleType("maya")
    maya.cmds = types.SimpleNamespace(menu=menu, menuItem=item,
                                     deleteUI=lambda name, **kw: menus.remove(name),
                                     about=lambda **kw: False, confirmDialog=Mock())
    maya.mel = types.SimpleNamespace(eval=Mock(return_value="MayaWindow"))
    monkeypatch.setitem(sys.modules, "maya", maya)
    path = Path(__file__).resolve().parents[1] / f"integrations/maya/{os.environ.get('FX_MAYA_TEST_RUNTIME', 'py2')}/fx_maya_bridge/ui.py"
    spec = importlib.util.spec_from_file_location("maya_ui_test", path)
    ui = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ui)
    ui.enabled = True
    ui.install()
    assert items[ui.START]["enable"] and not items[ui.STOP]["enable"]
    ui.start()
    assert "18770" in items[ui.STATE]["label"]
    assert not items[ui.START]["enable"] and items[ui.STOP]["enable"]
    ui.show_info()
    assert "stable" in maya.cmds.confirmDialog.call_args.kwargs["message"]
    ui.stop()
    assert items[ui.START]["enable"] and not items[ui.STOP]["enable"]
    assert ui.status()["port"] is None
    ui.uninstall()
    assert menus == {"unrelatedMenu"}
    ui.install()  # A deferred callback after unload must not recreate an orphan menu.
    assert menus == {"unrelatedMenu"}
