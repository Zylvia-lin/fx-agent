"""Gate both MCP execution adapters without changing upstream function signatures."""

import asyncio
import copy
from contextlib import nullcontext

from fx_agent.data_api import brief_task_state
from fx_agent.modules import HOUDINI, require_classified_modules
from fx_agent.store import compact
from fx_agent.task_gates import (
    HEAVY_BUILD_TOOLS,
    QUICK_TWEAK_MAX_EDITS,
    VISUAL_TOOLS,
    SourceAdaptation,
    TaskGates,
    TaskSpec,
    check_knowledge_gate,
    endpoint_lock,
    is_untitled_scene,
    same_path,
)

GATE_TOOLS = {
    "fx_begin_task",
    "fx_get_task",
    "fx_update_task",
    "fx_set_task_plan",
    "fx_task_question",
    "fx_finish_task",
}
BRIEF_TOOLS = {"fx_prepare_task_brief"}
SAVES = {"save_scene", "save_script"}
RENDER_VIEW_WRITES = {
    "fx_render_view_start",
    "fx_render_view_save",
    "fx_render_view_stop",
}
# Preview renders read the current scene and write preview artifacts without mutating nodes.
PREVIEW_JOBS = {
    "render_maya_frame",
    "render_viewport",
    "render_quad_view",
    "render_sheet",
    "render_node_network",
    "fx_render_view_start",
    "fx_render_view_save",
    "fx_render_view_stop",
}
JOBS = {
    "write_cache",
    "cook_frame_range",
    "cook_top_node",
    "start_render",
    "render_frames",
} | PREVIEW_JOBS
# These alter inspection/session state, not production nodes. Cancellation must stay available.
INSPECTION = {
    "set_frame",
    "set_update_mode",
    "set_selection",
    "set_viewport_camera",
    "set_viewport_display",
    "set_viewport_renderer",
    "frame_selection",
    "frame_all",
    "set_viewport_direction",
    "set_current_network",
    "set_viewer_context",
    "connect_houdini",
    "connect_nuke",
    "connect_maya",
    "connect_nuke_viewer",
    "set_nuke_viewer_state",
    "fit_nuke_viewer",
    "reload_nuke_tools",
    "frame_nuke_graph",
    "start_houdini",
    "start_maya",
    "stop_houdini",
    "cancel_top_cook",
    "pause_top_cook",
    "playbar_control",
    "log_status",
}
READ_PREFIXES = (
    "get_",
    "list_",
    "find_",
    "search_",
    "sample_",
    "inspect_",
    "compare_",
    "explain_",
    "capture_",
    "validate_",
    "verify_",
)


def is_scene_write(name):
    if name in RENDER_VIEW_WRITES:
        return True
    return (
        not name.startswith("fx_") and name not in INSPECTION and not name.startswith(READ_PREFIXES)
    )


def install_task_gates(mcp, software, root_provider, live_target):
    """Extend advertised schemas; strip gate arguments before upstream validation."""
    manager = mcp._tool_manager
    if getattr(manager, "_fx_gated", False):
        return

    @mcp.tool()
    async def fx_begin_task(spec: TaskSpec) -> dict:
        """Record target and requirements. Classify the actual work using fx_get_module_catalog."""
        require_classified_modules(spec.modules)
        return await asyncio.to_thread(TaskGates(root_provider()).begin, spec)

    @mcp.tool()
    async def fx_get_task(task_id: str, detail: bool = False, max_chars: int = 24000) -> dict:
        """Get compact gate state/missing steps; detail includes bounded questions and evidence."""
        return await asyncio.to_thread(
            TaskGates(root_provider()).get, task_id, detail, max_chars if detail else 0
        )

    @mcp.tool()
    async def fx_update_task(task_id: str, expected_revision: int, spec: TaskSpec) -> dict:
        """Revise changed requirements/inputs/target; invalidate affected plan and evidence."""
        require_classified_modules(spec.modules)
        return await asyncio.to_thread(
            TaskGates(root_provider()).update, task_id, expected_revision, spec
        )

    @mcp.tool()
    async def fx_set_task_plan(
        task_id: str,
        expected_revision: int,
        method: str,
        applies_when: str,
        changes: str,
        selected_sources: list[str],
        no_match_reason: str = "",
        source_adaptations: list[SourceAdaptation] | None = None,
    ) -> dict:
        """Release writes after actual scoped rules/module/memory reads.

        selected_sources uses memory: IDs actually expanded in this task.
        Explain no applicable match instead of inventing knowledge. Each selected
        source needs source_method, application and verification in source_adaptations.
        These records do not prove visual correctness.
        """
        return await asyncio.to_thread(
            TaskGates(root_provider()).plan,
            task_id,
            expected_revision,
            method,
            applies_when,
            changes,
            selected_sources,
            no_match_reason,
            source_adaptations,
        )

    @mcp.tool()
    async def fx_task_question(
        task_id: str,
        expected_revision: int,
        question: str = "",
        question_id: str = "",
        answer: str = "",
        user_quote: str = "",
        source_ref: str = "",
    ) -> dict:
        """Record a concrete uncertainty and ASK THE USER; unresolved questions block finish.

        Investigate available source/outputs first. Bundle related questions in
        conversation; do not silently skip or mark learning complete. Resolve a
        question only with the actual answer, exact user quote and source; the
        service stores the attribution but cannot authenticate dialogue text.
        """
        return await asyncio.to_thread(
            TaskGates(root_provider()).question,
            task_id,
            expected_revision,
            question,
            question_id,
            answer,
            user_quote,
            source_ref,
        )

    @mcp.tool()
    async def fx_finish_task(
        task_id: str, expected_revision: int, verification_note: str, case_id: str = ""
    ) -> dict:
        """Verify current save and structural check for the target scene.

        No project file archive is required in text-only mode (case_id is optional).
        Capture a current preview with fx_task_id before finishing. The
        verification_note must describe actually viewed results; user acceptance
        remains a separate requirement.
        """
        gates = TaskGates(root_provider())
        task = await asyncio.to_thread(gates.get, task_id, True)
        live_kwargs = {}
        if task["kind"] in {"production", "quick_tweak"}:
            target = await live_target(mcp.get_context())
            await asyncio.to_thread(gates.authorize, task_id, software, *target)
            if isinstance(gates, TaskGates):
                live_kwargs = {
                    "live_endpoint": target[0],
                    "live_scene_path": target[1],
                    "live_scene_context": target[2] if len(target) > 2 else None,
                }
        return await asyncio.to_thread(
            gates.finish, task_id, expected_revision, case_id, verification_note, **live_kwargs
        )

    original = manager.call_tool
    for tool in manager.list_tools():
        if tool.name in GATE_TOOLS | BRIEF_TOOLS:
            continue
        tool.parameters = copy.deepcopy(tool.parameters)
        tool.parameters.setdefault("properties", {})["fx_task_id"] = {
            "type": "string",
            "description": "Task evidence scope (use fx_task_id here; task management tools use task_id).",
        }
        if is_scene_write(tool.name) or tool.name == "fx_finalize_learning":
            tool.parameters.setdefault("required", []).append("fx_task_id")

    async def guarded(name, arguments, context=None, convert_result=False):
        if name in GATE_TOOLS:
            return await original(name, arguments, context=context, convert_result=convert_result)
        if name in BRIEF_TOOLS:
            result = await original(name, arguments, context=context, convert_result=False)
            # For Houdini production/learning briefs, locally load required upstream workflow guides
            # and record their receipts so a single brief satisfies the entire preflight gate.
            if (
                software == "houdini"
                and isinstance(result, dict)
                and result.get("software") == "houdini"
                and result.get("kind") != "quick_tweak"
                and manager.get_tool("get_workflow_guide") is not None
            ):
                brief_task_id = str(arguments.get("task_id", "") or result.get("task_id", ""))
                topics = ["discipline"] + sorted(
                    {
                        HOUDINI[m][1]
                        for m in result.get("modules", [])
                        if m in HOUDINI and HOUDINI[m][1] not in {None, "", "discipline"}
                    }
                )
                guides_out: dict[str, str] = {}
                gates = TaskGates(root_provider())
                budget = arguments.get("max_chars", 16000)
                for topic in topics:
                    guide_val = await original(
                        "get_workflow_guide",
                        {"topic": topic},
                        context=context,
                        convert_result=False,
                    )
                    text = guide_val if isinstance(guide_val, str) else str(guide_val)
                    candidate = guides_out | {topic: text}
                    if len(compact(result | {"workflow_guides": candidate})) > budget - 60:
                        continue
                    await asyncio.to_thread(
                        gates.observe, brief_task_id, "get_workflow_guide", {"topic": topic}, guide_val
                    )
                    guides_out[topic] = text
                if guides_out:
                    result["workflow_guides"] = guides_out
                result["task_state"] = await asyncio.to_thread(brief_task_state, gates, brief_task_id)
            if convert_result:
                result = manager.get_tool(name).fn_metadata.convert_result(result)
            return result
        arguments = dict(arguments)
        task_id = arguments.pop("fx_task_id", "")
        acceptance = (
            name == "fx_record_feedback"
            and isinstance(arguments.get("feedback"), dict)
            and arguments["feedback"].get("outcome") == "accepted"
        )
        scene_write = is_scene_write(name)
        required = scene_write or name == "fx_finalize_learning" or acceptance
        if required and not task_id:
            raise ValueError(
                "Task gate requires fx_task_id (note: fx_begin_task/fx_get_task/fx_prepare_task_brief "
                "use task_id, while gated DCC/knowledge tools use fx_task_id); "
                "call fx_begin_task then fx_prepare_task_brief"
            )
        if not task_id:
            return await original(name, arguments, context=context, convert_result=convert_result)
        gates = TaskGates(root_provider())
        task = await asyncio.to_thread(gates.get, task_id, True)
        # Only serialize exclusive endpoint mutations (scene writes, verifications, knowledge/finalization writes);
        # read-only evidence lookups do not block behind a running render/cook.
        visual = name in VISUAL_TOOLS[software]
        needs_lock = required or visual or name in {"verify_network", "find_broken_reads"}
        lock_ctx = endpoint_lock(gates.root, task["endpoint"]) if needs_lock else nullcontext()
        with lock_ctx:
            if needs_lock and (await asyncio.to_thread(gates.get, task_id, True))["endpoint"] != task["endpoint"]:
                raise ValueError("Task target changed; retry with current task state")
            if scene_write:
                if task["kind"] == "quick_tweak":
                    if name in HEAVY_BUILD_TOOLS:
                        raise ValueError(
                            f"Tool '{name}' is blocked in quick_tweak mode; "
                            "upgrade kind to 'production' via fx_update_task"
                        )
                    if name not in SAVES | JOBS and task["edit_seq"] >= QUICK_TWEAK_MAX_EDITS:
                        raise ValueError(
                            f"quick_tweak edit limit ({QUICK_TWEAK_MAX_EDITS}) reached; "
                            "upgrade kind to 'production' via fx_update_task"
                        )
                target = await live_target(context)
                endpoint, scene_path = target[:2]
                scene_context = target[2] if len(target) > 2 else None
                # Loading a selected work scene is permitted only when the requested
                # destination matches the task. Merging another scene still targets current.
                if name in {"load_scene", "load_script"} and not arguments.get("merge"):
                    destination = arguments.get("file_path", arguments.get("path", ""))
                    if not same_path(destination, task["scene_path"]):
                        raise ValueError("Task target differs from requested load destination")
                    scene_path = destination
                    scene_context = None
                if name in SAVES:
                    destination = arguments.get("file_path", arguments.get("path"))
                    if destination:
                        if not same_path(destination, task["scene_path"]):
                            raise ValueError(
                                "Save destination differs from task target; update the task first"
                            )
                        if (
                            same_path(scene_path, task.get("previous_scene_path", ""))
                            or is_untitled_scene(scene_path)
                        ):
                            scene_path = destination
                await asyncio.to_thread(
                    gates.authorize, task_id, software, endpoint, scene_path, scene_context
                )
                if name not in SAVES | JOBS:
                    await asyncio.to_thread(
                        gates.edit, task_id
                    )  # Invalidate before dispatch: partial failure is still a possible edit.
                elif name in SAVES or (name in JOBS and name not in PREVIEW_JOBS):
                    await asyncio.to_thread(
                        gates.invalidate,
                        task_id,
                        saved=name in SAVES,
                        verified=name in JOBS and name not in PREVIEW_JOBS,
                    )
            elif visual or name in {"verify_network", "find_broken_reads"}:
                target = await live_target(context)
                if (
                    task["software"] != software
                    or task["endpoint"] != target[0]
                    or not same_path(task["scene_path"], target[1])
                ):
                    raise ValueError(
                        "Verification target differs from task; omit fx_task_id for unrelated read-only investigation"
                    )
                if not visual:
                    await asyncio.to_thread(gates.invalidate, task_id, verified=True)
            elif name == "fx_finalize_learning" or acceptance:
                await asyncio.to_thread(
                    check_knowledge_gate,
                    gates,
                    task_id,
                    tool_name=name,
                    arguments=arguments,
                )
            viewport_capture = software == "houdini" and name in {"capture_screenshot", "render_viewport"}
            before = None
            if viewport_capture:
                before = await original("get_frame", {}, context=context, convert_result=False)
            call_args = arguments | {"fx_task_id": task_id} if name == "fx_submit_experience" else arguments
            result = await original(name, call_args, context=context, convert_result=False)
            if viewport_capture and isinstance(result, dict):
                after = await original("get_frame", {}, context=context, convert_result=False)
                frame = after.get("frame") if isinstance(after, dict) and before == after else None
                result = result | {"frame": frame}
            await asyncio.to_thread(gates.observe, task_id, name, arguments, result)
            if isinstance(result, dict):
                open_questions = [
                    q
                    for q in (await asyncio.to_thread(gates.get, task_id, True))["questions"]
                    if not q.get("answer")
                ]
                if open_questions:
                    result = result | {
                        "task_action_required": "ask_user",
                        "task_questions": [
                            {"question_id": q["question_id"], "question": q["question"]}
                            for q in open_questions
                        ],
                    }
            if convert_result:
                result = manager.get_tool(name).fn_metadata.convert_result(result)
            return result

    manager.call_tool = guarded
    manager._fx_gated = True
