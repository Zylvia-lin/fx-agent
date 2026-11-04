"""Extend the pinned upstream server, preserving its tools and request context."""

import asyncio
import os
from functools import lru_cache
from importlib.metadata import version
from pathlib import Path
from typing import Literal

from fx_agent import __version__
from fx_agent.archive import ArtifactInput, CaseArchive, create_case_async
from fx_agent.baselines import sync_baselines
from fx_agent.config import DEFAULT_HOUDINI_PORT, data_root
from fx_agent.data_import import import_data, import_records, resolve_memory
from fx_agent.dream import DreamAction
from fx_agent.handoff import RenderIssue, RenderIssues
from fx_agent.learning import Feedback, LearningStore, UserRule
from fx_agent.memory import Experience, MemoryStore
from fx_agent.module_guides import ModuleGuides, refresh_guides
from fx_agent.modules import module_catalog
from fx_agent.production import ProductionBridge, check_path, production_rules
from fx_agent.store import Store, scope_id
from fx_agent.task_tools import install_task_gates
from fx_agent.template_tools import register_template_tools


@lru_cache(maxsize=1)
def get_server():
    """Register workstation tools once on the upstream server singleton.

    Its lifespan, progress reporting, resources, prompts, validation and tool
    signatures remain upstream-owned. The wire server identity stays FXHoudini
    during this initial integration; the installed plugin identity is fx-agent.
    """
    os.environ.setdefault("HOUDINI_PORT", str(DEFAULT_HOUDINI_PORT))
    from fxhoudinimcp import prompts, resources, tools  # noqa: F401
    from fxhoudinimcp import server as upstream_server
    from fxhoudinimcp.server import mcp

    # The pinned lifespan resolves this constructor at startup; progress and
    # request-context behavior remain upstream-owned.
    upstream_server.HoudiniBridge = ProductionBridge
    register_template_tools(mcp, data_root)

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
                "target_version": "15.2",
                "connection_status_tool": "get_nuke_connection_status",
            },
            "production_rules_version": production_rules()["version"],
            "module_taxonomy_version": 1,
            "template_learning_version": 2,
            "effect_card_version": 3,
            "module_baseline_sync_tool": "fx_sync_module_baselines",
            "task_gate_version": 1,
            "task_gate_entry": "fx_begin_task",
        }

    @mcp.tool()
    async def fx_sync_module_baselines() -> dict:
        """Synchronize current upstream rules and project workflows in data/baselines.

        Preserves source files; adds, updates and removes current baseline files
        without keeping numbered history copies. Includes hashes and licenses.
        Writes module README indexes separately from learned supplements;
        baseline rules are not personal experiences and Dream does not rewrite them.
        """
        return await asyncio.to_thread(sync_baselines, data_root())

    @mcp.tool()
    async def fx_get_module_catalog(software: Literal["houdini", "nuke"] = "houdini") -> dict:
        """List functional memory tags and upstream guide topics, not guide full texts.

        Houdini baselines remain in get_workflow_guide; select only the relevant
        guide. Never relabel upstream documentation as a learned successful case.
        """
        return module_catalog(software)

    @mcp.tool()
    async def fx_get_module_knowledge(
        project_id: str = "",
        module: str = "",
        software: Literal["houdini", "nuke"] = "houdini",
        max_chars: int = 4000,
    ) -> dict:
        """Read the global module workflow supplement within a budget.

        Read the mapped upstream guide separately. Only accepted, finalized,
        globally reusable lessons enter generated sections; manual notes remain
        unverified references. get refreshes derived MDs from authoritative DB.
        """
        return await asyncio.to_thread(
            lambda: ModuleGuides(data_root()).get(project_id, module, software, max_chars)
        )

    @mcp.tool()
    async def fx_refresh_module_guides(project_id: str = "") -> dict:
        """Rebuild global module MDs; preserve text outside managed sections.

        pending_rebuild means the SQLite learning record remains saved, but
        Markdown synchronization needs repair. Never silently claim it updated.
        """
        return await asyncio.to_thread(lambda: refresh_guides(data_root(), project_id))

    @mcp.tool()
    async def fx_get_production_rules(
        project_id: str = "", max_chars: int = 6000, offset: int = 0
    ) -> dict:
        """Read baseline and revisioned user rules before using learned recipes.

        User text is stored with its quote and scope; it does not install code
        or silently change the bridge's machine checks.
        """
        if not 1024 <= max_chars <= 12000 or offset < 0:
            raise ValueError("Invalid max_chars or offset")
        if project_id:
            scope_id.validate_python(project_id)
        if not (data_root() / "memory.sqlite3").exists():
            return production_rules() | {
                "user_rules": [],
                "omitted_rules": 0,
                "next_offset": offset,
            }
        return await asyncio.to_thread(
            lambda: LearningStore(data_root()).get_rules(project_id, max_chars, offset)
        )

    @mcp.tool()
    async def fx_check_production_path(value: str, purpose: str = "read") -> dict:
        """Validate a proposed raw file path; not a live scene or dependency audit."""
        return check_path(value, purpose)

    @mcp.tool()
    async def fx_save_checkpoint(
        project_id: str,
        shot_id: str,
        summary: str,
        scene_path: str,
        next_steps: list[str],
        expected_revision: int,
        software: Literal["houdini", "nuke"] = "houdini",
    ) -> dict:
        """Persist reported progress. Revision 0 creates; stale updates are rejected.

        This records a path, not an archived HIP/NK or verified live scene.
        """
        return await asyncio.to_thread(
            lambda: Store(data_root()).save_checkpoint(
                project_id=project_id,
                shot_id=shot_id,
                summary=summary,
                scene_path=scene_path,
                next_steps=next_steps,
                expected_revision=expected_revision,
                software=software,
            )
        )

    @mcp.tool()
    async def fx_get_checkpoint(
        project_id: str, shot_id: str, software: Literal["houdini", "nuke"] = "houdini"
    ) -> dict:
        """Read complete saved progress for a shot; recheck the live scene before editing."""
        checkpoint = await asyncio.to_thread(
            lambda: Store(data_root()).get_checkpoint(project_id, shot_id, software)
        )
        return {"checkpoint": checkpoint, "requires_live_verification": True}

    @mcp.tool()
    async def fx_get_context(
        project_id: str,
        shot_id: str,
        since_hash: str = "",
        max_chars: int = 6000,
        software: Literal["houdini", "nuke"] = "houdini",
    ) -> dict:
        """Get saved progress within a JSON character budget; matching hash omits repeats.

        Fetch fx_get_checkpoint for details omitted by the budget. Not a live
        scene check and not an exact tokenizer budget.
        """
        return await asyncio.to_thread(
            lambda: Store(data_root()).get_context(
                project_id, shot_id, since_hash, max_chars, software
            )
        )

    @mcp.tool()
    async def fx_archive_case(
        project_id: str,
        shot_id: str,
        title: str,
        files: list[ArtifactInput],
        max_total_bytes: int = 512 * 1024 * 1024,
    ) -> dict:
        """Archive independent copies of explicit HIP/NK, images and dependency files.

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
        verification. Project-dependent entries need an archived project for their software. To correct
        an old entry, submit a distinct explicit user statement with supersedes.
        """
        return await asyncio.to_thread(
            lambda: MemoryStore(data_root()).submit(**experience.model_dump())
        )

    @mcp.tool()
    async def fx_search_memory(
        project_id: str = "",
        query: str = "",
        shot_id: str = "",
        include_candidates: bool = False,
        limit: int = 5,
        max_chars: int = 3000,
        software: Literal["houdini", "nuke"] = "houdini",
        module: str = "",
        source_project: str = "",
    ) -> dict:
        """Recall global experience cards within a JSON character budget.

        Chinese/English literal terms are supported; space-separated terms must
        all match. Project identifies context only; reusable knowledge crosses projects.
        Superseded entries never appear. Candidates are opt-in.
        """
        return await asyncio.to_thread(
            lambda: MemoryStore(data_root()).search(
                project_id,
                query,
                shot_id,
                include_candidates,
                limit,
                max_chars,
                software,
                module,
                source_project,
            )
        )

    @mcp.tool()
    async def fx_get_memory(memory_id: str, project_id: str = "") -> dict:
        """Expand one global memory's method, conditions, provenance and case reference."""
        return await asyncio.to_thread(lambda: MemoryStore(data_root()).get(memory_id, project_id))

    @mcp.tool()
    async def fx_record_user_rule(rule: UserRule) -> dict:
        """Persist an explicit dialogue requirement with exact quote, scope and revision.

        Do not label inferred preferences as user statements. Global scope is
        explicit; project rules require project_id. Revision 0 creates.
        """
        return await asyncio.to_thread(
            lambda: LearningStore(data_root()).record_rule(**rule.model_dump())
        )

    @mcp.tool()
    async def fx_get_user_rule(
        rule_key: str, scope: str = "global", project_id: str = "", revision: int = 0
    ) -> dict:
        """Expand one exact user rule with its full quote; revision=0 reads the latest.

        For rule cards marked detail_required, read this before acting. Project
        scope requires project_id; global scope leaves it empty.
        """
        return await asyncio.to_thread(
            lambda: LearningStore(data_root()).get_rule(rule_key, scope, project_id, revision)
        )

    @mcp.tool()
    async def fx_record_feedback(feedback: Feedback) -> dict:
        """Keep the user's correction, change, reasoning and observed result as history.

        Pending/improved/failed attempts are not successful workflow memories.
        Accepted requires actual user confirmation plus an archived HIP and preview.
        Revising feedback disables methods derived from its older revisions.
        """
        return await asyncio.to_thread(
            lambda: LearningStore(data_root()).record_feedback(**feedback.model_dump())
        )

    @mcp.tool()
    async def fx_get_learning_history(
        project_id: str,
        shot_id: str,
        effect_type: str = "",
        max_chars: int = 6000,
        before_cursor: int = 0,
        software: Literal["houdini", "nuke"] = "houdini",
        module: str = "",
    ) -> dict:
        """Read scoped correction history within a character budget, newest first.

        Continue with next_cursor as before_cursor when truncated. Cards marked
        detail_required need fx_get_feedback before distilling a final method.
        """
        return await asyncio.to_thread(
            lambda: LearningStore(data_root()).history(
                project_id, shot_id, effect_type, max_chars, before_cursor, software, module
            )
        )

    @mcp.tool()
    async def fx_get_feedback(feedback_id: str, project_id: str) -> dict:
        """Expand one feedback record's complete quote, changes and result within scope."""
        return await asyncio.to_thread(
            lambda: LearningStore(data_root()).get_feedback(feedback_id, project_id)
        )

    @mcp.tool()
    async def fx_finalize_learning(
        feedback_id: str,
        project_id: str,
        method: str,
        reason: str,
        applies_when: str,
        reuse_scope: str = "global",
    ) -> dict:
        """Distill the actually used method from the current accepted final-result record.

        Codex writes the method and conditions after inspecting the correction
        trace. The service checks acceptance, archive and scope atomically.
        Global reuse is default; choose shot only for shot-specific methods. Acceptance of an image is not proof of every causal explanation.
        """
        return await asyncio.to_thread(
            lambda: LearningStore(data_root()).finalize(
                feedback_id, project_id, method, reason, applies_when, reuse_scope
            )
        )

    @mcp.tool()
    async def fx_dream_memory(
        project_id: str = "",
        mode: str = "preview",
        expected_snapshot: str = "",
        actions: list[DreamAction] | None = None,
        cursor: int = 0,
        limit: int = 50,
        software: str = "",
        module: str = "",
    ) -> dict:
        """Read global Dream candidates; host reads full sources and authors semantic actions.

        Preview pages use next_cursor. Apply must repeat cursor/filter/limit and snapshot.
        Omit actions to apply exact aliases; supply actions for semantic revisions.
        Sources are retained. Conflict actions never become successful recipes.
        project_id is a deprecated compatibility argument, not a search boundary.
        """
        return await asyncio.to_thread(
            lambda: LearningStore(data_root()).dream(
                project_id,
                mode,
                expected_snapshot,
                [action.model_dump() for action in actions] if actions is not None else None,
                cursor,
                limit,
                software,
                module,
            )
        )

    @mcp.tool()
    async def fx_undo_dream(run_id: str) -> dict:
        """Undo an unchanged Dream result; refuse to overwrite subsequent knowledge changes."""
        return await asyncio.to_thread(lambda: LearningStore(data_root()).undo_dream(run_id))

    @mcp.tool()
    async def fx_get_dream_runs(run_id: str = "", offset: int = 0, limit: int = 10) -> dict:
        """List Dream progress cursors or expand one run's actions and provenance."""
        import json

        if offset < 0 or not 1 <= limit <= 50:
            raise ValueError("Invalid offset or limit")

        def read():
            with Store(data_root()).connection() as db:
                if run_id:
                    row = db.execute(
                        "SELECT payload FROM dream_runs WHERE run_id=?", (run_id,)
                    ).fetchone()
                    if not row:
                        raise ValueError("Dream run not found")
                    return json.loads(row[0])
                rows = db.execute(
                    "SELECT run_id,payload FROM dream_runs ORDER BY rowid DESC LIMIT ? OFFSET ?",
                    (limit + 1, offset),
                ).fetchall()
                items = []
                for row in rows[:limit]:
                    value = json.loads(row["payload"])
                    items.append(
                        {
                            key: value.get(key)
                            for key in (
                                "run_id",
                                "mode",
                                "applied",
                                "next_cursor",
                                "truncated",
                                "software",
                                "module",
                                "undone",
                            )
                        }
                    )
                return {
                    "items": items,
                    "next_offset": offset + len(items),
                    "truncated": len(rows) > limit,
                }

        return await asyncio.to_thread(read)

    @mcp.tool()
    async def fx_import_data(source: str, apply: bool = False) -> dict:
        """Preview or append another data directory into the configured root.

        Stop archive writers first. Preserve conflicts and snapshot; never overwrite
        local records. Run host-authored Dream after import to consolidate knowledge.
        """
        return await asyncio.to_thread(lambda: import_data(Path(source), data_root(), apply=apply))

    @mcp.tool()
    async def fx_get_import_records(
        import_id: str = "", table: str = "", offset: int = 0, limit: int = 20
    ) -> dict:
        """List import batches or expand divergent incoming/local rows for host review."""
        return await asyncio.to_thread(
            lambda: import_records(data_root(), import_id, table=table, offset=offset, limit=limit)
        )

    @mcp.tool()
    async def fx_resolve_import_memory(
        import_id: str, memory_id: str, decision: str, expected_snapshot: str, rationale: str
    ) -> dict:
        """Resolve memory divergence after reading both full versions and their evidence.

        keep_local retains the current record. keep_incoming/keep_both create a
        traceable separate incoming branch; source text is never replaced. Resolve
        case/feedback provenance first. Use a current Dream preview snapshot.
        """
        return await asyncio.to_thread(
            lambda: resolve_memory(
                data_root(), import_id, memory_id, decision, expected_snapshot, rationale
            )
        )

    @mcp.tool()
    async def fx_report_render_issue(issue: RenderIssue) -> dict:
        """Record Nuke-observed render input problems; does not launch Houdini or render."""
        return await asyncio.to_thread(
            lambda: RenderIssues(data_root()).report(**issue.model_dump())
        )

    @mcp.tool()
    async def fx_get_render_issue(issue_id: str, project_id: str, revision: int = 0) -> dict:
        """Read one revision (0=latest) and up to 20 compact history summaries."""
        return await asyncio.to_thread(
            lambda: RenderIssues(data_root()).get(issue_id, project_id, revision)
        )

    @mcp.tool()
    async def fx_list_render_issues(
        project_id: str,
        shot_id: str,
        max_chars: int = 3000,
        before_cursor: int = 0,
        status: str = "all",
    ) -> dict:
        """List bounded issue cards; expand an issue only when needed."""
        return await asyncio.to_thread(
            lambda: RenderIssues(data_root()).list(
                project_id, shot_id, max_chars, before_cursor, status
            )
        )

    @mcp.tool()
    async def fx_update_render_issue(
        issue_id: str,
        project_id: str,
        actor: Literal["houdini", "nuke"],
        status: Literal["open", "investigating", "awaiting_nuke_review", "closed"],
        summary: str,
        evidence: list[str],
        expected_revision: int,
    ) -> dict:
        """Houdini investigates; Nuke reviews before closure. Evidence is caller-reported."""
        return await asyncio.to_thread(
            lambda: RenderIssues(data_root()).update(
                issue_id, project_id, actor, status, summary, evidence, expected_revision
            )
        )

    async def live_target(context):
        bridge = upstream_server._get_bridge(context)
        info = await bridge.execute("scene.get_scene_info", {})
        settings = {
            key: info[key] for key in ("fps", "frame_range", "playback_range") if key in info
        }
        return f"127.0.0.1:{bridge.port}", info["hip_file"], settings

    install_task_gates(mcp, "houdini", data_root, live_target)
    return mcp
