"""Launch GUI Maya with the existing FX bridge, without desktop automation."""

import subprocess
from pathlib import Path


def start_maya(executable: str) -> dict:
    """Start exactly one process; a PID does not establish a bridge connection."""
    path = Path(executable)
    if not path.is_absolute() or path.name.lower() != "maya.exe" or not path.is_file():
        raise ValueError("Provide an existing absolute path to maya.exe")
    # Match a normal desktop launch. The shared Maya module starts the bridge.
    process = subprocess.Popen([str(path)], shell=False)
    return {"status": "launched", "pid": process.pid, "executable": str(path),
            "bridge_ready": False,
            "next": "FX bridge starts through the installed shared Maya module. "
                    "Wait for startup, list_maya_sessions, then explicitly connect_maya. "
                    "If unavailable, inspect startup errors; do not launch again automatically."}
