from unittest.mock import Mock

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from fx_agent.maya import server


@pytest.mark.asyncio
async def test_load_scene_guards_path_and_adopts_actual_maya_path(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "install_task_gates", lambda *a: None)
    monkeypatch.setattr(server, "register_shared_tools", lambda *a: None)
    scene = tmp_path / 'cottage.ma'
    scene.touch()
    connection = Mock()
    connection.target = {'scene_path': ''}
    connection.send.return_value = {'success': True, 'result': {'path': scene.as_posix()}}
    mcp = server.build_server(connection)
    result = await mcp._tool_manager.call_tool('load_scene', {'file_path': str(scene)}, convert_result=False)
    assert result['path'] == scene.as_posix()
    assert connection.target['scene_path'] == scene.as_posix()
    code = connection.send.call_args.args[1]['code']
    assert 'force=False' in code and 'Unsaved scene changes' in code
    compile(code, '<load_scene>', 'exec')
    connection.send.reset_mock()
    with pytest.raises(ToolError, match='absolute'):
        await mcp._tool_manager.call_tool('load_scene', {'file_path': 'missing.ma'})
    connection.send.assert_not_called()
