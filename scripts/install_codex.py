"""Install the current checkout into Codex on Windows; preview unless --apply."""

import argparse
import json
import os
import shutil
import sys
import tomllib
from datetime import datetime
from pathlib import Path

from fx_agent.config import DEFAULT_HOUDINI_PORT
from fx_agent.houdini_runtime import prepare_runtime, runtime_path, upstream_plugin
from fx_agent.install_config import replace_servers

ROOT = Path(__file__).resolve().parents[1]


def codex_config(original: str, root: Path, data: Path, port: int) -> str:
    """Replace only the two FX server tables; reject unsupported TOML layouts."""
    nuke_args = tomllib.loads(original).get("mcp_servers", {}).get("fx-nuke", {}).get("args", [])
    if isinstance(nuke_args, list):
        nuke_port = ""
        for index, argument in enumerate(nuke_args):
            if argument == "--port" and index + 1 < len(nuke_args):
                nuke_port = str(nuke_args[index + 1])
            elif isinstance(argument, str) and argument.startswith("--port="):
                nuke_port = argument.split("=", 1)[1]
        if nuke_port.isdecimal() and int(nuke_port) == port:
            raise ValueError("Houdini端口与已注册Nuke端口冲突，请选择其他端口。")

    def quote(value: Path) -> str:
        return json.dumps(str(value), ensure_ascii=False)  # TOML basic strings

    block = f'''[mcp_servers.fx-agent]
command = {quote(root / ".venv/Scripts/python.exe")}
args = ["-m", "fx_agent"]
startup_timeout_sec = 30
tool_timeout_sec = 3600

[mcp_servers.fx-agent.env]
HOUDINI_HOST = "127.0.0.1"
HOUDINI_PORT = "{port}"
FX_AGENT_DATA_ROOT = {quote(data)}
FXHOUDINIMCP_AUTO_LAYOUT = "0"
NO_PROXY = "localhost,127.0.0.1,::1"
PYTHONUTF8 = "1"
'''
    return replace_servers(original, {"fxhoudini", "fx-agent"}, block)


def houdini_package(port: int, existing: Path | None = None) -> str:
    """Use the dependency bundled in this checkout, preserving loopback binding."""
    from fxhoudinimcp.houdini_package import package_json

    package = json.loads(existing.read_text(encoding="utf-8")) if existing else {}
    if not isinstance(package, dict) or not isinstance(package.get("env", []), list):
        raise ValueError("Houdini 包格式无效，停止安装。")
    env = {}
    for item in package.get("env", []):
        if not isinstance(item, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in item.items()
        ):
            raise ValueError("Houdini 包含无法安全迁移的 env 格式，停止安装。")
        env.update(item)
    env.update(
        FXHOUDINIMCP_PORT=str(port),
        FXHOUDINIMCP_BIND="127.0.0.1",
        FXHOUDINIMCP_AUTOSTART="1",
        FXHOUDINIMCP_AUTO_LAYOUT="0",
    )
    generated = json.loads(package_json(existing=env))
    for item in generated["env"]:
        env.update(item)
    env["FXHOUDINIMCP"] = str(runtime_path(ROOT, Path(env["FXHOUDINIMCP"]))).replace("\\", "/")
    package.update(generated)
    package["env"] = [{key: value} for key, value in env.items()]
    return json.dumps(package, ensure_ascii=False, indent=2) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--houdini-pref", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, default=ROOT / "data")
    parser.add_argument("--port", type=int, default=DEFAULT_HOUDINI_PORT)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("此安装器当前只支持 Windows Codex。")
    if not 1 <= args.port <= 65535 or not args.data_root.is_absolute():
        parser.error("端口必须为 1..65535，数据目录必须为绝对路径。")
    pref = args.houdini_pref.resolve(strict=True)
    if not pref.is_dir():
        parser.error("Houdini 偏好路径必须是目录。")
    python = ROOT / ".venv/Scripts/python.exe"
    if Path(sys.executable).resolve() != python.resolve():
        parser.error("请使用当前工程 .venv/Scripts/python.exe 执行安装器。")
    config = Path.home() / ".codex/config.toml"
    old_package = pref / "packages/fxhoudinimcp.json"
    package = pref / "packages/fx-agent.json"
    original = config.read_text(encoding="utf-8") if config.exists() else ""
    writes = {
        config: codex_config(original, ROOT, args.data_root, args.port),
        package: houdini_package(
            args.port,
            package if package.exists() else old_package if old_package.exists() else None,
        ),
    }
    links = {}
    for name in ("fx-workflow", "fx-learning", "nuke-workflow", "fx-project-learn"):
        link = Path.home() / ".agents/skills" / name
        target = ROOT / "skills" / name
        if os.path.lexists(link):
            if link.resolve() != target.resolve():
                parser.error(f"技能路径已被其他内容占用：{link}")
        else:
            links[link] = target
    print(
        json.dumps(
            {
                "root": str(ROOT),
                "data_root": str(args.data_root),
                "port": args.port,
                "write": [str(p) for p in writes],
                "retire": str(old_package),
                "skills": [str(p) for p in links],
                "apply": args.apply,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if not args.apply:
        return
    # Publish a complete immutable plugin before updating profile references.
    # Reinstalling never edits the dependency or an already-running runtime.
    prepare_runtime(ROOT, upstream_plugin())
    # Backups stay local and may contain private settings. Never print their contents.
    backup = Path.home() / ".fx-agent-install-backups" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup.mkdir(parents=True)
    touched = [*writes, old_package]
    snapshots = {}
    for index, path in enumerate(touched):
        saved = backup / f"{index}-{path.name}"
        snapshots[path] = saved if path.exists() else None
        if path.exists():
            shutil.copy2(path, saved)
    (backup / "manifest.json").write_text(
        json.dumps({str(p): str(s) if s else None for p, s in snapshots.items()}, indent=2),
        encoding="utf-8",
    )
    created = []
    try:
        import _winapi

        for link, target in links.items():
            link.parent.mkdir(parents=True, exist_ok=True)
            _winapi.CreateJunction(str(target), str(link))
            created.append(link)
        for path, content in writes.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        old_package.unlink(missing_ok=True)
    except BaseException as error:
        recovery_errors = []
        for path, saved in snapshots.items():
            try:
                if saved:
                    shutil.copy2(saved, path)
                else:
                    path.unlink(missing_ok=True)
            except OSError as recovery_error:
                recovery_errors.append(f"{path}: {recovery_error}")
        for link in reversed(created):
            try:
                os.rmdir(link)  # Remove the junction itself, never its target.
            except OSError as recovery_error:
                recovery_errors.append(f"{link}: {recovery_error}")
        error.add_note(f"原配置备份：{backup}")
        if recovery_errors:
            error.add_note("部分回退失败，请根据备份恢复：\n" + "\n".join(recovery_errors))
        raise
    print(f"安装完成，原配置备份：{backup}")
    print("请重启 Houdini 并新建 Codex 聊天，再运行 scripts/check_houdini.py 验证。")


if __name__ == "__main__":
    main()
