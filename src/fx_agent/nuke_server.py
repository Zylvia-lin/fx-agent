"""Curated upstream action tools; FX Agent owns memory and cross-DCC feedback."""

import argparse
import asyncio
import logging
import threading
from types import SimpleNamespace

from mcp.server.fastmcp import FastMCP

from fx_agent.config import data_root
from fx_agent.nuke_sessions import list_sessions
from fx_agent.task_tools import install_task_gates


class SerializedConnection:
    """The upstream wire protocol lacks request IDs: never overlap commands."""

    def __init__(self, port: int = 8187):
        from nukemcp.connection import NukeConnection

        if not 1 <= port <= 65535:
            raise ValueError("Invalid Nuke port")
        self.port = port
        self._connection = NukeConnection("127.0.0.1", port)
        self._lock = threading.RLock()
        self.failed = False
        self.handshake = None

    def connect(self) -> dict:
        with self._lock:
            self.handshake = self._connection.connect(retries=1)
            return self.handshake

    def disconnect(self) -> None:
        with self._lock:
            self._connection.disconnect()
            self.handshake = None

    def select(self, port: int, pid: int, session_id: str, expected_script: str) -> dict:
        from nukemcp.connection import NukeConnection
        from nukemcp.version import parse_version

        if not 1 <= port <= 65535:
            raise ValueError("Invalid Nuke port")
        with self._lock:
            if self.handshake and self.port == port and not self.failed:
                candidate = self._connection
                handshake = self.handshake
            else:
                candidate = NukeConnection("127.0.0.1", port)
                handshake = None
            try:
                handshake = handshake or candidate.connect(retries=1)
                if handshake.get("pid") != pid or handshake.get("session_id") != session_id:
                    raise ValueError("Nuke session identity changed; refresh list_nuke_sessions")
                if not parse_version(handshake).at_least(15, 2):
                    raise ValueError("FX Nuke adapter requires Nuke 15.2 or newer")
                info = (
                    self.send_command("get_script_info")
                    if candidate is self._connection
                    else candidate.send_command("get_script_info")
                )
                if info["name"] != expected_script:
                    raise ValueError("Nuke script changed; refresh list_nuke_sessions")
            except Exception:
                if candidate is not self._connection:
                    candidate.disconnect()
                raise
            old = self._connection
            self._connection, self.port, self.handshake = candidate, port, handshake
            self.failed = False
            if old is not candidate:
                old.disconnect()
            return {"connected": True, "port": port, "handshake": handshake, "script": info}

    def send_command(self, command: str, params: dict | None = None, timeout: float = 10.0):
        from nukemcp.connection import NukeConnectionError

        with self._lock:
            if self.failed:
                raise RuntimeError(
                    "Nuke connection requires restart; check whether the previous operation completed before retrying"
                )
            try:
                return self._connection.send_command(command, params, timeout=timeout)
            except NukeConnectionError:
                self.failed = True
                self.disconnect()
                raise


def build_server(connection: SerializedConnection, handshake: dict | None = None) -> FastMCP:
    """Register tested compositing modules without upstream memory or plugin loading."""
    from nukemcp.tools import batch, comp, graph, render, script
    from nukemcp.version import parse_version

    version = parse_version(handshake or {"nuke_version": "15.2v1"})
    if not version.at_least(15, 2):
        raise ValueError(
            "FX Nuke adapter requires Nuke 15.2 or newer; this version was not validated"
        )
    mcp = FastMCP(
        "FXNuke",
        instructions="由当前宿主助手操作。先读真实工程，制作经验使用FX Agent共享记忆工具并指定software=nuke；素材问题通过渲染问题单反馈Houdini。",
    )
    state = SimpleNamespace(mcp=mcp, connection=connection, version=version, event_log=None)
    for module in (graph, script, comp, render, batch):
        module.register(state)
    mcp.remove_tool("execute_python")
    mcp.remove_tool("auto_layout")

    @mcp.tool()
    def get_nuke_connection_status() -> dict:
        """Verify the selected Nuke bridge and read its actual current script."""
        if connection.handshake is None or connection.failed:
            return {"connected": False, "sessions": list_nuke_sessions()["sessions"]}
        info = connection.send_command("get_script_info")
        return {
            "connected": True,
            "host": "127.0.0.1",
            "port": connection.port,
            "handshake": connection.handshake,
            "script": info,
            "sessions": list_nuke_sessions()["sessions"],
        }

    @mcp.tool(annotations={"readOnlyHint": True})
    def list_nuke_sessions() -> dict:
        """List local window snapshots; connect_nuke verifies PID, ID and script live."""
        current = connection.handshake or {}
        return {"sessions": list_sessions(data_root(), connection.port, current.get("session_id"))}

    @mcp.tool()
    def connect_nuke(port: int, pid: int, session_id: str, expected_script: str) -> dict:
        """Explicitly select a listed window. Copy all identity fields from its snapshot.

        Failure preserves the previous target. Does not open or save a script.
        """
        return connection.select(port, pid, session_id, expected_script)

    async def live_target(_context):
        info = connection.send_command("get_script_info")
        if info.get("status") == "ok" and isinstance(info.get("result"), dict):
            info = info["result"]
        settings = {
            key: info[key] for key in ("fps", "frame_range", "format", "colorspace") if key in info
        }
        session = connection.handshake or {}
        settings.update({key: session[key] for key in ("pid", "session_id") if key in session})
        return f"127.0.0.1:{connection.port}", info["name"], settings

    install_task_gates(mcp, "nuke", data_root, live_target)
    # Keep target selection, task validation and node dispatch in one critical section.
    manager = mcp._tool_manager
    original_call = manager.call_tool
    call_lock = asyncio.Lock()

    async def serialized_call(*args, **kwargs):
        async with call_lock:
            return await original_call(*args, **kwargs)

    manager.call_tool = serialized_call
    return mcp


def main() -> None:
    from nukemcp.connection import NukeConnectionError

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8187)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    connection = SerializedConnection(args.port)
    try:
        sessions = list_sessions(data_root())
        if len(sessions) == 1:
            target = sessions[0]
            try:
                connection.select(target["port"], target["pid"], target["session_id"], target["script"])
            except (NukeConnectionError, OSError, RuntimeError, ValueError) as error:
                logging.warning("Nuke startup target unavailable; use list_nuke_sessions: %s", error)
        server = build_server(connection)
        server.run(transport="stdio")
    finally:
        connection.disconnect()


if __name__ == "__main__":
    main()
