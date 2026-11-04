"""Gate both MCP execution adapters without changing upstream function signatures."""

import asyncio
import copy

from fx_agent.task_gates import TaskGates, TaskSpec, endpoint_lock, same_path

GATE_TOOLS = {
    "fx_begin_task",
    "fx_get_task",
    "fx_update_task",
    "fx_set_task_plan",
    "fx_task_question",
    "fx_finish_task",
}
KNOWLEDGE_WRITES = {
    "fx_learn_templates",
    "fx_annotate_template_effects",
    "fx_refine_effect",
    "fx_merge_effect_cards",
    "fx_learn_template_experience",
    "fx_export_template",
}
SAVES = {"save_scene", "save_script"}
JOBS = {
    "write_cache",
    "cook_frame_range",
    "cook_top_node",
    "start_render",
    "render_frames",
    "render_viewport",
    "render_quad_view",
    "render_sheet",
    "render_node_network",
}
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
    "start_houdini",
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
    return (
        not name.startswith("fx_") and name not in INSPECTION and not name.startswith(READ_PREFIXES)
    )


def install_task_gates(mcp, software, root_provider, live_target):
    """Extend advertised schemas; strip gate arguments before upstream validation."""
    manager = mcp._tool_manager
    if getattr(manager, "_fx_gated", False):
        return

    @mcp.tool()
    def fx_begin_task(spec: TaskSpec) -> dict:
        """Record requirements, target, acceptance and budget before production/learning."""
        return TaskGates(root_provider()).begin(spec)

    @mcp.tool()
    def fx_get_task(task_id: str, detail: bool = False) -> dict:
        """Get compact gate state/missing steps; detail includes questions and evidence."""
        return TaskGates(root_provider()).get(task_id, detail)

    @mcp.tool()
    def fx_update_task(task_id: str, expected_revision: int, spec: TaskSpec) -> dict:
        """Revise changed requirements/inputs/target; invalidate plan and previous evidence."""
        return TaskGates(root_provider()).update(task_id, expected_revision, spec)

    @mcp.tool()
    def fx_set_task_plan(
        task_id: str,
        expected_revision: int,
        method: str,
        applies_when: str,
        changes: str,
        selected_sources: list[str],
        no_match_reason: str = "",
    ) -> dict:
        """Release writes after actual scoped rules/module/effect/memory reads.

        selected_sources uses effect_card:/memory:/template_study: IDs actually
        expanded in this task. Explain no applicable match instead of inventing
        knowledge. A plan is host-authored, not proof of semantic correctness.
        """
        return TaskGates(root_provider()).plan(
            task_id,
            expected_revision,
            method,
            applies_when,
            changes,
            selected_sources,
            no_match_reason,
        )

    @mcp.tool()
    def fx_task_question(
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
        return TaskGates(root_provider()).question(
            task_id, expected_revision, question, question_id, answer, user_quote, source_ref
        )

    @mcp.tool()
    async def fx_finish_task(
        task_id: str, expected_revision: int, verification_note: str, case_id: str = ""
    ) -> dict:
        """Verify current save/structural check/archive/preview or reviewed learning studies.

        verification_note must describe actually viewed results. File existence
        cannot prove aesthetics; user acceptance remains a separate requirement.
        """
        gates = TaskGates(root_provider())
        task = gates.get(task_id, True)
        if task["kind"] == "production":
            target = await live_target(mcp.get_context())
            gates.authorize(task_id, software, *target)
        return await asyncio.to_thread(
            gates.finish, task_id, expected_revision, case_id, verification_note
        )

    original = manager.call_tool
    for tool in manager.list_tools():
        if tool.name in GATE_TOOLS:
            continue
        tool.parameters = copy.deepcopy(tool.parameters)
        tool.parameters.setdefault("properties", {})["fx_task_id"] = {
            "type": "string",
            "description": "Task evidence scope.",
        }
        if (
            is_scene_write(tool.name)
            or tool.name in KNOWLEDGE_WRITES
            or tool.name == "fx_finalize_learning"
        ):
            tool.parameters.setdefault("required", []).append("fx_task_id")

    async def guarded(name, arguments, context=None, convert_result=False):
        if name in GATE_TOOLS:
            return await original(name, arguments, context=context, convert_result=convert_result)
        arguments = dict(arguments)
        task_id = arguments.pop("fx_task_id", "")
        acceptance = (
            name == "fx_record_feedback"
            and arguments.get("feedback", {}).get("outcome") == "accepted"
        )
        knowledge_write = (
            name in KNOWLEDGE_WRITES
            or name == "fx_review_template_effects"
            and arguments.get("reviewed_node_paths") is not None
        )
        required = (
            is_scene_write(name) or knowledge_write or name == "fx_finalize_learning" or acceptance
        )
        if required and not task_id:
            raise ValueError(
                "Task gate requires fx_task_id; use fx_begin_task then read knowledge and save a plan"
            )
        if not task_id:
            return await original(name, arguments, context=context, convert_result=convert_result)
        gates = TaskGates(root_provider())
        task = gates.get(task_id, True)
        with endpoint_lock(gates.root, task["endpoint"]):
            if gates.get(task_id, True)["endpoint"] != task["endpoint"]:
                raise ValueError("Task target changed; retry with current task state")
            if is_scene_write(name):
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
                        if same_path(scene_path, task.get("previous_scene_path", "")):
                            scene_path = destination
                gates.authorize(task_id, software, endpoint, scene_path, scene_context)
                if name not in SAVES | JOBS:
                    gates.edit(
                        task_id
                    )  # Invalidate before dispatch: partial failure is still a possible edit.
                else:
                    gates.invalidate(task_id, saved=name in SAVES, verified=name in JOBS)
            elif name in {"verify_network", "find_broken_reads"}:
                gates.invalidate(task_id, verified=True)
                target = await live_target(context)
                if (
                    task["software"] != software
                    or task["endpoint"] != target[0]
                    or not same_path(task["scene_path"], target[1])
                ):
                    raise ValueError(
                        "Verification target differs from task; omit fx_task_id for unrelated read-only investigation"
                    )
            elif knowledge_write:
                if not task["plan"] or gates.get(task_id)["missing"]:
                    raise ValueError(
                        "Learning task needs current knowledge and a plan before writing studies"
                    )
                if task["kind"] == "learning":
                    gates.invalidate(task_id, verified=True)
            if name == "fx_finalize_learning" or acceptance:
                task = gates.get(task_id, True)
                if task["state"] != "verified" or task["kind"] != "production":
                    raise ValueError(
                        "Successful learning requires a verified production task and user acceptance"
                    )
                gates.authorize(task_id, task["software"], task["endpoint"], task["scene_path"])
                if acceptance:
                    feedback = arguments["feedback"]
                else:
                    from fx_agent.learning import LearningStore

                    feedback = LearningStore(gates.root).get_feedback(
                        arguments["feedback_id"], arguments["project_id"]
                    )
                if any(
                    feedback.get(k, "houdini" if k == "software" else "") != task[k]
                    for k in ("project_id", "shot_id", "software", "case_id")
                ):
                    raise ValueError("Accepted feedback/archive must belong to the verified task")
            result = await original(name, arguments, context=context, convert_result=False)
            gates.observe(task_id, name, arguments, result)
            if isinstance(result, dict):
                open_questions = [
                    q for q in gates.get(task_id, True)["questions"] if not q.get("answer")
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
