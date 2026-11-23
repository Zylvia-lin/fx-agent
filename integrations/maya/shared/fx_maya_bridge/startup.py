# -*- coding: utf-8 -*-
"""Shared module startup; no version preferences or command-line injection."""
from __future__ import print_function

import traceback

from maya import cmds, utils

_scheduled = False


def load_bridge():
    """Run after Maya initialization; failures remain visible in Script Editor."""
    try:
        if not cmds.pluginInfo("fx_maya_plugin.py", query=True, loaded=True):
            cmds.loadPlugin("fx_maya_plugin.py", quiet=True)
    except Exception:
        traceback.print_exc()
        cmds.warning("FX Maya MCP startup failed; see Script Editor traceback")


def schedule():
    global _scheduled
    if cmds.about(batch=True) or _scheduled:
        return
    _scheduled = True
    utils.executeDeferred(load_bridge)
