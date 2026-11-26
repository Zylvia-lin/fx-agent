"""MCP-only clients can discover the full workflow without duplicating it."""

import pytest
from fxhoudinimcp._loader import load_markdown

from fx_agent.data_api import fx_get_host_workflow
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
