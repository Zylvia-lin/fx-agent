"""Read-only Nuke MCP smoke test; --mock is synthetic and never proves live support."""

import argparse
import asyncio
import json
import os
import socket
import sys
import tempfile
import time
import uuid
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def check(port: int, synthetic: bool, local_root: Path | None = None) -> None:
    env = dict(os.environ)
    if local_root is not None:
        env.update(FX_AGENT_LOCAL_ROOT=str(local_root), FX_AGENT_DATA_ROOT=str(local_root))
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "fx_agent.nuke_server", "--port", str(port)],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=env,
    )
    async with stdio_client(params) as (reader, writer), ClientSession(reader, writer) as session:
        await session.initialize()
        listed = await session.list_tools()
        result = await session.call_tool("get_nuke_connection_status", {})
        if result.isError:
            raise RuntimeError(str(result.content))
        connection = json.loads(result.content[0].text)
        script = connection.get("script")
        if (
            connection.get("connected") is not True
            or connection.get("port") != port
            or not isinstance(script, dict)
            or not isinstance(script.get("name"), str)
        ):
            raise RuntimeError("Nuke connection or script could not be verified")
        print(
            json.dumps(
                {
                    "synthetic": synthetic,
                    "live_verified": not synthetic,
                    "tool_count": len(listed.tools),
                    "connection": connection,
                },
                ensure_ascii=False,
                indent=2,
            )
        )


def check_mock() -> None:
    from nukemcp.mock import MockNukeServer

    with tempfile.TemporaryDirectory(prefix="fx-nuke-smoke-") as temporary:
        root = Path(temporary)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        mock = MockNukeServer(port=port, nuke_version="15.2v1", variant="NukeX")
        handshake = mock.state.handshake() | {"session_id": uuid.uuid4().hex}
        mock.state.handshake = lambda: handshake
        registry = root / "nuke_sessions"
        registry.mkdir()
        record = {**handshake, "port": port, "script": mock.state.script_name, "updated_at": time.time()}
        (registry / "smoke.json").write_text(json.dumps(record), encoding="utf-8")
        try:
            mock.start()
            asyncio.run(check(port, True, root))
        finally:
            mock.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8187)
    parser.add_argument("--mock", action="store_true")
    args = parser.parse_args()
    if args.mock:
        check_mock()
    else:
        asyncio.run(check(args.port, False))
