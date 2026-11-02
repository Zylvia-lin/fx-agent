"""Extend the pinned upstream server, preserving its tools and request context."""

from functools import lru_cache
from importlib.metadata import version

from fx_agent import __version__
from fx_agent.config import data_root


@lru_cache(maxsize=1)
def get_server():
    """Register workstation tools once on the upstream server singleton.

    Its lifespan, progress reporting, resources, prompts, validation and tool
    signatures remain upstream-owned. The wire server identity stays FXHoudini
    during this initial integration; the installed plugin identity is fx-agent.
    """
    from fxhoudinimcp import prompts, resources, tools  # noqa: F401
    from fxhoudinimcp.server import mcp

    @mcp.tool()
    async def fx_status() -> dict:
        """Get workstation version and data location; does not inspect a live scene."""
        return {
            "version": __version__,
            "upstream_version": version("fxhoudinimcp"),
            "data_root": str(data_root()),
            "houdini_status_tool": "get_houdini_connection_status",
            "nuke": "not_integrated",
        }

    return mcp
