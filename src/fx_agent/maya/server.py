"""FX Maya stdio MCP. Each process owns its selected session independently."""

import argparse
import asyncio
import os
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP

from fx_agent.config import data_root, session_root
from fx_agent.data_api import shared_functions
from fx_agent.maya.client import DEFAULT_PORTS, MayaConnection, discover, validate_port
from fx_agent.maya.launcher import start_maya as launch_maya
from fx_agent.task_tools import GATE_TOOLS, install_task_gates
from fx_agent.tool_profiles import apply_tool_profile
from fx_agent.vfx_catalog import fx_search_published_templates
from fx_agent.workflow import HOST_STARTUP_INSTRUCTIONS

Renderer = Literal["arnold", "redshift"]


def register_shared_tools(mcp):
    """Reuse existing knowledge implementations and data root, not Houdini dispatch.

    Register original functions directly so Maya MCP stays decoupled from Houdini
    tool profiles and installs its own Maya task gate.
    """
    excluded = GATE_TOOLS | {
        "fx_status",
        "fx_check_production_path",
        "fx_report_render_issue",
        "fx_get_render_issue",
        "fx_list_render_issues",
        "fx_update_render_issue",
    }
    for function in shared_functions():
        if function.__name__ not in excluded and not function.__name__.startswith("fx_render_view_"):
            mcp.tool()(function)
    mcp.tool()(fx_search_published_templates)


def build_server(connection=None, ports=DEFAULT_PORTS):
    connection = connection or MayaConnection()
    render_connection = ContextVar("maya_render_connection", default=None)
    mcp = FastMCP(
        "fx-maya",
        instructions=HOST_STARTUP_INSTRUCTIONS + "显式选择 Maya 会话。超时先查现场，再决定后续动作。",
    )
    register_shared_tools(mcp)

    @mcp.tool()
    def start_maya(executable: str) -> dict:
        """Start GUI Maya from an explicit absolute maya.exe path without command-line flags. Shared module startup loads the FX bridge. No desktop automation, renderer loading, auto-selection or retry."""
        return launch_maya(executable)

    @mcp.tool()
    def list_maya_sessions() -> dict:
        """Probe configured ports and Maya registry live; never select the first window."""
        result = discover(session_root(), ports)
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
        result = connection.send(
            "load_scene",
            {"path": path.as_posix(), "discard_unsaved": bool(discard_unsaved)},
            timeout=120,
        )
        with connection.lock:
            if connection.target is not None:
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
    async def render_maya_frame(renderer: Renderer, camera: str, frame: int, output_dir: str, width: int = 640, height: int = 480, timeout: float = 600) -> dict:
        """Render once, wait outside Maya for native completion and image decoding. Timeout never repeats the render; inspect the returned job ID after reconnecting."""
        active = render_connection.get() or connection
        deadline = time.monotonic() + timeout
        receipt = await asyncio.to_thread(
            active.send,
            "render_frame",
            {"renderer": renderer, "camera": camera, "frame": frame, "output_dir": output_dir, "width": width, "height": height},
            timeout,
        )
        try:
            job_id = receipt["job_id"]
            while time.monotonic() < deadline:
                remaining = max(0.01, deadline - time.monotonic())
                result = await asyncio.to_thread(
                    active.send,
                    "render_status",
                    {"job_id": job_id},
                    min(10, remaining),
                )
                if result.get("success") is True and result.get("status") == "complete":
                    return result
                await asyncio.sleep(min(0.2, max(0, deadline - time.monotonic())))
            raise TimeoutError(f"Render outcome not verified; inspect job {job_id}, {receipt['output_dir']} and Maya/plugin/license logs after explicitly reconnecting. Do not replay automatically.")
        except (TimeoutError, OSError, ValueError, KeyError):
            # Transport/timeout monitoring failures leave an uncertain native operation.
            # Application-level RuntimeErrors reported by Maya do not poison the connection.
            active.failed = True
            raise

    @mcp.tool()
    def get_maya_render_status(job_id: str = "") -> dict:
        """List bounded existing jobs, or inspect one after explicit reconnection; never starts another render. Native completion restores settings and image decoding verifies output."""
        return connection.send("render_status", {"job_id": job_id})

    def execute_maya_python(code: str, timeout: float = 30) -> dict:
        """Last resort, scene-write gated. Set result to a JSON value. Return stdout/stderr even for code errors. Uses target Maya's Python dialect."""
        return connection.send("execute_python", {"code": code}, timeout)

    if os.getenv("FX_MAYA_ENABLE_PYTHON") == "1":
        mcp.tool()(execute_maya_python)

    @mcp.tool()
    def save_scene(path: str) -> dict:
        """Save current scene at task target. An untitled scene may be named at a new path; existing named scenes cannot be redirected."""
        result = connection.send("save_scene", {"path": path})
        # The naming operation was identity-checked in Maya. Adopt only its actual
        # returned path, so subsequent requests still validate the selected scene.
        with connection.lock:
            if connection.target is not None:
                connection.target["scene_path"] = result["path"]
        return result

    @mcp.tool()
    def verify_network() -> dict:
        """Structural check for unknown nodes; actual rendering/visual review is separate."""
        return connection.send("verify_network")

    async def live_target(_context):
        active = render_connection.get() or connection
        info = await asyncio.to_thread(active.info)
        return f"127.0.0.1:{info['port']}", info["scene_path"], {key: info[key] for key in ("pid", "session_id")}

    install_task_gates(mcp, "maya", data_root, live_target)
    manager = mcp._tool_manager
    original = manager.call_tool
    lock = asyncio.Lock()

    async def serialized(name, arguments, *args, **kwargs):
        # Bind before the task gate checks the target. Selection can then change
        # without diverting the render, and polling does not hold the server lock.
        if name == "render_maya_frame" and arguments.get("fx_task_id"):
            pinned = await asyncio.to_thread(connection.pinned)
            token = render_connection.set(pinned)
            try:
                return await original(name, arguments, *args, **kwargs)
            finally:
                render_connection.reset(token)
        async with lock:
            return await original(name, arguments, *args, **kwargs)

    manager.call_tool = serialized
    apply_tool_profile(mcp, software="maya")
    return mcp


def main():
    from fx_agent.shared_knowledge import prepare_local_data

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ports", type=int, nargs="+", default=list(DEFAULT_PORTS))
    args = parser.parse_args()
    for port in args.ports:
        validate_port(port)
    prepare_local_data()
    build_server(ports=tuple(args.ports)).run(transport="stdio")


if __name__ == "__main__":
    main()
