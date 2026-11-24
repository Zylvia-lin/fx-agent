"""Stable runtime paths, independent of plugin installation and current directory."""

import os
from pathlib import Path

DEFAULT_HOUDINI_PORT = 18186
CHECKOUT_DATA = Path(__file__).resolve().parents[2] / "data"


def local_data_root() -> Path:
    return (Path.home() / ".fx-agent" / "cache").resolve()


def data_root() -> Path:
    """Read the configured absolute data path without creating any files."""
    return configured_data_root(os.getenv("FX_AGENT_DATA_ROOT"))


def configured_data_root(configured: str | None) -> Path:
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            raise ValueError("FX_AGENT_DATA_ROOT must be an absolute path")
        resolved = path.resolve()
        if not resolved.is_relative_to(CHECKOUT_DATA.resolve()):
            return resolved
    return local_data_root()


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
        resolved = path.resolve()
        if not resolved.is_relative_to(CHECKOUT_DATA.resolve()):
            return resolved
    return data_root()
