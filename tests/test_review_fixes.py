"""Regression tests covering code review fixes across all subsystems."""

import importlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp import FastMCP

from fx_agent.baselines import sync_baselines
from fx_agent.data_import import TABLES, import_data
from fx_agent.effect_cards import EffectCardInput, EffectLibrary
from fx_agent.install_config import mcp_servers_json
from fx_agent.production_rules import production_rules
from fx_agent.store import Store
from fx_agent.task_gates import TaskGates, TaskSpec, install_task_gates, is_untitled_scene
from fx_agent.template_extract import _hip_parameters, extract_nuke
from fx_agent.templates import TemplateLibrary

ROOT = Path(__file__).resolve().parents[1]


def _task_spec(tmp_path: Path, **overrides) -> TaskSpec:
    data = dict(
        project_id="proj",
        shot_id="sh01",
        software="houdini",
        kind="production",
        scene_path=str(tmp_path / "shot.hip"),
        endpoint="127.0.0.1:18186",
        goal="制作能量粒子",
        acceptance=["连续自然"],
        budget="低分辨率草稿",
        modules=["particles"],
    )
    data.update(overrides)
    return TaskSpec(**data)


def _observe_base(gates: TaskGates, task_id: str, *, module_truncated: bool = False):
    gates.observe(task_id, "get_workflow_guide", {"topic": "discipline"}, "ok")
    gates.observe(task_id, "get_workflow_guide", {"topic": "dopparticles"}, "ok")
    gates.observe(
        task_id,
        "fx_get_production_rules",
        {"project_id": "proj", "offset": 0},
        {"user_rules": [], "omitted_rules": 0, "next_offset": 0, "software": "houdini"},
    )
    gates.observe(
        task_id,
        "fx_get_module_knowledge",
        {"software": "houdini", "module": "particles"},
        {"text": "trimmed", "truncated": module_truncated},
    )
    gates.observe(
        task_id,
        "fx_search_effects",
        {"software": "houdini", "query": "能量粒子"},
        {"items": [], "total": 0},
    )


def test_task_gate_truncated_module_clears_with_non_empty_search_and_no_match_reason(tmp_path):
    gates = TaskGates(tmp_path / "data")
    task = gates.begin(_task_spec(tmp_path))
    tid = task["task_id"]
    _observe_base(gates, tid, module_truncated=True)
    gates.observe(
        tid,
        "fx_search_memory",
        {"software": "houdini", "module": "particles", "query": "能量粒子"},
        {"items": [{"memory_id": "m1", "title": "旧方案"}], "truncated": False},
    )
    result = gates.plan(
        tid,
        task["revision"],
        method="新建基础粒子发射",
        applies_when="当前镜头尺度",
        changes="新增 POP 网络",
        selected_sources=[],
        no_match_reason="现有候选均为爆炸粒子，不适用于贴身能量流",
    )
    assert result["state"] == "ready"
    assert result["missing"] == []


@pytest.mark.asyncio
async def test_task_gate_allows_save_as_from_untitled_scene(tmp_path):
    assert is_untitled_scene("untitled.hip")
    assert is_untitled_scene("")
    data = tmp_path / "data"
    gates = TaskGates(data)
    target = tmp_path / "shot.hip"
    task = gates.begin(_task_spec(tmp_path, scene_path=str(target)))
    tid = task["task_id"]
    _observe_base(gates, tid)
    gates.observe(
        tid,
        "fx_search_memory",
        {"software": "houdini", "module": "particles", "query": "能量粒子"},
        {"items": []},
    )
    plan = gates.plan(
        tid,
        task["revision"],
        method="基础搭建",
        applies_when="新建工程",
        changes="保存到目标路径",
        selected_sources=[],
        no_match_reason="新镜头无历史案例",
    )
    assert plan["state"] == "ready"
    mcp = FastMCP("save-as-test")
    saved = []

    @mcp.tool()
    def save_scene(file_path: str = "") -> dict:
        saved.append(file_path)
        return {"saved": file_path}

    async def live_target(_ctx):
        return "127.0.0.1:18186", "untitled.hip", {"fps": 24.0}

    install_task_gates(mcp, "houdini", lambda: data, live_target)
    # Saving from untitled.hip into the task's registered target path must be allowed.
    await mcp.call_tool("save_scene", {"file_path": str(target), "fx_task_id": tid})
    assert saved == [str(target)]


def test_task_gate_module_conflict_does_not_auto_block_task(tmp_path):
    gates = TaskGates(tmp_path / "data")
    task = gates.begin(_task_spec(tmp_path))
    tid = task["task_id"]
    gates.observe(
        tid,
        "fx_get_module_knowledge",
        {"software": "houdini", "module": "particles"},
        {"text": "notes", "has_conflict": True, "conflict_memory_ids": ["mem-a", "mem-b"]},
    )
    state = gates.get(tid, detail=True)
    assert state["questions"] == []


def test_task_gate_deduplicates_rules_and_normalizes_module_alias(tmp_path):
    gates = TaskGates(tmp_path / "data")
    task = gates.begin(_task_spec(tmp_path))
    tid = task["task_id"]
    gates.observe(tid, "get_workflow_guide", {"topic": "discipline"}, "ok")
    gates.observe(tid, "get_workflow_guide", {"topic": "dopparticles"}, "ok")
    # First read full rules page at offset 0, then a narrower truncated read at offset 0.
    gates.observe(
        tid,
        "fx_get_production_rules",
        {"project_id": "proj", "offset": 0, "max_chars": 12000},
        {"user_rules": [], "omitted_rules": 0, "next_offset": 0, "software": "houdini"},
    )
    gates.observe(
        tid,
        "fx_get_production_rules",
        {"project_id": "proj", "offset": 0, "max_chars": 200},
        {"user_rules": [], "omitted_rules": 2, "next_offset": 1, "software": "houdini"},
    )
    gates.observe(
        tid,
        "fx_get_module_knowledge",
        {"software": "houdini", "module": "particles"},
        {"text": "trimmed", "truncated": True},
    )
    # Alias 'pop' should normalize to canonical 'particles'.
    gates.observe(
        tid,
        "fx_search_memory",
        {"software": "houdini", "module": "pop", "query": "能量粒子"},
        {"items": [], "truncated": False},
    )
    gates.observe(
        tid,
        "fx_search_effects",
        {"software": "houdini", "query": "能量粒子"},
        {"items": [], "total": 0},
    )
    plan = gates.plan(
        tid,
        task["revision"],
        method="POP 粒子",
        applies_when="通用",
        changes="新建 POP",
        selected_sources=[],
        no_match_reason="无匹配案例",
    )
    assert plan["state"] == "ready"


def test_task_gate_incremental_update_preserves_unaffected_evidence(tmp_path):
    gates = TaskGates(tmp_path / "data")
    spec = _task_spec(tmp_path)
    task = gates.begin(spec)
    tid = task["task_id"]
    _observe_base(gates, tid)
    gates.observe(
        tid,
        "fx_search_memory",
        {"software": "houdini", "module": "particles", "query": "能量粒子"},
        {"items": []},
    )
    updated = gates.update(tid, task["revision"], spec.model_copy(update={"goal": "提高粒子速度"}))
    # Rules, guides, and module knowledge should be preserved when only goal changes.
    plan = gates.plan(
        tid,
        updated["revision"],
        method="提高初速度",
        applies_when="同场景",
        changes="调整 velocity",
        selected_sources=[],
        no_match_reason="同任务微调",
    )
    assert plan["state"] == "ready"


def test_production_rules_software_nuke_omits_hip():
    nuke_rules = production_rules("nuke")
    assert nuke_rules["software"] == "nuke"
    assert "$HIP" not in json.dumps(nuke_rules, ensure_ascii=False)
    houdini_rules = production_rules("houdini")
    assert houdini_rules["software"] == "houdini"


@pytest.mark.asyncio
async def test_render_view_start_requires_task_gate(tmp_path):
    mcp = FastMCP("test-houdini")

    @mcp.tool()
    def fx_render_view_start(rop_path: str = "/out/mantra1") -> dict:
        return {"started": rop_path}

    async def live_target(_ctx):
        return "127.0.0.1:18186", str(tmp_path / "shot.hip"), {"fps": 24.0}

    install_task_gates(mcp, "houdini", lambda: tmp_path / "data", live_target)
    with pytest.raises(Exception, match="fx_task_id"):
        await mcp.call_tool("fx_render_view_start", {"rop_path": "/out/mantra1"})


def test_template_extract_scientific_notation_and_nuke_parser():
    parms = _hip_parameters("  rate ( 1e+05 )\n  scale [ 0 ] ( -2.5E-03 )\n")
    assert parms["rate"] == "1e+05"
    assert parms["scale"] == "-2.5E-03"
    nk = """
Root {
 first_frame 1001
 last_frame 1050
}
Read {
 name ReadA
 file "plate.%04d.exr"
}
Read {
 name ReadB
 file "fx.%04d.exr"
}
Merge2 {
 name Merge1
 operation plus
}
set Cclone_node [stack 0]
clone $Cclone_node {
 name ClonedMerge
 xpos 200
}
"""
    snap = extract_nuke(nk.encode("utf-8"))
    names = {node["name"] for node in snap["nodes"]}
    assert {"ReadA", "ReadB", "Merge1", "ClonedMerge"} <= names
    merge_edges = [c for c in snap["connections"] if c["dst_path"] == "/Merge1"]
    assert len(merge_edges) == 2


def test_effect_cards_long_author_label_and_prefix_exclusion(tmp_path):
    long_note = "  " + ("详细制作说明" * 30) + "  "
    nk_path = tmp_path / "comp.nk"
    nk_path.write_text(
        'Root {\n first_frame 1\n last_frame 10\n}\n'
        'Read {\n name Read1\n file "a.exr"\n}\n'
        f'Grade {{\n name Grade1\n label "{long_note.strip()}"\n}}\n'
        'Group {\n name TechRig\n}\n Camera2 {\n  name CamMain\n }\nend_group\n',
        encoding="utf-8",
    )
    data = tmp_path / "data"
    lib = TemplateLibrary(data)
    learned = lib.ingest(str(nk_path), project_id="p", shot_id="s")
    study_id = learned["items"][0]["study_id"]
    cards = EffectLibrary(data)
    card_payload = {
        "scheme_key": "comp_grade",
        "scheme_name": "基础调色",
        "effect_key": "grade_pass",
        "title": "主画面调色",
        "effect_family": ["compositing"],
        "effect_kind": "color_grade",
        "behaviors": ["color_shift"],
        "naming_basis": "user",
        "subject": "主画面",
        "appearance": "暖色调",
        "motion": "静态",
        "variant_notes": "标准暖色",
        "scope": "frames 1-10",
        "method": "Read 接 Grade",
        "reuse_steps": ["连接 Read1 到 Grade1"],
        "limitations": "仅示例工程",
        "evidence_note": "基于用户说明与静态 NK 解析",
        "modules": ["compositing"],
        "node_paths": ["/Read1", "/Grade1"],
        "author_labels": [long_note],
        "basis": "user",
        "user_quote": "这是主画面暖色调色",
    }
    validated = EffectCardInput.model_validate(card_payload)
    assert len(validated.author_labels[0]) > 80
    assert not validated.author_labels[0].startswith(" ")

    annotated = cards.annotate(study_id, [card_payload])
    card_id = annotated["items"][0]["card_id"]
    # Review with prefix wildcard exclusion for /TechRig/*
    snapshot_nodes = sorted(
        {n["path"] for n in lib.get(study_id, section="nodes", max_chars=20000)["content"]}
    )
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    review = cards.review(
        study_id,
        reviewed_node_paths=snapshot_nodes,
        expected_card_ids=[card_id],
        exclusions=[
            {"node_path": "/TechRig/*", "reason": "技术相机组"},
            {"node_path": "/TechRig", "reason": "技术相机组"},
            {"node_path": "/Root_0", "reason": "工程根节点"},
        ],
        media_source=str(media_dir),
    )
    assert review["state"] == "reviewed"
    # Merge preview must validate non-existent node_paths before apply.
    with pytest.raises(ValueError):
        cards.merge(
            [card_id, card_id],
            {**card_payload, "effect_key": "merged", "node_paths": ["/DoesNotExist"]},
            reason="invalid preview",
            apply=False,
        )


def test_data_import_tables_and_baselines_stale_lock_recovery(tmp_path):
    assert "dream_runs" in TABLES
    assert "memory_aliases" in TABLES
    source = tmp_path / "src_data"
    target = tmp_path / "dst_data"
    Store(source)
    # Place a stale .baseline.lock file before syncing; OS flock should succeed without error.
    (source / ".baseline.lock").write_text("stale", encoding="utf-8")
    sync_baselines(source)
    assert not (source / ".baseline.lock").exists()
    result = import_data(source, target, apply=True)
    assert result["baseline_snapshots"]["added"] == 2


def test_houdini_startup_csrf_guard_and_fail_closed_bind(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "houdini_startup_test", ROOT / "integrations/houdini/startup/startup.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    class FakeApp:
        def handle_api(self, _request):
            return "ok"

    app = FakeApp()
    mod._install_csrf_guard(app)
    # Cross-origin browser request must be rejected with PermissionError.
    with pytest.raises(PermissionError):
        app.handle_api(SimpleNamespace(headers={"Origin": "https://evil.example"}))
    # Non-browser localhost request without Origin/Sec-Fetch-Site passes.
    monkeypatch.delenv("FX_AGENT_HOUDINI_TOKEN", raising=False)
    assert app.handle_api(SimpleNamespace(headers={})) == "ok"
    # Fail-closed bind when hwebserver.setSettingsForPort fails.
    with pytest.raises(RuntimeError):
        mod._bind_localhost_only(SimpleNamespace(setSettingsForPort=None))


def test_nuke_bridge_preserves_selection_on_create_node(monkeypatch):
    nuke_dir = str(ROOT / "integrations/nuke")
    if nuke_dir not in sys.path:
        sys.path.insert(0, nuke_dir)
    import fx_agent_nuke_graph as graph_mod

    class FakeNode:
        def __init__(self, name, sel=False):
            self._name = name
            self._sel = sel

        def fullName(self):
            return self._name

        def name(self):
            return self._name

        def Class(self):
            return "Grade"

        def isSelected(self):
            return self._sel

        def setSelected(self, v):
            self._sel = bool(v)

        def xpos(self):
            return 0

        def ypos(self):
            return 0

        def screenWidth(self):
            return 80

        def screenHeight(self):
            return 20

        def inputs(self):
            return 0

        def knob(self, _k):
            return None

        def knobs(self):
            return {}

    existing = FakeNode("Existing", sel=True)
    created = FakeNode("Grade1", sel=True)
    selection_during_create = []

    class FakeGroup:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def fullName(self):
            return "root"

        def nodes(self):
            return [existing]

    class FakeNuke:
        def thisGroup(self):
            return FakeGroup()

        def root(self):
            return FakeGroup()

        def selectedNodes(self):
            return [n for n in [existing, created] if n.isSelected()]

        def createNode(self, _cls, inpanel=False):
            selection_during_create.append(existing.isSelected())
            return created

        def allNodes(self):
            return [existing, created]

    monkeypatch.setitem(sys.modules, "nuke", FakeNuke())
    result = graph_mod.create_node({"node_class": "Grade"})
    assert result["status"] == "ok"
    assert result["result"]["name"] == "Grade1"
    assert selection_during_create == [False]
    assert existing.isSelected() is True


def test_maya_bridge_windows_save_path_and_encode_fallback(tmp_path, monkeypatch):
    maya_shared = str(ROOT / "integrations/maya/shared")
    if maya_shared not in sys.path:
        sys.path.insert(0, maya_shared)

    class FakeCmds:
        def __init__(self):
            self.saved = False

        def file(self, **kwargs):
            if kwargs.get("query") and kwargs.get("sceneName"):
                return "C:/Projects/Shot/scene.ma"
            if kwargs.get("save"):
                self.saved = True
                return "C:/Projects/Shot/scene.ma"
            return None

    class FakeUtils:
        @staticmethod
        def executeInMainThreadWithResult(fn):
            return fn()

    fake = FakeCmds()
    monkeypatch.setitem(sys.modules, "maya", SimpleNamespace(cmds=fake, utils=FakeUtils))
    monkeypatch.setitem(sys.modules, "maya.cmds", fake)
    monkeypatch.setitem(sys.modules, "maya.utils", FakeUtils)
    sys.modules.pop("fx_maya_bridge.lighting", None)
    import fx_maya_bridge as bridge_mod

    maya_lighting = importlib.import_module("fx_maya_bridge.lighting")
    res = maya_lighting.dispatch("save_scene", {"path": r"C:\Projects\Shot\scene.ma"})
    assert fake.saved is True
    assert res["path"] == "C:/Projects/Shot/scene.ma"

    bridge = bridge_mod.Bridge(0, str(tmp_path))
    bridge.running = True
    monkeypatch.setattr(bridge, "run_request", lambda _req: {"id": "1", "bad": float("nan")})

    sent = []

    class FakePeer:
        def settimeout(self, _t):
            pass

        def sendall(self, data):
            sent.append(data)

        def close(self):
            pass

    monkeypatch.setattr(bridge_mod, "receive", lambda _p: {"id": "1", "command": "x"})
    bridge.handle(FakePeer())
    assert sent
    payload = json.loads(sent[0][4:].decode("utf-8"))
    assert payload["status"] == "error"
    assert payload["id"] == "1"


def test_install_config_mcp_servers_json_and_nuke_menu_data_root(tmp_path):
    cfg = mcp_servers_json(
        ROOT, tmp_path / "data", houdini_port=18186, nuke_port=8187, maya_ports=[18190]
    )
    assert set(cfg["mcpServers"]) == {"fx-agent", "fx-nuke", "fx-maya"}
    assert cfg["mcpServers"]["fx-nuke"]["env"]["FX_AGENT_DATA_ROOT"] == str(tmp_path / "data")

    spec = importlib.util.spec_from_file_location("install_nuke_reg", ROOT / "scripts/install_nuke.py")
    inst = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(inst)
    menu = inst.menu_content("", ROOT, 8187, data_root=tmp_path / "custom_data")
    assert "FX_AGENT_DATA_ROOT" in menu
    assert "custom_data" in menu
