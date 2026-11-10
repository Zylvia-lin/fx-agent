"""Wait for native completion, never infer it from a paused file writer."""

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
    return mcp._tool_manager.get_tool('render_maya_frame').fn, connection


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
