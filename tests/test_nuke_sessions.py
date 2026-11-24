"""Multi-window discovery never silently changes the selected production scene."""

import asyncio
import json
import time
from unittest.mock import Mock

import pytest

from fx_agent.nuke_server import SerializedConnection, build_server
from fx_agent.nuke_sessions import list_sessions


def test_registry_lists_current_and_ignores_expired_or_invalid_records(tmp_path):
    root = tmp_path / "nuke_sessions"
    root.mkdir()
    record = dict(pid=12, port=8188, session_id="abc", script="shot.nk", updated_at=time.time())
    (root / "12.json").write_text(json.dumps(record))
    (root / "old.json").write_text(json.dumps(record | {"updated_at": 0}))
    (root / "bad.json").write_text("{broken")
    assert list_sessions(tmp_path, 8188, "abc")[0]["current"] is True
    assert len(list_sessions(tmp_path)) == 1


def test_switch_checks_identity_before_replacing_current_connection(monkeypatch):
    import nukemcp.connection

    conn = SerializedConnection()
    old = conn._connection
    candidate = Mock()
    candidate.connect.return_value = dict(nuke_version="15.2v1", pid=42, session_id="new")
    candidate.send_command.return_value = {"name": "B.nk"}
    monkeypatch.setattr(nukemcp.connection, "NukeConnection", lambda *args: candidate)
    with pytest.raises(ValueError, match="identity"):
        conn.select(8188, 43, "new", "B.nk")
    assert conn._connection is old
    candidate.disconnect.assert_called_once()
    candidate.reset_mock()
    result = conn.select(8188, 42, "new", "B.nk")
    assert result["script"]["name"] == "B.nk"
    assert conn.port == 8188
    assert conn.handshake["pid"] == 42


def test_switch_rejects_changed_script_and_old_nuke(monkeypatch):
    import nukemcp.connection

    conn = SerializedConnection()
    candidate = Mock()
    candidate.connect.return_value = dict(nuke_version="15.2v1", pid=42, session_id="new")
    candidate.send_command.return_value = {"name": "changed.nk"}
    monkeypatch.setattr(nukemcp.connection, "NukeConnection", lambda *args: candidate)
    with pytest.raises(ValueError, match="script"):
        conn.select(8188, 42, "new", "B.nk")
    candidate.connect.return_value["nuke_version"] = "13.2v1"
    with pytest.raises(ValueError, match="15.2"):
        conn.select(8188, 42, "new", "B.nk")
    assert conn.port == 8187


@pytest.mark.asyncio
async def test_mcp_starts_disconnected_and_lists_without_task_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("FX_AGENT_LOCAL_ROOT", str(tmp_path))
    conn = SerializedConnection()
    server = build_server(conn)
    names = {tool.name for tool in await server.list_tools()}
    assert {"list_nuke_sessions", "connect_nuke"} <= names
    status = await server.call_tool("get_nuke_connection_status", {})
    assert '"connected":false' in str(status).replace(" ", "")
    result = await server.call_tool("list_nuke_sessions", {})
    assert "Task gate requires" not in str(result)
    conn.select = Mock(return_value={"connected": True})
    await server.call_tool("connect_nuke", {
        "port": 8188, "pid": 42, "session_id": "abc", "expected_script": "shot.nk",
    })
    conn.select.assert_called_once()


@pytest.mark.asyncio
async def test_status_recovers_single_session_after_connection_refused(monkeypatch):
    import nukemcp.connection

    import fx_agent.nuke_server as module

    conn = SerializedConnection()
    target = dict(port=8187, pid=42, session_id="new", script="A.nk")
    monkeypatch.setattr(module, "list_sessions", lambda *args: [target])
    candidate = Mock()
    candidate.connect.side_effect = [
        nukemcp.connection.NukeConnectionError("connection refused"),
        dict(nuke_version="15.2v1", pid=42, session_id="new"),
    ]
    candidate.send_command.return_value = {"name": "A.nk"}
    monkeypatch.setattr(nukemcp.connection, "NukeConnection", lambda *args: candidate)
    server = build_server(conn)

    unavailable = await server.call_tool("get_nuke_connection_status", {})
    assert "connection refused" in str(unavailable)
    recovered = await server.call_tool("get_nuke_connection_status", {})
    assert '"connected":true' in str(recovered).replace(" ", "")
    assert conn.handshake["session_id"] == "new"
    candidate.send_command.assert_called_once_with("get_script_info")


@pytest.mark.asyncio
@pytest.mark.parametrize(("session_count", "failed"), [(2, False), (1, True)])
async def test_status_does_not_guess_target_or_recover_after_timeout(
    monkeypatch, session_count, failed
):
    import fx_agent.nuke_server as module

    conn = SerializedConnection()
    conn.failed = failed
    monkeypatch.setattr(module, "list_sessions", lambda *args: [{}] * session_count)
    conn.select = Mock()
    status = await build_server(conn).call_tool("get_nuke_connection_status", {})
    assert '"connected":false' in str(status).replace(" ", "")
    assert ("reselect_required" in str(status)) is failed
    conn.select.assert_not_called()


def test_failed_new_connection_keeps_previous_target(monkeypatch):
    import nukemcp.connection

    conn = SerializedConnection()
    old = conn._connection
    candidate = Mock()
    candidate.connect.side_effect = RuntimeError("unreachable")
    monkeypatch.setattr(nukemcp.connection, "NukeConnection", lambda *args: candidate)
    with pytest.raises(RuntimeError, match="unreachable"):
        conn.select(8188, 42, "new", "B.nk")
    assert conn._connection is old and conn.port == 8187
    candidate.disconnect.assert_called_once()


def test_reselect_timeout_invalidates_untagged_current_connection():
    from nukemcp.connection import NukeConnectionError

    conn = SerializedConnection()
    backend = Mock()
    conn._connection = backend
    conn.handshake = dict(nuke_version="15.2v1", pid=42, session_id="same")
    backend.send_command.side_effect = NukeConnectionError("timeout")
    with pytest.raises(NukeConnectionError):
        conn.select(8187, 42, "same", "A.nk")
    assert conn.failed
    assert conn.handshake is None
    backend.disconnect.assert_called_once()
    with pytest.raises(RuntimeError, match="restart"):
        conn.send_command("create_node")


def test_unreachable_single_advertisement_does_not_prevent_mcp_start(monkeypatch):
    from nukemcp.connection import NukeConnectionError

    import fx_agent.nuke_server as module

    conn, server = Mock(), Mock()
    conn.select.side_effect = NukeConnectionError("refused")
    monkeypatch.setattr(module, "SerializedConnection", lambda port: conn)
    monkeypatch.setattr(module, "build_server", lambda connection: server)
    monkeypatch.setattr("fx_agent.shared_knowledge.prepare_local_data", lambda: None)
    monkeypatch.setattr(module, "list_sessions", lambda root: [
        dict(port=8187, pid=42, session_id="expired", script="A.nk")
    ])
    monkeypatch.setattr("sys.argv", ["fx-nuke"])
    module.main()
    server.run.assert_called_once_with(transport="stdio")
    conn.disconnect.assert_called_once()


@pytest.mark.asyncio
async def test_selection_waits_for_entire_mcp_call(tmp_path, monkeypatch):
    monkeypatch.setenv("FX_AGENT_DATA_ROOT", str(tmp_path))
    conn = SerializedConnection()
    server = build_server(conn)
    active = False

    @server.tool()
    async def get_slow_probe() -> dict:
        nonlocal active
        active = True
        await asyncio.sleep(0.02)
        active = False
        return {}

    def select(*args):
        assert not active, "Target changed while another tool was running"
        return {"connected": True}

    conn.select = select
    await asyncio.gather(
        server.call_tool("get_slow_probe", {}),
        server.call_tool("connect_nuke", {
            "port": 8188, "pid": 42, "session_id": "new", "expected_script": "B.nk",
        }),
    )
