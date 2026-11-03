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
