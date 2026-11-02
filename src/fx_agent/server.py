"""Extend the pinned upstream server, preserving its tools and request context."""

import asyncio
from functools import lru_cache
from importlib.metadata import version

from fx_agent import __version__
from fx_agent.config import data_root
from fx_agent.store import Store


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

    @mcp.tool()
    async def fx_save_checkpoint(
        project_id: str,
        shot_id: str,
        summary: str,
        scene_path: str,
        next_steps: list[str],
        expected_revision: int,
    ) -> dict:
        """Persist reported progress. Revision 0 creates; stale updates are rejected.

        This records a path, not an archived HIP or verified live scene.
        """
        return await asyncio.to_thread(
            lambda: Store(data_root()).save_checkpoint(
                project_id=project_id,
                shot_id=shot_id,
                summary=summary,
                scene_path=scene_path,
                next_steps=next_steps,
                expected_revision=expected_revision,
            )
        )

    @mcp.tool()
    async def fx_get_checkpoint(project_id: str, shot_id: str) -> dict:
        """Read complete saved progress for a shot; recheck the live scene before editing."""
        checkpoint = await asyncio.to_thread(
            lambda: Store(data_root()).get_checkpoint(project_id, shot_id)
        )
        return {"checkpoint": checkpoint, "requires_live_verification": True}

    @mcp.tool()
    async def fx_get_context(
        project_id: str, shot_id: str, since_hash: str = "", max_chars: int = 6000
    ) -> dict:
        """Get saved progress within a JSON character budget; matching hash omits repeats.

        Fetch fx_get_checkpoint for details omitted by the budget. Not a live
        scene check and not an exact tokenizer budget.
        """
        return await asyncio.to_thread(
            lambda: Store(data_root()).get_context(project_id, shot_id, since_hash, max_chars)
        )

    return mcp
