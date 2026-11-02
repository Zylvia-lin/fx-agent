"""Extend the pinned upstream server, preserving its tools and request context."""

import asyncio
from functools import lru_cache
from importlib.metadata import version

from fx_agent import __version__
from fx_agent.archive import ArtifactInput, CaseArchive, create_case_async
from fx_agent.config import data_root
from fx_agent.memory import Experience, MemoryStore
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

    @mcp.tool()
    async def fx_archive_case(
        project_id: str,
        shot_id: str,
        title: str,
        files: list[ArtifactInput],
        max_total_bytes: int = 512 * 1024 * 1024,
    ) -> dict:
        """Archive independent copies of explicit HIP, images and dependency files.

        Reads saved files, not unsaved scene edits. No automatic dependency
        discovery or reproducibility claim. Default copy limit is 512 MiB.
        Roles: project, reference, preview, dependency. Sources must be absolute.
        """
        manifest = await create_case_async(
            data_root(),
            project_id=project_id,
            shot_id=shot_id,
            title=title,
            files=files,
            max_total_bytes=max_total_bytes,
        )
        return {key: value for key, value in manifest.items() if key != "files"} | {
            "file_count": len(manifest["files"])
        }

    @mcp.tool()
    async def fx_get_case(case_id: str, project_id: str, detail: bool = False) -> dict:
        """Read a case summary, or expand its file manifest with detail=true."""
        return await asyncio.to_thread(
            lambda: (
                CaseArchive(data_root()).get(case_id, project_id)
                if detail
                else CaseArchive(data_root()).summary(case_id, project_id)
            )
        )

    @mcp.tool()
    async def fx_validate_case(case_id: str, project_id: str) -> dict:
        """Validate copied file checksums. Does not open HIP or verify visual reproduction."""
        return await asyncio.to_thread(lambda: CaseArchive(data_root()).verify(case_id, project_id))

    @mcp.tool()
    async def fx_submit_experience(experience: Experience) -> dict:
        """Screen milestone knowledge, keeping inferences as non-default candidates.

        Codex must truthfully label source_type. User statements are not visual
        verification. Project-dependent entries need an archived HIP. To correct
        an old entry, submit a distinct explicit user statement with supersedes.
        """
        return await asyncio.to_thread(
            lambda: MemoryStore(data_root()).submit(**experience.model_dump())
        )

    @mcp.tool()
    async def fx_search_memory(
        project_id: str,
        query: str,
        shot_id: str = "",
        include_candidates: bool = False,
        limit: int = 5,
        max_chars: int = 3000,
    ) -> dict:
        """Recall project-scoped experience cards within a JSON character budget.

        Chinese/English literal terms are supported; space-separated terms must
        all match. No semantic embedding or cross-project sharing in this version.
        Superseded entries never appear. Candidates are opt-in.
        """
        return await asyncio.to_thread(
            lambda: MemoryStore(data_root()).search(
                project_id, query, shot_id, include_candidates, limit, max_chars
            )
        )

    @mcp.tool()
    async def fx_get_memory(memory_id: str, project_id: str) -> dict:
        """Expand one scoped memory's method, conditions, provenance and case reference."""
        return await asyncio.to_thread(lambda: MemoryStore(data_root()).get(memory_id, project_id))

    return mcp
