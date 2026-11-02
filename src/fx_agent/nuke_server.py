"""Curated upstream action tools; FX Agent owns memory and cross-DCC feedback."""

import argparse
import logging
import threading
from types import SimpleNamespace

from mcp.server.fastmcp import FastMCP


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

    def connect(self) -> dict:
        return self._connection.connect(retries=1)

    def disconnect(self) -> None:
        self._connection.disconnect()

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


def build_server(connection: SerializedConnection, handshake: dict) -> FastMCP:
    """Register tested compositing modules without upstream memory or plugin loading."""
    from nukemcp.tools import batch, comp, graph, render, script
    from nukemcp.version import parse_version

    version = parse_version(handshake)
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
        info = connection.send_command("get_script_info")
        return {
            "connected": True,
            "host": "127.0.0.1",
            "port": connection.port,
            "handshake": handshake,
            "script": info,
        }

    return mcp


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8187)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    connection = SerializedConnection(args.port)
    try:
        server = build_server(connection, connection.connect())
        server.run(transport="stdio")
    finally:
        connection.disconnect()


if __name__ == "__main__":
    main()
