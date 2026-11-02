"""Prepare the pinned Nuke bridge, then optionally register it in Windows Codex."""

import argparse
import hashlib
import importlib.util
import io
import json
import shutil
import socket
import sys
import tokenize
import tomllib
import urllib.request
from datetime import datetime
from pathlib import Path

from fx_agent.install_config import replace_servers

ROOT = Path(__file__).resolve().parents[1]
REVISION = "6cbed80a2f35b997263fc51c988c5bb1405657fd"
DOWNLOADS = {
    "fx_agent_nuke_addon.py": (
        "nuke_addon/nuke_mcp_addon.py",
        "ad84dd6183ae39bef86b5183d639809bef0c2b4af59a0ab33963eb20e85b5135",
    ),
    "LICENSE.nuke-mcp": (
        "LICENSE",
        "6cf746c3c041c73a92088e454f55cccef08060f56ec67cc9cdc5e019394530a1",
    ),
}
BEGIN, END = "# BEGIN FX AGENT NUKE", "# END FX AGENT NUKE"


def port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        try:
            listener.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def nuke_config(original: str, root: Path, port: int, enabled: bool = True) -> str:
    block = f'''[mcp_servers.fx-nuke]
command = {json.dumps(str(root / ".venv/Scripts/python.exe"))}
args = ["-m", "fx_agent.nuke_server", "--port", "{port}"]
startup_timeout_sec = 30
tool_timeout_sec = 3600
enabled = {str(enabled).lower()}

[mcp_servers.fx-nuke.env]
FX_AGENT_DATA_ROOT = {json.dumps(str(root / "data"))}
PYTHONUTF8 = "1"
'''
    return replace_servers(original, {"fx-nuke"}, block)


def menu_content(original: str, root: Path, port: int) -> str:
    markers = [
        (token.string, token.start[0], token.end[0])
        for token in tokenize.generate_tokens(io.StringIO(original).readline)
        if token.type == tokenize.COMMENT and token.start[1] == 0 and token.string in (BEGIN, END)
    ]
    if markers and [marker[0] for marker in markers] != [BEGIN, END]:
        raise ValueError("Existing Nuke startup marker is invalid")
    if markers:
        lines = original.splitlines(keepends=True)
        original = "".join(lines[: markers[0][1] - 1] + lines[markers[1][2] :])
    block = f"""{BEGIN}
import nuke as _fx_host
if (_fx_host.NUKE_VERSION_MAJOR, _fx_host.NUKE_VERSION_MINOR) >= (15, 2):
    import sys as _fx_sys
    _fx_path = {str(root / "integrations/nuke")!r}
    if _fx_path not in _fx_sys.path:
        _fx_sys.path.insert(0, _fx_path)
    import fx_agent_nuke as _fx_nuke
    _fx_nuke.start(port={port})
{END}
"""
    result = original.rstrip() + "\n\n" + block
    compile(result, "menu.py", "exec")
    return result


def prepare_vendor(destination: Path) -> None:
    """Download only immutable files with checked bytes and include the MIT license."""
    pending = {}
    for name, (source, checksum) in DOWNLOADS.items():
        path = destination / name
        if path.exists():
            content = path.read_bytes()
        else:
            url = f"https://raw.githubusercontent.com/kleer001/nuke-mcp/{REVISION}/{source}"
            with urllib.request.urlopen(url, timeout=30) as response:
                content = response.read(1024 * 1024 + 1)
        if hashlib.sha256(content).hexdigest() != checksum:
            raise ValueError(f"Pinned bridge checksum mismatch: {name}")
        pending[path] = content
    destination.mkdir(parents=True, exist_ok=True)
    for path, content in pending.items():
        path.write_bytes(content)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8187)
    parser.add_argument(
        "--require-shared-agent",
        action="store_true",
        help="仅更新Nuke时，要求当前工程共享记忆入口已安装",
    )
    parser.add_argument("--prepare", action="store_true", help="仅下载并校验桥接，不修改用户配置")
    parser.add_argument("--apply", action="store_true", help="下载桥接并注册当前工程和Nuke启动脚本")
    parser.add_argument(
        "--register-disabled", action="store_true", help="预注册禁用的MCP入口，不修改Nuke启动配置"
    )
    args = parser.parse_args()
    if sum((args.prepare, args.apply, args.register_disabled)) > 1:
        parser.error("--prepare、--apply和--register-disabled只能选择一个。")
    if not 1 <= args.port <= 65535 or args.port in (8100, 8186, 54321):
        parser.error("Nuke端口必须独立，不能使用Houdini的8186或原MCP默认8100、54321。")
    if Path(sys.executable).resolve() != ROOT / ".venv/Scripts/python.exe":
        parser.error("请使用当前工程的.venv/Scripts/python.exe运行。")
    config, menu = Path.home() / ".codex/config.toml", Path.home() / ".nuke/menu.py"
    original = config.read_text(encoding="utf-8") if config.exists() else ""
    if args.require_shared_agent:
        shared = tomllib.loads(original).get("mcp_servers", {}).get("fx-agent", {})
        configured = shared.get("env", {}).get("FX_AGENT_DATA_ROOT", "")
        if (
            not configured
            or not Path(configured).is_absolute()
            or Path(configured).resolve() != (ROOT / "data").resolve()
            or shared.get("enabled", True) is not True
            or not Path(shared.get("command", "")).is_absolute()
            or Path(shared.get("command", "")).resolve()
            != (ROOT / ".venv/Scripts/python.exe").resolve()
            or shared.get("args") != ["-m", "fx_agent"]
        ):
            parser.error("请先使用 Both 或 Houdini 安装当前工程的共享 Agent 入口。")
    houdini_port = (
        tomllib.loads(original)
        .get("mcp_servers", {})
        .get("fx-agent", {})
        .get("env", {})
        .get("HOUDINI_PORT", "8186")
    )
    if str(args.port) == str(houdini_port):
        parser.error("Nuke端口与当前Houdini配置冲突。")
    if not port_available(args.port):
        parser.error("端口已占用；请选择其他端口或先停止已有桥接，不会自动切换。")
    writes = {config: nuke_config(original, ROOT, args.port, enabled=not args.register_disabled)}
    if not args.register_disabled:
        writes[menu] = menu_content(
            menu.read_text(encoding="utf-8") if menu.exists() else "", ROOT, args.port
        )
    print(
        json.dumps(
            {
                "port": args.port,
                "root": str(ROOT),
                "apply": args.apply,
                "prepare": args.prepare,
                "registered_disabled": args.register_disabled,
                "paths": [str(path) for path in writes],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if not args.apply and not args.prepare and not args.register_disabled:
        return
    if not importlib.util.find_spec("nukemcp"):
        parser.error("请先运行 uv sync --frozen --group nuke")
    prepare_vendor(ROOT / "integrations/nuke/vendor")
    if args.prepare:
        return
    backup = (
        Path.home() / ".fx-agent-install-backups" / datetime.now().strftime("nuke-%Y%m%d-%H%M%S-%f")
    )
    backup.mkdir(parents=True)
    snapshots = {}
    for index, path in enumerate(writes):
        saved = backup / f"{index}-{path.name}"
        snapshots[path] = saved if path.exists() else None
        if path.exists():
            shutil.copy2(path, saved)
    (backup / "manifest.json").write_text(
        json.dumps({str(p): str(s) if s else None for p, s in snapshots.items()}, indent=2),
        encoding="utf-8",
    )
    try:
        for path, content in writes.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    except BaseException as error:
        for path, saved in snapshots.items():
            try:
                if saved:
                    shutil.copy2(saved, path)
                else:
                    path.unlink(missing_ok=True)
            except OSError as recovery_error:
                error.add_note(f"回退失败 {path}: {recovery_error}")
        error.add_note(f"备份：{backup}")
        raise
    if args.register_disabled:
        print(f"已预注册禁用的fx-nuke，未修改Nuke启动配置；备份：{backup}")
    else:
        print(f"已注册fx-nuke，备份：{backup}。启动Nuke15.2并新建Codex聊天后验证。")


if __name__ == "__main__":
    main()
