import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def api(monkeypatch, tmp_path):
    class Rop:
        def path(self):
            return '/obj/ropnet1/test'

    class Viewer:
        rendering = False
        active = False
        starts = 0

        def name(self): return 'test_view'
        def type(self): return 'IPR'
        def isRendering(self): return self.rendering
        def isActive(self): return self.active
        def ropNode(self): return rop
        def setAutoUpdate(self, value): pass
        def setPreview(self, value): self.preview = value
        def isPreviewOn(self): return self.preview
        def setRopNode(self, value): assert value is rop
        def startRender(self): self.starts += 1
        def imageResolution(self): return (2, 1)
        def planes(self): return ('C',)
        def pixels(self, plane): return ((0, 1, 0, 0), (0, 0, 0, 0))
        def saveFrame(self, path, snapshot, xres, yres, color, alpha, scope, lut, gamma, convert):
            Path(path).write_bytes(b'checked image')
            return True
        def killRender(self): self.rendering = self.active = False

    rop = Rop()
    viewer = Viewer()
    host = SimpleNamespace(isUIAvailable=lambda: True, node=lambda path: rop,
        RopNode=Rop, ui=SimpleNamespace(paneTabs=lambda: [viewer]),
        paneTabType=SimpleNamespace(IPRViewer='IPR'), session=SimpleNamespace(),
        frame=lambda: 155, setFrame=lambda value: None, expandString=lambda value: value,
        OperationFailed=RuntimeError)
    monkeypatch.setitem(__import__('sys').modules, 'hou', host)
    path = Path(__file__).resolve().parents[1] / 'integrations/houdini/fx_agent_render_view.py'
    spec = importlib.util.spec_from_file_location('render_view_live_adapter', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, viewer, host


def test_emissive_rgb_with_zero_alpha_is_reported_as_zero_alpha(api, tmp_path):
    module, viewer, host = api
    request = module.start('/obj/ropnet1/test', 'test_view')
    result = module.save(str(tmp_path / 'check.exr'), 'test_view', request['token'])
    assert result['saved']
    assert result['rgb_nonzero_pixels'] == 1
    assert result['alpha_nonzero_pixels'] == 0
    assert result['rgba_max'] == [0, 1, 0, 0]


def test_running_render_and_stale_token_cannot_be_saved_or_replaced(api, tmp_path):
    module, viewer, host = api
    request = module.start('/obj/ropnet1/test', 'test_view')
    viewer.rendering = True
    with pytest.raises(ValueError, match='busy'):
        module.start('/obj/ropnet1/test', 'test_view')
    with pytest.raises(ValueError, match='still running'):
        module.save(str(tmp_path / 'check.exr'), 'test_view', request['token'])
    with pytest.raises(ValueError, match='Stale'):
        module.stop('test_view', 'another-render')
    assert viewer.rendering
    assert viewer.starts == 1


def test_frame_change_and_existing_file_require_explicit_action(api, tmp_path):
    module, viewer, host = api
    request = module.start('/obj/ropnet1/test', 'test_view')
    host.frame = lambda: 156
    with pytest.raises(ValueError, match='frame changed'):
        module.save(str(tmp_path / 'check.exr'), 'test_view', request['token'])
    host.frame = lambda: 155
    path = tmp_path / 'existing.exr'
    path.write_bytes(b'user file')
    with pytest.raises(ValueError, match='Output exists'):
        module.save(str(path), 'test_view', request['token'])
    assert path.read_bytes() == b'user file'


@pytest.mark.asyncio
async def test_render_view_tools_are_registered():
    from fx_agent.server import get_server
    tools = {tool.name: tool for tool in await get_server().list_tools()}
    for name in ['start', 'status', 'save', 'stop']:
        assert 'fx_render_view_' + name in tools
        assert tools['fx_render_view_' + name].inputSchema['additionalProperties'] is False
