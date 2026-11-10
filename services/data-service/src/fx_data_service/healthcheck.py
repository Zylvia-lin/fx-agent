"""Authenticated container readiness probe without printing credentials."""

import os
from pathlib import Path
from urllib.request import Request, urlopen

path = os.getenv("FX_AGENT_API_KEY_FILE")
key = Path(path).read_text(encoding="utf-8").strip() if path else os.environ["FX_AGENT_API_KEY"]
port = os.getenv("FX_AGENT_PORT", "18790")
request = Request(
    f"http://127.0.0.1:{port}/internal/health", headers={"Authorization": "Bearer " + key}
)
with urlopen(request, timeout=4) as response:
    if response.status != 200:
        raise SystemExit(1)
