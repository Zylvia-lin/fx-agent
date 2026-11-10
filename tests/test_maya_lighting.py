"""Renderer adapter regressions found in real Maya 2020; no simulated render claims."""

import importlib.util
import os
import sys
import types
from pathlib import Path
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def lighting(monkeypatch):
    folder = ROOT / f"integrations/maya/{os.environ.get('FX_MAYA_TEST_RUNTIME', 'py2')}/fx_maya_bridge"
    package = types.ModuleType("lighting_test")
    package.__path__ = [str(folder)]
    monkeypatch.setitem(sys.modules, "lighting_test", package)
    maya = types.ModuleType("maya")
    maya.cmds = Mock()
    monkeypatch.setitem(sys.modules, "maya", maya)
    spec = importlib.util.spec_from_file_location("lighting_test.lighting", folder / "lighting.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "lighting_test.lighting", module)
    spec.loader.exec_module(module)
    return module, maya.cmds


def test_create_accepts_shadingnode_transform_result(lighting, monkeypatch):
    module, cmds = lighting
    cmds.pluginInfo.side_effect = lambda plugin, **kw: True if "loaded" in kw else "test-version"
    cmds.allNodeTypes.return_value = ["aiAreaLight"]
    cmds.shadingNode.return_value = "temporaryTransform"
    cmds.nodeType.return_value = "transform"
    cmds.rename.return_value = "keyLight"
    cmds.listRelatives.return_value = ["|keyLight|keyLightShape"]
    updated = Mock(return_value={"shape": "|keyLight|keyLightShape"})
    monkeypatch.setattr(module, "update_light", updated)
    assert module.create_light("arnold", "area", "keyLight")["shape"].endswith("keyLightShape")
    cmds.rename.assert_called_once_with("temporaryTransform", "keyLight")
    updated.assert_called_once_with("arnold", "|keyLight|keyLightShape", {})


def test_dome_exposure_and_intensity_use_actual_rs_attributes(lighting, monkeypatch):
    module, cmds = lighting
    monkeypatch.setattr(module, "light_shape", lambda renderer, node: "domeShape")
    monkeypatch.setattr(module, "inspect_light", lambda renderer, node: {})
    cmds.nodeType.return_value = "RedshiftDomeLight"
    cmds.listRelatives.return_value = ["dome"]
    cmds.attributeQuery.return_value = True
    setter = Mock()
    monkeypatch.setattr(module, "set_value", setter)
    module.update_light("redshift", "dome", {"exposure": 2, "intensity": 3})
    assert setter.call_args_list[0].args == ("domeShape", "exposure0", 2)
    assert setter.call_args_list[1].args == ("domeShape", "multiplier", 3)
    cmds.attributeQuery.return_value = False
    with pytest.raises(ValueError, match="Unsupported attribute"):
        module.update_light("redshift", "dome", {"exposure": 2})


def test_time_attributes_and_unicode_errors_survive(lighting):
    module, cmds = lighting
    cmds.attributeQuery.return_value = True
    cmds.getAttr.return_value = "time"
    module.set_value("defaultRenderGlobals", "startFrame", 1)
    cmds.setAttr.assert_called_once_with("defaultRenderGlobals.startFrame", 1)
    assert module.text_value(ValueError("中文错误")) == "中文错误"
    result = module.execute_python("import sys\nprint('灯光')\nsys.stderr.write('诊断')\nraise ValueError('错误')")
    assert result["success"] is False
    assert "灯光" in result["stdout"] and "诊断" in result["stderr"] and "错误" in result["error"]


def test_unloaded_renderer_is_not_automatically_loaded(lighting):
    module, cmds = lighting
    cmds.pluginInfo.return_value = False
    with pytest.raises(RuntimeError, match="not loaded"):
        module.create_light("redshift", "area", "key")
    cmds.loadPlugin.assert_not_called()
    cmds.shadingNode.assert_not_called()


@pytest.mark.parametrize("renderer", ["arnold", "redshift"])
def test_bridge_returns_submission_not_success_and_restores_settings(lighting, monkeypatch, tmp_path, renderer):
    module, cmds = lighting
    monkeypatch.setattr(module, "require_renderer", lambda renderer: {})
    monkeypatch.setattr(module, "unique_node", lambda name: "cameraShape")
    cmds.nodeType.return_value = "camera"
    cmds.listRelatives.return_value = ["cameraTransform"]
    cmds.ls.return_value = ["cameraShape"]
    cmds.about.return_value = False
    cmds.getAttr.side_effect = lambda plug, **kw: True if kw.get("settable") else (
        "string" if kw.get("type") else (None if plug.endswith(("imageFilePrefix", "postRenderMel")) else 1))
    cmds.currentTime.return_value = 12
    setter = Mock()
    monkeypatch.setattr(module, "set_value", setter)
    receipt = module.render_frame(renderer, "camera", 1, str(tmp_path), 64, 64)
    assert receipt["status"] == "submitted" and "success" not in receipt
    assert "output_paths" not in receipt
    if renderer == "redshift":
        with pytest.raises(RuntimeError, match="render active"):
            module.dispatch("execute_python", {"code": "result=True"})
        module.complete_render(receipt["job_id"])
    if renderer == "redshift":
        assert cmds.rsRender.call_args.kwargs == {"render": True, "animation": True, "camera": "cameraTransform"}
    else:
        assert cmds.arnoldRender.call_args.kwargs["batch"] is True
    assert any(call.args == ("defaultRenderGlobals", "imageFilePrefix", "") for call in setter.call_args_list)
    assert cmds.currentTime.call_args.args == (12,)


def test_restore_failure_keeps_render_error_and_attempts_remaining_settings(lighting, monkeypatch, tmp_path):
    module, cmds = lighting
    monkeypatch.setattr(module, "require_renderer", lambda renderer: {})
    monkeypatch.setattr(module, "unique_node", lambda name: "cameraShape")
    cmds.nodeType.return_value = "camera"
    cmds.getAttr.side_effect = lambda plug, **kw: True if kw.get("settable") else 1
    cmds.currentTime.return_value = 12
    cmds.arnoldRender.side_effect = RuntimeError("original renderer failure")
    calls = []

    def setter(node, attr, value):
        calls.append((node, attr, value))
        if attr == "width" and value == 1:
            raise RuntimeError("restore width failure")

    monkeypatch.setattr(module, "set_value", setter)
    with pytest.raises(RuntimeError) as error:
        module.render_frame("arnold", "camera", 1, str(tmp_path), 64, 64)
    assert "original renderer failure" in str(error.value)
    assert "restore width failure" in str(error.value)
    assert len(calls) == 14
    assert cmds.currentTime.call_args.args == (12,)


def test_output_requires_native_completion_and_successful_decode(lighting, monkeypatch, tmp_path):
    module, cmds = lighting
    image = Mock()
    factory = Mock(return_value=image)
    monkeypatch.setattr(sys.modules["maya"], "OpenMaya", types.SimpleNamespace(MImage=factory), raising=False)
    (tmp_path / "beauty.iff").write_bytes(b"partial header")
    module._render_jobs["one"] = {"finished": False, "error": "", "folder": str(tmp_path),
                                  "renderer": "redshift", "frame": 1, "old": {},
                                  "old_frame": 1, "scene_path": "old.ma"}
    assert module.render_status("one")["status"] == "rendering"
    factory.assert_not_called()
    cmds.file.return_value = "old.ma"
    module.complete_render("one")
    image.readFromFile.side_effect = RuntimeError("truncated image")
    with pytest.raises(RuntimeError, match="truncated"):
        module.render_status("one")
    image.readFromFile.side_effect = None
    assert module.render_status("one")["success"]


def test_render_callback_never_restores_settings_in_a_different_scene(lighting):
    module, cmds = lighting
    module._render_jobs["one"] = {"finished": False, "error": "", "old": {"node.attr": 1},
                                  "old_frame": 1, "scene_path": "old.ma"}
    cmds.file.return_value = "another.ma"
    module.complete_render("one")
    cmds.setAttr.assert_not_called()
    cmds.currentTime.assert_not_called()
    assert "another scene" in module._render_jobs["one"]["error"]
