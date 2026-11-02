"""Read-only protocol smoke test from the project's independent MCP process."""

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main() -> None:
    root = Path(__file__).resolve().parents[1]
    env = {
        **os.environ,
        "HOUDINI_HOST": "127.0.0.1",
        "HOUDINI_PORT": os.getenv("HOUDINI_PORT", "8100"),
        "NO_PROXY": "localhost,127.0.0.1,::1",
        "PYTHONUTF8": "1",
    }
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "fx_agent"], cwd=str(root), env=env
    )
    async with (
        stdio_client(params) as (reader, writer),
        ClientSession(reader, writer) as session,
    ):
        initialized = await session.initialize()
        tools = await session.list_tools()
        summary = {
            "server": initialized.serverInfo.model_dump(),
            "tool_count": len(tools.tools),
            "checks": {},
        }
        for name in ("fx_status", "get_houdini_connection_status", "get_scene_info"):
            result = await session.call_tool(name, {})
            if result.isError:
                raise RuntimeError(f"{name}: {result.content}")
            payload = json.loads(result.content[0].text)
            if name == "get_houdini_connection_status" and not payload.get("connected"):
                raise RuntimeError(f"Houdini disconnected: {payload}")
            summary["checks"][name] = payload
        print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
