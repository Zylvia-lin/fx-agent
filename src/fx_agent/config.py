"""Stable runtime paths, independent of plugin installation and current directory."""

import os
from pathlib import Path

DEFAULT_HOUDINI_PORT = 18186


def data_root() -> Path:
    """Read the configured absolute data path without creating any files."""
    from fx_agent.data_context import current_scope

    scope = current_scope()
    if scope:
        return scope[0]
    configured = os.getenv("FX_AGENT_DATA_ROOT")
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            raise ValueError("FX_AGENT_DATA_ROOT must be an absolute path")
        return path.resolve()
    # Source checkout installation: keep runtime state beside code for migration.
    # Packaged distributions should pin FX_AGENT_DATA_ROOT during installation.
    return Path(__file__).resolve().parents[2] / "data"


def database_path(root: Path) -> Path:
    """Only the service's authoritative root uses its separate local database."""
    from fx_agent.data_context import current_scope

    scope = current_scope()
    if scope and Path(root).resolve() == scope[0].resolve():
        return scope[1] / "memory.sqlite3"
    return Path(root) / "memory.sqlite3"


def session_root() -> Path:
    """Local DCC registries never belong in a shared knowledge directory."""
    configured = os.getenv("FX_AGENT_LOCAL_ROOT")
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            raise ValueError("FX_AGENT_LOCAL_ROOT must be absolute")
        return path.resolve()
    return data_root()
