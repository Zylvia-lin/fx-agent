"""Shared knowledge operations. No DCC connections or MCP server initialization."""

import asyncio
from typing import Annotated, Literal

from pydantic import Field

from fx_agent.baselines import sync_baselines
from fx_agent.config import data_root, database_path
from fx_agent.dream import DreamAction
from fx_agent.handoff import RenderIssue, RenderIssues
from fx_agent.learning import Feedback, LearningStore, UserRule
from fx_agent.memory import Experience, MemoryStore
from fx_agent.module_guides import ModuleGuides, refresh_guides
from fx_agent.modules import module_catalog, require_classified_modules
from fx_agent.production_rules import production_rules
from fx_agent.store import Store, compact, scope_id
from fx_agent.task_gates import TaskGates

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
    offset: int = 0,
) -> dict:
    """Read the module workflow supplement; an omitted module selects general.

    Read the mapped upstream guide separately. Only accepted, finalized,
    globally reusable lessons enter generated sections; manual notes remain
    unverified references. get refreshes derived MDs from authoritative DB.
    """
    return await asyncio.to_thread(
        lambda: ModuleGuides(data_root()).get(project_id, module, software, max_chars, offset)
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
    if not database_path(data_root()).exists():
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


async def fx_submit_experience(experience: Experience, fx_task_id: str = "") -> dict:
    """Admit a project-agnostic, text-only reusable experience with explicit provenance.

    Do not include local/UNC paths or archive project files/textures. Explicit user
    statements and observations linked to a verified task enter active recall and
    refresh module guides. Other observations and inferences stay candidates.
    Choose specific modules for this lesson from fx_get_module_catalog.
    To correct an old entry, submit a distinct explicit user statement with supersedes.
    """
    require_classified_modules(experience.modules)
    return await asyncio.to_thread(
        lambda: MemoryStore(data_root()).submit(_task_id=fx_task_id, **experience.model_dump())
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
    offset: int = 0,
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
            offset,
        )
    )


async def fx_get_memory(memory_id: str, project_id: str = "") -> dict:
    """Expand one global text-only memory's method, conditions and provenance."""
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
    """Keep the user's correction, change, reasoning and observed result as text history.

    Pending/improved/failed attempts are not successful workflow memories.
    Accepted requires actual user confirmation (no project/file archiving needed).
    Revising feedback disables methods derived from its older revisions.
    Choose modules based on the technique actually changed.
    """
    require_classified_modules(feedback.modules)
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
    modules: list[str] | None = None,
) -> dict:
    """Distill a reusable text-only method from the current accepted final-result record.

    The host writes the desensitized method and conditions after inspecting the
    correction trace. The service checks acceptance and scope atomically, then
    refreshes global module Markdown guides. Global reuse is default; choose shot
    only for shot-specific methods. Supply modules specific to the distilled
    method when they differ from the feedback tags.
    """
    return await asyncio.to_thread(
        lambda: LearningStore(data_root()).finalize(
            feedback_id, project_id, method, reason, applies_when, reuse_scope, modules=modules
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
    Classify each semantic action by the technique it synthesizes. Sources are
    retained. Conflict actions never become successful recipes.
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


async def fx_get_host_workflow(
    software: Literal["houdini", "nuke", "maya"] = "houdini",
) -> dict:
    """Read the full FX Agent workflow for the selected DCC before production."""
    from fx_agent.workflow import HOST_WORKFLOW_INSTRUCTIONS

    result = {"software": software, "workflow": HOST_WORKFLOW_INSTRUCTIONS}
    if software == "houdini":
        from fxhoudinimcp._loader import load_markdown

        result["upstream_instructions"] = load_markdown("instructions/server_instructions.md")
    return result


def brief_task_state(gates: TaskGates, task_id: str) -> dict:
    state = gates.get(task_id)
    missing = list(dict.fromkeys(state["missing"]))
    return {key: state[key] for key in ("state", "revision", "open_questions")} | {
        "missing": missing[:8],
        "omitted_missing": max(0, len(missing) - 8),
    }


async def fx_prepare_task_brief(
    task_id: str,
    query: str = "",
    include_candidates: bool = False,
    max_chars: int = 16000,
) -> dict:
    """Aggregate scoped rules, context, module guides and memories in one read.

    Automatically records verified task-gate receipts for every component fetched
    so production or quick_tweak tasks avoid 6+ serial preflight tool calls.
    For Houdini, the local MCP wrapper also loads required upstream workflow guides.
    """
    if not 2048 <= max_chars <= 32000:
        raise ValueError("max_chars must be between 2048 and 32000")

    def build() -> dict:
        root = data_root()
        gates = TaskGates(root)
        task = gates.get(task_id, detail=True)
        project_id = task["project_id"]
        shot_id = task["shot_id"]
        software = task["software"]
        modules = list(task["modules"])
        kind = task["kind"]
        search_query = (query.strip() or task["goal"])[:200]
        search_query = " ".join(search_query.split()[:16])
        brief = {
            "task_id": task_id,
            "kind": kind,
            "software": software,
            "modules": modules,
            "query": search_query,
            "production_rules": production_rules(software),
        }
        # Reserve room for the compact gate status and explicit continuation hints.
        content_limit = max_chars - 700
        deferred = []
        receipts = []

        def include(key, value):
            if len(compact(brief | {key: value})) > content_limit:
                return False
            brief[key] = value
            return True

        def observe(tool, arguments, result):
            receipts.append((tool, arguments, result))

        learning = LearningStore(root)
        rule_args = {"project_id": project_id, "software": software}
        rules = None
        offset = 0
        for _ in range(20):
            page = learning.get_rules(
                project_id, min(6000, max(1024, max_chars // 3)), offset, software,
                include_provenance=False,
            )
            if rules and rules["rules_hash"] != page["rules_hash"]:
                break
            merged = page | {"user_rules": (rules or {}).get("user_rules", []) + page["user_rules"]}
            if not include("production_rules", merged):
                break
            rules = merged
            observe("fx_get_production_rules", rule_args | {"offset": offset}, page)
            if not page["omitted_rules"] or page["next_offset"] <= offset:
                break
            offset = page["next_offset"]
        if rules is None or rules["omitted_rules"]:
            deferred.append("fx_get_production_rules")
        expanded = []
        for rule in (rules or {}).get("user_rules", []):
            if not rule.get("detail_required"):
                continue
            args = {
                "rule_key": rule["rule_key"],
                "scope": rule["scope"],
                "revision": rule["revision"],
                "project_id": project_id if rule["scope"] == "project" else "",
            }
            full = learning.get_rule(**args)
            if include("expanded_user_rules", expanded + [full]):
                expanded.append(full)
                observe("fx_get_user_rule", args, full)
            elif "fx_get_user_rule" not in deferred:
                deferred.append("fx_get_user_rule")

        if kind != "quick_tweak":
            guides = ModuleGuides(root)
            module_knowledge = {}
            truncated_modules = []
            per_module_budget = min(3600, max(1024, max_chars // (4 * len(modules))))
            for mod in modules:
                guide = guides.get(project_id, mod, software, per_module_budget, 0)
                if include("module_knowledge", module_knowledge | {mod: guide}):
                    module_knowledge[mod] = guide
                    observe("fx_get_module_knowledge", rule_args | {"module": mod, "offset": 0}, guide)
                    if guide.get("truncated"):
                        truncated_modules.append(mod)
                else:
                    truncated_modules.append(mod)
            if truncated_modules:
                deferred.append("fx_get_module_knowledge")

            mem_store = MemoryStore(root)
            primary_mod = modules[0] if len(modules) == 1 else ""
            mem_args = rule_args | {"shot_id": shot_id, "query": search_query}
            mem_budget = min(4000, max(512, max_chars // 4))

            def search(module, candidates, limit=5):
                args = mem_args | {"module": module, "include_candidates": candidates}
                result = mem_store.search(**args, limit=limit, max_chars=mem_budget)
                return args, result

            args, memories = search(primary_mod, include_candidates)
            if include("memory_matches", memories):
                observe("fx_search_memory", args, memories)
                if not include_candidates and memories.get("reference_hint", {}).get("has_matches"):
                    args, candidates = search(primary_mod, True)
                    if include("candidate_memories", candidates):
                        observe("fx_search_memory", args, candidates)
                    else:
                        deferred.append("fx_search_memory")
            else:
                deferred.append("fx_search_memory")
            module_matches = {}
            for mod in truncated_modules:
                if mod == primary_mod:
                    continue
                args, matches = search(mod, include_candidates, 3)
                if include("module_memory_matches", module_matches | {mod: matches}):
                    module_matches[mod] = matches
                    observe("fx_search_memory", args, matches)
            include("template_library_hint", "需要现成工程时调用 fx_search_published_templates。")

        context = Store(root).get_context(project_id, shot_id, "", min(2000, max(768, max_chars // 8)), software)
        if not include("context", context):
            deferred.append("fx_get_context")
        if deferred:
            brief["deferred_reads"] = deferred
        for tool, args, result in receipts:
            gates.observe(task_id, tool, args, result)
        brief["task_state"] = brief_task_state(gates, task_id)
        return brief

    return await asyncio.to_thread(build)


def shared_functions():
    return [
        value for name, value in globals().items() if name.startswith("fx_") and callable(value)
    ]
