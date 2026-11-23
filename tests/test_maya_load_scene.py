import threading
from unittest.mock import Mock

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from fx_agent.maya import server


@pytest.mark.asyncio
async def test_load_scene_guards_path_and_adopts_actual_maya_path(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "install_task_gates", lambda *a: None)
    monkeypatch.setattr(server, "register_shared_tools", lambda *a: None)
    scene = tmp_path / "cottage.ma"
    scene.touch()
    connection = Mock()
    connection.lock = threading.RLock()
    connection.target = {"scene_path": ""}
    connection.send.return_value = {"path": scene.as_posix()}
    mcp = server.build_server(connection)
    result = await mcp._tool_manager.call_tool("load_scene", {"file_path": str(scene)}, convert_result=False)
    assert result["path"] == scene.as_posix()
    assert connection.target["scene_path"] == scene.as_posix()
    connection.send.assert_called_once_with(
        "load_scene", {"path": scene.as_posix(), "discard_unsaved": False}, timeout=120
    )
    connection.send.reset_mock()
    with pytest.raises(ToolError, match="absolute"):
        await mcp._tool_manager.call_tool("load_scene", {"file_path": "missing.ma"})
    connection.send.assert_not_called()
