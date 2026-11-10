"""FX Maya stdio MCP. Each process owns its selected session independently."""

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP

from fx_agent.config import data_root
from fx_agent.maya.client import DEFAULT_PORTS, MayaConnection, discover, validate_port
from fx_agent.maya.launcher import start_maya as launch_maya
from fx_agent.task_tools import GATE_TOOLS, install_task_gates

Renderer = Literal["arnold", "redshift"]


def register_shared_tools(mcp):
    """Reuse existing knowledge implementations and data root, not Houdini dispatch.

    Register original functions so this MCP installs its own Maya task gate.
    Houdini's upstream lifespan is never entered and no DCC connection is opened.
    """
    from fx_agent.server import get_server

    excluded = GATE_TOOLS | {"fx_status", "fx_check_production_path", "fx_report_render_issue", "fx_get_render_issue",
                            "fx_list_render_issues", "fx_update_render_issue"}
    for tool in get_server()._tool_manager.list_tools():
        if tool.name.startswith("fx_") and tool.name not in excluded and not tool.name.startswith("fx_render_view_"):
            mcp.add_tool(tool.fn, name=tool.name, description=tool.description,
                         annotations=tool.annotations)


def build_server(connection=None, ports=DEFAULT_PORTS):
    connection = connection or MayaConnection()
    mcp = FastMCP("fx-maya", instructions="由当前宿主操作。显式选择Maya会话；读取真实灯光与渲染器状态。共用FX知识库，指定software=maya。修改前完成任务登记、知识读取与计划；超时后检查场景，不自动重试。")
    register_shared_tools(mcp)

    @mcp.tool()
    def start_maya(executable: str) -> dict:
        """Start GUI Maya from an explicit absolute maya.exe path without command-line flags. Shared module startup loads the FX bridge. No desktop automation, renderer loading, auto-selection or retry."""
        return launch_maya(executable)

    @mcp.tool()
    def list_maya_sessions() -> dict:
        """Probe configured ports and Maya registry live; never select the first window."""
        result = discover(data_root(), ports)
        target = connection.target or {}
        for item in result["sessions"]:
            item["current"] = all(item[key] == target.get(key) for key in ("port", "pid", "session_id"))
        return result

    @mcp.tool()
    def connect_maya(port: int, pid: int, session_id: str, expected_scene: str) -> dict:
        """Copy all identity fields from a live listing. Failed switch preserves target."""
        return connection.select(port, pid, session_id, expected_scene)

    @mcp.tool()
    def get_maya_scene_info() -> dict:
        """Read selected scene with live session and scene identity validation."""
        return connection.info()

    @mcp.tool()
    def load_scene(file_path: str, discard_unsaved: bool = False) -> dict:
        """Open the task's saved .ma/.mb in the explicitly selected session. Unsaved edits are refused unless discard_unsaved is explicitly true. No automatic retry."""
        path = Path(file_path)
        if not path.is_absolute() or path.suffix.lower() not in {".ma", ".mb"} or not path.is_file():
            raise ValueError("Existing absolute .ma/.mb path required")
        code = ("from maya import cmds\n"
                "if cmds.file(query=True, modified=True) and not %s:\n"
                "    raise RuntimeError('Unsaved scene changes; save first or explicitly discard')\n"
                "cmds.file(u%s, open=True, force=%s, prompt=False)\n"
                "result={'path':cmds.file(query=True, sceneName=True)}\n"
                % (discard_unsaved, json.dumps(path.as_posix()), discard_unsaved))
        response = connection.send("execute_python", {"code": code}, timeout=120)
        if not response.get("success"):
            raise RuntimeError(response.get("error", "Maya could not open scene"))
        result = response["result"]
        connection.target["scene_path"] = result["path"]
        return result

    @mcp.tool()
    def get_maya_renderer_status() -> dict:
        """Actual plugin loaded states and versions. Never loads/installs plugins."""
        return connection.send("renderer_status")

    @mcp.tool()
    def list_maya_lights(renderer: Renderer) -> dict:
        """List supported lights and actual scalar attributes, types, values and transforms."""
        return connection.send("list_lights", {"renderer": renderer}, timeout=30)

    @mcp.tool()
    def inspect_maya_light(renderer: Renderer, node: str) -> dict:
        """Inspect one unambiguous light; use full DAG paths when names collide."""
        return connection.send("inspect_light", {"renderer": renderer, "node": node}, timeout=30)

    @mcp.tool()
    def create_maya_light(renderer: Renderer, light_type: Literal["area", "point", "spot", "directional", "environment"], name: str, parameters: dict | None = None) -> dict:
        """Create renderer-specific light. parameters uses native attrs plus position/rotation/scale and exposure alias. No unit conversion."""
        return connection.send("create_light", {"renderer": renderer, "light_type": light_type, "name": name, "parameters": parameters})

    @mcp.tool()
    def update_maya_light(renderer: Renderer, node: str, parameters: dict) -> dict:
        """Set native attributes or position/rotation/scale; exposure resolves by node type. Dimensions use actual attributes or scale."""
        return connection.send("update_light", {"renderer": renderer, "node": node, "parameters": parameters})

    @mcp.tool()
    def set_maya_hdri(renderer: Renderer, node: str, path: str, color_space: str = "") -> dict:
        """Set a dome HDRI from an existing absolute path; inspect color space names first."""
        return connection.send("set_hdri", {"renderer": renderer, "node": node, "path": path, "color_space": color_space})

    @mcp.tool()
    def set_maya_light_link(renderer: Renderer, node: str, objects: list[str], linked: bool = True) -> dict:
        """Create/break illumination links, returning actual resulting links."""
        return connection.send("light_link", {"renderer": renderer, "node": node, "objects": objects, "linked": linked})

    @mcp.tool()
    def render_maya_frame(renderer: Renderer, camera: str, frame: int, output_dir: str, width: int = 640, height: int = 480, timeout: float = 600) -> dict:
        """Render once, wait outside Maya for native completion and image decoding. Timeout never repeats the render; inspect the returned job ID after reconnecting."""
        deadline = time.monotonic() + timeout
        receipt = connection.send("render_frame", {"renderer": renderer, "camera": camera, "frame": frame, "output_dir": output_dir, "width": width, "height": height}, timeout)
        try:
            job_id = receipt["job_id"]
            while time.monotonic() < deadline:
                result = connection.send("render_status", {"job_id": job_id}, timeout=min(10, max(0.01, deadline - time.monotonic())))
                if result.get("success") is True and result.get("status") == "complete":
                    return result
                time.sleep(min(0.2, max(0, deadline - time.monotonic())))
            raise TimeoutError(f"Render outcome not verified; inspect job {job_id}, {receipt['output_dir']} and Maya/plugin/license logs after explicitly reconnecting. Do not replay automatically.")
        except Exception:
            # Any monitoring failure leaves an uncertain native operation. The
            # Maya side independently blocks writes until its completion callback.
            connection.failed = True
            raise

    @mcp.tool()
    def get_maya_render_status(job_id: str = "") -> dict:
        """List bounded existing jobs, or inspect one after explicit reconnection; never starts another render. Native completion restores settings and image decoding verifies output."""
        return connection.send("render_status", {"job_id": job_id})

    @mcp.tool()
    def execute_maya_python(code: str, timeout: float = 30) -> dict:
        """Last resort, scene-write gated. Set result to a JSON value. Return stdout/stderr even for code errors. Uses target Maya's Python dialect."""
        return connection.send("execute_python", {"code": code}, timeout)

    @mcp.tool()
    def save_scene(path: str) -> dict:
        """Save current scene at task target. An untitled scene may be named at a new path; existing named scenes cannot be redirected."""
        result = connection.send("save_scene", {"path": path})
        # The naming operation was identity-checked in Maya. Adopt only its actual
        # returned path, so subsequent requests still validate the selected scene.
        connection.target["scene_path"] = result["path"]
        return result

    @mcp.tool()
    def verify_network() -> dict:
        """Structural check for unknown nodes; actual rendering/visual review is separate."""
        return connection.send("verify_network")

    async def live_target(_context):
        info = connection.info()
        return f"127.0.0.1:{info['port']}", info["scene_path"], {key: info[key] for key in ("pid", "session_id")}

    install_task_gates(mcp, "maya", data_root, live_target)
    manager = mcp._tool_manager
    original = manager.call_tool
    lock = asyncio.Lock()

    async def serialized(*args, **kwargs):
        async with lock:
            return await original(*args, **kwargs)

    manager.call_tool = serialized
    return mcp


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ports", type=int, nargs="+", default=list(DEFAULT_PORTS))
    args = parser.parse_args()
    for port in args.ports:
        validate_port(port)
    build_server(ports=tuple(args.ports)).run(transport="stdio")


if __name__ == "__main__":
    main()
