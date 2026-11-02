"""Dialogue rules, revisioned correction traces and reversible memory consolidation."""

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fx_agent.archive import CaseArchive
from fx_agent.memory import MemoryStore
from fx_agent.module_guides import refresh_guides
from fx_agent.modules import MODULE_MATCH_SQL, normalize_modules
from fx_agent.production import production_rules
from fx_agent.store import Identifier, compact, scope_id


class UserRule(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    rule_key: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    title: str = Field(min_length=1, max_length=200)
    statement: str = Field(min_length=1, max_length=3000)
    user_quote: str = Field(min_length=1, max_length=2000)
    source_ref: str = Field(min_length=1, max_length=512)
    scope: Literal["global", "project"]
    project_id: str = ""
    expected_revision: int = Field(ge=0)


class Feedback(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    project_id: Identifier
    software: Literal["houdini", "nuke"] = "houdini"
    modules: list[str] = Field(
        default_factory=lambda: ["unclassified"],
        min_length=1,
        max_length=4,
        description="Functional module IDs from fx_get_module_catalog; final learning inherits these tags.",
    )
    shot_id: Identifier
    effect_type: Identifier
    attempt_id: Identifier
    problem: str = Field(min_length=1, max_length=1000)
    change: str = Field(min_length=1, max_length=3000)
    reason: str = Field(min_length=1, max_length=1500)
    result: str = Field(min_length=1, max_length=1500)
    outcome: Literal["pending", "improved", "accepted", "failed"]
    user_quote: str = Field(min_length=1, max_length=2000)
    source_ref: str = Field(min_length=1, max_length=512)
    case_id: str = ""
    expected_revision: int = Field(ge=0)

    @model_validator(mode="after")
    def valid_modules(self):
        self.modules = normalize_modules(self.modules, self.software)
        return self


class LearningStore(MemoryStore):
    """Codex extracts statements; the service enforces scope, evidence and revisions."""

    def record_rule(self, **values) -> dict:
        request = UserRule(**values)
        if request.scope == "project":
            project = scope_id.validate_python(request.project_id)
            scope = "project:" + project
        else:
            if request.project_id:
                raise ValueError("Global rule must not carry a project_id")
            scope = "global"
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT COALESCE(MAX(revision),0) FROM user_rules WHERE scope=? AND rule_key=?",
                (scope, request.rule_key),
            ).fetchone()[0]
            if current != request.expected_revision:
                raise ValueError(f"Rule revision conflict: current {current}")
            record = request.model_dump(exclude={"expected_revision"}) | {
                "revision": current + 1,
                "created_at": datetime.now(UTC).isoformat(),
            }
            db.execute(
                "INSERT INTO user_rules VALUES (?,?,?,?)",
                (scope, request.rule_key, current + 1, compact(record)),
            )
        return record

    def get_rules(self, project_id: str = "", max_chars: int = 6000, offset: int = 0) -> dict:
        if not 1024 <= max_chars <= 12000 or offset < 0:
            raise ValueError("max_chars must be 1024..12000")
        scope = "project:" + scope_id.validate_python(project_id) if project_id else ""
        with self.connection() as db:
            rows = db.execute(
                "SELECT r.payload FROM user_rules r JOIN (SELECT scope,rule_key,MAX(revision) AS rev FROM user_rules WHERE scope IN ('global',?) GROUP BY scope,rule_key) x ON r.scope=x.scope AND r.rule_key=x.rule_key AND r.revision=x.rev ORDER BY r.scope,r.rule_key",
                (scope,),
            ).fetchall()
        result = production_rules() | {
            "user_rules": [],
            "omitted_rules": max(0, len(rows) - offset),
            "budget_unit": "json_characters",
            "next_offset": offset,
        }
        for index, row in enumerate(rows[offset:], start=offset):
            rule = json.loads(row["payload"])
            card = {
                key: rule[key]
                for key in (
                    "rule_key",
                    "title",
                    "statement",
                    "scope",
                    "revision",
                    "user_quote",
                    "source_ref",
                )
            }
            result["user_rules"].append(card)
            previous_offset = result["next_offset"]
            result["next_offset"] = index + 1
            if len(compact(result)) > max_chars:
                result["user_rules"][-1] = {
                    key: rule[key] for key in ("rule_key", "scope", "revision")
                } | {"detail_required": True}
            if len(compact(result)) > max_chars:
                result["user_rules"].pop()
                result["next_offset"] = previous_offset
                break
            result["omitted_rules"] -= 1
        return result

    def get_rule(
        self, rule_key: str, scope: str = "global", project_id: str = "", revision: int = 0
    ) -> dict:
        if scope not in ("global", "project") or revision < 0:
            raise ValueError("Invalid rule scope or revision")
        if scope == "global" and project_id:
            raise ValueError("Global rule must not carry project_id")
        key = "global" if scope == "global" else "project:" + scope_id.validate_python(project_id)
        with self.connection() as db:
            row = db.execute(
                "SELECT payload FROM user_rules WHERE scope=? AND rule_key=? AND (?=0 OR revision=?) ORDER BY revision DESC LIMIT 1",
                (key, rule_key, revision, revision),
            ).fetchone()
        if row is None:
            raise ValueError("Rule not found in this scope")
        return json.loads(row["payload"])

    def get_feedback(self, feedback_id: str, project_id: str) -> dict:
        with self.connection() as db:
            row = db.execute(
                "SELECT payload FROM learning_feedback WHERE feedback_id=? AND project_id=?",
                (feedback_id, scope_id.validate_python(project_id)),
            ).fetchone()
        if row is None:
            raise ValueError("Feedback not found in this project")
        result = json.loads(row["payload"])
        result.setdefault("modules", ["unclassified"])
        return result

    def record_feedback(self, **values) -> dict:
        request = Feedback(**values)
        if request.outcome == "accepted" and not request.case_id:
            raise ValueError("Accepted feedback requires an archived case")
        if request.case_id:
            archive = CaseArchive(self.root)
            case = archive.get(request.case_id, request.project_id)
            if case["shot_id"] != request.shot_id:
                raise ValueError("Case belongs to another shot")
            if not archive.verify(request.case_id, request.project_id)["valid"]:
                raise ValueError("Case evidence failed checksum validation")
            if request.outcome == "accepted" and not {"project", "preview"} <= {
                item["role"] for item in case["files"]
            }:
                raise ValueError("Accepted case requires project and preview files")
            projects = [item for item in case["files"] if item["role"] == "project"]
            if projects and not any(
                item["role"] == "project"
                and item["archive_path"].startswith(f"projects/{request.software}/")
                for item in projects
            ):
                raise ValueError("Case project belongs to another software")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT feedback_id,revision FROM learning_feedback WHERE project_id=? AND shot_id=? AND attempt_id=? AND software=? ORDER BY revision DESC LIMIT 1",
                (request.project_id, request.shot_id, request.attempt_id, request.software),
            ).fetchone()
            current = previous["revision"] if previous else 0
            if current != request.expected_revision:
                raise ValueError(f"Feedback revision conflict: current {current}")
            record = request.model_dump(exclude={"expected_revision"}) | {
                "feedback_id": uuid.uuid4().hex,
                "revision": current + 1,
                "created_at": datetime.now(UTC).isoformat(),
            }
            db.execute(
                "INSERT INTO learning_feedback VALUES (?,?,?,?,?,?,?)",
                (
                    record["feedback_id"],
                    request.project_id,
                    request.shot_id,
                    request.attempt_id,
                    current + 1,
                    compact(record),
                    request.software,
                ),
            )
            if previous:
                db.execute(
                    "UPDATE memories SET status='disabled' WHERE memory_id IN (SELECT r.memory_id FROM learning_results r JOIN learning_feedback f ON f.feedback_id=r.feedback_id WHERE f.project_id=? AND f.shot_id=? AND f.attempt_id=? AND f.software=?)",
                    (request.project_id, request.shot_id, request.attempt_id, request.software),
                )
        if previous:
            return record | {"module_guides": refresh_guides(self.root, request.project_id)}
        return record

    def history(
        self,
        project_id: str,
        shot_id: str,
        effect_type: str = "",
        max_chars: int = 6000,
        before_cursor: int = 0,
        software: Literal["houdini", "nuke"] = "houdini",
        module: str = "",
    ) -> dict:
        project_id, shot_id = (
            scope_id.validate_python(project_id),
            scope_id.validate_python(shot_id),
        )
        if not 1024 <= max_chars <= 12000 or before_cursor < 0:
            raise ValueError("max_chars must be 1024..12000")
        if software not in ("houdini", "nuke"):
            raise ValueError("Invalid software")
        selected_module = normalize_modules([module], software)[0] if module else ""
        with self.connection() as db:
            rows = db.execute(
                "SELECT rowid AS cursor,payload FROM learning_feedback WHERE project_id=? AND shot_id=? "
                "AND (?='' OR json_extract(payload,'$.effect_type')=?) "
                "AND software=? "
                + ("AND " + MODULE_MATCH_SQL + " " if selected_module else "")
                + "AND (?=0 OR rowid<?) ORDER BY rowid DESC LIMIT 101",
                (
                    project_id,
                    shot_id,
                    effect_type,
                    effect_type,
                    software,
                    *((selected_module,) if selected_module else ()),
                    before_cursor,
                    before_cursor,
                ),
            ).fetchall()
        result = {
            "items": [],
            "truncated": len(rows) > 100,
            "budget_unit": "json_characters",
            "next_cursor": 0,
        }
        for row in rows[:100]:
            record = json.loads(row["payload"])
            record.setdefault("modules", ["unclassified"])
            card = {
                key: (
                    value[:160]
                    if isinstance(value, str)
                    and key
                    not in (
                        "feedback_id",
                        "project_id",
                        "shot_id",
                        "effect_type",
                        "attempt_id",
                        "case_id",
                    )
                    else value
                )
                for key, value in record.items()
            }
            card["detail_required"] = card != record
            result["items"].append(card)
            previous_cursor = result["next_cursor"]
            result["next_cursor"] = row["cursor"]
            if len(compact(result)) > max_chars:
                result["items"][-1] = {
                    key: record[key] for key in ("feedback_id", "revision", "outcome")
                } | {"detail_required": True}
            if len(compact(result)) > max_chars:
                result["items"].pop()
                result["next_cursor"] = previous_cursor
                result["truncated"] = True
                break
        return result

    def finalize(
        self,
        feedback_id: str,
        project_id: str,
        method: str,
        reason: str,
        applies_when: str,
        reuse_scope: str = "project",
    ) -> dict:
        project_id = scope_id.validate_python(project_id)
        if reuse_scope not in ("project", "shot"):
            raise ValueError("reuse_scope must be project or shot")
        if any(
            not isinstance(value, str) or not value.strip()
            for value in (method, reason, applies_when)
        ):
            raise ValueError("Final method, reason and applies_when must be non-empty")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT payload FROM learning_feedback WHERE feedback_id=? AND project_id=?",
                (feedback_id, project_id),
            ).fetchone()
            if row is None:
                raise ValueError("Feedback not found in this project")
            feedback = json.loads(row["payload"])
            latest = db.execute(
                "SELECT feedback_id FROM learning_feedback WHERE project_id=? AND shot_id=? AND attempt_id=? AND software=? ORDER BY revision DESC LIMIT 1",
                (
                    project_id,
                    feedback["shot_id"],
                    feedback["attempt_id"],
                    feedback.get("software", "houdini"),
                ),
            ).fetchone()[0]
            if latest != feedback_id:
                raise ValueError("Feedback is not the current revision")
            if feedback["outcome"] != "accepted":
                raise ValueError("Only accepted final results can become reusable workflows")
            result = self.submit(
                _db=db,
                _origin_feedback_id=feedback_id,
                project_id=project_id,
                software=feedback.get("software", "houdini"),
                modules=feedback.get("modules", ["unclassified"]),
                shot_id=feedback["shot_id"] if reuse_scope == "shot" else "",
                kind="workflow",
                title=f"{feedback['effect_type']}：{feedback['problem'][:60]}",
                method=method,
                reason=reason,
                applies_when=applies_when,
                source_type="observed",
                source_ref=f"feedback:{feedback_id}; {feedback['source_ref']}; 用户确认：{feedback['user_quote'][:512]}",
                project_dependent=True,
                case_id=feedback["case_id"],
            )
            existing = db.execute(
                "SELECT memory_id FROM learning_results WHERE feedback_id=?", (feedback_id,)
            ).fetchone()
            if existing and existing["memory_id"] != result["memory_id"]:
                raise ValueError(
                    "Finalize already recorded a different method; revise the feedback first"
                )
            db.execute(
                "INSERT OR IGNORE INTO learning_results VALUES (?,?)",
                (feedback_id, result["memory_id"]),
            )
        return result | {"module_guides": refresh_guides(self.root, project_id)}

    def _dream_plan(self, db, project_id: str) -> dict:
        rows = db.execute(
            "SELECT memory_id,payload,status FROM memories WHERE project_id=? AND status='active' ORDER BY memory_id LIMIT 501",
            (project_id,),
        ).fetchall()
        aliases = db.execute(
            "SELECT duplicate_id,canonical_id FROM memory_aliases WHERE project_id=? ORDER BY duplicate_id",
            (project_id,),
        ).fetchall()
        snapshot = hashlib.sha256(
            compact(
                [project_id, [list(row) for row in rows], [list(row) for row in aliases]]
            ).encode()
        ).hexdigest()
        if len(rows) > 500:
            count = db.execute(
                "SELECT COUNT(*) FROM memories WHERE project_id=? AND status='active'",
                (project_id,),
            ).fetchone()[0]
            return {
                "snapshot": snapshot,
                "actions": [],
                "deferred_actions": 0,
                "active_memories": count,
                "unique_recipes": None,
                "capacity_limited": True,
                "scope": "reset_only_above_500_active_memories",
            }
        current = {row["duplicate_id"]: row["canonical_id"] for row in aliases}
        groups = {}
        for row in rows:
            memory = json.loads(row["payload"])
            memory.setdefault("software", "houdini")
            memory.setdefault("modules", ["unclassified"])
            key = compact(
                [
                    memory[name]
                    for name in (
                        "software",
                        "modules",
                        "shot_id",
                        "kind",
                        "title",
                        "method",
                        "reason",
                        "applies_when",
                        "project_dependent",
                    )
                ]
            )
            groups.setdefault(key, []).append(memory)
        actions = []
        for group in groups.values():
            group.sort(
                key=lambda memory: (
                    memory["source_type"] == "user",
                    memory["created_at"],
                    memory["memory_id"],
                ),
                reverse=True,
            )
            for duplicate in group[1:]:
                if current.get(duplicate["memory_id"]) != group[0]["memory_id"]:
                    actions.append(
                        {
                            "duplicate_id": duplicate["memory_id"],
                            "canonical_id": group[0]["memory_id"],
                        }
                    )
        return {
            "snapshot": snapshot,
            "actions": actions[:20],
            "deferred_actions": max(0, len(actions) - 20),
            "active_memories": len(rows),
            "unique_recipes": len(groups),
            "scope": "exact_duplicate_recall_index_only",
        }

    def dream(self, project_id: str, mode: str = "preview", expected_snapshot: str = "") -> dict:
        project_id = scope_id.validate_python(project_id)
        if mode not in ("preview", "apply", "reset"):
            raise ValueError("Dream mode must be preview, apply or reset")
        with self.connection() as db:
            if mode != "preview":
                db.execute("BEGIN IMMEDIATE")
            plan = self._dream_plan(db, project_id)
            if mode == "preview":
                return plan
            if expected_snapshot != plan["snapshot"]:
                raise ValueError("Dream snapshot changed; preview again before applying")
            if mode == "apply" and plan.get("capacity_limited"):
                raise ValueError("Dream planning limit exceeded; reset remains available")
            if mode == "reset":
                applied = db.execute(
                    "DELETE FROM memory_aliases WHERE project_id=?", (project_id,)
                ).rowcount
            else:
                for action in plan["actions"]:
                    db.execute(
                        "INSERT INTO memory_aliases VALUES (?,?,?) ON CONFLICT(duplicate_id) DO UPDATE SET canonical_id=excluded.canonical_id",
                        (action["duplicate_id"], action["canonical_id"], project_id),
                    )
                applied = len(plan["actions"])
            report = plan | {
                "mode": mode,
                "applied": applied,
                "created_at": datetime.now(UTC).isoformat(),
            }
            db.execute(
                "INSERT INTO dream_runs VALUES (?,?,?,?,?)",
                (uuid.uuid4().hex, project_id, plan["snapshot"], mode, compact(report)),
            )
        return report
