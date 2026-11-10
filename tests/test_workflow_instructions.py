"""MCP-only clients must receive the workflow during initialization."""

from fxhoudinimcp._loader import load_markdown

from fx_agent.server import HOST_WORKFLOW_INSTRUCTIONS, get_server


def test_initialize_advertises_workflow_and_preserves_upstream_instructions():
    server = get_server()
    advertised = server._mcp_server.create_initialization_options().instructions
    upstream = load_markdown("instructions/server_instructions.md")

    assert advertised.startswith(HOST_WORKFLOW_INSTRUCTIONS)
    assert advertised.endswith(upstream)
    assert get_server() is server
    assert server._mcp_server.create_initialization_options().instructions == advertised
