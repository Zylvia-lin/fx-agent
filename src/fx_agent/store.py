"""Transactional, project-scoped production state in a local SQLite database."""

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter

Identifier = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=128, pattern=r"^[^\x00-\x1f\x7f]+$"
    ),
]
Step = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
scope_id = TypeAdapter(Identifier)


def compact(value: object) -> str:
    """Serialize exactly the text used for context budget accounting."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class CheckpointInput(BaseModel):
    """A checkpoint records reported progress, not a claim of live validation."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    project_id: Identifier
    software: Literal["houdini", "nuke"] = "houdini"
    shot_id: Identifier
    summary: str = Field(min_length=1, max_length=10000)
    scene_path: str = Field(min_length=1, max_length=4096)
    next_steps: list[Step] = Field(min_length=1, max_length=20)
    expected_revision: int = Field(ge=0)


class Store:
    """Open connections per operation, allowing tools to run in worker threads."""

    SCHEMA_VERSION = 4

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "memory.sqlite3"
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            schema = db.execute("PRAGMA user_version").fetchone()[0]
            if schema not in (0, 1, 2, 3, self.SCHEMA_VERSION):
                raise ValueError(f"Unsupported database schema {schema}; refusing to overwrite")
            db.execute(
                "CREATE TABLE IF NOT EXISTS checkpoints ("
                "project_id TEXT NOT NULL, shot_id TEXT NOT NULL, revision INTEGER NOT NULL, "
                "payload TEXT NOT NULL, PRIMARY KEY(project_id, shot_id))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS memories ("
                "memory_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, shot_id TEXT NOT NULL, "
                "status TEXT NOT NULL, content_hash TEXT NOT NULL, payload TEXT NOT NULL, "
                "search_text TEXT NOT NULL, UNIQUE(project_id,content_hash))"
            )
            db.execute("CREATE INDEX IF NOT EXISTS memories_scope ON memories(project_id,status)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS user_rules (scope TEXT,rule_key TEXT,revision INTEGER,payload TEXT NOT NULL,PRIMARY KEY(scope,rule_key,revision))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS learning_feedback (feedback_id TEXT PRIMARY KEY,project_id TEXT,shot_id TEXT,attempt_id TEXT,revision INTEGER,payload TEXT NOT NULL,UNIQUE(project_id,shot_id,attempt_id,revision))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS learning_results (feedback_id TEXT PRIMARY KEY,memory_id TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS memory_aliases (duplicate_id TEXT PRIMARY KEY,canonical_id TEXT NOT NULL,project_id TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS dream_runs (run_id TEXT PRIMARY KEY,project_id TEXT,snapshot TEXT,mode TEXT,payload TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS render_issues (issue_id TEXT,project_id TEXT,shot_id TEXT,revision INTEGER,payload TEXT NOT NULL,PRIMARY KEY(issue_id,revision))"
            )
            for table, columns, definition in (
                (
                    "checkpoints",
                    "project_id,shot_id,revision,payload",
                    "project_id TEXT NOT NULL,shot_id TEXT NOT NULL,revision INTEGER NOT NULL,payload TEXT NOT NULL,software TEXT NOT NULL,PRIMARY KEY(project_id,shot_id,software)",
                ),
                (
                    "learning_feedback",
                    "feedback_id,project_id,shot_id,attempt_id,revision,payload",
                    "feedback_id TEXT PRIMARY KEY,project_id TEXT,shot_id TEXT,attempt_id TEXT,revision INTEGER,payload TEXT NOT NULL,software TEXT NOT NULL,UNIQUE(project_id,shot_id,attempt_id,software,revision)",
                ),
            ):
                if "software" not in {row[1] for row in db.execute(f"PRAGMA table_info({table})")}:
                    db.execute(f"ALTER TABLE {table} RENAME TO {table}_v3")
                    db.execute(f"CREATE TABLE {table} ({definition})")
                    db.execute(f"INSERT INTO {table} SELECT {columns},'houdini' FROM {table}_v3")
                    db.execute(f"DROP TABLE {table}_v3")
            db.execute(f"PRAGMA user_version={self.SCHEMA_VERSION}")

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Commit successful operations, roll back errors, always close handles."""
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            with db:
                yield db

    def save_checkpoint(self, **values: Any) -> dict:
        """Compare revisions within the same write transaction before updating."""
        checkpoint = CheckpointInput(**values)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT revision FROM checkpoints WHERE project_id=? AND shot_id=? AND software=?",
                (checkpoint.project_id, checkpoint.shot_id, checkpoint.software),
            ).fetchone()
            current = previous["revision"] if previous else 0
            if current != checkpoint.expected_revision:
                raise ValueError(f"Checkpoint revision conflict: expected {current}")
            payload = checkpoint.model_dump(exclude={"expected_revision"}) | {
                "revision": current + 1,
                "updated_at": datetime.now(UTC).isoformat(),
            }
            db.execute(
                "INSERT INTO checkpoints VALUES (?,?,?,?,?) ON CONFLICT(project_id,shot_id,software) "
                "DO UPDATE SET revision=excluded.revision,payload=excluded.payload",
                (
                    checkpoint.project_id,
                    checkpoint.shot_id,
                    current + 1,
                    compact(payload),
                    checkpoint.software,
                ),
            )
        return payload

    def get_checkpoint(
        self, project_id: str, shot_id: str, software: str = "houdini"
    ) -> dict | None:
        """Read the full checkpoint; its live scene must be rechecked on resume."""
        project_id, shot_id = (
            scope_id.validate_python(project_id),
            scope_id.validate_python(shot_id),
        )
        if software not in ("houdini", "nuke"):
            raise ValueError("Invalid software")
        with self.connection() as db:
            row = db.execute(
                "SELECT payload FROM checkpoints WHERE project_id=? AND shot_id=? AND software=?",
                (project_id, shot_id, software),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def get_context(
        self,
        project_id: str,
        shot_id: str,
        since_hash: str = "",
        max_chars: int = 6000,
        software: str = "houdini",
    ) -> dict:
        """Return bounded JSON characters, explicitly not an exact token count."""
        if not 768 <= max_chars <= 12000:
            raise ValueError("max_chars must be between 768 and 12000")
        # Validate identifiers even when the requested project does not exist.
        project_id, shot_id = (
            scope_id.validate_python(project_id),
            scope_id.validate_python(shot_id),
        )
        checkpoint = self.get_checkpoint(project_id, shot_id, software)
        digest = hashlib.sha256(
            compact([project_id, shot_id, software, checkpoint]).encode()
        ).hexdigest()
        response = {
            "project_id": project_id,
            "shot_id": shot_id,
            "content_hash": digest,
            "unchanged": digest == since_hash,
            "requires_live_verification": True,
            "budget_unit": "json_characters",
        }
        if len(compact(response | {"checkpoint": None, "truncated": False})) > max_chars:
            raise ValueError("max_chars too small for the required scope identifiers")
        if digest == since_hash:
            return response
        response.update(checkpoint=checkpoint, truncated=False)
        if checkpoint:
            while len(compact(response)) > max_chars:
                response["truncated"] = True
                if checkpoint["next_steps"]:
                    checkpoint["next_steps"].pop()
                elif checkpoint["summary"]:
                    checkpoint["summary"] = checkpoint["summary"][: len(checkpoint["summary"]) // 2]
                elif "scene_path" in checkpoint:
                    del checkpoint["scene_path"]
                elif "project_id" in checkpoint:
                    # Identity and revision still provide an exact full-fetch key.
                    del checkpoint["project_id"]
                    del checkpoint["shot_id"]
                else:
                    raise ValueError("max_chars too small for identifiers and checkpoint revision")
        return response
