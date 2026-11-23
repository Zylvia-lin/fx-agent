"""Read the published catalog from the local VFX Template Library desktop app."""

import asyncio
import http.client
import json
import logging
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlencode

_SOURCE = "vfx-template-library"
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_DEFAULT_MAX_CHARS = 12000
_MIN_MAX_CHARS = 256
_MAX_MAX_CHARS = 60000
_log = logging.getLogger(__name__)


def _catalog_dir() -> Path | None:
    state = os.environ.get("VFX_USER_DATA")
    if state:
        return Path(state)
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / _SOURCE
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / _SOURCE
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / _SOURCE
    return Path.home() / ".config" / _SOURCE


def _catalog_connection():
    try:
        directory = _catalog_dir()
        if directory is None:
            return None
        connection = json.loads((directory / "fx-agent-catalog.json").read_text(encoding="utf-8"))
        port, token = connection["port"], connection["token"]
        if (
            type(port) is int
            and 1 <= port <= 65535
            and isinstance(token, str)
            and re.fullmatch(r"[0-9a-f-]{72}", token)
        ):
            return port, token
        _log.debug("Ignoring malformed fx-agent-catalog.json connection metadata")
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def _request(client, route, token):
    client.request("GET", route, headers={"Authorization": f"Bearer {token}"})
    response = client.getresponse()
    raw = response.read(_MAX_RESPONSE_BYTES + 1)
    if response.status != 200 or len(raw) > _MAX_RESPONSE_BYTES:
        return {}
    result = json.loads(raw)
    if isinstance(result, dict) and result.get("source") == _SOURCE:
        return result
    return {}


def _trim_budget(payload: dict, max_chars: int) -> dict:
    trimmed = dict(payload)
    items = list(trimmed.get("items") or []) if isinstance(trimmed.get("items"), list) else None
    if items is None:
        return trimmed
    trimmed["items"] = items
    truncated = bool(trimmed.get("truncated", False))
    while len(json.dumps(trimmed, ensure_ascii=False)) > max_chars and len(items) > 1:
        items.pop()
        truncated = True
    if truncated:
        trimmed["truncated"] = True
        trimmed["returned_count"] = len(items)
    return trimmed


def _search(query: str, offset: int, limit: int, max_chars: int = _DEFAULT_MAX_CHARS) -> dict:
    unavailable = {"source": _SOURCE, "available": False}
    connection = _catalog_connection()
    if connection is None:
        return unavailable

    port, token = connection
    client = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        status = _request(client, "/integrations/fx-agent/status", token)
        if status.get("ready") is not True:
            return unavailable
        params = urlencode({"query": query, "offset": offset, "limit": limit})
        result = _request(client, "/integrations/fx-agent/templates?" + params, token)
        if not result:
            return unavailable
        return _trim_budget({**result, "available": True}, max_chars)
    except (OSError, ValueError, http.client.HTTPException):
        return unavailable
    finally:
        client.close()


async def fx_search_published_templates(
    query: str = "",
    offset: int = 0,
    limit: int = 10,
    max_chars: int = _DEFAULT_MAX_CHARS,
) -> dict:
    """按名称或标签查询本机特效模板库发布目录。调用时自动检查桌面端是否可用。available=false 表示没有可用的本机模板库，跳过本来源并继续其他查询；仅 available=true 时读取 items 和 nextOffset。"""
    query = query.strip()
    if (
        len(query) > 100
        or not 0 <= offset <= 2147483647
        or not 1 <= limit <= 25
        or not _MIN_MAX_CHARS <= max_chars <= _MAX_MAX_CHARS
    ):
        raise ValueError(
            "查询最多 100 字符，offset 必须为非负整数，limit 必须在 1 到 25 之间，max_chars 必须在 256 到 60000 之间。"
        )
    return await asyncio.to_thread(_search, query, offset, limit, max_chars)
