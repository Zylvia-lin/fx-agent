"""Read-only live MCP smoke test: list windows, select each, return to a named script."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def check(final_script: str) -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "fx_agent.nuke_server"],
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    async with stdio_client(params) as (reader, writer), ClientSession(reader, writer) as session:
        await session.initialize()

        async def call(name, arguments):
            result = await session.call_tool(name, arguments)
            if result.isError:
                raise RuntimeError(str(result.content))
            return json.loads(result.content[0].text)

        listed = (await call("list_nuke_sessions", {}))["sessions"]
        final = [item for item in listed if Path(item["script"]).name == final_script]
        if len(listed) < 2 or len(final) != 1:
            raise ValueError("Need at least two live windows and exactly one matching final script")
        verified = []
        for target in [*listed, final[0]]:
            selected = await call("connect_nuke", {
                "port": target["port"], "pid": target["pid"],
                "session_id": target["session_id"], "expected_script": target["script"],
            })
            info = await call("get_script_info", {})
            assert info["name"] == target["script"]
            assert selected["handshake"]["pid"] == target["pid"]
            verified.append({"pid": target["pid"], "port": target["port"], "script": info})
        print(json.dumps({"synthetic": False, "verified_switches": verified}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--final-script", required=True)
    asyncio.run(check(parser.parse_args().final_script))
