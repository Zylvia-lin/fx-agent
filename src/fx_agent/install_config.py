"""Minimal, verified TOML server registration shared by local installers."""

import json
import re
import tomllib

DATA_ENVIRONMENT = (
    "FX_AGENT_DATA_MODE", "FX_AGENT_SERVER_IP", "FX_AGENT_SERVER_PORT", "FX_AGENT_API_KEY",
    "FX_AGENT_CA_FILE", "FX_AGENT_LOCAL_ROOT", "FX_AGENT_CLIENT_CONFIG", "FX_AGENT_SERVICE_URL",
    "FX_AGENT_SERVICE_TOKEN", "FX_AGENT_UPLOAD_FILES", "FX_AGENT_DEVICE_ID", "FX_AGENT_DATA_PROJECT",
)


def forward_data_environment(original: str, previous: dict | None = None) -> str:
    """Whitelist variable names for stdio hosts; never copy secret values."""
    expected = tomllib.loads(original)
    result = original
    for name in ("fx-agent", "fx-maya", "fx-nuke"):
        server = expected.get("mcp_servers", {}).get(name)
        if server is None:
            continue
        names = list((previous or {}).get(name, {}).get("env_vars", []))
        names.extend(value for value in server.get("env_vars", []) if value not in names)
        names.extend(value for value in DATA_ENVIRONMENT if value not in names)
        alternatives = "|".join(re.escape(value) for value in (name, '"' + name + '"', "'" + name + "'"))
        match = re.search(rf"(?m)^\[mcp_servers\.(?:{alternatives})\][^\r\n]*\r?\n", result)
        if not match:
            raise ValueError("Unsupported FX MCP table format; original preserved")
        following = re.search(r"(?m)^\[", result[match.end():])
        end = match.end() + following.start() if following else len(result)
        body = result[match.end():end]
        body = re.sub(r"(?ms)^env_vars\s*=\s*\[.*?\][^\S\r\n]*(?:#[^\r\n]*)?(?:\r?\n|$)", "", body, count=1)
        def literal(value):
            if isinstance(value, dict):
                return "{ " + ", ".join(json.dumps(k) + " = " + json.dumps(v) for k, v in value.items()) + " }"
            return json.dumps(value)
        line = "env_vars = [" + ", ".join(literal(value) for value in names) + "]\n"
        result = result[:match.end()] + line + body + result[end:]
        server["env_vars"] = names
    if tomllib.loads(result) != expected:
        raise ValueError("Unexpected MCP settings changed; original preserved")
    return result


def replace_servers(original: str, names: set[str], block: str) -> str:
    """Replace explicit server tables only, validating every unrelated parsed value."""
    before = tomllib.loads(original)
    kept = []
    dropping = False
    alternatives = "|".join(
        re.escape(value) for name in names for value in (name, f'"{name}"', f"'{name}'")
    )
    for line in original.splitlines(keepends=True):
        if line.lstrip().startswith("["):
            header = line.strip().split("#", 1)[0].strip()
            dropping = bool(
                re.fullmatch(
                    rf"\[mcp_servers\.(?:{alternatives})(?:\.env)?\]",
                    header,
                )
            )
        if not dropping:
            kept.append(line)
    retained = "".join(kept).rstrip()
    parsed = tomllib.loads(retained)
    expected_servers = {k: v for k, v in before.get("mcp_servers", {}).items() if k not in names}
    expected = {k: v for k, v in before.items() if k != "mcp_servers"}
    if expected_servers:
        expected["mcp_servers"] = expected_servers
    if parsed.get("mcp_servers") == {}:
        parsed.pop("mcp_servers")
    if parsed != expected:
        raise ValueError("不支持的 FX 配置格式，已停止；请保留原配置并手动迁移。")
    result = (retained + "\n\n" if retained else "") + forward_data_environment(block, before.get("mcp_servers", {}))
    tomllib.loads(result)
    return result
