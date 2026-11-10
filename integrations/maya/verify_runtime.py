# -*- coding: utf-8 -*-
"""Explicit disposable-scene mayapy check, Python 2/3. Never run in a user scene.

Usage: mayapy verify_runtime.py ABSOLUTE_REPORT_DIR PORT [hold_seconds]
Loads already installed renderer plugins only for this validation process.
"""
from __future__ import print_function

import json
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "py2" if sys.version_info[0] == 2 else "py3"))
report_dir, port = os.path.abspath(sys.argv[1]), int(sys.argv[2])
if not os.path.isdir(report_dir):
    os.makedirs(report_dir)


def write_json(name, value):
    with open(os.path.join(report_dir, name), "wb") as stream:
        stream.write(json.dumps(value, ensure_ascii=True, indent=2).encode("utf-8"))


print("Initializing real Maya runtime", sys.version)
sys.stdout.flush()
import maya.standalone

maya.standalone.initialize(name="python")
import fx_maya_bridge
from fx_maya_bridge import lighting
from maya import cmds, utils

report = {"python": sys.version, "maya": cmds.about(version=True), "checks": []}


def check(label, function, *args, **kwargs):
    try:
        value = function(*args, **kwargs)
        if isinstance(value, dict) and value.get("success") is False:
            raise RuntimeError(value.get("error", "Operation reported failure"))
        report["checks"].append({"name": label, "success": True, "result": value})
        return value
    except Exception:
        report["checks"].append({"name": label, "success": False, "error": traceback.format_exc()})
        return None
    finally:
        write_json("report.json", report)


def render_and_wait(renderer):
    receipt = lighting.render_frame(renderer, "persp", 1, report_dir, 64, 64)
    deadline = time.time() + 60
    while time.time() < deadline:
        # Disposable standalone verifier only: return idle control to Maya, just
        # as the external MCP's file wait does for a normal GUI bridge.
        utils.processIdleEvents()
        status = lighting.render_status(receipt["job_id"])
        if status.get("success") is True and status.get("status") == "complete":
            return status
        time.sleep(0.2)
    raise RuntimeError("No stable rendered output: " + receipt["output_dir"])


try:
    cmds.file(new=True, force=True)
    scene = os.path.join(report_dir, u"maya_\u706f\u5149.ma").replace("\\", "/")
    cmds.file(rename=scene)
    cmds.file(save=True, type="mayaAscii")
    check("bridge_start", lambda: fx_maya_bridge.start(port=port, data_root=report_dir))
    check("python_unicode_stdout_stderr", lambda: lighting.execute_python(
        u"import sys\nprint(u'\u706f\u5149')\nsys.stderr.write(u'\u8bca\u65ad')\nresult = {'path': cmds.file(q=True, sn=True)}"))
    hdri = os.path.join(report_dir, u"\u73af\u5883.hdr")
    with open(hdri, "wb") as stream:
        stream.write(b"#?RADIANCE\nFORMAT=32-bit_rle_rgbe\n\n-Y 4 +X 4\n" + b"\x80\x80\x80\x81" * 16)
    for renderer, plugin in (("arnold", "mtoa"), ("redshift", "redshift4maya")):
        check(renderer + "_load_installed_plugin", cmds.loadPlugin, plugin, quiet=True)
        if not lighting.renderer_status()[renderer]["loaded"]:
            continue
        cmds.file(new=True, force=True)
        cmds.file(rename=scene)
        sphere = cmds.polySphere(name="testSubject")[0]
        cmds.xform("persp", translation=(6, 4, 8), rotation=(-20, 36, 0))
        lights = {}
        for kind in ("area", "point", "spot", "directional", "environment"):
            item = check(renderer + "_create_" + kind,
                         lighting.create_light, renderer, kind, renderer + "_" + kind)
            if item:
                lights[kind] = item["shape"]
        if "area" in lights:
            check(renderer + "_update", lighting.update_light, renderer, lights["area"],
                  {"position": [2, 4, 3], "rotation": [-40, 20, 0], "scale": [3, 3, 3],
                   "intensity": 2, "exposure": 1, "color": [1, 0.8, 0.6]})
            check(renderer + "_link", lighting.light_link, renderer, lights["area"], [sphere], True)
            check(renderer + "_unlink", lighting.light_link, renderer, lights["area"], [sphere], False)
        if "environment" in lights:
            check(renderer + "_hdri", lighting.set_hdri, renderer, lights["environment"], hdri)
            check(renderer + "_dome_exposure", lighting.update_light, renderer, lights["environment"], {"exposure": 0.5, "intensity": 1})
        check(renderer + "_list", lighting.list_lights, renderer)
        if renderer == "arnold":
            import mtoa.core
            mtoa.core.createOptions()
            if cmds.objExists("defaultArnoldRenderOptions.AASamples"):
                cmds.setAttr("defaultArnoldRenderOptions.AASamples", 2)
        check(renderer + "_render", render_and_wait, renderer)
    cmds.file(save=True, type="mayaAscii")
    report["identity"] = fx_maya_bridge.identity(port)
    write_json("ready.json", report["identity"])
    until = time.time() + (int(sys.argv[3]) if len(sys.argv) > 3 else 0)
    while time.time() < until and not os.path.exists(os.path.join(report_dir, "stop")):
        utils.processIdleEvents()
        time.sleep(0.02)
finally:
    write_json("report.json", report)
    fx_maya_bridge.stop()
    maya.standalone.uninitialize()
