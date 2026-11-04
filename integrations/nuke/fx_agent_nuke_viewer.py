"""Native Viewer APIs only: no desktop input, focus changes, or screen scraping."""

import math
import os
import uuid
from importlib import import_module
from pathlib import Path


def _viewer(name):
    import nuke

    if not nuke.GUI:
        raise ValueError("Viewer operations require a GUI Nuke session")
    node = nuke.toNode(name)
    if node is None or node.Class() != "Viewer":
        raise ValueError(f"Not a Viewer node: {name}")
    return nuke, node


def get_script_info(params):
    import fx_agent_nuke_addon as addon

    result = addon._fx_original_script_info(params)
    fmt = addon._get_nuke().root().format()
    result["result"]["format"] = {
        "width": fmt.width(), "height": fmt.height(),
        "pixel_aspect": fmt.pixelAspect(), "name": fmt.name(),
    }
    return result


def _window(nuke, viewer):
    window = nuke.activeViewer()
    return window if window is not None and window.node().fullName() == viewer.fullName() else None


def _fit_actions(nuke):
    menu = nuke.menu("Viewer") if hasattr(nuke, "menu") else None
    found = []

    def walk(parent, prefix=""):
        for item in parent.items():
            name = prefix + item.name()
            if hasattr(item, "items"):
                walk(item, name + "/")
            elif item.shortcut().strip().lower() == "f":
                found.append((name, item))

    if menu is not None:
        walk(menu)
    return found


def get_viewer_state(params):
    nuke, viewer = _viewer(params["viewer_name"])
    keys = ("frame", "channels", "gain", "gamma", "viewerProcess", "input_number", "zoom_lock")
    settings = {key: viewer[key].value() for key in keys if key in viewer.knobs()}
    window = _window(nuke, viewer)
    a_input = window.activeInput() if window else int(settings.get("input_number", 0))
    source = viewer.input(a_input) if a_input is not None and a_input >= 0 else None
    return {"status": "ok", "result": {
        "viewer_name": viewer.fullName(), "script": nuke.root().name(),
        "settings": settings, "a_input": a_input,
        "b_input": window.activeInput(secondary=True) if window else None,
        "inputs": [viewer.input(i).fullName() if viewer.input(i) else None for i in range(viewer.inputs())],
        "source_node": source.fullName() if source else None,
        "available_channels": list(source.channels()) if source else [],
        "active_viewer_window": window is not None,
        "fit_actions": [name for name, _ in _fit_actions(nuke)],
    }}


def set_viewer_state(params):
    nuke, viewer = _viewer(params["viewer_name"])
    changes = {}
    channels = params.get("channels")
    if channels is not None:
        channels = {"red": "rgba.red", "green": "rgba.green", "blue": "rgba.blue",
                    "alpha": "rgba.alpha"}.get(channels, channels)
        available = get_viewer_state(params)["result"]["available_channels"]
        layers = {channel.split(".")[0] for channel in available}
        if channels not in layers | set(available):
            raise ValueError(f"Channel or layer is unavailable: {channels}")
        changes["channels"] = channels
    exposure, gamma = params.get("exposure"), params.get("gamma")
    if exposure is not None:
        if not math.isfinite(exposure) or not -20 <= exposure <= 20:
            raise ValueError("Exposure must be finite and between -20 and 20 stops")
        changes["gain"] = 2.0 ** exposure
    if gamma is not None:
        if not math.isfinite(gamma) or gamma <= 0:
            raise ValueError("Gamma must be finite and positive")
        changes["gamma"] = gamma
    if params.get("frame") is not None:
        changes["frame"] = int(params["frame"])
    if params.get("viewer_process") is not None:
        key = "viewerProcess"
        if key not in viewer.knobs() or params["viewer_process"] not in viewer[key].values():
            raise ValueError("Viewer process is unavailable")
        changes[key] = params["viewer_process"]
    window = _window(nuke, viewer)
    inputs = [(params.get(key), secondary) for key, secondary in (("a_input", False), ("b_input", True))]
    for index, _ in inputs:
        if index is not None:
            if window is None:
                raise ValueError("A/B activation requires this Viewer to be the active Viewer; no focus was changed")
            if index < 0 or viewer.input(index) is None:
                raise ValueError(f"Viewer input is not connected: {index}")
    if any(key not in viewer.knobs() for key in changes):
        raise ValueError("Requested Viewer control is unavailable")
    for key, value in changes.items():
        viewer[key].setValue(value)
    for index, secondary in inputs:
        if index is not None:
            window.activateInput(index, secondary=secondary)
    return get_viewer_state(params)


def connect_viewer(params):
    nuke, viewer = _viewer(params["viewer_name"])
    source = nuke.toNode(params["source_node"])
    index = params.get("input_index", 0)
    if source is None or source is viewer or not 0 <= index <= 9:
        raise ValueError("Specify an existing source and a Viewer input from 0 to 9")
    viewer.setInput(index, source)
    return get_viewer_state(params)


def fit_viewer(params):
    nuke, viewer = _viewer(params["viewer_name"])
    if _window(nuke, viewer) is None:
        raise ValueError("Fit requires this Viewer to be the active Viewer; no focus was changed")
    actions = _fit_actions(nuke)
    if len(actions) != 1:
        raise ValueError("Cannot identify a unique native Viewer F action; no keyboard input was sent")
    name, action = actions[0]
    action.invoke()
    result = get_viewer_state(params)
    result["result"]["invoked_action"] = name
    return result


def _capture_directory():
    root = os.getenv("FX_AGENT_DATA_ROOT")
    return (Path(root) if root else Path(__file__).resolve().parents[2] / "data") / "screenshots" / "nuke"


def _image_size(path):
    import fx_agent_nuke_addon as addon

    qt_gui = import_module(addon.QObject.__module__.split(".")[0] + ".QtGui")
    image = qt_gui.QImage(str(path))
    if image.isNull():
        raise RuntimeError("Nuke did not produce a readable Viewer capture")
    return [image.width(), image.height()]


def capture_viewer(params):
    _, viewer = _viewer(params["viewer_name"])
    value = params.get("output_path")
    path = Path(value) if value else _capture_directory() / f"{os.getpid()}-{uuid.uuid4().hex}.jpg"
    if not path.is_absolute() or path.suffix.lower() != ".jpg":
        raise ValueError("Viewer.capture requires an absolute .jpg output path")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Reserve a unique destination; never overwrite a user's existing image.
    with path.open("xb"):
        pass
    try:
        viewer.capture(str(path))
        dimensions = _image_size(path)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    result = get_viewer_state(params)["result"]
    result.update({"path": str(path), "dimensions": dimensions,
                   "capture_method": "nuke.Viewer.capture",
                   "render_complete": "not_guaranteed"})
    return {"status": "ok", "result": result}


def _main_window():
    import fx_agent_nuke_addon as addon

    widgets = import_module(addon.QObject.__module__.split(".")[0] + ".QtWidgets")
    matches = [w for w in widgets.QApplication.topLevelWidgets() if w.objectName() == "NukeMainWindow"]
    if len(matches) != 1:
        matches = [w for w in widgets.QApplication.topLevelWidgets()
                   if w.metaObject().className() == "Foundry::UI::DockMainWindow"]
    if len(matches) != 1:
        raise ValueError("Cannot identify a unique Nuke main widget; no desktop capture attempted")
    return matches[0]


def capture_interface(params):
    window = _main_window()
    value = params.get("output_path")
    path = Path(value) if value else _capture_directory() / f"{os.getpid()}-{uuid.uuid4().hex}-ui.png"
    if not path.is_absolute() or path.suffix.lower() != ".png":
        raise ValueError("Interface capture requires an absolute .png path")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb"):
        pass
    try:
        pixmap = window.grab()
        if pixmap.isNull() or not pixmap.save(str(path), "PNG"):
            raise RuntimeError("Nuke widget capture failed")
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return {"status": "ok", "result": {
        "path": str(path), "dimensions": [pixmap.width(), pixmap.height()],
        "capture_method": "Nuke QWidget.grab", "pid": os.getpid(),
    }}


def frame_graph(params):
    import nuke

    zoom = params.get("zoom", 0.2)
    if not math.isfinite(zoom) or not 0.01 <= zoom <= 4:
        raise ValueError("Graph zoom must be finite and between 0.01 and 4")
    nodes = [nuke.toNode(name) for name in params["node_names"]]
    if not nodes or any(node is None for node in nodes):
        raise ValueError("Specify existing nodes to frame")
    if any(node.parent() != nodes[0].parent() for node in nodes):
        raise ValueError("Graph framing cannot cross Group boundaries")
    left, top = min(node.xpos() for node in nodes), min(node.ypos() for node in nodes)
    right = max(node.xpos() + node.screenWidth() for node in nodes)
    bottom = max(node.ypos() + node.screenHeight() for node in nodes)
    center = [(left + right) / 2, (top + bottom) / 2]
    nuke.zoom(zoom, center)
    return {"status": "ok", "result": {"zoom": zoom, "center": center,
                                         "node_names": [node.fullName() for node in nodes]}}


def install(addon):
    if not hasattr(addon, "_fx_original_script_info"):
        addon._fx_original_script_info = addon.COMMANDS["get_script_info"]
    addon.COMMANDS.update({
        "get_script_info": get_script_info,
        "get_nuke_viewer_state": get_viewer_state,
        "set_nuke_viewer_state": set_viewer_state,
        "connect_nuke_viewer": connect_viewer,
        "capture_nuke_viewer": capture_viewer,
        "fit_nuke_viewer": fit_viewer,
        "capture_nuke_interface": capture_interface,
        "frame_nuke_graph": frame_graph,
    })
