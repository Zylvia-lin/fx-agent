"""Optional Nuke adapter tests use a synthetic 15.2 server, never the real app."""

import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

pytest.importorskip("nukemcp", reason="Install --group nuke for optional adapter tests")

from nukemcp.mock import MockNukeServer  # noqa: E402

from fx_agent.nuke_server import SerializedConnection, build_server  # noqa: E402


@pytest.fixture
def connection():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    mock = MockNukeServer(port=port, nuke_version="15.2v1", variant="NukeX")
    mock.start()
    conn = SerializedConnection(port)
    handshake = conn.connect()
    try:
        yield conn, handshake
    finally:
        conn.disconnect()
        mock.stop()


@pytest.mark.asyncio
async def test_curated_tools_work_without_second_memory_system(connection, tmp_path, monkeypatch):
    from fx_agent.task_gates import TaskGates, TaskSpec

    monkeypatch.setenv("FX_AGENT_DATA_ROOT", str(tmp_path))
    conn, handshake = connection
    server = build_server(conn, handshake)
    gates = TaskGates(tmp_path)
    task = gates.begin(
        TaskSpec(
            project_id="p",
            shot_id="s",
            software="nuke",
            scene_path="untitled.nk",
            endpoint=f"127.0.0.1:{conn.port}",
            goal="合成",
            acceptance=["调整有效"],
            budget="草稿",
            modules=["compositing"],
        )
    )
    task_id = task["task_id"]
    gates.observe(
        task_id,
        "fx_get_production_rules",
        {"project_id": "p"},
        {"user_rules": [], "omitted_rules": 0, "next_offset": 0},
    )
    gates.observe(
        task_id, "fx_get_module_knowledge", {"software": "nuke", "module": "compositing"}, {}
    )
    for name in ("fx_search_effects", "fx_search_memory"):
        gates.observe(task_id, name, {"software": "nuke", "query": "合成"}, {"items": []})
    gates.plan(task_id, 1, "调色", "当前素材", "调低亮度", [], "没有匹配案例")
    names = {tool.name for tool in await server.list_tools()}
    assert {
        "create_node",
        "modify_node",
        "batch_set_knob",
        "find_broken_reads",
        "setup_write_node",
        "get_nuke_connection_status",
    } <= names
    assert (
        not {"execute_python", "auto_layout", "write_memory", "read_memory", "search_memory"}
        & names
    )
    await server.call_tool(
        "create_node", {"node_class": "Grade", "name": "grade_test", "fx_task_id": task_id}
    )
    await server.call_tool(
        "batch_set_knob",
        {
            "node_names": ["grade_test"],
            "knob_name": "multiply",
            "value": 0.5,
            "fx_task_id": task_id,
        },
    )
    result = await server.call_tool("get_node_info", {"node_name": "grade_test"})
    assert "0.5" in str(result)


def test_version_rejects_older_nuke(connection):
    conn, handshake = connection
    with pytest.raises(ValueError, match="15.2"):
        build_server(conn, handshake | {"nuke_version": "13.2v1"})


def test_commands_are_serialized_and_timeout_requires_explicit_recovery():
    from nukemcp.connection import NukeConnectionError

    conn = SerializedConnection(8187)
    backend = Mock()
    conn._connection = backend
    active = threading.Lock()

    def send(command, params, timeout):
        assert active.acquire(blocking=False), "Concurrent commands can swap untagged responses"
        try:
            threading.Event().wait(0.02)
            return command
        finally:
            active.release()

    backend.send_command.side_effect = send
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(conn.send_command, ["a", "b", "c", "d"])) == ["a", "b", "c", "d"]
    backend.send_command.side_effect = NukeConnectionError("timeout")
    with pytest.raises(NukeConnectionError):
        conn.send_command("create_node")
    assert conn.failed
    backend.disconnect.assert_called_once()
    with pytest.raises(RuntimeError, match="restart"):
        conn.send_command("create_node")
