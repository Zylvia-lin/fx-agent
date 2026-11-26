import pytest

from fx_agent.data_api import fx_prepare_task_brief
from fx_agent.memory import MemoryStore
from fx_agent.store import compact
from fx_agent.task_gates import TaskGates, TaskSpec


@pytest.mark.asyncio
@pytest.mark.parametrize("max_chars", [2048, 14000])
async def test_quick_tweak_recalls_particle_matte_experience(tmp_path, monkeypatch, max_chars):
    monkeypatch.setenv("FX_AGENT_DATA_ROOT", str(tmp_path))
    lesson = MemoryStore(tmp_path).submit(
        software="houdini",
        modules=["rendering"],
        kind="troubleshooting",
        title="Mantra Forced Matte 与角色层接缝优先排查 Geo Time Samples",
        method="只将 Mantra 的 Geo Time Samples 改为 1，检查角色轮廓的粒子暗缝。",
        reason="角色 Alpha 与 Forced Matte 的变形覆盖不一致时会留下空边。",
        applies_when="Mantra 粒子分层使用动画角色做 Forced Matte。",
        source_type="user",
        source_ref="test: user verified result",
    )
    gates = TaskGates(tmp_path)
    task = gates.begin(TaskSpec(
        project_id="test", shot_id="test", software="houdini", kind="quick_tweak",
        scene_path=str(tmp_path / "untouched.hip"), endpoint="127.0.0.1:18186",
        goal="houdini渲染的粒子为什么跟遮罩附近会对不上，有间隙",
        acceptance=["解释暗缝"], budget="只读", modules=["particles", "rendering"],
    ))

    brief = await fx_prepare_task_brief(task["task_id"], max_chars=max_chars)
    assert len(compact(brief)) <= max_chars
    details = gates.get(task["task_id"], detail=True)
    search_receipts = [r for r in details["receipts"].values() if r["tool"] == "fx_search_memory"]
    if "memory_matches" not in brief and max_chars == 2048:
        assert "fx_search_memory" in brief["deferred_reads"]
        assert not search_receipts
    else:
        assert lesson["memory_id"] in {item["memory_id"] for item in brief["memory_matches"]["items"]}
        assert search_receipts
    assert brief["task_state"]["missing"] == []
    assert details["edit_seq"] == 0
