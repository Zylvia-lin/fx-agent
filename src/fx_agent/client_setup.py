"""Preview or install workstation routing without storing tokens in Codex TOML."""

import argparse
import json
import os
import re
import shutil
import tomllib
from datetime import UTC, datetime
from pathlib import Path


def configure_codex(original: str, client: Path, local: Path, data: Path) -> str:
    before = tomllib.loads(original)
    values = {
        "FX_AGENT_CLIENT_CONFIG": str(client),
        "FX_AGENT_LOCAL_ROOT": str(local),
        "FX_AGENT_DATA_ROOT": str(data),
    }
    result = original
    expected = tomllib.loads(original)
    count = 0
    for name in ("fx-agent", "fx-maya", "fx-nuke"):
        server = before.get("mcp_servers", {}).get(name)
        if not server:
            continue
        count += 1
        expected["mcp_servers"][name].setdefault("env", {}).update(values)
        pattern = (
            rf"(?m)^\[mcp_servers\.{re.escape(name)}\.env\][^\r\n]*\r?\n(?:(?!^\[).*(?:\n|$))*"
        )
        found = re.search(pattern, result)
        if not found:
            result = result.rstrip() + f"\n\n[mcp_servers.{name}.env]\n"
            found = re.search(pattern, result)
        block = found.group()
        for key, value in values.items():
            replacement = key + " = " + json.dumps(value, ensure_ascii=False) + "\n"
            key_pattern = rf"(?m)^{key}\s*=.*(?:\n|$)"
            if re.search(key_pattern, block):
                block = re.sub(key_pattern, lambda match, line=replacement: line, block)
            else:
                block = block.rstrip() + "\n" + replacement
        result = result[: found.start()] + block + result[found.end() :]
    if not count:
        raise ValueError("Install FX Agent MCP servers before configuring shared data")
    if tomllib.loads(result) != expected:
        raise ValueError("Unsupported Codex configuration; original preserved")
    return result


def configure_maya(original: str, local: Path) -> str:
    value = local.as_posix()
    if "\n" in value or "\r" in value:
        raise ValueError("Local path contains newlines")
    content = re.sub(r"(?m)^FX_AGENT_LOCAL_ROOT\s*=.*\r?\n?", "", original)
    return re.sub(
        r"(?m)^(FX_AGENT_DATA_ROOT\s*=.*)$",
        lambda match: match[0] + "\nFX_AGENT_LOCAL_ROOT = " + value,
        content,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-config", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--local-root", type=Path, default=Path.home() / ".fx-agent/local-state")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if any(not p.is_absolute() for p in (args.client_config, args.data_root, args.local_root)):
        parser.error("All paths must be absolute")
    config = json.loads(args.client_config.read_text(encoding="utf-8"))
    if not (config.get("url") or config.get("server_ip")) or len(config.get("api_key", config.get("token", ""))) < 32:
        parser.error("Invalid private client configuration")
    # Verify connectivity before changing any existing workstation settings.
    from fx_agent.multidevice import RemoteClient

    connection = RemoteClient(config_path=args.client_config)
    status = connection.status()
    if status.get("protocol") != 1:
        parser.error("Unsupported data service protocol")
    codex = Path.home() / ".codex/config.toml"
    writes = {
        codex: configure_codex(
            codex.read_text(encoding="utf-8"), args.client_config, args.local_root, args.data_root
        )
    }
    maya = Path.home() / "Documents/maya/modules/fx_maya.mod"
    if maya.exists():
        writes[maya] = configure_maya(maya.read_text(encoding="utf-8"), args.local_root)
    print(
        json.dumps(
            {
                "apply": args.apply,
                "device_id": status["device_id"],
                "write": [str(path) for path in writes],
                "data_root": str(args.data_root),
            },
            ensure_ascii=False,
        )
    )
    if not args.apply:
        return
    backup = (
        Path.home()
        / ".fx-agent-install-backups"
        / ("shared-data-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f"))
    )
    backup.mkdir(parents=True)
    for index, path in enumerate(writes):
        shutil.copy2(path, backup / (str(index) + "-" + path.name))
    for path, text in writes.items():
        temporary = path.with_name(path.name + ".shared-data.tmp")
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        os.replace(temporary, path)
    if os.name == "nt":
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE
        ) as key:
            for name, path in (
                ("FX_AGENT_CLIENT_CONFIG", args.client_config),
                ("FX_AGENT_LOCAL_ROOT", args.local_root),
                ("FX_AGENT_DATA_ROOT", args.data_root),
            ):
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, str(path))
        import ctypes

        result = ctypes.c_size_t()
        ctypes.windll.user32.SendMessageTimeoutW(
            0xFFFF, 0x001A, 0, "Environment", 0x0002, 1000, ctypes.byref(result)
        )
    print("Configured shared knowledge; restart Codex and DCC applications. Backup: " + str(backup))


if __name__ == "__main__":
    main()
