"""MCP-only clients can discover the workflow and consult knowledge without DCC."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from fxhoudinimcp._loader import load_markdown

from fx_agent.data_api import fx_get_host_workflow
from fx_agent.memory import MemoryStore
from fx_agent.server import get_server
from fx_agent.workflow import HOST_STARTUP_INSTRUCTIONS, HOST_WORKFLOW_INSTRUCTIONS


@pytest.mark.asyncio
async def test_initialization_points_to_complete_on_demand_workflow():
    server = get_server()
    advertised = server._mcp_server.create_initialization_options().instructions
    assert advertised == HOST_STARTUP_INSTRUCTIONS
    assert any(tool.name == "fx_get_host_workflow" for tool in await server.list_tools())
    workflow = await fx_get_host_workflow()
    assert workflow["workflow"] == HOST_WORKFLOW_INSTRUCTIONS
    assert workflow["upstream_instructions"] == load_markdown("instructions/server_instructions.md")
    assert get_server() is server


@pytest.mark.asyncio
async def test_particle_matte_advice_uses_knowledge_without_probing_scene(tmp_path, monkeypatch):
    from fxhoudinimcp import server as upstream

    monkeypatch.setenv("FX_AGENT_DATA_ROOT", str(tmp_path))
    discover = AsyncMock()
    monkeypatch.setattr(upstream, "_discover", discover)
    lesson = MemoryStore(tmp_path).submit(
        software="houdini", modules=["rendering"], kind="troubleshooting",
        title="Mantra Forced Matte 粒子遮罩接缝与 Geo Time Samples",
        method="只将 Geo Time Samples 调成 1 做对照。", reason="检查变形时间采样是否错配。",
        applies_when="Mantra Forced Matte 角色轮廓存在粒子暗缝。",
        source_type="user", source_ref="test fixture",
    )
    server = get_server()
    async with server._mcp_server.lifespan(server._mcp_server) as state:
        await asyncio.sleep(0)
        assert upstream.current_bridge() is state["bridge"]
        hits = await server.call_tool("fx_search_memory", {
            "software": "houdini", "module": "rendering", "query": "粒子遮罩接缝",
        })
        match = json.loads(hits[0].text)["items"][0]
        assert match["memory_id"] == lesson["memory_id"]
        detail = await server.call_tool("fx_get_memory", {"memory_id": match["memory_id"]})
        assert "Geo Time Samples" in json.loads(detail[0].text)["method"]
    discover.assert_not_called()
