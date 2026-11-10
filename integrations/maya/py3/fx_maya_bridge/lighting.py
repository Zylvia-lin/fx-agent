# -*- coding: utf-8 -*-
"""Main-thread lighting commands; selective AutoMaya adaptation plus RS adapter.

No automatic loading, installing or replacing renderer plugins. Raw attributes
retain renderer-specific units. See THIRD_PARTY.md for upstream attribution.
"""
from __future__ import print_function

import json
import os
import sys
import traceback
import uuid

from maya import cmds

from .protocol import text_value

PLUGINS = {"arnold": "mtoa", "redshift": "redshift4maya"}
ARNOLD_TYPES = {"area": "aiAreaLight", "point": "pointLight", "spot": "spotLight",
                "directional": "directionalLight", "environment": "aiSkyDomeLight"}
RS_TYPES = {"area": 0, "point": 1, "spot": 2, "directional": 3}
RS_NODES = ("RedshiftPhysicalLight", "RedshiftDomeLight")
if "_render_jobs" not in globals():
    _render_jobs = {}


def renderer_status():
    result = {}
    for renderer, plugin in PLUGINS.items():
        try:
            loaded = bool(cmds.pluginInfo(plugin, query=True, loaded=True))
            result[renderer] = {"plugin": plugin, "loaded": loaded,
                                "version": cmds.pluginInfo(plugin, query=True, version=True) if loaded else None}
        except RuntimeError as exc:
            result[renderer] = {"plugin": plugin, "loaded": False, "version": None,
                                "error": text_value(exc)}
    return result


def require_renderer(renderer):
    if renderer not in PLUGINS:
        raise ValueError("renderer must be arnold or redshift")
    status = renderer_status()[renderer]
    if not status["loaded"]:
        raise RuntimeError("Renderer plugin not loaded: %s; enable it explicitly in Maya" % status["plugin"])
    return status


def unique_node(name):
    matches = cmds.ls(name, long=True) or []
    if len(matches) != 1:
        raise ValueError("Node missing or ambiguous: " + name)
    return matches[0]


def light_shape(renderer, node):
    require_renderer(renderer)
    node = unique_node(node)
    if cmds.nodeType(node) == "transform":
        shapes = cmds.listRelatives(node, shapes=True, fullPath=True) or []
        if len(shapes) != 1:
            raise ValueError("Expected one light shape: " + node)
        node = shapes[0]
    allowed = set(ARNOLD_TYPES.values()) if renderer == "arnold" else set(RS_NODES)
    if cmds.nodeType(node) not in allowed:
        raise ValueError("Node is not a supported %s light: %s" % (renderer, node))
    return node


def require_attr(node, attr):
    if "." in attr or "[" in attr or not cmds.attributeQuery(attr, node=node, exists=True):
        raise ValueError("Unsupported attribute in this Maya/plugin version: %s.%s" % (node, attr))
    return node + "." + attr


def set_value(node, attr, value):
    plug = require_attr(node, attr)
    kind = cmds.getAttr(plug, type=True)
    if kind == "string":
        cmds.setAttr(plug, value, type="string")
    elif kind in ("double3", "float3"):
        if not isinstance(value, (list, tuple)) or len(value) != 3:
            raise ValueError("Expected three values: " + plug)
        cmds.setAttr(plug, *value, type=kind)
    elif kind in ("double", "float", "long", "short", "byte", "bool", "enum", "doubleAngle", "doubleLinear", "time"):
        cmds.setAttr(plug, value)
    else:
        raise ValueError("Unsupported attribute value type: " + kind)


def inspect_light(renderer, node):
    shape = light_shape(renderer, node)
    transform = (cmds.listRelatives(shape, parent=True, fullPath=True) or [shape])[0]
    attributes, unreadable = {}, {}
    for attr in cmds.listAttr(shape, scalar=True) or []:
        try:
            attributes[attr] = {"type": cmds.getAttr(shape + "." + attr, type=True),
                                "value": cmds.getAttr(shape + "." + attr),
                                "settable": cmds.getAttr(shape + "." + attr, settable=True)}
        except (RuntimeError, ValueError, TypeError) as exc:
            unreadable[attr] = text_value(exc)
    return {"shape": shape, "transform": transform, "node_type": cmds.nodeType(shape),
            "renderer": renderer, "attributes": attributes, "unreadable_attributes": unreadable,
            "position": cmds.xform(transform, query=True, worldSpace=True, translation=True),
            "rotation": cmds.xform(transform, query=True, worldSpace=True, rotation=True),
            "scale": cmds.getAttr(transform + ".scale")[0]}


def list_lights(renderer):
    require_renderer(renderer)
    types = sorted(set(ARNOLD_TYPES.values())) if renderer == "arnold" else RS_NODES
    nodes = []
    for kind in types:
        if kind in (cmds.allNodeTypes() or []):
            nodes.extend(cmds.ls(type=kind, long=True) or [])
    return {"renderer": renderer, "lights": [inspect_light(renderer, node) for node in sorted(set(nodes))]}


def update_light(renderer, node, parameters):
    shape = light_shape(renderer, node)
    transform = cmds.listRelatives(shape, parent=True, fullPath=True)[0]
    aliases = {"intensity": "intensity", "color": "color"}
    kind = cmds.nodeType(shape)
    if kind == "RedshiftDomeLight":
        aliases["intensity"] = "multiplier"
    aliases["exposure"] = ("exposure0" if kind == "RedshiftDomeLight" else
                           "exposure" if kind in ("RedshiftPhysicalLight", "aiAreaLight", "aiSkyDomeLight") else "aiExposure")
    # Validate all attribute names before making any edit. Maya may still reject a
    # locked/connected or invalid numeric value; such partial failures are errors.
    for key in parameters:
        if key not in ("position", "rotation", "scale"):
            require_attr(shape, aliases.get(key, key))
    for key, value in parameters.items():
        if key == "position":
            cmds.xform(transform, worldSpace=True, translation=value)
        elif key == "rotation":
            cmds.xform(transform, worldSpace=True, rotation=value)
        elif key == "scale":
            set_value(transform, "scale", value)
        else:
            set_value(shape, aliases.get(key, key), value)
    return inspect_light(renderer, shape)


def create_light(renderer, light_type, name, parameters=None):
    require_renderer(renderer)
    if light_type not in ARNOLD_TYPES:
        raise ValueError("light_type must be area, point, spot, directional or environment")
    node_type = ARNOLD_TYPES[light_type] if renderer == "arnold" else (
        "RedshiftDomeLight" if light_type == "environment" else "RedshiftPhysicalLight")
    if node_type not in cmds.allNodeTypes():
        raise ValueError("Light node unavailable in this plugin version: " + node_type)
    made = cmds.shadingNode(node_type, asLight=True, name=name + "Shape")
    # Maya/plugin versions differ: shadingNode(asLight=True) can return either
    # the transform or shape. Resolve from actual node type, never its spelling.
    if cmds.nodeType(made) == "transform":
        transform = made
    else:
        transform = cmds.listRelatives(made, parent=True, fullPath=True)[0]
    transform = cmds.rename(transform, name)
    shape = cmds.listRelatives(transform, shapes=True, fullPath=True)[0]
    if renderer == "redshift" and light_type != "environment":
        # Verified against local RS templates; still require the runtime enum.
        labels = cmds.attributeQuery("lightType", node=shape, listEnum=True) or []
        mapping, value = {}, 0
        for item in labels[0].split(":") if labels else []:
            parts = item.split("=", 1)
            if len(parts) == 2:
                value = int(parts[1])
            mapping[parts[0].strip().lower()] = value
            value += 1
        if light_type not in mapping:
            raise ValueError("Unsupported Redshift lightType enum: " + repr(labels))
        set_value(shape, "lightType", mapping[light_type])
    return update_light(renderer, shape, parameters or {})


def set_hdri(renderer, node, path, color_space=""):
    shape = light_shape(renderer, node)
    if not os.path.isabs(path) or not os.path.isfile(path):
        raise ValueError("HDRI must be an existing absolute path: " + path)
    kind = cmds.nodeType(shape)
    if renderer == "redshift" and kind == "RedshiftDomeLight":
        require_attr(shape, "tex0")
        if color_space:
            require_attr(shape, "tex0_colorSpace")
        set_value(shape, "tex0", path)
        if color_space:
            set_value(shape, "tex0_colorSpace", color_space)
        return {"shape": shape, "path": path, "attribute": "tex0"}
    if renderer != "arnold" or kind != "aiSkyDomeLight":
        raise ValueError("HDRI requires an Arnold sky dome or Redshift dome")
    require_attr(shape, "color")
    if cmds.listConnections(shape + ".color", source=True, destination=False):
        raise ValueError("Dome color already connected; inspect existing network before replacing")
    texture = cmds.shadingNode("file", asTexture=True)
    set_value(texture, "fileTextureName", path)
    if color_space:
        set_value(texture, "colorSpace", color_space)
    cmds.connectAttr(texture + ".outColor", shape + ".color")
    return {"shape": shape, "texture": texture, "path": path, "attribute": "color"}


def light_link(renderer, node, objects, linked=True):
    shape = light_shape(renderer, node)
    targets = [unique_node(obj) for obj in objects]
    if not targets:
        raise ValueError("objects must not be empty")
    if linked:
        cmds.lightlink(make=True, light=shape, object=targets)
    else:
        cmds.lightlink(b=True, light=shape, object=targets)
    return {"shape": shape, "linked": linked, "objects": targets,
            "actual_links": cmds.lightlink(query=True, light=shape) or []}


def complete_render(job_id, failure=""):
    """Native post-render callback, on Maya main thread. Never target another scene."""
    failure = text_value(failure)
    job = _render_jobs[job_id]
    if job["finished"]:
        if failure:
            job["error"] += failure
        return
    errors = []
    if (cmds.file(query=True, sceneName=True) or "") != job["scene_path"]:
        errors.append("Scene changed during render; settings were not applied to another scene")
    else:
        for plug, value in job["old"].items():
            node, attr = plug.split(".")
            try:
                set_value(node, attr, value)
            except Exception as exc:
                errors.append(plug + ": " + text_value(exc))
        try:
            cmds.currentTime(job["old_frame"])
        except Exception as exc:
            errors.append("currentTime: " + text_value(exc))
    job["error"] = failure + (u"\nRestore errors: " + u"; ".join(errors) if errors else "")
    job["finished"] = True


def render_status(job_id=""):
    from maya import OpenMaya

    if not job_id:
        return {"jobs": [{"job_id": key, "renderer": job["renderer"],
                          "frame": job["frame"], "finished": job["finished"],
                          "error": job["error"], "output_dir": job["folder"]}
                         for key, job in sorted(_render_jobs.items())]}
    if job_id not in _render_jobs:
        raise ValueError("Unknown render job")
    job = _render_jobs[job_id]
    if job["error"]:
        raise RuntimeError(job["error"])
    if not job["finished"]:
        return {"status": "rendering", "job_id": job_id, "output_dir": job["folder"]}
    outputs = []
    for folder, _dirs, names in os.walk(job["folder"]):
        for name in names:
            path = os.path.join(folder, name)
            if os.path.splitext(name)[1].lower() in (".exr", ".iff", ".png", ".tif", ".tiff", ".jpg", ".jpeg", ".tga", ".bmp") and os.path.getsize(path) > 0:
                # A native completion callback plus actual decoding, not a pause
                # in file size growth, establishes a usable rendered image.
                image = OpenMaya.MImage()
                image.readFromFile(path)
                outputs.append(os.path.abspath(path))
    if not outputs:
        raise RuntimeError("Render ended without an image in " + job["folder"])
    return {"success": True, "status": "complete", "job_id": job_id,
            "renderer": job["renderer"], "frame": job["frame"],
            "output_dir": job["folder"], "output_paths": sorted(outputs),
            "validation": "native_completion_and_image_decode; visual review required"}


def render_frame(renderer, camera, frame, output_dir, width=640, height=480):
    if any(not job["finished"] for job in _render_jobs.values()):
        raise RuntimeError("A render is still active; inspect get_maya_render_status, never replay")
    for key in list(_render_jobs):
        if len(_render_jobs) >= 20 and _render_jobs[key]["finished"]:
            del _render_jobs[key]
    require_renderer(renderer)
    camera = unique_node(camera)
    camera_shapes = [camera] if cmds.nodeType(camera) == "camera" else (
        cmds.listRelatives(camera, shapes=True, fullPath=True, type="camera") or [])
    if len(camera_shapes) != 1:
        raise ValueError("Expected one render camera")
    if not os.path.isabs(output_dir) or not (1 <= width <= 16384 and 1 <= height <= 16384):
        raise ValueError("Absolute output directory and resolution 1..16384 required")
    if int(frame) != frame:
        raise ValueError("Single-frame render requires an integer frame")
    job_id = uuid.uuid4().hex
    folder = os.path.join(output_dir, "fx_maya_" + job_id)
    os.makedirs(folder)
    prefix = os.path.join(folder, "beauty").replace("\\", "/")
    settings = {"defaultRenderGlobals.currentRenderer": renderer,
                "defaultRenderGlobals.imageFilePrefix": prefix,
                "defaultRenderGlobals.animation": renderer == "redshift",
                "defaultRenderGlobals.startFrame": frame,
                "defaultRenderGlobals.endFrame": frame,
                "defaultResolution.width": width, "defaultResolution.height": height}
    if renderer == "redshift":
        # Older RS keeps a legacy prefix alongside Maya's globals (the installed
        # redshiftCreateCommonTab updates both). Preserve both after this job.
        if cmds.attributeQuery("imageFilePrefix", node="redshiftOptions", exists=True):
            settings["redshiftOptions.imageFilePrefix"] = prefix
        for node in cmds.ls(type="camera", long=True) or []:
            settings[node + ".renderable"] = node == camera_shapes[0]
        callback = require_attr("redshiftOptions", "postRenderMel")
        original_callback = cmds.getAttr(callback) or ""
        code = "import fx_maya_bridge.lighting as fx; fx.complete_render('%s')" % job_id
        settings[callback] = original_callback + "\n;python(" + json.dumps(code) + ");"
    old = dict((plug, cmds.getAttr(plug)) for plug in settings)
    # Maya returns None for an unset string; setAttr(type='string') requires "".
    for plug in old:
        if old[plug] is None and cmds.getAttr(plug, type=True) == "string":
            old[plug] = ""
        if not cmds.getAttr(plug, settable=True):
            raise ValueError("Render setting is locked or connected: " + plug)
    old_frame = cmds.currentTime(query=True)
    job = {"old": old, "old_frame": old_frame, "folder": folder,
           "renderer": renderer, "frame": frame, "finished": False, "error": "",
           "scene_path": cmds.file(query=True, sceneName=True) or ""}
    _render_jobs[job_id] = job
    try:
        for plug, value in settings.items():
            node, attr = plug.split(".")
            set_value(node, attr, value)
        cmds.currentTime(frame)
        if renderer == "arnold":
            # AutoMaya's sequence form writes images rather than only Render View.
            cmds.arnoldRender(batch=True, seq=str(int(frame)), camera=camera_shapes[0], width=width, height=height)
            complete_render(job_id)
        else:
            # Batch flags are for batch Maya. In GUI Maya the native animation
            # path writes frames; the range above is exactly the requested frame.
            camera_transform = cmds.listRelatives(camera_shapes[0], parent=True, fullPath=True)[0]
            arguments = {"render": True, "camera": camera_transform}
            arguments["batch" if cmds.about(batch=True) else "animation"] = True
            cmds.rsRender(**arguments)
        # The local external MCP waits for files after returning control to Maya.
        # RS 3.5 finishes image writing during idle; never wait in this callback.
        result = {"status": "submitted", "job_id": job_id, "renderer": renderer,
                  "frame": frame, "output_dir": folder}
    except Exception:
        complete_render(job_id, traceback.format_exc())
    if job["error"]:
        raise RuntimeError(job["error"])
    return result


class Capture(object):
    def __init__(self):
        self.parts = []

    def write(self, value):
        if isinstance(value, bytes):
            value = value.decode("utf-8", "replace")
        self.parts.append(value)

    def flush(self):
        pass


def execute_python(code):
    """Fresh namespace. Set result to a JSON value. Capture both streams on error."""
    stdout, stderr = Capture(), Capture()
    previous = sys.stdout, sys.stderr
    namespace = {"cmds": cmds, "__name__": "__fx_maya__"}
    try:
        sys.stdout, sys.stderr = stdout, stderr
        eval(compile(code, "<fx-maya>", "exec"), namespace, namespace)
        result = namespace.get("result")
        json.dumps(result, allow_nan=False)
        response = {"success": True, "result": result}
    except Exception:
        response = {"success": False, "error": traceback.format_exc()}
    finally:
        sys.stdout, sys.stderr = previous
    response.update(stdout=u"".join(stdout.parts), stderr=u"".join(stderr.parts))
    return response


def dispatch(command, params):
    if any(not job["finished"] for job in _render_jobs.values()) and command not in {
        "scene_info", "renderer_status", "list_lights", "inspect_light", "render_status"
    }:
        raise RuntimeError("Maya render active; scene writes are blocked until native completion")
    handlers = {"renderer_status": renderer_status, "list_lights": list_lights,
                "inspect_light": inspect_light, "create_light": create_light,
                "update_light": update_light, "set_hdri": set_hdri,
                "light_link": light_link, "render_frame": render_frame,
                "render_status": render_status,
                "execute_python": execute_python}
    if command == "scene_info":
        return {"scene_path": cmds.file(query=True, sceneName=True) or "",
                "modified": cmds.file(query=True, modified=True), "renderers": renderer_status()}
    if command == "save_scene":
        path = params["path"]
        if not os.path.isabs(path) or os.path.splitext(path)[1].lower() not in (".ma", ".mb"):
            raise ValueError("Absolute .ma/.mb path required")
        current = cmds.file(query=True, sceneName=True)
        if current and current != path:
            raise ValueError("Save requires current scene path; rename explicitly under the task gate")
        if not current:
            if os.path.exists(path):
                raise ValueError("New scene destination already exists; refusing overwrite")
            cmds.file(rename=path)
        cmds.file(save=True, type="mayaAscii" if path.lower().endswith(".ma") else "mayaBinary")
        return {"success": True, "path": cmds.file(query=True, sceneName=True)}
    if command == "verify_network":
        unknown = cmds.ls(type="unknown", long=True) or []
        return {"healthy": not unknown, "unknown_nodes": unknown,
                "scope": "unknown-node structural check only; render and visual review separately"}
    if command not in handlers:
        raise ValueError("Unknown Maya command: " + str(command))
    return handlers[command](**params)
