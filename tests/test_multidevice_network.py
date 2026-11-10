"""Real HTTP routing and MCP gates with two credentials and a synthetic local DCC."""

import json
import socket
import threading
import time

import pytest
import uvicorn
from mcp.server.fastmcp import FastMCP

from fx_agent.data_service import DeviceConfig, ServiceConfig, create_app
from fx_agent.multidevice import RemoteClient
from fx_agent.task_gates import TaskGates
from fx_agent.task_tools import install_task_gates


@pytest.fixture
def live_service(tmp_path):
    shared = tmp_path / "shared"
    settings = ServiceConfig(data_root=shared, database_root=tmp_path / "database",
                             devices={"a": DeviceConfig(name="A", token="a" * 40),
                                      "b": DeviceConfig(name="B", token="b" * 40)})
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(create_app(settings), log_level="error", lifespan="on"))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    try:
        yield url, shared
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        assert not thread.is_alive()


async def test_remote_mcp_production_journey_keeps_dcc_local(live_service, monkeypatch, tmp_path):
    url, shared = live_service
    monkeypatch.setenv("FX_AGENT_SERVICE_URL", url)
    monkeypatch.setenv("FX_AGENT_SERVICE_TOKEN", "a" * 40)
    monkeypatch.setenv("FX_AGENT_LOCAL_ROOT", str(tmp_path / "workstation"))
    scene = shared / "scene.hip"
    preview = shared / "preview.png"
    preview.write_bytes(b"synthetic viewed preview")
    edits = []
    mcp = FastMCP("workstation")

    # Register actual public schemas. In service mode none of these bodies may
    # touch workstation storage; they must route through HTTP.
    from fx_agent.server import get_server

    for name in ("fx_get_production_rules", "fx_get_module_knowledge", "fx_search_effects",
                 "fx_search_memory", "fx_archive_case", "fx_record_feedback", "fx_finalize_learning"):
        original = get_server()._tool_manager.get_tool(name)
        mcp.add_tool(original.fn, name=name)

    @mcp.tool()
    def get_workflow_guide(topic: str) -> str:
        return "local upstream guide " + topic

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
        return {"healthy": True}

    async def live(_):
        return "127.0.0.1:18186", str(scene), {"fps": 24}

    install_task_gates(mcp, "houdini", lambda: tmp_path / "forbidden-local-data", live)

    async def call(name, arguments):
        result = await mcp.call_tool(name, arguments)
        if isinstance(result, tuple):
            result = result[0]
        try:
            return json.loads(result[0].text)
        except ValueError:
            return result[0].text

    definition = dict(project_id="p", shot_id="s", software="houdini", scene_path=str(scene),
                      endpoint="127.0.0.1:18186", goal="draft", acceptance=["preview"],
                      budget="draft", modules=["particles"])
    task = await call("fx_begin_task", {"spec": definition})
    scope = {"fx_task_id": task["task_id"]}
    for name, arguments in [
        ("fx_get_production_rules", {"project_id": "p"}),
        ("fx_get_module_knowledge", {"module": "particles"}),
        ("fx_search_effects", {"query": "no-match"}),
        ("fx_search_memory", {"project_id": "p", "query": "no-match"}),
        ("get_workflow_guide", {"topic": "discipline"}),
        ("get_workflow_guide", {"topic": "dopparticles"}),
    ]:
        await call(name, arguments | scope)
    task = await call("fx_set_task_plan", dict(task_id=task["task_id"], expected_revision=1,
                      method="draft", applies_when="test", changes="node", selected_sources=[],
                      no_match_reason="No applicable records in actual searches"))
    await call("create_node", {"name": "local-node"} | scope)
    await call("verify_network", scope)
    await call("save_scene", scope)
    case = await call("fx_archive_case", dict(project_id="p", shot_id="s", title="test",
                     files=[{"path": str(scene), "role": "project"},
                            {"path": str(preview), "role": "preview"}]) | scope)
    finished = await call("fx_finish_task", dict(task_id=task["task_id"], expected_revision=task["revision"],
                          verification_note="synthetic preview inspected", case_id=case["case_id"]))
    assert finished["state"] == "verified"
    assert edits == ["local-node"]
    assert not (tmp_path / "forbidden-local-data").exists()
    assert not (shared / "memory.sqlite3").exists()
    assert RemoteClient().status()["device_id"] == "a"
    assert RemoteClient().status()["last_backup"]
    accepted = await call("fx_record_feedback", {"feedback": dict(project_id="p", shot_id="s",
                    effect_type="particles", attempt_id="draft-1", modules=["particles"],
                    problem="draft", change="local node", reason="test", result="preview",
                    outcome="accepted", user_quote="synthetic acceptance", source_ref="test",
                    expected_revision=0, case_id=case["case_id"])} | scope)
    learned = await call("fx_finalize_learning", dict(feedback_id=accepted["feedback_id"],
                        project_id="p", method="local particle draft", reason="verified preview",
                        applies_when="synthetic particle test") | scope)
    assert learned
    monkeypatch.setenv("FX_AGENT_SERVICE_TOKEN", "b" * 40)
    with pytest.raises(PermissionError):
        TaskGates(shared).get(task["task_id"])
    assert RemoteClient().call("tool.fx_get_case", {"case_id": case["case_id"], "project_id": "p"})["case_id"] == case["case_id"]
    shared_memory = RemoteClient().call("tool.fx_search_memory", {"query": "local particle draft"})
    assert shared_memory["items"]


def test_client_config_requires_https_outside_loopback(monkeypatch):
    monkeypatch.setenv("FX_AGENT_SERVICE_URL", "http://192.168.10.2:18790")
    monkeypatch.setenv("FX_AGENT_SERVICE_TOKEN", "a" * 40)
    monkeypatch.delenv("FX_AGENT_ALLOW_INSECURE_HTTP", raising=False)
    with pytest.raises(ValueError, match="HTTPS"):
        RemoteClient()
