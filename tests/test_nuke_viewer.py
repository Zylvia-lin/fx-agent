"""Viewer inspection never drives the desktop or captures another application."""

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.fixture
def viewer_module(monkeypatch, tmp_path):
    path = Path(__file__).resolve().parents[1] / "integrations/nuke/fx_agent_nuke_viewer.py"
    spec = importlib.util.spec_from_file_location("fx_viewer_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    values = {"channels": "rgba", "gain": 1.0, "gamma": 1.0, "frame": 113,
              "viewerProcess": "sRGB", "input_number": 0}
    knobs = {}
    for key, value in values.items():
        knob = Mock()
        knob.value.side_effect = lambda key=key: values[key]
        knob.setValue.side_effect = lambda value, key=key: values.__setitem__(key, value)
        knobs[key] = knob
    knobs["viewerProcess"].values.return_value = ["sRGB", "None"]
    source = Mock()
    source.name.return_value = "Read1"
    source.fullName.return_value = "Read1"
    source.channels.return_value = ["rgba.red", "rgba.green", "rgba.blue", "rgba.alpha"]

    class Viewer:
        def Class(self): return "Viewer"
        def name(self): return "Viewer1"
        def fullName(self): return "Viewer1"
        def knobs(self): return knobs
        def __getitem__(self, key): return knobs[key]
        def input(self, index): return source if index in (0, 1) else None
        def inputs(self): return 2
        def capture(self, path): Path(path).write_bytes(b"test-jpeg")
        setInput = Mock()

    viewer = Viewer()
    window = Mock()
    window.node.return_value = viewer
    window.activeInput.side_effect = lambda secondary=False: 1 if secondary else 0
    host = SimpleNamespace(GUI=True, toNode=lambda name: viewer if name == "Viewer1" else source,
                           allNodes=lambda cls: [viewer], activeViewer=lambda: window,
                           root=lambda: SimpleNamespace(name=lambda: "shot.nk"))
    monkeypatch.setitem(sys.modules, "nuke", host)
    monkeypatch.setattr(module, "_capture_directory", lambda: tmp_path)
    monkeypatch.setattr(module, "_image_size", lambda path: [640, 360])
    return module, viewer, values, window, host


def test_state_exposes_actual_channels_and_viewer_controls(viewer_module):
    module, _, _, _, _ = viewer_module
    state = module.get_viewer_state({"viewer_name": "Viewer1"})["result"]
    assert "rgba.alpha" in state["available_channels"]
    assert state["settings"]["gamma"] == 1.0
    assert state["a_input"] == 0 and state["b_input"] == 1


def test_channel_exposure_frame_and_readback(viewer_module):
    module, _, values, window, _ = viewer_module
    result = module.set_viewer_state({"viewer_name": "Viewer1", "channels": "rgba.alpha",
                                      "exposure": 2, "gamma": 1.2, "frame": 120, "a_input": 1})
    assert values["gain"] == 4
    assert values["channels"] == "rgba.alpha" and values["frame"] == 120
    assert result["result"]["settings"]["gamma"] == 1.2
    window.activateInput.assert_called_once_with(1, secondary=False)


def test_invalid_setting_does_not_partially_apply(viewer_module):
    module, _, values, _, _ = viewer_module
    before = values.copy()
    with pytest.raises(ValueError):
        module.set_viewer_state({"viewer_name": "Viewer1", "frame": 120, "gamma": -1})
    assert values == before
    with pytest.raises(ValueError):
        module.set_viewer_state({"viewer_name": "Viewer1", "channels": "missing.alpha"})
    with pytest.raises(ValueError):
        module.set_viewer_state({"viewer_name": "Viewer1", "a_input": 9})
    assert values == before


def test_capture_uses_native_viewer_and_never_overwrites(viewer_module):
    module, _, values, window, _ = viewer_module
    before = values.copy()
    result = module.capture_viewer({"viewer_name": "Viewer1"})["result"]
    assert Path(result["path"]).read_bytes() == b"test-jpeg"
    assert result["render_complete"] == "not_guaranteed"
    assert result["dimensions"] == [640, 360]
    assert before == values
    window.activateInput.assert_not_called()
    with pytest.raises(FileExistsError):
        module.capture_viewer({"viewer_name": "Viewer1", "output_path": result["path"]})


def test_wrong_active_window_is_not_silently_activated(viewer_module):
    module, _, values, _, host = viewer_module
    host.activeViewer = lambda: None
    before = values.copy()
    with pytest.raises(ValueError, match="active"):
        module.set_viewer_state({"viewer_name": "Viewer1", "frame": 120, "a_input": 0})
    assert values == before


def test_same_short_name_in_group_cannot_target_wrong_viewer(viewer_module):
    module, _, values, window, _ = viewer_module
    wrong = Mock()
    wrong.name.return_value = "Viewer1"
    wrong.fullName.return_value = "Group1.Viewer1"
    window.node.return_value = wrong
    before = values.copy()
    with pytest.raises(ValueError, match="active"):
        module.set_viewer_state({"viewer_name": "Viewer1", "frame": 120, "a_input": 0})
    with pytest.raises(ValueError, match="active"):
        module.fit_viewer({"viewer_name": "Viewer1"})
    window.activateInput.assert_not_called()
    assert values == before


def test_fit_invokes_only_unique_native_f_action(viewer_module):
    module, _, _, _, host = viewer_module
    action = Mock(spec=["name", "shortcut", "invoke"])
    action.name.return_value = "Focus Selection"
    action.shortcut.return_value = "F"
    host.menu = lambda name: SimpleNamespace(items=lambda: [action])
    result = module.fit_viewer({"viewer_name": "Viewer1"})
    assert result["result"]["invoked_action"] == "Focus Selection"
    action.invoke.assert_called_once()
    host.menu = lambda name: SimpleNamespace(items=lambda: [action, action])
    with pytest.raises(ValueError, match="unique"):
        module.fit_viewer({"viewer_name": "Viewer1"})
    host.menu = lambda name: None
    with pytest.raises(ValueError, match="unique"):
        module.fit_viewer({"viewer_name": "Viewer1"})
    assert action.invoke.call_count == 1


def test_main_thread_result_with_nuke_object_is_json_safe(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "integrations/nuke/fx_agent_nuke.py"
    spec = importlib.util.spec_from_file_location("fx_json_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    class Bridge:
        def _run_in_nuke(self, callback, *args): return callback(*args)
    addon = SimpleNamespace(NukeMCPServer=Bridge, _get_nuke=lambda: SimpleNamespace(
        GUI=True, executeInMainThreadWithResult=lambda callback, args: callback(*args)))
    monkeypatch.setitem(sys.modules, "fx_agent_nuke_addon", addon)
    module._install_dispatch(addon)
    value = Bridge()._run_in_nuke(lambda: {"format": object(), "gain": [1.0, 2.0]})
    json.dumps(value)
    assert value["gain"] == [1.0, 2.0]


def test_script_format_contains_stable_dimensions(viewer_module, monkeypatch):
    module, _, _, _, _ = viewer_module
    fmt = SimpleNamespace(width=lambda: 1920, height=lambda: 804,
                          pixelAspect=lambda: 1, name=lambda: "shot")
    addon = SimpleNamespace(
        _fx_original_script_info=lambda params: {"status": "ok", "result": {"format": "<Format object>"}},
        _get_nuke=lambda: SimpleNamespace(root=lambda: SimpleNamespace(format=lambda: fmt)),
    )
    monkeypatch.setitem(sys.modules, "fx_agent_nuke_addon", addon)
    assert module.get_script_info({})["result"]["format"] == {
        "width": 1920, "height": 804, "pixel_aspect": 1, "name": "shot",
    }
