"""Fixed production conventions, independent of learned memories and recipes."""

import copy
import os
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from fxhoudinimcp.bridge import HoudiniBridge

from fx_agent.production_rules import CACHE_PATH
from fx_agent.production_rules import production_rules as production_rules

FILE_PARMS = frozenset({"file", "filename", "fileName", "filepath", "file_path", "texture", "tex0"})
CHECKED_COMMANDS = frozenset(
    {
        "parameters.set_parameter",
        "parameters.set_parameters",
        "parameters.set_expression",
        "parameters.revert_parameter",
        "parameters.link_parameters",
        "cache.write_cache",
    }
)




def check_path(value: Any, purpose: str = "read") -> dict:
    """Check literal, unexpanded paths without executing Houdini expressions."""
    if purpose not in ("read", "filecache"):
        raise ValueError("purpose must be read or filecache")
    valid = isinstance(value, str)
    if purpose == "filecache":
        valid = valid and value == CACHE_PATH
    elif valid:
        match = re.match(r"^\$(?:HIP|\{HIP\})/", value)
        tail = value[match.end() :] if match else ""
        valid = bool(match and tail)
        valid = valid and not any(char in value for char in ("\\", "`", ":", "\x00"))
        valid = valid and not any(ord(char) < 32 or ord(char) == 127 for char in value)
        valid = valid and all(part not in ("", ".", "..") for part in tail.split("/"))
        variables_removed = re.sub(
            r"\$(?:\{(?:F\d*|FF|OS|UDIM)\}|(?:F\d*|FF|OS|UDIM)(?![A-Za-z0-9_]))", "", tail
        )
        valid = valid and "$" not in variables_removed
    return {
        "valid": bool(valid),
        "purpose": purpose,
        "required": CACHE_PATH if purpose == "filecache" else "$HIP/...",
    }


def _require(value: Any, purpose: str = "read") -> None:
    if not check_path(value, purpose)["valid"]:
        raise ValueError(
            f"工程路径规范：{purpose} 必须使用 {check_path(value, purpose)['required']}；不能使用外部路径或未验证表达式"
        )


def _base_type(name: str) -> str:
    return re.sub(r"::\d+(?:\.\d+)*$", "", name).split("::")[-1]


def _file_values(parms: dict, expressions: dict | None = None) -> None:
    if set(expressions or {}) & FILE_PARMS:
        raise ValueError("文件路径使用含 $HIP 的字符串，不能使用未验证的表达式")
    for name, value in parms.items():
        if name in FILE_PARMS:
            _require(value)


async def guard_command(
    command: str, params: dict | None, read: Callable[..., Awaitable[Any]]
) -> dict:
    """Preflight covered writes before dispatch; copy request data before injecting defaults."""
    prepared = copy.deepcopy(params or {})
    if command == "workflow.build_sop_chain":
        graph = {
            "nodes": [
                {"type": step.get("type", ""), "parms": step.get("params", {})}
                for step in prepared.get("steps", [])
            ]
        }
        checked = await guard_command("graph.build_network", graph, read)
        for step, node in zip(prepared.get("steps", []), checked["nodes"], strict=True):
            if node.get("parms"):
                step["params"] = node["parms"]
    elif command == "scene.import_file":
        raise ValueError(
            "上游 import_file 不展开 $HIP 且会写入绝对路径；请用 build_network 创建相应读取节点并设置 $HIP 路径"
        )
    elif command == "graph.build_network":
        for node in prepared.get("nodes", []):
            parms = node.get("parms", {})
            exprs = node.get("expressions", {}) | node.get("exprs", {})
            _file_values(parms, exprs)
            kind = _base_type(node.get("type", ""))
            if kind == "file":
                _require(parms.get("file"))
            elif kind == "filecache":
                if set(exprs) & {"file", "filemethod", "sopoutput"} or "sopoutput" in parms:
                    raise ValueError("File Cache 使用 Explicit/file，不能改写派生 sopoutput")
                _require(parms.get("file", CACHE_PATH), "filecache")
                if parms.get("filemethod", "explicit") not in ("explicit", 1):
                    raise ValueError("File Cache filemethod 必须为 explicit")
                parms.update(filemethod="explicit", file=CACHE_PATH)
                node["parms"] = parms
    elif command == "nodes.create_node" and _base_type(prepared.get("node_type", "")) in (
        "file",
        "filecache",
    ):
        raise ValueError(
            "使用 build_network 同时创建 File/File Cache 和合规路径，避免默认路径不符合 $HIP 规范"
        )
    elif command in CHECKED_COMMANDS:
        changes = prepared.get("params", {}) if command.endswith("set_parameters") else {}
        if command.endswith("set_parameter"):
            changes = {prepared["parm_name"]: prepared.get("value")}
        elif command.endswith("set_expression"):
            changes = {prepared["parm_name"]: {"expr": prepared.get("expression")}}
        _file_values(changes)
        key = prepared.get("parm_name", prepared.get("dest_parm"))
        if command.endswith(("revert_parameter", "link_parameters")) and key in FILE_PARMS | {
            "filemethod",
            "sopoutput",
        }:
            raise ValueError("文件参数不能通过恢复默认或通道链接绕过路径规范，请显式设置合规路径")
        relevant = set(changes) & {"file", "filemethod", "sopoutput"}
        if relevant or command == "cache.write_cache":
            node_path = prepared.get("node_path", "")
            info = await read("nodes.get_node_info", {"node_path": node_path})
            if _base_type(info.get("type", {}).get("name", "")) == "filecache":
                if "sopoutput" in changes:
                    raise ValueError("不能直接覆盖 File Cache 的派生 sopoutput")
                values = dict(changes)
                for parm in ("filemethod", "file"):
                    if parm not in values:
                        current = await read(
                            "parameters.get_parameter", {"node_path": node_path, "parm_name": parm}
                        )
                        values[parm] = (
                            current.get("raw_value") if parm == "file" else current.get("value")
                        )
                if values["filemethod"] not in ("explicit", 1):
                    raise ValueError(
                        "File Cache 必须先通过 set_parameters 设置 filemethod=explicit 和规范 file"
                    )
                _require(values["file"], "filecache")
                if command == "cache.write_cache":
                    scene = await read("scene.get_scene_info", {})
                    actual = await read(
                        "parameters.get_parameter",
                        {
                            "node_path": node_path,
                            "parm_name": "sopoutput",
                        },
                    )
                    name = node_path.rsplit("/", 1)[-1]
                    frame = scene["current_frame"]
                    if int(frame) != frame:
                        raise ValueError("写缓存前请停在整数帧，以验证实际 sopoutput")
                    expected = (
                        Path(scene["hip_file"]).parent
                        / "geo"
                        / name
                        / f"{name}.{int(frame):04d}.bgeo.sc"
                    )
                    if os.path.normcase(os.path.abspath(actual["value"])) != os.path.normcase(
                        os.path.abspath(expected)
                    ):
                        raise ValueError(
                            "File Cache 实际 sopoutput 与规范不符，先恢复原生输出表达式"
                        )
    return prepared


class ProductionBridge(HoudiniBridge):
    """Adapt the pinned upstream bridge without editing its installation."""

    async def execute(self, command: str, params=None, timeout=None):
        prepared = await guard_command(command, params, super().execute)
        result = await super().execute(command, prepared, timeout)
        if command in {
            "workflow.setup_pyro_sim",
            "workflow.setup_rbd_sim",
            "workflow.setup_flip_sim",
        }:
            cache_path = result.get("cache_path") if isinstance(result, dict) else None
            if cache_path:
                info = await super().execute("nodes.get_node_info", {"node_path": cache_path})
                if _base_type(info.get("type", {}).get("name", "")) == "filecache":
                    # The upstream workflow creates but does not write this cache.
                    written = await super().execute(
                        "parameters.set_parameters",
                        {
                            "node_path": cache_path,
                            "params": {"filemethod": "explicit", "file": CACHE_PATH},
                        },
                    )
                    if written.get("errors") or written.get("locked_parms"):
                        raise ValueError(
                            f"场景已创建，但 {cache_path} 缓存路径规范化失败：{written}"
                        )
                    mode = await super().execute(
                        "parameters.get_parameter",
                        {
                            "node_path": cache_path,
                            "parm_name": "filemethod",
                        },
                    )
                    path = await super().execute(
                        "parameters.get_parameter",
                        {
                            "node_path": cache_path,
                            "parm_name": "file",
                        },
                    )
                    if (
                        mode.get("value") not in ("explicit", 1)
                        or path.get("raw_value") != CACHE_PATH
                    ):
                        raise ValueError(
                            f"场景已创建，但 {cache_path} 缓存路径回读不符合规范；请修复后继续"
                        )
        return result
