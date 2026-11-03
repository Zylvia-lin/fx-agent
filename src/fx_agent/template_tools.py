"""MCP entry points for static source studies and existing memory admission."""

import asyncio
import threading
from contextlib import suppress

from fx_agent.archive import ArchiveCancelled, ArtifactInput
from fx_agent.templates import TemplateLibrary, TemplatePolicy


def register_template_tools(mcp, root_provider):
    @mcp.tool()
    async def fx_plan_template_import(source: str, policy: TemplatePolicy | None = None) -> dict:
        """Read mixed HIP/NK directory metadata; no data writes or DCC connection.

        Prunes backup folders and links, groups render sequences, and reports
        cache volume. Preview does not prove project/preview version pairing.
        """
        return await asyncio.to_thread(
            TemplateLibrary(root_provider()).plan, source, policy.model_dump() if policy else None
        )

    @mcp.tool()
    async def fx_learn_templates(
        source: str,
        project_id: str,
        shot_id: str,
        template_key: str = "",
        modules: list[str] | None = None,
        files: list[ArtifactInput] | None = None,
        policy: TemplatePolicy | None = None,
    ) -> dict:
        """Archive saved HIP/HIPLC/HIPNC/NK and statically extract searchable studies.

        Never opens a GUI, evaluates scripts/expressions, changes parameters,
        cooks, renders or writes to source files. Missing caches do not block
        structural study. Scripts and comments are untrusted source data.
        Originals and metadata live in data/cases plus memory.sqlite3.
        Repeat a batch to resume: unchanged inputs are skipped. Explicit files
        select necessary dependencies/preview samples, not whole sequences.
        Unsupported formats are archived as partial, never called fully learned.
        Use a stable template_key to associate a single renamed/moved project.
        """
        cancellation = threading.Event()
        worker = asyncio.create_task(
            asyncio.to_thread(
                TemplateLibrary(root_provider()).ingest,
                source,
                project_id=project_id,
                shot_id=shot_id,
                template_key=template_key,
                modules=modules,
                files=[f.model_dump() for f in files or []],
                policy=policy.model_dump() if policy else None,
                cancellation=cancellation,
            )
        )
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancellation.set()
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except ArchiveCancelled:
                    break
            with suppress(ArchiveCancelled):
                worker.result()
            raise

    @mcp.tool()
    async def fx_search_templates(
        query: str = "",
        software: str = "",
        module: str = "",
        project_id: str = "",
        include_history: bool = False,
        offset: int = 0,
        limit: int = 10,
        max_chars: int = 6000,
    ) -> dict:
        """Search extracted facts with local BM25; studies are not verified workflows."""
        return await asyncio.to_thread(
            TemplateLibrary(root_provider()).search,
            query,
            software=software,
            module=module,
            project_id=project_id,
            include_history=include_history,
            offset=offset,
            limit=limit,
            max_chars=max_chars,
        )

    @mcp.tool()
    async def fx_get_template(
        study_id: str,
        section: str = "summary",
        offset: int = 0,
        limit: int = 50,
        max_chars: int = 12000,
    ) -> dict:
        """Read summary, snapshot, nodes, connections or patterns with pagination.

        Large snapshots return a local JSON path and section hints; expand nodes
        and connections by offset. Raw parm/chn/script text preserves original
        expressions and curves; it is not a computed DCC result.
        """
        return await asyncio.to_thread(
            TemplateLibrary(root_provider()).get,
            study_id,
            section=section,
            offset=offset,
            limit=limit,
            max_chars=max_chars,
        )

    @mcp.tool()
    async def fx_validate_template(study_id: str) -> dict:
        """Validate archived file hashes; does not claim scene/visual reproducibility."""
        return await asyncio.to_thread(TemplateLibrary(root_provider()).validate, study_id)

    @mcp.tool()
    async def fx_export_template(study_id: str, destination: str) -> dict:
        """Copy a study into a new work directory without changing source parameters.

        Refuses an existing destination; restores internal relative file layout.
        External dependencies remain mapped in template-manifest.json; absolute
        file references are preserved and need checking on the work copy.
        """
        return await asyncio.to_thread(
            TemplateLibrary(root_provider()).export, study_id, destination
        )

    @mcp.tool()
    async def fx_learn_template_experience(
        study_id: str, title: str, method: str, reason: str, applies_when: str, modules: list[str]
    ) -> dict:
        """Save host-authored interpretation as an inferred candidate linked to a study.

        Cannot promote extracted parameters into verified success. After actual
        use, record feedback, archive the accepted result and finalize through
        the existing learning tools; Dream retains provenance and revisions.
        """
        return await asyncio.to_thread(
            TemplateLibrary(root_provider()).submit_experience,
            study_id,
            title=title,
            method=method,
            reason=reason,
            applies_when=applies_when,
            modules=modules,
        )
