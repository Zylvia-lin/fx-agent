# -*- coding: utf-8 -*-
"""Maya Python 2/3 plugin, discovered through fx_maya.mod.

Load via Plug-in Manager; unload stops the bridge. No renderer plugin changes.
"""
import json
import sys

from maya import OpenMayaMPx, cmds, utils

_root = cmds.moduleInfo(moduleName="fx_maya", path=True)
if _root not in sys.path:
    sys.path.insert(0, _root)
import fx_maya_bridge
from fx_maya_bridge import ui

COMMAND = "fxMayaBridgeStatus"


def refresh_bridge():
    """Plugin unload/reload refreshes code only after its old listener stopped."""
    if fx_maya_bridge._bridge is not None and fx_maya_bridge._bridge.running:
        return
    try:
        reload_module = reload
    except NameError:
        from importlib import reload as reload_module
    session_id = fx_maya_bridge.SESSION_ID
    for name in ("fx_maya_bridge.protocol", "fx_maya_bridge.lighting", "fx_maya_bridge.ui", "fx_maya_bridge"):
        if name in sys.modules:
            reload_module(sys.modules[name])
    fx_maya_bridge.SESSION_ID = session_id


class StatusCommand(OpenMayaMPx.MPxCommand):
    def doIt(self, args):
        self.setResult(json.dumps(ui.status(), ensure_ascii=True))


def creator():
    return OpenMayaMPx.asMPxPtr(StatusCommand())


def initializePlugin(obj):
    plugin = OpenMayaMPx.MFnPlugin(obj, "FX Agent", "0.1.0", "Any")
    plugin.registerCommand(COMMAND, creator)
    try:
        refresh_bridge()
        fx_maya_bridge.start()
        ui.enabled = True
        utils.executeDeferred(ui.install)
    except Exception:
        ui.enabled = False
        try:
            fx_maya_bridge.stop()
        finally:
            plugin.deregisterCommand(COMMAND)
        raise


def uninitializePlugin(obj):
    try:
        ui.uninstall()
    finally:
        try:
            fx_maya_bridge.stop()
        finally:
            OpenMayaMPx.MFnPlugin(obj).deregisterCommand(COMMAND)
