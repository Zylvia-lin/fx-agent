"""Rule-based admission and scoped recall; no hidden model calls or inferred facts."""

import hashlib
import json
import uuid
from contextlib import nullcontext
from datetime import UTC, datetime
from sqlite3 import Connection
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from fx_agent.archive import CaseArchive
from fx_agent.module_guides import refresh_guides
from fx_agent.modules import MODULE_MATCH_SQL, normalize_modules
from fx_agent.store import Identifier, Store, compact, scope_id


class Experience(BaseModel):
    """Codex supplies provenance and decision rationale at a production milestone."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    project_id: Identifier
    software: Literal["houdini", "nuke"] = "houdini"
    modules: list[str] = Field(
        default_factory=lambda: ["unclassified"],
        min_length=1,
        max_length=4,
        description="Functional module IDs from fx_get_module_catalog, e.g. sop, particles, pyro, flip, rbd. Classify new experience explicitly.",
    )
    shot_id: str = Field(default="", max_length=128)
    kind: Literal["preference", "workflow", "render", "lighting", "compositing", "troubleshooting"]
    title: str = Field(min_length=1, max_length=200)
    method: str = Field(min_length=1, max_length=6000)
    reason: str = Field(default="", max_length=3000)
    applies_when: str = Field(default="", max_length=2000)
    source_type: Literal["user", "observed", "inferred"] = "inferred"
    source_ref: str = Field(min_length=1, max_length=2000)
    project_dependent: bool = True
    case_id: str = ""
    supersedes: str = ""

    @field_validator("shot_id")
    @classmethod
    def valid_shot(cls, value: str) -> str:
        return scope_id.validate_python(value) if value else ""

    @model_validator(mode="after")
    def valid_modules(self):
        self.modules = normalize_modules(self.modules, self.software)
        return self


class MemoryStore(Store):
    """Share the versioned database while keeping admission separate from state."""

    def submit(
        self, *, _db: Connection | None = None, _origin_feedback_id: str = "", **values
    ) -> dict:
        """Return an explainable admission decision and retain provenance."""
        request = Experience(**values)
        if request.kind != "preference" and (not request.reason or not request.applies_when):
            return {"decision": "rejected", "reason": "missing_reusable_decision_or_conditions"}

        case = None
        if request.case_id:
            archive = CaseArchive(self.root)
            case = archive.get(request.case_id, request.project_id)
            if request.shot_id and case["shot_id"] != request.shot_id:
                raise ValueError("Case does not belong to the requested shot")
            if not archive.verify(request.case_id, request.project_id)["valid"]:
                raise ValueError("Case archive failed checksum validation")
            if request.project_dependent and not any(
                item["role"] == "project"
                and item["archive_path"].startswith(f"projects/{request.software}/")
                for item in case["files"]
            ):
                raise ValueError(
                    "Project-dependent experience requires an archived project for its software"
                )

        status, evidence, reason = "candidate", "unverified", "inference_requires_evidence"
        if request.project_dependent and case is None:
            reason = "project_archive_required"
        elif request.source_type == "user":
            status = "active"
            evidence = "user_reported_with_archive" if case else "user_reported"
            reason = "explicit_user_statement"
        elif (
            request.source_type == "observed"
            and case
            and any(item["role"] == "preview" for item in case["files"])
        ):
            status, evidence, reason = (
                "active",
                "codex_observation_with_archive",
                "observation_with_archived_output",
            )
        if request.supersedes and (request.source_type != "user" or status != "active"):
            raise ValueError("Only an admitted explicit user correction can supersede a memory")

        content = request.model_dump(exclude={"source_ref"})
        if request.software == "houdini":
            content.pop("software")  # Preserve content hashes of existing Houdini memories.
        if request.modules == ["unclassified"]:
            content.pop("modules")
        if _origin_feedback_id:
            content["origin_feedback_id"] = _origin_feedback_id
        digest = hashlib.sha256(compact(content).encode()).hexdigest()
        with self.connection() if _db is None else nullcontext(_db) as db:
            if _db is None:
                db.execute("BEGIN IMMEDIATE")
            duplicate = db.execute(
                "SELECT memory_id,status FROM memories WHERE project_id=? AND content_hash=?",
                (request.project_id, digest),
            ).fetchone()
            if duplicate:
                return {
                    "decision": "duplicate",
                    "memory_id": duplicate["memory_id"],
                    "status": duplicate["status"],
                    "reason": "same_scoped_content",
                }
            previous = None
            if request.supersedes:
                previous = db.execute(
                    "SELECT memory_id,status,shot_id,payload FROM memories "
                    "WHERE memory_id=? AND project_id=?",
                    (request.supersedes, request.project_id),
                ).fetchone()
                if previous is None:
                    raise ValueError("Correction target does not belong to the requested project")
                if previous["shot_id"] != request.shot_id:
                    raise ValueError("Correction must keep the same shot scope")
                if previous["status"] not in ("active", "candidate"):
                    raise ValueError("Correction target is not a current memory revision")
                old = json.loads(previous["payload"])
                old.setdefault("modules", ["unclassified"])
                if old.get("software", "houdini") != request.software:
                    raise ValueError("Correction must keep the same software scope")
                if old["modules"] == request.modules and all(
                    old[key] == value for key, value in content.items() if key != "supersedes"
                ):
                    raise ValueError("Correction must contain a new, distinct replacement")
            memory_id = uuid.uuid4().hex
            record = request.model_dump() | {
                "memory_id": memory_id,
                "created_at": datetime.now(UTC).isoformat(),
                "evidence_status": evidence,
                "visual_verified": False,
                "reproduction_status": "not_tested",
            }
            if _origin_feedback_id:
                record["origin_feedback_id"] = _origin_feedback_id
            search_text = " ".join(
                [request.title, request.method, request.reason, request.applies_when, request.kind]
            ).casefold()
            db.execute(
                "INSERT INTO memories VALUES (?,?,?,?,?,?,?)",
                (
                    memory_id,
                    request.project_id,
                    request.shot_id,
                    status,
                    digest,
                    compact(record),
                    search_text,
                ),
            )
            if previous:
                db.execute(
                    "UPDATE memories SET status='superseded' WHERE memory_id=?",
                    (request.supersedes,),
                )
        result = {
            "decision": "admitted" if status == "active" else "candidate",
            "memory_id": memory_id,
            "status": status,
            "reason": reason,
            "evidence_status": evidence,
        }
        if _db is None and request.supersedes:
            result["module_guides"] = refresh_guides(self.root, request.project_id)
        return result

    def get(self, memory_id: str, project_id: str) -> dict:
        """Read full history, including a superseded entry, in the project scope."""
        project_id = scope_id.validate_python(project_id)
        with self.connection() as db:
            row = db.execute(
                "SELECT payload,status FROM memories WHERE memory_id=? AND project_id=?",
                (memory_id, project_id),
            ).fetchone()
        if row is None:
            raise ValueError("Memory does not belong to the requested project or does not exist")
        result = json.loads(row["payload"]) | {"status": row["status"]}
        result.setdefault("software", "houdini")
        result.setdefault("modules", ["unclassified"])
        with self.connection() as db:
            related = db.execute(
                "SELECT duplicate_id FROM memory_aliases WHERE project_id=? AND canonical_id=? ORDER BY duplicate_id",
                (project_id, memory_id),
            ).fetchall()
        if related:
            result["related_memory_ids"] = [item["duplicate_id"] for item in related]
        return result

    def search(
        self,
        project_id: str,
        query: str,
        shot_id: str = "",
        include_candidates: bool = False,
        limit: int = 5,
        max_chars: int = 3000,
        software: Literal["houdini", "nuke"] = "houdini",
        module: str = "",
    ) -> dict:
        """Literal multilingual substring matching, scoped before ranking and expansion."""
        project_id = scope_id.validate_python(project_id)
        if software not in ("houdini", "nuke"):
            raise ValueError("Invalid software")
        selected_module = normalize_modules([module], software)[0] if module else ""
        if shot_id:
            shot_id = scope_id.validate_python(shot_id)
        if not 1 <= limit <= 20 or not 512 <= max_chars <= 12000 or len(query) > 200:
            raise ValueError("Invalid search limit, max_chars or query length")
        tokens = query.casefold().split()
        result = {"items": [], "budget_unit": "json_characters", "truncated": False}
        if not tokens and not selected_module:
            return result
        if len(tokens) > 16:
            raise ValueError("Search query has too many terms")
        clauses = [
            "project_id=?",
            "COALESCE(json_extract(payload,'$.software'),'houdini')=?",
            "status IN ('active','candidate')" if include_candidates else "status='active'",
        ]
        clauses.append(
            "NOT EXISTS (SELECT 1 FROM memory_aliases a JOIN memories c ON c.memory_id=a.canonical_id WHERE a.duplicate_id=memories.memory_id AND a.project_id=memories.project_id AND c.project_id=memories.project_id AND c.shot_id=memories.shot_id AND c.status='active')"
        )
        params = [project_id, software]
        if selected_module:
            clauses.append(MODULE_MATCH_SQL)
            params.append(selected_module)
        if shot_id:
            clauses.append("(shot_id='' OR shot_id=?)")
            params.append(shot_id)
        for token in tokens:
            clauses.append("instr(search_text,?)>0")
            params.append(token)
        with self.connection() as db:
            rows = db.execute(
                "SELECT payload,status FROM memories WHERE "
                + " AND ".join(clauses)
                + " ORDER BY rowid DESC LIMIT ?",
                [*params, limit + 1],
            ).fetchall()
        for row in rows[:limit]:
            record = json.loads(row["payload"]) | {"status": row["status"]}
            record.setdefault("software", "houdini")
            record.setdefault("modules", ["unclassified"])
            card = {
                key: record[key]
                for key in (
                    "memory_id",
                    "software",
                    "modules",
                    "title",
                    "kind",
                    "status",
                    "evidence_status",
                    "case_id",
                )
            } | {"summary": record["method"][:240]}
            result["items"].append(card)
            if len(compact(result)) > max_chars:
                result["items"].pop()
                result["truncated"] = True
                break
        result["truncated"] = result["truncated"] or len(rows) > limit
        return result
