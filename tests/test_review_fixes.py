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
from fx_agent.install_config import mcp_servers_json
from fx_agent.production_rules import production_rules
from fx_agent.store import Store
from fx_agent.task_gates import TaskGates, TaskSpec, install_task_gates, is_untitled_scene

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


def test_baselines_stale_lock_recovery(tmp_path):
    source = tmp_path / "src_data"
    Store(source)
    # Place a stale .baseline.lock file before syncing; OS flock should succeed without error.
    (source / ".baseline.lock").write_text("stale", encoding="utf-8")
    result = sync_baselines(source)
    assert not (source / ".baseline.lock").exists()
    assert len(result["snapshots"]) == 2


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


@pytest.mark.asyncio
async def test_prepare_task_brief_auto_paginates_all_production_rules(tmp_path, monkeypatch):
    from fx_agent.data_api import fx_prepare_task_brief
    from fx_agent.learning import LearningStore
    from fx_agent.task_gates import TaskGates, TaskSpec

    monkeypatch.setenv("FX_AGENT_DATA_ROOT", str(tmp_path))
    store = LearningStore(tmp_path)
    for i in range(25):
        store.record_rule(
            rule_key=f"rule-{i:02d}",
            title=f"生产规范 {i:02d}",
            statement=f"这是第 {i:02d} 条跨项目通用制作规范，要求明确核验节点参数与输出路径。" * 4,
            user_quote=f"用户原话要求第 {i:02d} 条长期规范必须严格执行并保持一致。" * 3,
            source_ref=f"chat:{i}",
            scope="global",
            expected_revision=0,
        )
    gates = TaskGates(tmp_path)
    task = gates.begin(
        TaskSpec(
            project_id="proj",
            shot_id="sh010",
            software="houdini",
            kind="quick_tweak",
            scene_path="D:/work/sh010_v01.hip",
            endpoint="127.0.0.1:18186",
            goal="微调粒子速度",
            acceptance=["速度更柔和"],
            budget="快速微调",
            modules=["particles"],
        )
    )
    from fx_agent.store import compact

    tid = task["task_id"]
    brief = await fx_prepare_task_brief(tid, max_chars=2048)
    assert len(compact(brief)) <= 2048
    assert "rules" in brief["task_state"]["missing"]
    assert "fx_get_production_rules" in brief["deferred_reads"]
    brief = await fx_prepare_task_brief(tid, max_chars=16000)
    assert len(compact(brief)) <= 16000
    assert brief["production_rules"]["omitted_rules"] == 0
    assert len(brief["production_rules"]["user_rules"]) == 25
    assert "rules" not in brief["task_state"]["missing"]


@pytest.mark.asyncio
async def test_houdini_csrf_guard_protects_foreign_reason_endpoints_and_client_token(monkeypatch):
    from fx_agent.production import ProductionBridge

    spec = importlib.util.spec_from_file_location(
        "houdini_startup_guard_test", ROOT / "integrations/houdini/startup/startup.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    registered = {}

    class FakeUpstreamApp:
        @staticmethod
        def _foreign_request_reason(_request):
            return None

        @staticmethod
        def _forbidden(reason):
            return {"forbidden": reason}

        @staticmethod
        def _api_function(namespace):
            def dec(fn):
                registered[(namespace, fn.__name__)] = fn
                return fn

            return dec

        @staticmethod
        def session_info(_request):
            return {"hip_file": "D:/secret.hip"}

        @staticmethod
        def list_commands(_request):
            return {"commands": ["a"]}

    app = FakeUpstreamApp()
    monkeypatch.setenv("FX_AGENT_HOUDINI_TOKEN", "secret-token-123")
    mod._install_csrf_guard(app)

    # _foreign_request_reason rejects missing token and Sec-Fetch-Site: cross-site.
    assert app._foreign_request_reason(SimpleNamespace(headers={})) is not None
    assert (
        app._foreign_request_reason(
            SimpleNamespace(headers={"X-FX-Houdini-Token": "secret-token-123", "Sec-Fetch-Site": "cross-site"})
        )
        is not None
    )
    assert (
        app._foreign_request_reason(
            SimpleNamespace(headers={"X-FX-Houdini-Token": "secret-token-123"})
        )
        is None
    )
    # session_info and list_commands are guarded and re-registered.
    assert "forbidden" in registered[("mcp", "session_info")](SimpleNamespace(headers={}))
    assert registered[("mcp", "list_commands")](
        SimpleNamespace(headers={"X-FX-Houdini-Token": "secret-token-123"})
    ) == {"commands": ["a"]}

    # ProductionBridge attaches X-FX-Houdini-Token when configured.
    bridge = ProductionBridge(host="127.0.0.1", port=18186)
    client = await bridge._get_client()
    assert client.headers.get("X-FX-Houdini-Token") == "secret-token-123"
    await client.aclose()

    # stop() triggers hwebserver.requestShutdown() when available.
    shutdown_called = []
    monkeypatch.setitem(
        sys.modules,
        "hwebserver",
        SimpleNamespace(requestShutdown=lambda: shutdown_called.append(True)),
    )
    mod._server_started = True
    mod.stop()
    assert shutdown_called == [True]


def test_nuke_disconnect_closes_upstream_sock_attribute(monkeypatch):
    from fx_agent.nuke_server import SerializedConnection

    events = []

    class FakeSock:
        def shutdown(self, how):
            events.append(("shutdown", how))

        def close(self):
            events.append(("close",))

    class FakeConn:
        def __init__(self, host, port):
            self.sock = FakeSock()

        def disconnect(self):
            events.append(("disconnect",))

    monkeypatch.setitem(
        sys.modules, "nukemcp.connection", SimpleNamespace(NukeConnection=FakeConn)
    )
    conn = SerializedConnection(8187)
    conn.disconnect()
    assert events == [("shutdown", 2), ("close",), ("disconnect",)]


def test_nuke_bounded_client_socket_enforces_stop_and_frame_limits():
    nuke_dir = str(ROOT / "integrations/nuke")
    if nuke_dir not in sys.path:
        sys.path.insert(0, nuke_dir)
    import fx_agent_nuke as nuke_mod

    timeouts = []

    class FakeClient:
        def __init__(self, chunks):
            self._chunks = list(chunks)

        def settimeout(self, t):
            timeouts.append(t)

        def recv(self, _bufsize):
            item = self._chunks.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

    server = SimpleNamespace(_running=True)
    # Upstream settimeout(None) is clamped to 1.0s; timeout while running retries, stop exits.
    client = FakeClient([TimeoutError(), b'{"ok":1}\n'])
    proxy = nuke_mod._BoundedClientSocket(client, server)
    proxy.settimeout(None)
    assert timeouts == [1.0]
    assert proxy.recv(1024) == b'{"ok":1}\n'

    server._running = False
    assert proxy.recv(1024) == b""

    # Oversized unterminated frame drops the connection to prevent OOM.
    server._running = True
    big = FakeClient([b"x" * (nuke_mod._MAX_FRAME_BYTES + 1)])
    assert nuke_mod._BoundedClientSocket(big, server).recv(1024) == b""
