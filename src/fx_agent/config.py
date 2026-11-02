"""Stable runtime paths, independent of plugin installation and current directory."""

import os
from pathlib import Path


def data_root() -> Path:
    """Read the configured absolute data path without creating any files."""
    configured = os.getenv("FX_AGENT_DATA_ROOT")
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            raise ValueError("FX_AGENT_DATA_ROOT must be an absolute path")
        return path.resolve()
    # Codex can virtualize LOCALAPPDATA per execution. A profile-relative default
    # survives those runs; an explicit absolute root is preferred for installation.
    return (Path.home() / ".fx-agent").resolve()
