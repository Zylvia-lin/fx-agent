"""Shared knowledge operations. No DCC connections or MCP server initialization."""

import asyncio
import threading
from contextlib import suppress
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field

from fx_agent.archive import ArchiveCancelled, ArtifactInput, CaseArchive, create_case_async
from fx_agent.baselines import sync_baselines
from fx_agent.config import data_root, database_path
from fx_agent.data_import import import_data, import_records, resolve_memory
from fx_agent.dream import DreamAction
from fx_agent.effect_cards import EffectCardInput, EffectExclusion, EffectLibrary, MediaReview
from fx_agent.effect_study import EffectStudy
from fx_agent.handoff import RenderIssue, RenderIssues
from fx_agent.learning import Feedback, LearningStore, UserRule
from fx_agent.memory import Experience, MemoryStore
from fx_agent.module_guides import ModuleGuides, refresh_guides
from fx_agent.modules import module_catalog
from fx_agent.production_rules import production_rules
from fx_agent.store import Store, compact, scope_id
from fx_agent.templates import TemplateLibrary, TemplatePolicy

RULES_PAGINATION_ERROR = (
    "max_chars must be 1024..12000 and offset must be >= 0; paginate using next_offset"
)


async def fx_sync_module_baselines() -> dict:
    """Synchronize current upstream rules and project workflows in data/baselines.

    Preserves source files; adds, updates and removes current baseline files
    without keeping numbered history copies. Includes hashes and licenses.
    Writes module README indexes separately from learned supplements;
    baseline rules are not personal experiences and Dream does not rewrite them.
    """
    return await asyncio.to_thread(sync_baselines, data_root())


async def fx_get_module_catalog(software: Literal["houdini", "nuke", "maya"] = "houdini") -> dict:
    """List functional memory tags and upstream guide topics, not guide full texts.

    Houdini baselines remain in get_workflow_guide; select only the relevant
    guide. Never relabel upstream documentation as a learned successful case.
    """
    return module_catalog(software)


async def fx_get_module_knowledge(
    project_id: str = "",
    module: str = "",
    software: Literal["houdini", "nuke", "maya"] = "houdini",
    max_chars: int = 4000,
) -> dict:
    """Read the module workflow supplement; an omitted module selects general.

    Read the mapped upstream guide separately. Only accepted, finalized,
    globally reusable lessons enter generated sections; manual notes remain
    unverified references. get refreshes derived MDs from authoritative DB.
    """
    return await asyncio.to_thread(
        lambda: ModuleGuides(data_root()).get(project_id, module, software, max_chars)
    )


async def fx_refresh_module_guides(project_id: str = "") -> dict:
    """Rebuild global module MDs; preserve text outside managed sections.

    pending_rebuild means the SQLite learning record remains saved, but
    Markdown synchronization needs repair. Never silently claim it updated.
    """
    return await asyncio.to_thread(lambda: refresh_guides(data_root(), project_id))


async def fx_get_production_rules(
    project_id: str = "",
    max_chars: Annotated[
        int,
        Field(
            ge=1024,
            le=12000,
            description="JSON character budget: 1024..12000. Read subsequent pages with next_offset.",
        ),
    ] = 6000,
    offset: Annotated[
        int,
        Field(
            ge=0,
            description="Non-negative rule offset; use next_offset from the previous response.",
        ),
    ] = 0,
    software: Literal["houdini", "nuke", "maya"] = "houdini",
) -> dict:
    """Read baseline and revisioned user rules before using learned recipes.

    User text is stored with its quote and scope; it does not install code
    or silently change the bridge's machine checks.
    """
    if not 1024 <= max_chars <= 12000 or offset < 0:
        raise ValueError(RULES_PAGINATION_ERROR)
    if project_id:
        scope_id.validate_python(project_id)
    from fx_agent.data_context import current_backend

    if not current_backend() and not database_path(data_root()).exists():
        return production_rules(software) | {
            "user_rules": [],
            "omitted_rules": 0,
            "next_offset": offset,
        }
    return await asyncio.to_thread(
        lambda: LearningStore(data_root()).get_rules(project_id, max_chars, offset, software)
    )


async def fx_save_checkpoint(
    project_id: str,
    shot_id: str,
    summary: str,
    scene_path: str,
    next_steps: list[str],
    expected_revision: int,
    software: Literal["houdini", "nuke", "maya"] = "houdini",
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


async def fx_get_checkpoint(
    project_id: str, shot_id: str, software: Literal["houdini", "nuke", "maya"] = "houdini"
) -> dict:
    """Read complete saved progress for a shot; recheck the live scene before editing."""
    checkpoint = await asyncio.to_thread(
        lambda: Store(data_root()).get_checkpoint(project_id, shot_id, software)
    )
    return {"checkpoint": checkpoint, "requires_live_verification": True}


async def fx_get_context(
    project_id: str,
    shot_id: str,
    since_hash: str = "",
    max_chars: int = 6000,
    software: Literal["houdini", "nuke", "maya"] = "houdini",
) -> dict:
    """Get saved progress within a JSON character budget; matching hash omits repeats.

    Fetch fx_get_checkpoint for details omitted by the budget. Not a live
    scene check and not an exact tokenizer budget.
    """
    return await asyncio.to_thread(
        lambda: Store(data_root()).get_context(project_id, shot_id, since_hash, max_chars, software)
    )


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


async def fx_get_case(case_id: str, project_id: str, detail: bool = False) -> dict:
    """Read a case summary, or expand its file manifest with detail=true."""
    return await asyncio.to_thread(
        lambda: (
            CaseArchive(data_root()).get(case_id, project_id)
            if detail
            else CaseArchive(data_root()).summary(case_id, project_id)
        )
    )


async def fx_validate_case(case_id: str, project_id: str) -> dict:
    """Validate copied file checksums. Does not open HIP or verify visual reproduction."""
    return await asyncio.to_thread(lambda: CaseArchive(data_root()).verify(case_id, project_id))


async def fx_submit_experience(experience: Experience) -> dict:
    """Screen milestone knowledge, keeping inferences as non-default candidates.

    Codex must truthfully label source_type. User statements are not visual
    verification. Project-dependent entries need an archived project for their software. To correct
    an old entry, submit a distinct explicit user statement with supersedes.
    """
    return await asyncio.to_thread(
        lambda: MemoryStore(data_root()).submit(**experience.model_dump())
    )


async def fx_search_memory(
    project_id: str = "",
    query: str = "",
    shot_id: str = "",
    include_candidates: bool = False,
    limit: int = 5,
    max_chars: int = 3000,
    software: Literal["houdini", "nuke", "maya"] = "houdini",
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


async def fx_get_memory(memory_id: str, project_id: str = "") -> dict:
    """Expand one global memory's method, conditions, provenance and case reference."""
    return await asyncio.to_thread(lambda: MemoryStore(data_root()).get(memory_id, project_id))


async def fx_record_user_rule(rule: UserRule) -> dict:
    """Persist an explicit dialogue requirement with exact quote, scope and revision.

    Do not label inferred preferences as user statements. Global scope is
    explicit; project rules require project_id. Revision 0 creates.
    """
    return await asyncio.to_thread(
        lambda: LearningStore(data_root()).record_rule(**rule.model_dump())
    )


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


async def fx_record_feedback(feedback: Feedback) -> dict:
    """Keep the user's correction, change, reasoning and observed result as history.

    Pending/improved/failed attempts are not successful workflow memories.
    Accepted requires actual user confirmation plus an archived HIP and preview.
    Revising feedback disables methods derived from its older revisions.
    """
    return await asyncio.to_thread(
        lambda: LearningStore(data_root()).record_feedback(**feedback.model_dump())
    )


async def fx_get_learning_history(
    project_id: str,
    shot_id: str,
    effect_type: str = "",
    max_chars: int = 6000,
    before_cursor: int = 0,
    software: Literal["houdini", "nuke", "maya"] = "houdini",
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


async def fx_get_feedback(feedback_id: str, project_id: str) -> dict:
    """Expand one feedback record's complete quote, changes and result within scope."""
    return await asyncio.to_thread(
        lambda: LearningStore(data_root()).get_feedback(feedback_id, project_id)
    )


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


async def fx_undo_dream(run_id: str) -> dict:
    """Undo an unchanged Dream result; refuse to overwrite subsequent knowledge changes."""
    return await asyncio.to_thread(lambda: LearningStore(data_root()).undo_dream(run_id))


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


async def fx_import_data(source: str, apply: bool = False) -> dict:
    """Preview or append another data directory into the configured root.

    Stop archive writers first. Preserve conflicts and snapshot; never overwrite
    local records. Run host-authored Dream after import to consolidate knowledge.
    """
    return await asyncio.to_thread(lambda: import_data(Path(source), data_root(), apply=apply))


async def fx_get_import_records(
    import_id: str = "", table: str = "", offset: int = 0, limit: int = 20
) -> dict:
    """List import batches or expand divergent incoming/local rows for host review."""
    return await asyncio.to_thread(
        lambda: import_records(data_root(), import_id, table=table, offset=offset, limit=limit)
    )


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


async def fx_report_render_issue(issue: RenderIssue) -> dict:
    """Record Nuke-observed render input problems; does not launch Houdini or render."""
    return await asyncio.to_thread(lambda: RenderIssues(data_root()).report(**issue.model_dump()))


async def fx_get_render_issue(issue_id: str, project_id: str, revision: int = 0) -> dict:
    """Read one revision (0=latest) and up to 20 compact history summaries."""
    return await asyncio.to_thread(
        lambda: RenderIssues(data_root()).get(issue_id, project_id, revision)
    )


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


async def fx_plan_effect_study(
    study_id: str,
    media_source: str = "",
    section: str = "all",
    offset: int = 0,
    limit: int = 50,
    max_chars: int = 12000,
    node_path: str = "",
) -> dict:
    """Trace author labels and render outputs, inventory supplied movies/sequences.

    Re-extracts the archived original without changing old history. Supply the
    user-provided media directory explicitly; never scans a live GUI scene.
    Paginate annotations/render_outputs/media. Inspect sample_paths, the whole
    movie and dense temporal samples before naming effects. All items start
    not_viewed; path matches and spatial box candidates are not visual proof.
    Unclear GEO: follow related_node_paths to outputs, inspect their sequences,
    then ask the user only about remaining missing/ambiguous evidence.
    """
    return await asyncio.to_thread(
        EffectStudy(data_root()).plan,
        study_id,
        media_source,
        section=section,
        offset=offset,
        limit=limit,
        max_chars=max_chars,
        node_path=node_path,
    )


async def fx_get_effect_learning_queue(
    offset: int = 0,
    limit: int = 20,
    max_chars: int = 12000,
) -> dict:
    """List current studies needing second-pass identification or user answers.

    Old cards remain searchable. Revise using all current supersedes, preserve
    effect_key, and record new evidence/names. Never invent missing visuals.
    """
    return await asyncio.to_thread(
        EffectStudy(data_root()).queue, offset=offset, limit=limit, max_chars=max_chars
    )


async def fx_refine_effect(
    card_id: str,
    updates: dict,
    sources: list[str],
    reason: str,
) -> dict:
    """Add names/details newly understood during iteration or Dream.

    Read current card first. Updates are EffectCardInput descriptive fields;
    omitted fields are preserved. Sources use effect_card:, memory:, feedback:
    or dream: followed by an existing ID. A new immutable revision supersedes
    this card; stale heads are refused. Never use this to rename a different
    variant. Archive new representative samples with fx_archive_case, then
    reference that case_id in visual_evidence to improve an old study.
    """
    return await asyncio.to_thread(
        EffectLibrary(data_root()).refine, card_id, updates, sources, reason
    )


async def fx_plan_template_import(source: str, policy: TemplatePolicy | None = None) -> dict:
    """Read mixed HIP/NK directory metadata; no data writes or DCC connection.

    Prunes backup folders and links, groups render sequences, and reports
    cache volume. Preview does not prove project/preview version pairing.
    """
    return await asyncio.to_thread(
        TemplateLibrary(data_root()).plan, source, policy.model_dump() if policy else None
    )


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
            TemplateLibrary(data_root()).ingest,
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

    def search():
        root = data_root()
        result = TemplateLibrary(root).search(
            query,
            software=software,
            module=module,
            project_id=project_id,
            include_history=include_history,
            offset=offset,
            limit=limit,
            max_chars=max_chars,
        )
        effects = EffectLibrary(root).search(
            query,
            software=software,
            module=module,
            project_id=project_id,
            include_history=include_history,
            limit=3,
            max_chars=max_chars,
        )
        result["effect_matches"] = effects
        # Preserve useful discovery cards when the combined response is small-budget.
        for collection, start, id_key in (
            (result, offset, "study_id"),
            (effects, 0, "card_id"),
        ):
            while collection["items"] and len(compact(result)) > max_chars:
                if len(collection["items"]) == 1 and not collection["items"][0].get(
                    "detail_required"
                ):
                    item = collection["items"][0]
                    collection["items"] = [
                        {
                            id_key: item[id_key],
                            "title": item["title"][:80],
                            "detail_required": True,
                        }
                    ]
                else:
                    collection["items"].pop()
                collection["next_offset"] = start + len(collection["items"])
                collection["truncated"] = collection["next_offset"] < collection["total"]
        return result

    return await asyncio.to_thread(search)


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

    def read():
        root = data_root()
        result = TemplateLibrary(root).get(
            study_id, section=section, offset=offset, limit=limit, max_chars=max_chars
        )
        if section == "summary":
            catalog = EffectLibrary(root).catalog(study_id)
            result["effect_catalog"] = catalog
            if len(compact(result)) > max_chars:
                result = {
                    "study_id": study_id,
                    "effect_catalog": catalog,
                    "detail_required": True,
                    "truncated": True,
                    "next_sections": ["nodes", "dependencies", "history"],
                    "effect_lookup_tool": "fx_search_effects",
                }
        return result

    return await asyncio.to_thread(read)


async def fx_validate_template(study_id: str) -> dict:
    """Validate archived file hashes; does not claim scene/visual reproducibility."""
    return await asyncio.to_thread(TemplateLibrary(data_root()).validate, study_id)


async def fx_export_template(study_id: str, destination: str) -> dict:
    """Copy a study into a new work directory without changing source parameters.

    Refuses an existing destination; restores internal relative file layout.
    External dependencies remain mapped in template-manifest.json; absolute
    file references are preserved and need checking on the work copy.
    """
    return await asyncio.to_thread(TemplateLibrary(data_root()).export, study_id, destination)


async def fx_learn_template_experience(
    study_id: str,
    title: str,
    method: str,
    reason: str,
    applies_when: str,
    modules: list[str],
    effect_card_id: str = "",
) -> dict:
    """Save host-authored interpretation as an inferred candidate linked to a study.

    Cannot promote extracted parameters into verified success. After actual
    use, record feedback, archive the accepted result and finalize through
    the existing learning tools; Dream retains provenance and revisions.
    """
    return await asyncio.to_thread(
        TemplateLibrary(data_root()).submit_experience,
        study_id,
        title=title,
        method=method,
        reason=reason,
        applies_when=applies_when,
        modules=modules,
        effect_card_id=effect_card_id,
    )


async def fx_annotate_template_effects(study_id: str, cards: list[EffectCardInput]) -> dict:
    """Save one text card per effect variant after inspecting the source study.

    Shared scheme_key/node_paths do not merge different effect_key cards.
    Describe subject, appearance, motion, parameter/animation/render/comp
    differences, scope, node evidence and concrete reuse steps. Descriptions
    are host-authored, never auto-inferred from node names. Use supersedes
    for all current heads when revising; originals and source files stay intact.
    """
    return await asyncio.to_thread(
        EffectLibrary(data_root()).annotate, study_id, [c.model_dump() for c in cards]
    )


async def fx_merge_effect_cards(
    card_ids: list[str], card: EffectCardInput, reason: str, apply: bool = False
) -> dict:
    """Preview or merge same-effect object copies/render components into one card.

    Read every full current source card first; explain why they compose one
    effect rather than independent looks/motions. Supply a NEW effect_key and
    a host-authored combined description with every source node, input, reuse
    node, preview and visual evidence. Preserve per-layer differences in notes
    and aliases. Default preview does not write. apply atomically creates a
    reference card; originals remain readable/history-searchable. No promotion
    to successful experience. Do not merge merely similar names or shared nodes.
    """
    return await asyncio.to_thread(
        EffectLibrary(data_root()).merge,
        card_ids,
        card.model_dump(),
        reason,
        apply=apply,
    )


async def fx_search_effects(
    query: str = "",
    software: str = "",
    module: str = "",
    project_id: str = "",
    scheme_key: str = "",
    study_id: str = "",
    include_history: bool = False,
    offset: int = 0,
    limit: int = 10,
    max_chars: int = 6000,
) -> dict:
    """Find individual looks/motions from natural descriptions such as body energy particles.

    Uses weighted local text retrieval and host-provided aliases, not a vector
    model. Each scheme may have many variants. Open fx_get_effect to compare
    motion, animation and rendering differences before exporting a work copy.
    """
    return await asyncio.to_thread(
        EffectLibrary(data_root()).search,
        query,
        software=software,
        module=module,
        project_id=project_id,
        scheme_key=scheme_key,
        study_id=study_id,
        include_history=include_history,
        offset=offset,
        limit=limit,
        max_chars=max_chars,
    )


async def fx_get_effect(
    card_id: str, section: str = "card", offset: int = 0, max_chars: int = 12000
) -> dict:
    """Read a versioned effect card, or paginate its Markdown with section=text.

    Card text is authoritative in SQLite; generated data/effects MDs are views.
    Inspect node paths and evidence, then use fx_export_template(study_id,...).
    Reference cards are not verified successful workflows.
    """
    return await asyncio.to_thread(
        EffectLibrary(data_root()).get,
        card_id,
        section=section,
        offset=offset,
        max_chars=max_chars,
    )


async def fx_review_template_effects(
    study_id: str,
    reviewed_node_paths: list[str] | None = None,
    expected_card_ids: list[str] | None = None,
    exclusions: list[EffectExclusion] | None = None,
    note: str = "",
    media_source: str = "",
    media_reviews: list[MediaReview] | None = None,
) -> dict:
    """Preview effect annotation coverage; optionally record an explicit host review.

    To write, provide all reviewed source node paths, current card IDs and a
    reason for each technical node not mapped to a card. A source variant may
    share nodes with others. This records host review, not proof that every
    conceivable parameter combination or visual effect was discovered.
    Revisions/merged branches make previous reviews stale automatically.
    Completion also requires explicit media_source and one media_review per
    inventoried movie/sequence/image. Unavailable or unassigned media are saved
    as issues and prevent completion. Identity questions also block completion.
    """
    return await asyncio.to_thread(
        EffectLibrary(data_root()).review,
        study_id,
        reviewed_node_paths=reviewed_node_paths,
        expected_card_ids=expected_card_ids,
        exclusions=[x.model_dump() for x in exclusions or []],
        note=note,
        media_source=media_source,
        media_reviews=[x.model_dump() for x in media_reviews or []],
    )


def shared_functions():
    return [
        value for name, value in globals().items() if name.startswith("fx_") and callable(value)
    ]
