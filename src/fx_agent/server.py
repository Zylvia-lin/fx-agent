"""Extend the pinned upstream server, preserving its tools and request context."""

import os
from contextlib import asynccontextmanager
from functools import lru_cache
from importlib.metadata import version

from fx_agent import __version__
from fx_agent.config import DEFAULT_HOUDINI_PORT, data_root
from fx_agent.data_api import shared_functions
from fx_agent.modules import TAXONOMY_VERSION
from fx_agent.production import ProductionBridge, check_path, production_rules
from fx_agent.task_tools import install_task_gates
from fx_agent.tool_profiles import apply_tool_profile, resolve_tool_profile
from fx_agent.vfx_catalog import fx_search_published_templates
from fx_agent.workflow import HOST_STARTUP_INSTRUCTIONS


@asynccontextmanager
async def houdini_lifespan(_server):
    """Provide the bridge to tools and resources without a startup probe."""
    from fxhoudinimcp import server as upstream_server

    plugin_timeout = float(os.getenv("FXHOUDINIMCP_TIMEOUT", "120"))
    bridge = ProductionBridge(
        host=os.getenv("HOUDINI_HOST", "localhost"),
        port=int(os.getenv("HOUDINI_PORT") or DEFAULT_HOUDINI_PORT),
        timeout=float(os.getenv("HOUDINI_TIMEOUT", str(plugin_timeout + 15))),
    )
    upstream_server._bridge = bridge
    try:
        yield {"bridge": bridge}
    finally:
        upstream_server._bridge = None
        await bridge.close()


@lru_cache(maxsize=1)
def get_server():
    """Register workstation tools once on the upstream server singleton.

    Progress reporting, resources, prompts, validation and tool signatures
    remain upstream-owned. The wire server identity stays FXHoudini
    during this initial integration; the installed plugin identity is fx-agent.
    """
    os.environ.setdefault("HOUDINI_PORT", str(DEFAULT_HOUDINI_PORT))
    from fxhoudinimcp import prompts, resources, tools  # noqa: F401
    from fxhoudinimcp import server as upstream_server
    from fxhoudinimcp.server import mcp

    # MCP clients may repeat initialization instructions on every advertised tool.
    mcp._mcp_server.instructions = HOST_STARTUP_INSTRUCTIONS

    mcp._mcp_server.lifespan = houdini_lifespan
    for function in shared_functions():
        mcp.tool()(function)
    mcp.tool()(fx_search_published_templates)

    from fxhoudinimcp._sdk import Context

    from fx_agent.render_view import render_view_command

    @mcp.tool()
    async def fx_render_view_start(ctx: Context, node_path: str, pane_name: str = "FX_Render_View", frame: float | None = None, preview: bool = False) -> dict:
        """Start a single frame in native Houdini Render View; leaves ROP parameters unchanged. Requires GUI. Poll fx_render_view_status, then save only after rendering stops."""
        return await render_view_command(upstream_server._get_bridge(ctx), "start", node_path=node_path, pane_name=pane_name, frame=frame, preview=preview)

    @mcp.tool()
    async def fx_render_view_status(ctx: Context, pane_name: str = "FX_Render_View") -> dict:
        """Read native Render View progress, requested ROP/frame/token, image resolution and planes."""
        return await render_view_command(upstream_server._get_bridge(ctx), "status", pane_name=pane_name)

    @mcp.tool()
    async def fx_render_view_save(ctx: Context, output_path: str, pane_name: str = "FX_Render_View", token: str | None = None, overwrite: bool = False) -> dict:
        """Save a completed Render View frame and measure RGB plus Alpha pixels. Rejects running renders, stale tokens, changed frames and unapproved overwrites. Does not treat nonzero RGB as valid Alpha."""
        return await render_view_command(upstream_server._get_bridge(ctx), "save", output_path=output_path, pane_name=pane_name, token=token, overwrite=overwrite)

    @mcp.tool()
    async def fx_render_view_stop(ctx: Context, pane_name: str = "FX_Render_View", token: str | None = None) -> dict:
        """Stop only the named Render View; an optional token guards against cancelling a newer render."""
        return await render_view_command(upstream_server._get_bridge(ctx), "stop", pane_name=pane_name, token=token)

    @mcp.tool()
    async def fx_status() -> dict:
        """Get workstation version and data location; does not inspect a live scene."""
        return {
            "version": __version__,
            "upstream_version": version("fxhoudinimcp"),
            "data_root": str(data_root()),
            "houdini_status_tool": "get_houdini_connection_status",
            "nuke": {
                "mcp_name": "fx-nuke",
                "target_version": "11.0",
                "connection_status_tool": "get_nuke_connection_status",
            },
            "maya": {
                "mcp_name": "fx-maya",
                "connection_status_tool": "get_maya_scene_info",
                "session_tool": "list_maya_sessions",
            },
            "production_rules_version": production_rules()["version"],
            "knowledge_mode": "text_only",
            "tool_profile": resolve_tool_profile(),
            "template_library_tool": "fx_search_published_templates",
            "module_taxonomy_version": TAXONOMY_VERSION,
            "module_baseline_sync_tool": "fx_sync_module_baselines",
            "maintenance_tools_require_profile": "full",
            "task_gate_version": 2,
            "task_gate_entry": "fx_begin_task",
        }


    @mcp.tool()
    async def fx_check_production_path(value: str, purpose: str = "read") -> dict:
        """Validate a proposed raw file path; not a live scene or dependency audit."""
        return check_path(value, purpose)


    async def live_target(context):
        bridge = upstream_server._get_bridge(context)
        info = await bridge.execute("scene.get_scene_info", {})
        settings = {"fps": info["fps"]} if "fps" in info else {}
        return f"127.0.0.1:{bridge.port}", info["hip_file"], settings

    install_task_gates(mcp, "houdini", data_root, live_target)
    apply_tool_profile(mcp, software="houdini")
    return mcp
