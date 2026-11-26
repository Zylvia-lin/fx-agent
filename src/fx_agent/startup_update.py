"""Best-effort GitHub fast-forward before loading the MCP server."""

import logging
import os
import shutil
import subprocess
from pathlib import Path

_LOG = logging.getLogger(__name__)
_RESTARTED = "FX_AGENT_UPDATE_RESTARTED"
_OFFICIAL_ORIGINS = {
    "https://github.com/zylvia-lin/fx-agent",
    "git@github.com:zylvia-lin/fx-agent",
    "ssh://git@github.com/zylvia-lin/fx-agent",
}


def update_checkout(root: Path) -> bool:
    """Advance only a clean, tracking main checkout of the official repository."""
    env = os.environ | {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}

    def git(*args, timeout=5):
        return subprocess.run(
            ["git", *args], cwd=root, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", check=True, timeout=timeout,
        ).stdout.strip()

    try:
        if git("symbolic-ref", "--quiet", "--short", "HEAD") != "main":
            return False
        if git("rev-parse", "--abbrev-ref", "@{upstream}") != "origin/main":
            return False
        origin = git("remote", "get-url", "origin").lower().rstrip("/").removesuffix(".git")
        if origin not in _OFFICIAL_ORIGINS:
            return False
        if git("status", "--porcelain=v1", "--untracked-files=normal"):
            _LOG.info("FX Agent update skipped: local checkout has changes")
            return False
        head = git("rev-parse", "HEAD")
        git("fetch", "--no-tags", "origin", "refs/heads/main:refs/remotes/origin/main", timeout=10)
        upstream = git("rev-parse", "refs/remotes/origin/main")
        if head == upstream:
            return False
        git("merge-base", "--is-ancestor", head, upstream)
        if git("rev-parse", "HEAD") != head or git("status", "--porcelain=v1", "--untracked-files=normal"):
            _LOG.info("FX Agent update skipped: local checkout changed during fetch")
            return False
        git("merge", "--ff-only", "--no-edit", upstream, timeout=15)
    except subprocess.CalledProcessError as exc:
        _LOG.info("FX Agent update skipped: git %s exited with %s", exc.cmd[1], exc.returncode)
        return False
    except (OSError, subprocess.TimeoutExpired) as exc:
        _LOG.info("FX Agent update skipped: %s", exc)
        return False
    _LOG.info("FX Agent updated from %s to %s", head[:12], upstream[:12])
    return True


def restart_if_updated(root: Path) -> None:
    """Restart on the new checkout so uv can sync the updated locked dependencies."""
    if os.environ.pop(_RESTARTED, "") or os.environ.get("FX_AGENT_AUTO_UPDATE") == "0":
        return
    if not (root / ".git").is_dir() or not (root / "uv.lock").is_file():
        return
    uv = shutil.which("uv")
    if uv is None or not update_checkout(root):
        return
    result = subprocess.run(
        [uv, "run", "--directory", str(root), "--frozen", "--no-dev", "fx-agent"],
        env=os.environ | {_RESTARTED: "1"},
    )
    raise SystemExit(result.returncode)
