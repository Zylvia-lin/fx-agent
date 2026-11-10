"""Preview/install FX Maya .mod and independent stdio MCP, preserving other DCCs."""

import argparse
import json
import os
import tomllib
from datetime import datetime
from pathlib import Path

from fx_agent.config import data_root
from fx_agent.install_config import replace_servers
from fx_agent.maya.client import DEFAULT_PORTS, validate_port

ROOT = Path(__file__).resolve().parents[1]
MAYA_RUNTIMES = [(version, "py2") for version in (2017, 2018, 2019, 2020)] + [
    (version, "py3") for version in (2022, 2023, 2024, 2025, 2026, 2027)
]


def maya_config(original, root, data, ports):
    for port in ports:
        validate_port(port)
    config = tomllib.loads(original)
    for name in ("fx-agent", "fx-nuke"):
        server = config.get("mcp_servers", {}).get(name, {})
        configured = server.get("env", {}).get("FX_AGENT_DATA_ROOT")
        if configured and Path(configured).resolve() != data.resolve():
            raise ValueError("Maya must share FX_AGENT_DATA_ROOT with " + name)
        args = server.get("args", [])
        for index, arg in enumerate(args):
            existing = args[index + 1] if arg == "--port" and index + 1 < len(args) else (
                arg.split("=", 1)[1] if isinstance(arg, str) and arg.startswith("--port=") else None)
            if existing is not None and str(existing).isdigit() and int(existing) in ports:
                raise ValueError("Maya discovery port conflicts with " + name)
        existing = server.get("env", {}).get("HOUDINI_PORT")
        if existing is not None and str(existing).isdigit() and int(existing) in ports:
            raise ValueError("Maya discovery port conflicts with Houdini")
    block = f'''[mcp_servers.fx-maya]
command = {json.dumps(str(root / ".venv/Scripts/python.exe"))}
args = {json.dumps(["-m", "fx_agent.maya.server", "--ports", *map(str, ports)])}
startup_timeout_sec = 30
tool_timeout_sec = 3600

[mcp_servers.fx-maya.env]
FX_AGENT_DATA_ROOT = {json.dumps(str(data), ensure_ascii=False)}
PYTHONUTF8 = "1"
'''
    return replace_servers(original, {"fx-maya"}, block)


def module_content(root, data, port=None):
    if port is not None:
        validate_port(port)
    module = (root / "integrations/maya").resolve()
    if any("\n" in str(value) or "\r" in str(value) for value in (module, data)):
        raise ValueError("Module paths cannot contain newlines")
    return "\n".join(
        f"+ MAYAVERSION:{version} PLATFORM:win64 fx_maya any {(module / runtime).as_posix()}\n"
        "scripts: scripts\nPYTHONPATH +:= .\nPYTHONPATH +:= scripts\nMAYA_PLUG_IN_PATH +:= plug-ins\n"
        f"FX_AGENT_DATA_ROOT = {data.as_posix()}\n"
        + (f"FX_MAYA_PORT = {port}\n" if port is not None else "")
        for version, runtime in MAYA_RUNTIMES
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--modules-dir", type=Path, required=True,
                        help="Maya user modules directory (global or version-specific)")
    parser.add_argument("--data-root", type=Path)
    parser.add_argument("--ports", type=int, nargs="+", default=list(DEFAULT_PORTS))
    parser.add_argument("--bridge-port", type=int, help="Optional fixed port; omit for multi-session free bind")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    config = Path.home() / ".codex/config.toml"
    original = config.read_text(encoding="utf-8") if config.exists() else ""
    shared = tomllib.loads(original).get("mcp_servers", {}).get("fx-agent", {}).get("env", {}).get("FX_AGENT_DATA_ROOT")
    data = args.data_root or (Path(shared) if shared else data_root())
    if not data.is_absolute() or not args.modules_dir.is_absolute():
        parser.error("data root and modules directory must be absolute")
    if args.bridge_port is not None and args.bridge_port not in args.ports:
        parser.error("Fixed bridge port must be included in discovery ports")
    module = args.modules_dir / "fx_maya.mod"
    if module.exists():
        headers = [line.split() for line in module.read_text(encoding="utf-8").splitlines() if line.startswith("+")]
        if not headers or any("fx_maya" not in header for header in headers):
            parser.error("Existing fx_maya.mod is not managed by this installer")
    writes = {config: maya_config(original, ROOT, data, args.ports),
              module: module_content(ROOT, data, args.bridge_port)}
    skill = Path.home() / ".agents/skills/maya-workflow"
    skill_target = ROOT / "skills/maya-workflow"
    if os.path.lexists(skill) and skill.resolve() != skill_target.resolve():
        parser.error("Maya workflow skill path is occupied by another installation")
    print(json.dumps({"apply": args.apply, "write": [str(p) for p in writes],
                      "skill": str(skill),
                      "data_root": str(data), "ports": args.ports,
                      "next": "Start any Maya version normally; the shared module userSetup loads the FX bridge and menu"}, ensure_ascii=False, indent=2))
    if not args.apply:
        return
    backup = Path.home() / ".fx-agent-install-backups" / datetime.now().strftime("maya-%Y%m%d-%H%M%S-%f")
    backup.mkdir(parents=True)
    snapshots = {path: path.read_bytes() if path.exists() else None for path in writes}
    for index, (path, content) in enumerate(snapshots.items()):
        if content is not None:
            (backup / (str(index) + "-" + path.name)).write_bytes(content)
    (backup / "manifest.json").write_text(json.dumps([str(p) for p in writes]), encoding="utf-8")
    created_skill = False
    try:
        if not os.path.lexists(skill):
            import _winapi

            skill.parent.mkdir(parents=True, exist_ok=True)
            _winapi.CreateJunction(str(skill_target), str(skill))
            created_skill = True
        for path, content in writes.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + ".fx-maya.tmp")
            try:
                with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                    stream.write(content)
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
    except Exception:
        for path, content in snapshots.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(content)
        if created_skill and skill.is_junction() and skill.resolve() == skill_target.resolve():
            os.rmdir(skill)  # Remove only this installer-created junction, never its target.
        raise
    print("Installed; backup: " + str(backup))


if __name__ == "__main__":
    main()
