"""Read-only Nuke MCP smoke test; --mock is synthetic and never proves live support."""

import argparse
import asyncio
import json
import socket
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def check(port: int, synthetic: bool) -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "fx_agent.nuke_server", "--port", str(port)],
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    async with stdio_client(params) as (reader, writer), ClientSession(reader, writer) as session:
        await session.initialize()
        listed = await session.list_tools()
        result = await session.call_tool("get_nuke_connection_status", {})
        if result.isError:
            raise RuntimeError(str(result.content))
        print(
            json.dumps(
                {
                    "synthetic": synthetic,
                    "live_verified": not synthetic,
                    "tool_count": len(listed.tools),
                    "connection": json.loads(result.content[0].text),
                },
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8187)
    parser.add_argument("--mock", action="store_true")
    args = parser.parse_args()
    mock = None
    if args.mock:
        from nukemcp.mock import MockNukeServer

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        mock = MockNukeServer(port=port, nuke_version="15.2v1", variant="NukeX")
        mock.start()
    else:
        port = args.port
    try:
        asyncio.run(check(port, args.mock))
    finally:
        if mock:
            mock.stop()
