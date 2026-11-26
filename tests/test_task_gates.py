"""Task gates must reject skipped steps before dispatching any scene write."""

import json
from pathlib import Path

import pytest
from mcp.server.fastmcp import FastMCP

from fx_agent.task_gates import SourceAdaptation, TaskGates, TaskSpec, install_task_gates


def spec(tmp_path, **changes):
    return TaskSpec(
        project_id="p",
        shot_id="s",
        software="houdini",
        kind="production",
        scene_path=str(tmp_path / "scene.hip"),
        endpoint="127.0.0.1:18186",
        goal="制作贴身能量粒子",
        acceptance=["粒子贴合角色且运动连续"],
        budget="低分辨率草稿，最多三轮",
        modules=["particles"],
        **changes,
    )


def evidence(gates, task_id):
    gates.observe(task_id, "get_workflow_guide", {"topic": "discipline"}, "discipline")
    gates.observe(task_id, "get_workflow_guide", {"topic": "dopparticles"}, "particles")
    gates.observe(
        task_id,
        "fx_get_production_rules",
        {"project_id": "p"},
        {"user_rules": [], "omitted_rules": 0, "next_offset": 0},
    )
    gates.observe(
        task_id,
        "fx_get_module_knowledge",
        {"software": "houdini", "module": "particles"},
        {"text": "baseline"},
    )
    gates.observe(
        task_id, "fx_search_memory", {"software": "houdini", "query": "贴身能量"}, {"items": []}
    )


def ready(tmp_path, *, root=None, scene_path=None):
    gates = TaskGates(root or tmp_path / "data")
    definition = spec(tmp_path)
    if scene_path:
        definition = definition.model_copy(update={"scene_path": str(scene_path)})
    task = gates.begin(definition)
    evidence(gates, task["task_id"])
    task = gates.plan(
        task["task_id"],
        task["revision"],
        "先以低分辨率粒子草稿验证贴附",
        "当前角色输入及尺度",
        "替换输入并核对动画",
        [],
        "已检索，无适用案例",
    )
    return gates, task


def adaptation(source):
    return {
        "source": source,
        "source_method": "读取源方法及关键连接",
        "application": "保留速度平流并适配Y轴，改动写入节点方案",
        "verification": "实际查看前中后帧的丝流和轮廓",
        "source_node_paths": [],
    }


@pytest.mark.parametrize("path", ["", " " * 3, "/" + "a" * 4096])
def test_adaptation_source_paths_are_bounded(path):
    with pytest.raises(ValueError):
        SourceAdaptation.model_validate(
            adaptation("memory:" + "a" * 32) | {"source_node_paths": [path]}
        )


def test_empty_active_search_with_reference_hint_requires_reference_inspection(tmp_path):
    gates, task = ready(tmp_path)
    gates.observe(
        task["task_id"],
        "fx_search_memory",
        {"module": "particles", "query": "能量"},
        {"items": [], "reference_hint": {"has_matches": True}},
    )
    with pytest.raises(ValueError, match="candidate_search"):
        gates.plan(task["task_id"], task["revision"], "方法", "条件", "差异", [], "没有正式经验")
    gates.observe(
        task["task_id"],
        "fx_search_memory",
        {"module": "particles", "query": "能量", "include_candidates": True, "source_project": "unrelated"},
        {"items": []},
    )
    assert "candidate_search:particles" in gates.get(task["task_id"])["missing"]
    gates.observe(
        task["task_id"],
        "fx_search_memory",
        {"module": "particles", "query": "能量", "include_candidates": True},
        {"items": [{"memory_id": "a" * 32, "status": "candidate"}]},
    )
    result = gates.plan(
        task["task_id"],
        task["revision"],
        "方法",
        "条件",
        "差异",
        [],
        "候选依赖不同，按已读指南重新设计",
    )
    assert result["state"] == "ready"


def test_compact_json_receipt_retains_rules_offsets_and_errors(tmp_path):
    gates = TaskGates(tmp_path / "data")
    task = gates.begin(spec(tmp_path))
    evidence(gates, task["task_id"])
    gates.observe(task["task_id"], "fx_get_production_rules", {"project_id": "p"},
                  json.dumps({"user_rules": [], "omitted_rules": 0, "next_offset": 1}))
    assert "rules" not in gates.get(task["task_id"])["missing"]
    gates.observe(task["task_id"], "fx_get_production_rules", {"project_id": "p", "offset": 1},
                  json.dumps({"error": "failed read"}))
    receipts = gates.get(task["task_id"], True)["receipts"].values()
    rules = [r for r in receipts if r["tool"] == "fx_get_production_rules"]
    assert len(rules) == 1 and rules[0]["next_offset"] == 1


def test_plan_requires_actual_reads_and_reuses_evidence(tmp_path):
    gates = TaskGates(tmp_path / "data")
    task = gates.begin(spec(tmp_path))
    with pytest.raises(ValueError, match="knowledge"):
        gates.plan(task["task_id"], task["revision"], "方法", "条件", "差异", [], "未找到")
    evidence(gates, task["task_id"])
    task = gates.plan(
        task["task_id"], task["revision"], "方法", "条件", "差异", [], "未找到适用案例"
    )
    assert task["state"] == "ready"
    assert TaskGates(gates.root).get(task["task_id"])["state"] == "ready"
    for _ in range(3):
        gates.authorize(task["task_id"], "houdini", task["endpoint"], task["scene_path"])


def test_wrong_target_revision_and_changed_requirements_refuse(tmp_path):
    gates, task = ready(tmp_path)
    with pytest.raises(ValueError, match="target"):
        gates.authorize(task["task_id"], "houdini", task["endpoint"], "D:/other.hip")
    with pytest.raises(ValueError, match="revision"):
        gates.update(task["task_id"], 0, spec(tmp_path))
    changed = gates.update(task["task_id"], task["revision"], spec(tmp_path))
    assert changed["state"] == "needs_knowledge"
    with pytest.raises(ValueError, match="ready"):
        gates.authorize(task["task_id"], "houdini", task["endpoint"], task["scene_path"])


def test_questions_request_user_and_block_completion(tmp_path):
    gates, task = ready(tmp_path)
    question = gates.question(task["task_id"], task["revision"], "该序列对应v002还是v003？")
    assert question["action_required"] == "ask_user"
    task = gates.get(task["task_id"])
    with pytest.raises(ValueError, match="question"):
        gates.finish(task["task_id"], task["revision"], "", "未解决不能结束")
    with pytest.raises(ValueError, match="quote"):
        gates.question(
            task["task_id"],
            task["revision"],
            "",
            question["question_id"],
            "v003",
            "",
            "用户本轮答复",
        )
    answered = gates.question(
        task["task_id"],
        task["revision"],
        "",
        question["question_id"],
        "v003",
        "对应v003",
        "用户本轮答复",
    )
    assert answered["action_required"] == "continue"


@pytest.mark.asyncio
async def test_tool_dispatch_is_guarded_without_changing_upstream_arguments(tmp_path):
    mcp = FastMCP("gated-test")
    calls = []

    @mcp.tool()
    def create_node(name: str) -> dict:
        calls.append(name)
        return {"created": name}

    @mcp.tool()
    def fx_search_memory(query: str = "") -> dict:
        return {"items": [], "total": 0}

    async def live(_context):
        return "127.0.0.1:18186", str(tmp_path / "scene.hip")

    install_task_gates(mcp, "houdini", lambda: tmp_path / "data", live)
    schema = {t.name: t.inputSchema for t in await mcp.list_tools()}
    assert "fx_task_id" in schema["create_node"]["required"]
    with pytest.raises(ValueError, match="task"):
        await mcp.call_tool("create_node", {"name": "forbidden"})
    assert calls == []
    gates, task = ready(tmp_path)
    result = await mcp.call_tool("create_node", {"name": "allowed", "fx_task_id": task["task_id"]})
    assert calls == ["allowed"] and "allowed" in str(result)
    assert gates.get(task["task_id"], detail=True)["edit_seq"] == 1


def test_completion_requires_saved_current_scene_and_verification(tmp_path, record_preview):
    gates, task = ready(tmp_path)
    path = Path(task["scene_path"])
    path.write_bytes(b"saved scene")
    with pytest.raises(ValueError, match="saved"):
        gates.finish(task["task_id"], task["revision"], "", "已查看预览")
    gates.observe(task["task_id"], "save_scene", {}, {"success": True})
    with pytest.raises(ValueError, match="verification"):
        gates.finish(task["task_id"], task["revision"], "", "已查看预览")
    gates.observe(task["task_id"], "verify_network", {}, {"valid": True})
    with pytest.raises(ValueError, match="preview image"):
        gates.finish(task["task_id"], task["revision"], "", "ok")
    record_preview(gates, task["task_id"])
    done = gates.finish(
        task["task_id"], task["revision"], "", "已查看预览，贴合且运动连续"
    )
    assert done["state"] == "verified"
    assert gates.get(task["task_id"], True)["reviewed_previews"][0]["frame"] == 101
    gates.edit(task["task_id"])
    assert gates.get(task["task_id"])["state"] == "ready"


def test_rules_pagination_cannot_be_skipped(tmp_path):
    gates = TaskGates(tmp_path / "data")
    task = gates.begin(spec(tmp_path))
    evidence(gates, task["task_id"])
    gates.update(task["task_id"], task["revision"], spec(tmp_path))
    gates.observe(
        task["task_id"],
        "fx_get_production_rules",
        {"offset": 999, "project_id": "p"},
        {"user_rules": [], "omitted_rules": 0, "next_offset": 999},
    )
    assert "rules" in gates.get(task["task_id"])["missing"]


def test_unread_source_and_truncated_module_do_not_count_as_full_read(tmp_path):
    gates, task = ready(tmp_path)
    with pytest.raises(ValueError, match="full"):
        gates.plan(
            task["task_id"], task["revision"], "方法", "条件", "差异", ["memory:" + "a" * 32]
        )
    updated = gates.update(task["task_id"], task["revision"], spec(tmp_path))
    gates.observe(
        task["task_id"], "fx_get_module_knowledge", {"module": "particles"}, {"truncated": True}
    )
    assert "module:particles" in gates.get(updated["task_id"])["missing"]


def test_confirmation_preview_and_unhealthy_network_never_verify(tmp_path):
    gates, task = ready(tmp_path)
    gates.observe(
        task["task_id"],
        "save_script",
        {"confirm": False},
        {"action": "save_script", "message": "Ask user"},
    )
    gates.observe(
        task["task_id"], "verify_network", {}, {"healthy": False, "error_nodes": ["/obj/bad"]}
    )
    value = gates.get(task["task_id"], True)
    assert value["saved_seq"] == -1 and value["verified_seq"] == -1
    gates.observe(
        task["task_id"], "verify_network", {}, {"healthy": True, "nodes": [{"errors": ["bad"]}]}
    )
    assert gates.get(task["task_id"], True)["verified_seq"] == -1


def test_large_module_uses_scoped_search_and_full_relevant_source(tmp_path):
    gates = TaskGates(tmp_path / "data")
    task = gates.begin(spec(tmp_path))
    evidence(gates, task["task_id"])
    gates.observe(
        task["task_id"],
        "fx_get_module_knowledge",
        {"software": "houdini", "module": "particles"},
        {"truncated": True},
    )
    assert "module:particles" in gates.get(task["task_id"])["missing"]
    gates.observe(
        task["task_id"],
        "fx_get_memory",
        {"memory_id": "a" * 32},
        {"memory_id": "a" * 32, "modules": ["particles"], "method": "完整方法"},
    )
    task = gates.plan(task["task_id"], 1, "方法", "条件", "差异", ["memory:" + "a" * 32],
                      source_adaptations=[adaptation("memory:" + "a" * 32)])
    assert task["state"] == "ready" and not task["missing"]


def test_large_module_with_no_matching_memory_has_a_valid_fallback(tmp_path):
    gates = TaskGates(tmp_path / "data")
    task = gates.begin(spec(tmp_path))
    evidence(gates, task["task_id"])
    gates.observe(
        task["task_id"],
        "fx_get_module_knowledge",
        {"software": "houdini", "module": "particles"},
        {"truncated": True},
    )
    assert "module:particles" in gates.get(task["task_id"])["missing"]
    gates.observe(
        task["task_id"],
        "fx_search_memory",
        {"module": "particles", "query": "贴身粒子"},
        {"items": [], "total": 0},
    )
    task = gates.plan(
        task["task_id"],
        1,
        "依据原指南做草稿",
        "当前输入",
        "适配资产",
        [],
        "已按相关模块检索，没有适用经验",
    )
    assert task["state"] == "ready"


def test_nuke_full_memory_reads_are_scoped_by_payload_not_missing_software_argument(tmp_path):
    gates = TaskGates(tmp_path / "data")
    definition = spec(tmp_path).model_copy(
        update={"software": "nuke", "modules": ["compositing"], "endpoint": "127.0.0.1:8187"}
    )
    task = gates.begin(definition)
    gates.observe(
        task["task_id"],
        "fx_get_production_rules",
        {"project_id": "p"},
        {"user_rules": [], "omitted_rules": 0, "next_offset": 0},
    )
    gates.observe(
        task["task_id"],
        "fx_get_module_knowledge",
        {"software": "nuke", "module": "compositing"},
        {},
    )
    gates.observe(
        task["task_id"], "fx_search_memory", {"software": "nuke", "query": "发光"}, {"items": []}
    )
    gates.observe(
        task["task_id"],
        "fx_get_memory",
        {"memory_id": "a" * 32},
        {"memory_id": "a" * 32, "software": "nuke", "modules": ["compositing"]},
    )
    task = gates.plan(task["task_id"], 1, "发光", "当前输入", "调整阈值", ["memory:" + "a" * 32],
                      source_adaptations=[adaptation("memory:" + "a" * 32)])
    assert task["state"] == "ready"


def test_empty_truncated_search_is_not_zero_matching_knowledge(tmp_path):
    gates = TaskGates(tmp_path / "data")
    task = gates.begin(spec(tmp_path))
    evidence(gates, task["task_id"])
    gates.observe(
        task["task_id"],
        "fx_get_module_knowledge",
        {"software": "houdini", "module": "particles"},
        {"truncated": True},
    )
    gates.observe(
        task["task_id"],
        "fx_search_memory",
        {"module": "particles", "query": "贴身粒子"},
        {"items": [], "truncated": True},
    )
    with pytest.raises(ValueError, match="module:particles"):
        gates.plan(task["task_id"], 1, "方法", "条件", "差异", [], "不能把被截断当没有结果")


def test_another_task_edit_invalidates_existing_save_and_checks(tmp_path):
    gates, task = ready(tmp_path)
    gates.observe(task["task_id"], "save_scene", {}, {"success": True})
    gates.observe(task["task_id"], "verify_network", {}, {"healthy": True})
    other = gates.begin(spec(tmp_path))
    gates.edit(other["task_id"])
    value = gates.get(task["task_id"], True)
    assert value["saved_seq"] == -1 and value["verified_seq"] == -1


def test_changed_inputs_rules_and_live_settings_require_recheck(tmp_path):
    from fx_agent.learning import LearningStore

    gates, task = ready(tmp_path)
    gates.authorize(task["task_id"], "houdini", task["endpoint"], task["scene_path"], {"fps": 24})
    with pytest.raises(ValueError, match="settings"):
        gates.authorize(
            task["task_id"], "houdini", task["endpoint"], task["scene_path"], {"fps": 25}
        )
    LearningStore(gates.root).record_rule(
        rule_key="new-rule",
        title="新要求",
        statement="新要求",
        user_quote="这是新要求",
        source_ref="本轮对话",
        scope="global",
        expected_revision=0,
    )
    with pytest.raises(ValueError, match="rules changed"):
        gates.authorize(task["task_id"], "houdini", task["endpoint"], task["scene_path"])


def test_endpoint_lock_prevents_parallel_writers(tmp_path):
    from fx_agent.task_gates import endpoint_lock

    with endpoint_lock(tmp_path, "127.0.0.1:18186"), pytest.raises(ValueError, match="busy"):  # noqa: SIM117 - tests a second lock acquisition
        with endpoint_lock(tmp_path, "127.0.0.1:18186"):  # noqa: SIM117 - inner acquisition is the tested operation
            pytest.fail("Second operation acquired a held endpoint")


@pytest.mark.asyncio
async def test_failed_verification_invalidates_earlier_success(tmp_path):
    mcp = FastMCP("verify-fail")

    @mcp.tool()
    def verify_network() -> dict:
        return {"healthy": False, "error_nodes": ["/obj/error"]}

    async def live(_context):
        return "127.0.0.1:18186", str(tmp_path / "scene.hip")

    install_task_gates(mcp, "houdini", lambda: tmp_path / "data", live)
    gates, task = ready(tmp_path)
    gates.observe(task["task_id"], "verify_network", {}, {"healthy": True})
    await mcp.call_tool("verify_network", {"fx_task_id": task["task_id"]})
    assert gates.get(task["task_id"], True)["verified_seq"] == -1


@pytest.mark.asyncio
async def test_wrong_live_scene_cannot_supply_verification_receipt(tmp_path):
    mcp = FastMCP("wrong-scene")
    calls = []

    @mcp.tool()
    def verify_network() -> dict:
        calls.append("verified")
        return {"healthy": True}

    async def live(_context):
        return "127.0.0.1:18186", "D:/different.hip"

    install_task_gates(mcp, "houdini", lambda: tmp_path / "data", live)
    gates, task = ready(tmp_path)
    with pytest.raises(ValueError, match="Verification target"):
        await mcp.call_tool("verify_network", {"fx_task_id": task["task_id"]})
    assert calls == [] and gates.get(task["task_id"], True)["verified_seq"] == -1
    await mcp.call_tool("verify_network", {})
    assert calls == ["verified"]


def test_real_conflict_shapes_create_questions(tmp_path):
    gates, task = ready(tmp_path)
    gates.observe(
        task["task_id"],
        "fx_get_memory",
        {"memory_id": "a" * 32},
        {"memory_id": "a" * 32, "conflict_memory_ids": ["b" * 32], "status": "disputed"},
    )
    assert gates.get(task["task_id"])["open_questions"] == 1
    with pytest.raises(ValueError, match="questions"):
        gates.authorize(task["task_id"], "houdini", task["endpoint"], task["scene_path"])


@pytest.mark.asyncio
async def test_full_mcp_journey_and_acceptance_revoke_after_edit(tmp_path):
    """Real dispatcher/evidence/database, with a synthetic DCC only."""
    mcp = FastMCP("complete-journey")
    scene = tmp_path / "scene.hip"
    edits = []
    frames = [101]

    @mcp.tool()
    def fx_get_production_rules(project_id: str) -> dict:
        return {"user_rules": [], "omitted_rules": 0, "next_offset": 0}

    @mcp.tool()
    def fx_get_module_knowledge(module: str, software: str = "houdini") -> dict:
        return {"module": module, "content": "existing baseline"}

    @mcp.tool()
    def fx_search_memory(query: str) -> dict:
        return {"items": []}

    @mcp.tool()
    def get_workflow_guide(topic: str) -> str:
        return "guide " + topic

    @mcp.tool()
    def create_node(name: str) -> dict:
        edits.append(name)
        return {"created": name}

    @mcp.tool()
    def save_scene() -> dict:
        scene.write_text(str(edits), encoding="utf-8")
        return {"success": True}

    @mcp.tool()
    def verify_network() -> dict:
        return {"healthy": True, "nodes": []}

    @mcp.tool()
    def get_frame() -> dict:
        return {"frame": frames[-1]}

    @mcp.tool()
    def capture_screenshot() -> dict:
        image = tmp_path / f"journey-{frames[-1]}.png"
        image.write_bytes(b"test preview")
        return {"output_path": str(image)}

    @mcp.tool()
    def fx_record_feedback(feedback: dict) -> dict:
        return feedback

    async def live(_context):
        return "127.0.0.1:18186", str(scene), {"fps": 24}

    install_task_gates(mcp, "houdini", lambda: tmp_path / "data", live)

    async def call(name, args):
        result = await mcp.call_tool(name, args)
        return json.loads(result[0].text)

    task = await call("fx_begin_task", {"spec": spec(tmp_path).model_dump()})
    scope = {"fx_task_id": task["task_id"]}
    for name, args in [
        ("fx_get_production_rules", {"project_id": "p"}),
        ("fx_get_module_knowledge", {"module": "particles"}),
        ("fx_search_memory", {"query": "贴身能量"}),
        ("get_workflow_guide", {"topic": "discipline"}),
        ("get_workflow_guide", {"topic": "dopparticles"}),
    ]:
        await mcp.call_tool(name, args | scope)
    task = await call(
        "fx_set_task_plan",
        {
            "task_id": task["task_id"],
            "expected_revision": 1,
            "method": "低分辨率粒子",
            "applies_when": "当前角色",
            "changes": "适配当前输入",
            "selected_sources": [],
            "no_match_reason": "实际检索无匹配",
        },
    )
    await mcp.call_tool("create_node", {"name": "draft"} | scope)
    await mcp.call_tool("verify_network", scope)
    await mcp.call_tool("save_scene", scope)
    await mcp.call_tool("capture_screenshot", scope)
    frames.append(125)
    await mcp.call_tool("capture_screenshot", scope)
    task = await call(
        "fx_finish_task",
        {
            "task_id": task["task_id"],
            "expected_revision": task["revision"],
            "case_id": "",
            "verification_note": "实际查看对应预览",
        },
    )
    assert task["state"] == "verified"
    gates = TaskGates(tmp_path / "data")
    finishes = gates.get(task["task_id"], True)["reviewed_previews"]
    assert {image["frame"] for image in finishes} == {101, 125}
    from fx_agent.memory import MemoryStore

    memory = MemoryStore(gates.root)
    lesson = dict(
        project_id="p", software="houdini", modules=["particles"], kind="workflow",
        title="可复用的粒子验证方法", method="先用低分辨率核对运动", reason="便于调参",
        applies_when="需要连续运动", source_type="observed", source_ref="current result",
    )
    candidate = memory.submit(**lesson)
    assert candidate["status"] == "candidate"
    verified = memory.submit(_task_id=task["task_id"], **lesson)
    assert verified["memory_id"] == candidate["memory_id"]
    assert verified["evidence_status"] == "live_verified_text"
    assert memory.get(verified["memory_id"], "p")["verified_task_id"] == task["task_id"]
    with pytest.raises(ValueError, match="same scope"):
        memory.submit(_task_id=task["task_id"], **(lesson | {"project_id": "other"}))
    feedback = {
        "outcome": "accepted",
        "project_id": "p",
        "shot_id": "s",
        "software": "houdini",
    }
    assert (await call("fx_record_feedback", {"feedback": feedback} | scope))[
        "outcome"
    ] == "accepted"
    await mcp.call_tool("create_node", {"name": "new-draft"} | scope)
    with pytest.raises(ValueError, match="verified"):
        await mcp.call_tool("fx_record_feedback", {"feedback": feedback} | scope)
