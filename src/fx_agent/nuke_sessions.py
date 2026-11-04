"""Read local Nuke bridge advertisements; selection must verify them on the wire."""

import json
import time
from pathlib import Path


def list_sessions(root: Path, current_port=None, current_id=None) -> list[dict]:
    sessions = []
    for path in (root / "nuke_sessions").glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if (
                not isinstance(record["pid"], int)
                or record["pid"] <= 0
                or not isinstance(record["port"], int)
                or not 1 <= record["port"] <= 65535
                or not isinstance(record["session_id"], str)
                or not record["session_id"]
                or not isinstance(record["script"], str)
                or not 0 <= time.time() - record["updated_at"] < 30
            ):
                continue
        except (OSError, ValueError, KeyError, TypeError):
            continue
        record["current"] = record["port"] == current_port and record["session_id"] == current_id
        record["identity_verified"] = False
        sessions.append(record)
    return sorted(sessions, key=lambda record: (record["port"], record["pid"]))
