"""Regression tests for all 5 phases of the FX Agent optimization plan."""

import json
from pathlib import Path

import pytest
from mcp.server.fastmcp import FastMCP

from fx_agent.baselines import sync_baselines, validate_baselines
from fx_agent.data_api import fx_prepare_task_brief
from fx_agent.embeddings import set_embedder
from fx_agent.memory import MemoryStore
from fx_agent.store import Store
from fx_agent.task_gates import QUICK_TWEAK_MAX_EDITS, TaskGates, TaskSpec, install_task_gates
from fx_agent.tool_profiles import (
    HOUDINI_TOOL_GROUPS,
    PRODUCTION_HIDDEN_FX_TOOLS,
    PROTECTED_TOOLS,
    TEXT_ONLY_HIDDEN_FX_TOOLS,
    apply_tool_profile,
)

ROOT = Path(__file__).resolve().parents[1]


def _spec(tmp_path: Path, **overrides) -> TaskSpec:
    data = dict(
        project_id="proj",
        shot_id="sh01",
        software="houdini",
        kind="production",
        scene_path=str(tmp_path / "shot.hip"),
        endpoint="127.0.0.1:18186",
        goal="制作贴身能量粒子",
        acceptance=["轮廓清晰且多帧连贯"],
        budget="低分辨率草稿",
        modules=["particles"],
    )
    data.update(overrides)
    return TaskSpec(**data)


# --- Phase 1: fx_prepare_task_brief ---


@pytest.mark.asyncio
async def test_phase1_prepare_task_brief_satisfies_houdini_gate_in_one_call(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setenv("FX_AGENT_DATA_ROOT", str(data))
    mcp = FastMCP("brief-houdini")

    @mcp.tool()
    def get_workflow_guide(topic: str) -> str:
        return f"guide content for {topic}"

    mcp.tool()(fx_prepare_task_brief)

    async def live_target(_ctx):
        return "127.0.0.1:18186", str(tmp_path / "shot.hip"), {"fps": 24.0}

    install_task_gates(mcp, "houdini", lambda: data, live_target)
    gates = TaskGates(data)
    task = gates.begin(_spec(tmp_path))
    tid = task["task_id"]

    res = await mcp.call_tool("fx_prepare_task_brief", {"task_id": tid})
    brief = json.loads(res[0].text)
    assert brief["task_id"] == tid
    assert brief["task_state"]["missing"] == []
    assert "discipline" in brief["workflow_guides"]
    assert "dopparticles" in brief["workflow_guides"]

    plan = gates.plan(
        tid,
        task["revision"],
        method="POP 粒子发射",
        applies_when="当前镜头",
        changes="创建 POP 网络",
        selected_sources=[],
        no_match_reason="新镜头暂无历史案例",
    )
    assert plan["state"] == "ready"
    assert plan["missing"] == []


# --- Phase 2: quick_tweak task gate tier ---


@pytest.mark.asyncio
async def test_phase2_quick_tweak_allows_local_edits_and_enforces_hard_rails(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setenv("FX_AGENT_DATA_ROOT", str(data))
    scene = tmp_path / "shot.hip"
    scene.write_text("hip", encoding="utf-8")
    live_scene = [str(scene)]

    mcp = FastMCP("quick-tweak-test")
    mcp.tool()(fx_prepare_task_brief)
    edits = []

    @mcp.tool()
    def set_parameter(node: str, value: float) -> dict:
        edits.append((node, value))
        return {"ok": True}

    @mcp.tool()
    def setup_pyro_sim(source: str) -> dict:
        return {"ok": True}

    @mcp.tool()
    def save_scene(file_path: str = "") -> dict:
        return {"saved": file_path or str(scene)}

    @mcp.tool()
    def fx_finalize_learning(
        feedback_id: str, project_id: str, method: str, reason: str, applies_when: str
    ) -> dict:
        return {"ok": True}

    async def live_target(_ctx):
        return "127.0.0.1:18186", live_scene[0], {"fps": 24.0}

    install_task_gates(mcp, "houdini", lambda: data, live_target)
    gates = TaskGates(data)
    spec = _spec(tmp_path, kind="quick_tweak", scene_path=str(scene))
    task = gates.begin(spec)
    tid = task["task_id"]

    # Before reading rules, even quick_tweak must block scene writes.
    with pytest.raises(Exception, match="missing|rules"):
        await mcp.call_tool("set_parameter", {"node": "/obj/light1", "value": 2.0, "fx_task_id": tid})

    # One brief call unlocks quick_tweak directly without fx_set_task_plan.
    res = await mcp.call_tool("fx_prepare_task_brief", {"task_id": tid})
    assert json.loads(res[0].text)["task_state"]["missing"] == []

    # Heavy build tools remain strictly blocked in quick_tweak.
    with pytest.raises(Exception, match="quick_tweak"):
        await mcp.call_tool("setup_pyro_sim", {"source": "/obj/geo1", "fx_task_id": tid})

    # Wrong live scene path is still strictly rejected.
    live_scene[0] = str(tmp_path / "other.hip")
    with pytest.raises(Exception, match="Task target differs"):
        await mcp.call_tool("set_parameter", {"node": "/obj/light1", "value": 2.0, "fx_task_id": tid})
    live_scene[0] = str(scene)

    # Local parameter tweaks work up to QUICK_TWEAK_MAX_EDITS.
    for idx in range(QUICK_TWEAK_MAX_EDITS):
        await mcp.call_tool(
            "set_parameter", {"node": "/obj/light1", "value": float(idx), "fx_task_id": tid}
        )
    assert len(edits) == QUICK_TWEAK_MAX_EDITS

    # Exceeding QUICK_TWEAK_MAX_EDITS forces upgrade to production.
    with pytest.raises(Exception, match="edit limit"):
        await mcp.call_tool("set_parameter", {"node": "/obj/light1", "value": 99.0, "fx_task_id": tid})

    # Saving and finishing quick_tweak succeeds without archive, but cannot finalize learning.
    await mcp.call_tool("save_scene", {"file_path": str(scene), "fx_task_id": tid})
    finished = gates.finish(tid, task["revision"], "", "已调亮主光并核对渲染预览")
    assert finished["state"] == "verified"

    with pytest.raises(Exception, match="production"):
        await mcp.call_tool(
            "fx_finalize_learning",
            {
                "feedback_id": "fb1",
                "project_id": "proj",
                "method": "m",
                "reason": "r",
                "applies_when": "a",
                "fx_task_id": tid,
            },
        )

    # In-place upgrade from quick_tweak -> production preserves existing rules receipt.
    upgraded = gates.update(
        tid, finished["revision"], spec.model_copy(update={"kind": "production"})
    )
    assert upgraded["kind"] == "production"
    assert "rules" not in upgraded["missing"]


# --- Phase 3: fx-visual-critique skill & baselines ---


def test_phase3_visual_critique_skill_and_baselines_synced(tmp_path):
    critique_dir = ROOT / "skills/fx-visual-critique"
    assert (critique_dir / "SKILL.md").is_file()
    for ref in (
        "nuke_comp_critique.md",
        "houdini_fx_critique.md",
        "maya_lighting_critique.md",
    ):
        assert (critique_dir / "references" / ref).is_file()

    # Workflow skills reference visual critique, brief aggregation, and quick_tweak.
    for wf in ("fx-workflow", "nuke-workflow", "maya-workflow"):
        text = (ROOT / "skills" / wf / "SKILL.md").read_text(encoding="utf-8")
        assert "fx-visual-critique" in text
        assert "fx_prepare_task_brief" in text
        assert "quick_tweak" in text

    data = tmp_path / "data"
    result = sync_baselines(data)
    project_snap = next(s for s in result["snapshots"] if s["source"] == "fx-agent")
    folder = data / project_snap["relative_path"]
    assert (folder / "skills/fx-visual-critique/SKILL.md").is_file()
    assert (folder / "skills/fx-visual-critique/references/nuke_comp_critique.md").is_file()
    assert validate_baselines(data)["valid"] is True


# --- Phase 4: MCP tool profile filtering ---


def test_phase4_tool_profile_filtering():
    mcp = FastMCP("profile-test")
    sample_tools = (
        list(PROTECTED_TOOLS)
        + list(PRODUCTION_HIDDEN_FX_TOOLS)
        + list(HOUDINI_TOOL_GROUPS["chop"])
        + list(HOUDINI_TOOL_GROUPS["hda"])[:3]
        + ["set_parameter", "fx_search_memory"]
    )
    for name in dict.fromkeys(sample_tools):
        mcp.tool(name=name)(lambda: {"ok": True})

    # Full profile preserves everything.
    full = apply_tool_profile(mcp, software="houdini", profile="full")
    assert full["removed"] == []

    # Default text_only hides low-frequency maintenance tools while retaining fx_dream_memory.
    apply_tool_profile(mcp, software="houdini", profile="text_only")
    rem_text = set(mcp._tool_manager._tools)
    assert rem_text >= PROTECTED_TOOLS
    assert "fx_dream_memory" in rem_text
    assert not (TEXT_ONLY_HIDDEN_FX_TOOLS & rem_text)

    # Production profile + hidden Houdini groups prunes maintenance & selected sub-domains.
    prod = apply_tool_profile(
        mcp, software="houdini", profile="production", hide_groups="chop,hda"
    )
    remaining = set(mcp._tool_manager._tools)
    assert remaining >= PROTECTED_TOOLS
    assert "set_parameter" in remaining and "fx_search_memory" in remaining
    assert not (PRODUCTION_HIDDEN_FX_TOOLS & remaining)
    assert not (HOUDINI_TOOL_GROUPS["chop"] & remaining)
    assert len(prod["removed"]) >= 8

    # Switching back to full restores stashed tools cleanly.
    restored = apply_tool_profile(mcp, software="houdini", profile="full")
    assert restored["removed"] == []
    assert set(mcp._tool_manager._tools) >= PRODUCTION_HIDDEN_FX_TOOLS

    with pytest.raises(ValueError, match="Unsupported FX_AGENT_TOOL_PROFILE"):
        apply_tool_profile(mcp, software="houdini", profile="unknown_profile")
    with pytest.raises(ValueError, match="Unknown FX_AGENT_HOUDINI_HIDE_GROUPS"):
        apply_tool_profile(mcp, software="houdini", profile="full", hide_groups="bogus")


# --- Phase 5: Hybrid semantic retrieval ---


def test_phase5_hybrid_embeddings_and_graceful_fallback(tmp_path):
    data = tmp_path / "data"
    store = Store(data)
    with store.connection() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == Store.SCHEMA_VERSION
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "embeddings" in tables

    mem = MemoryStore(data)
    mem.submit(
        project_id="p",
        shot_id="s",
        software="nuke",
        modules=["compositing"],
        kind="compositing",
        title="飘带状发光能量线合成",
        method="用多层指数 Glow 叠加并按亮度遮罩压制核心过曝",
        reason="保留丝带柔顺层次而不丢失边缘衰减",
        applies_when="能量飘带合成",
        source_type="user",
        source_ref="user",
        project_dependent=False,
    )
    mem.submit(
        project_id="p",
        shot_id="s",
        software="nuke",
        modules=["compositing"],
        kind="compositing",
        title="暗部黑场匹配经验",
        method="用 Grade lift 对齐背景暗部 RGB",
        reason="防止前景 CG 暗部发死",
        applies_when="实拍夜景合成",
        source_type="user",
        source_ref="user",
        project_dependent=False,
    )

    # Pure lexical query with zero shared terms returns empty.
    assert mem.search("p", "ethereal silk trail", software="nuke")["items"] == []
    assert mem.search("p", "shadow floor alignment", software="nuke")["items"] == []

    # Install an in-process embedder that maps semantically related concepts to matching vectors.
    embed_calls = []

    def fake_embed(texts: list[str]) -> list[list[float]]:
        embed_calls.append(list(texts))
        out = []
        for t in texts:
            low = t.lower()
            if "silk" in low or "飘带" in low:
                out.append([1.0, 0.0, 0.0])
            elif "shadow" in low or "黑场" in low:
                out.append([0.0, 1.0, 0.0])
            else:
                out.append([0.0, 0.0, 1.0])
        return out

    try:
        set_embedder(fake_embed, model="test-embed-v1")
        silk_hits = mem.search("p", "ethereal silk trail", software="nuke")
        assert len(silk_hits["items"]) == 1
        assert "飘带" in silk_hits["items"][0]["title"]

        mem_hits = mem.search("p", "shadow floor alignment", software="nuke")
        assert len(mem_hits["items"]) == 1
        assert "黑场" in mem_hits["items"][0]["title"]

        # Second search hits SQLite vector cache and query LRU cache without re-embedding!
        calls_before = len(embed_calls)
        assert len(mem.search("p", "ethereal silk trail", software="nuke")["items"]) == 1
        assert len(embed_calls) == calls_before

        # Broken embedder degrades silently to lexical search without raising.
        def broken(_texts):
            raise RuntimeError("embedding server down")

        set_embedder(broken, model="broken-v1")
        lex_silk = mem.search("p", "飘带", software="nuke")
        assert len(lex_silk["items"]) == 1
        lex_mem = mem.search("p", "黑场", software="nuke")
        assert len(lex_mem["items"]) == 1
    finally:
        set_embedder(None)
