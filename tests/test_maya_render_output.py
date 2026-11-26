"""Wait for native completion, never infer it from a paused file writer."""

import asyncio
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


def render_tool(monkeypatch, send):
    from fx_agent.maya import server

    monkeypatch.setattr(server, 'register_shared_tools', lambda mcp: None)
    connection = SimpleNamespace(send=Mock(side_effect=send), failed=False)
    mcp = server.build_server(connection)
    fn = mcp._tool_manager.get_tool('render_maya_frame').fn
    return (lambda *args, **kwargs: asyncio.run(fn(*args, **kwargs))), connection


def test_paused_partial_writer_is_not_mistaken_for_native_completion(monkeypatch, tmp_path):
    finished = threading.Event()
    path = tmp_path / 'beauty.iff'
    receipt = {'job_id': 'one', 'status': 'submitted', 'output_dir': str(tmp_path)}

    def send(command, *args, **kwargs):
        if command == 'render_frame':
            return receipt
        return {'success': True, 'status': 'complete', 'output_paths': [str(path)]} if finished.is_set() else {'status': 'rendering'}

    tool, connection = render_tool(monkeypatch, send)

    def writer():
        with path.open('wb') as stream:
            stream.write(b'header')
            stream.flush()
            time.sleep(0.6)
            stream.write(b'pixels')
        finished.set()

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        result = tool('redshift', 'persp', 1, str(tmp_path), timeout=2)
        assert result['success'] and finished.is_set()
        assert path.read_bytes() == b'headerpixels'
        assert [c.args[0] for c in connection.send.call_args_list].count('render_frame') == 1
        assert not connection.failed
    finally:
        thread.join()


def test_missing_native_completion_times_out_without_replay(monkeypatch, tmp_path):
    def send(command, *args, **kwargs):
        return {'job_id': 'one', 'status': 'rendering', 'output_dir': str(tmp_path)}

    tool, connection = render_tool(monkeypatch, send)
    with pytest.raises(TimeoutError, match='Do not replay'):
        tool('arnold', 'persp', 1, str(tmp_path), timeout=0.02)
    assert connection.failed
    assert [c.args[0] for c in connection.send.call_args_list].count('render_frame') == 1


def test_monitoring_exception_locks_unknown_operation(monkeypatch, tmp_path):
    def send(command, *args, **kwargs):
        if command == 'render_frame':
            return {'job_id': 'one', 'output_dir': str(tmp_path)}
        raise PermissionError('image decoder could not read output')

    tool, connection = render_tool(monkeypatch, send)
    with pytest.raises(PermissionError):
        tool('redshift', 'persp', 1, str(tmp_path), timeout=1)
    assert connection.failed


@pytest.mark.asyncio
async def test_render_keeps_its_maya_session_when_selection_changes(monkeypatch, tmp_path):
    from fx_agent.maya import client, server
    from fx_agent.task_gates import TaskSpec

    monkeypatch.setenv("FX_AGENT_DATA_ROOT", str(tmp_path))
    first = {"port": 18770, "pid": 71, "session_id": "a", "scene_path": str(tmp_path / "a.ma")}
    second = {"port": 18771, "pid": 72, "session_id": "b", "scene_path": str(tmp_path / "b.ma")}
    sessions = {target["port"]: target for target in (first, second)}
    monkeypatch.setattr(client, "registry", lambda _: {18770: {"a": "token-a"}, 18771: {"b": "token-b"}})
    monkeypatch.setattr(client, "probe", lambda port, token, timeout: dict(sessions[port]))
    selected_second = threading.Event()
    first_poll = threading.Event()
    image = tmp_path / "render.iff"
    image.write_bytes(b"fixture image")
    calls = []

    def request(port, command, params, target, timeout, *, token):
        assert target == sessions[port]
        if command == "scene_info":
            return {"scene_path": target["scene_path"]}
        calls.append((port, command, token))
        if command == "render_frame":
            return {"job_id": "job-a", "output_dir": str(tmp_path)}
        if port != first["port"]:
            raise RuntimeError("Unknown render job")
        first_poll.set()
        if selected_second.is_set():
            return {"success": True, "status": "complete", "frame": 1, "output_paths": [str(image)]}
        return {"status": "rendering"}

    monkeypatch.setattr(client, "request", request)
    connection = client.MayaConnection(tmp_path)
    connection.select(first["port"], first["pid"], first["session_id"], first["scene_path"])
    mcp = server.build_server(connection)

    async def call(name, args):
        return json.loads((await mcp.call_tool(name, args))[0].text)

    spec = TaskSpec(project_id="p", shot_id="s", software="maya", kind="quick_tweak",
                    scene_path=first["scene_path"], endpoint="127.0.0.1:18770", goal="preview",
                    acceptance=["visible"], budget="preview", modules=["lighting"])
    task = await call("fx_begin_task", {"spec": spec.model_dump()})
    await call("fx_prepare_task_brief", {"task_id": task["task_id"]})
    render = asyncio.create_task(call("render_maya_frame", {
        "renderer": "arnold", "camera": "persp", "frame": 1, "output_dir": str(tmp_path),
        "fx_task_id": task["task_id"], "timeout": 5,
    }))
    try:
        assert await asyncio.to_thread(first_poll.wait, 3)
        await call("connect_maya", {"port": second["port"], "pid": second["pid"],
                                   "session_id": second["session_id"], "expected_scene": second["scene_path"]})
        selected_second.set()
        assert (await render)["status"] == "complete"
    finally:
        if not render.done():
            render.cancel()
            with pytest.raises(asyncio.CancelledError):
                await render
    assert calls.count((first["port"], "render_frame", "token-a")) == 1
    assert all(port == first["port"] and token == "token-a" for port, _, token in calls)
    assert connection.target == second
