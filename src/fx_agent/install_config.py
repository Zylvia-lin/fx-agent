"""Minimal, verified TOML server registration shared by local installers."""

import re
import tomllib


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
    result = (retained + "\n\n" if retained else "") + block
    tomllib.loads(result)
    return result
