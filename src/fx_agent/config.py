"""Stable runtime paths, independent of plugin installation and current directory."""

import os
from pathlib import Path

DEFAULT_HOUDINI_PORT = 18186


def data_root() -> Path:
    """Read the configured absolute data path without creating any files."""
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
    """Return the local SQLite database path under the configured data root."""
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
