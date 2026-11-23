# -*- coding: utf-8 -*-
"""Native Maya menu for the local bridge, compatible with Python 2/3."""
from __future__ import print_function

import json

from maya import cmds, mel

MENU = "fxMayaMCPMenu"
STATE = "fxMayaMCPMenuState"
START = "fxMayaMCPMenuStart"
STOP = "fxMayaMCPMenuStop"
enabled = False


def status():
    import fx_maya_bridge as bridge

    active = bridge._bridge is not None and bridge._bridge.running
    info = bridge.identity(bridge._bridge.port if active else 0)
    info["running"] = active
    if not active:
        info["port"] = None
    return info


def refresh(*unused):
    if not cmds.menu(MENU, exists=True):
        return
    info = status()
    label = (u"运行中 | 127.0.0.1:%s | PID %s" % (info["port"], info["pid"])) if info["running"] else u"MCP 桥接已停止"
    cmds.menuItem(STATE, edit=True, label=label)
    cmds.menuItem(START, edit=True, enable=not info["running"])
    cmds.menuItem(STOP, edit=True, enable=info["running"])


def start(*unused):
    import fx_maya_bridge as bridge

    bridge.start()
    refresh()


def stop(*unused):
    import fx_maya_bridge as bridge

    bridge.stop()
    refresh()


def show_info(*unused):
    cmds.confirmDialog(title="FX Maya MCP", message=json.dumps(status(), ensure_ascii=False, indent=2), button=[u"关闭"])


def install(*unused):
    if not enabled or cmds.about(batch=True):
        return
    if cmds.menu(MENU, exists=True):
        cmds.deleteUI(MENU, menu=True)
    parent = mel.eval("$fxMayaMainWindow = $gMainWindow")
    cmds.menu(MENU, label="FX Maya MCP", parent=parent, tearOff=False, postMenuCommand=refresh)
    cmds.menuItem(STATE, parent=MENU, label=u"读取状态…", enable=False)
    cmds.menuItem(parent=MENU, divider=True)
    cmds.menuItem(START, parent=MENU, label=u"启动 MCP 桥接", command=start)
    cmds.menuItem(STOP, parent=MENU, label=u"停止 MCP 桥接", command=stop)
    cmds.menuItem(parent=MENU, label=u"查看会话信息…", command=show_info)
    refresh()


def uninstall():
    global enabled
    enabled = False
    if not cmds.about(batch=True) and cmds.menu(MENU, exists=True):
        cmds.deleteUI(MENU, menu=True)
