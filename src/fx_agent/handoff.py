"""Evidence-based Nuke-to-Houdini issue records; no software is invoked here."""

import json
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from fx_agent.store import Identifier, Store, compact, scope_id

Evidence = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
Status = Literal["open", "investigating", "awaiting_nuke_review", "closed"]


class RenderIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    project_id: Identifier
    shot_id: Identifier
    render_path: str = Field(min_length=1, max_length=4096)
    first_frame: int
    last_frame: int
    expected: str = Field(min_length=1, max_length=2000)
    observed: str = Field(min_length=1, max_length=2000)
    evidence: list[Evidence] = Field(min_length=1, max_length=10)
    source_ref: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def frame_order(self):
        if self.first_frame > self.last_frame:
            raise ValueError("Invalid frame range")
        return self


class IssueUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    actor: Literal["houdini", "nuke"]
    status: Status
    summary: str = Field(min_length=1, max_length=2000)
    evidence: list[Evidence] = Field(min_length=1, max_length=10)
    expected_revision: int = Field(ge=1)


class RenderIssues(Store):
    """Both DCC workflows share a database but have separate handoff responsibilities."""

    def report(self, **values) -> dict:
        record = RenderIssue(**values).model_dump() | {
            "issue_id": uuid.uuid4().hex,
            "revision": 1,
            "status": "open",
            "actor": "nuke",
            "summary": "Nuke 请求核查渲染素材",
            "updated_at": datetime.now(UTC).isoformat(),
            "evidence_verified_by_service": False,
        }
        with self.connection() as db:
            db.execute(
                "INSERT INTO render_issues VALUES (?,?,?,?,?)",
                (record["issue_id"], record["project_id"], record["shot_id"], 1, compact(record)),
            )
        return record

    def get(self, issue_id: str, project_id: str, revision: int = 0) -> dict:
        project_id = scope_id.validate_python(project_id)
        if revision < 0:
            raise ValueError("Invalid revision")
        with self.connection() as db:
            rows = db.execute(
                "SELECT payload FROM render_issues WHERE issue_id=? AND project_id=? AND (?=0 OR revision<=?) ORDER BY revision DESC LIMIT 20",
                (issue_id, project_id, revision, revision),
            ).fetchall()
        if not rows:
            raise ValueError("Issue does not belong to this project")
        latest = json.loads(rows[0][0])
        if revision and latest["revision"] != revision:
            raise ValueError("Issue revision does not exist")
        return latest | {
            "history": [
                {key: record[key] for key in ("revision", "actor", "status", "updated_at")}
                | {"summary": record["summary"][:160]}
                for record in (json.loads(row[0]) for row in reversed(rows))
            ],
            "history_may_be_truncated": latest["revision"] > len(rows),
            "older_revision": max(0, latest["revision"] - len(rows)),
        }

    def update(
        self,
        issue_id: str,
        project_id: str,
        actor: str,
        status: str,
        summary: str,
        evidence: list[str],
        expected_revision: int,
    ) -> dict:
        request = IssueUpdate(
            actor=actor,
            status=status,
            summary=summary,
            evidence=evidence,
            expected_revision=expected_revision,
        )
        project_id = scope_id.validate_python(project_id)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT payload FROM render_issues WHERE issue_id=? AND project_id=? ORDER BY revision DESC LIMIT 1",
                (issue_id, project_id),
            ).fetchone()
            if not row:
                raise ValueError("Issue does not belong to this project")
            previous = json.loads(row[0])
            if previous["revision"] != request.expected_revision:
                raise ValueError("Issue revision conflict; read the latest version")
            transitions = {
                "open": {("houdini", "investigating"), ("houdini", "awaiting_nuke_review")},
                "investigating": {
                    ("houdini", "investigating"),
                    ("houdini", "awaiting_nuke_review"),
                },
                "awaiting_nuke_review": {("nuke", "closed"), ("nuke", "open")},
                "closed": {("nuke", "open")},
            }
            if (actor, status) not in transitions[previous["status"]]:
                raise ValueError("Invalid issue transition or software responsibility")
            record = (
                previous
                | request.model_dump(exclude={"expected_revision"})
                | {
                    "revision": previous["revision"] + 1,
                    "updated_at": datetime.now(UTC).isoformat(),
                }
            )
            db.execute(
                "INSERT INTO render_issues VALUES (?,?,?,?,?)",
                (issue_id, project_id, record["shot_id"], record["revision"], compact(record)),
            )
        return record

    def list(
        self,
        project_id: str,
        shot_id: str,
        max_chars: int = 3000,
        before_cursor: int = 0,
        status: str = "all",
    ) -> dict:
        project_id, shot_id = (
            scope_id.validate_python(project_id),
            scope_id.validate_python(shot_id),
        )
        if not 1024 <= max_chars <= 12000 or before_cursor < 0:
            raise ValueError("max_chars must be 1024..12000")
        if status not in ("all", "open", "investigating", "awaiting_nuke_review", "closed"):
            raise ValueError("Invalid status")
        with self.connection() as db:
            rows = db.execute(
                "SELECT rowid AS cursor,payload FROM render_issues r WHERE project_id=? AND shot_id=? AND revision=(SELECT MAX(revision) FROM render_issues x WHERE x.issue_id=r.issue_id) AND (?=0 OR rowid<?) AND (?='all' OR json_extract(payload,'$.status')=?) ORDER BY rowid DESC LIMIT 51",
                (project_id, shot_id, before_cursor, before_cursor, status, status),
            ).fetchall()
        result = {
            "items": [],
            "truncated": len(rows) > 50,
            "budget_unit": "json_characters",
            "next_cursor": 0,
        }
        for row in rows[:50]:
            record = json.loads(row["payload"])
            result["items"].append(
                {key: record[key] for key in ("issue_id", "revision", "status", "actor")}
                | {"summary": record["summary"][:120]}
            )
            previous_cursor = result["next_cursor"]
            result["next_cursor"] = row["cursor"]
            if len(compact(result)) > max_chars:
                result["items"].pop()
                result["next_cursor"] = previous_cursor
                result["truncated"] = True
                break
        return result
