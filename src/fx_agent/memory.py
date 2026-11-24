"""Rule-based admission and scoped recall; no hidden model calls or inferred facts."""

import hashlib
import json
import re
import uuid
from contextlib import nullcontext
from datetime import UTC, datetime
from sqlite3 import Connection
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from fx_agent.module_guides import refresh_guides
from fx_agent.modules import MODULE_MATCH_SQL, normalize_modules
from fx_agent.store import Identifier, Store, compact, scope_id

_LOCAL_ABS_PATH_RE = re.compile(
    r"(?:(?<![A-Za-z0-9_$])[A-Za-z]:[/\\]|\\\\[^\\\s]+\\[^\\\s]+|//[A-Za-z0-9_.-]+/[^\s/]+)"
)


def validate_text_only_hygiene(*fields: str) -> None:
    """Reject local drive or UNC paths inside reusable text-only experiences."""
    for value in fields:
        if value and _LOCAL_ABS_PATH_RE.search(value):
            raise ValueError(
                "Text-only reusable experience must not contain local or UNC absolute paths; "
                "replace them with $HIP/... or project-agnostic descriptions"
            )


class Experience(BaseModel):
    """Codex supplies provenance and decision rationale at a production milestone."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    project_id: Identifier = "__global__"
    software: Literal["houdini", "nuke", "maya"] = "houdini"
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
    project_dependent: bool = False
    case_id: str = ""
    supersedes: str = ""
    reuse_scope: Literal["global", "shot"] = "global"

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
        validate_text_only_hygiene(
            request.title, request.method, request.reason, request.applies_when
        )

        status, evidence, reason = "candidate", "unverified", "inference_requires_evidence"
        if request.source_type == "user":
            status = "active"
            evidence = "user_reported"
            reason = "explicit_user_statement"
        elif request.source_type == "observed":
            status = "active"
            evidence = "live_verified_text"
            reason = "observed_text_experience"
        if request.supersedes and (request.source_type != "user" or status != "active"):
            raise ValueError("Only an admitted explicit user correction can supersede a memory")

        content = request.model_dump(exclude={"source_ref"})
        if request.reuse_scope == "global":
            content.pop("reuse_scope")  # Preserve legacy global content hashes.
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
                    old.get(key) == value for key, value in content.items() if key != "supersedes"
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
                [
                    request.title,
                    request.method,
                    request.reason,
                    request.applies_when,
                    request.kind,
                    *request.modules,
                ]
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
        if _db is None and (
            request.supersedes
            or (
                status == "active"
                and request.reuse_scope == "global"
                and request.modules != ["unclassified"]
            )
        ):
            result["module_guides"] = refresh_guides(self.root, request.project_id)
        return result

    def get(self, memory_id: str, project_id: str = "") -> dict:
        """Read full history, including a superseded entry, in the project scope."""
        if project_id:
            scope_id.validate_python(project_id)
        with self.connection() as db:
            row = db.execute(
                "SELECT payload,status,project_id,shot_id FROM memories WHERE memory_id=?",
                (memory_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Memory does not belong to the requested project or does not exist")
            result = json.loads(row["payload"]) | {"status": row["status"]}
            result.setdefault("software", "houdini")
            result.setdefault("modules", ["unclassified"])
            reuse_scope = result.get("reuse_scope", "shot" if row["shot_id"] else "global")
            if project_id and reuse_scope == "shot" and row["project_id"] != project_id:
                raise ValueError("Memory does not belong to the requested project or does not exist")
            related = db.execute(
                "SELECT duplicate_id FROM memory_aliases WHERE canonical_id=? ORDER BY duplicate_id",
                (memory_id,),
            ).fetchall()
            sources = db.execute(
                "SELECT source_id FROM knowledge_sources WHERE memory_id=? ORDER BY source_id",
                (memory_id,),
            ).fetchall()
            valid = db.execute(
                "SELECT 1 FROM memories WHERE memory_id=? AND " + usable_sql("memories"),
                (memory_id,),
            ).fetchone()
            conflicts = db.execute(
                "SELECT DISTINCT note.memory_id FROM knowledge_sources cf JOIN memories note ON note.memory_id=cf.memory_id "
                "WHERE note.status='disputed' AND (cf.source_id=? OR cf.source_id IN (SELECT source_id FROM knowledge_sources WHERE memory_id=?))",
                (memory_id, memory_id),
            ).fetchall()
        if sources:
            result["source_memory_ids"] = [row[0] for row in sources]
        if result["status"] == "active" and not valid:
            result["status"] = "needs_review"
        if related:
            result["related_memory_ids"] = [item["duplicate_id"] for item in related]
        if conflicts:
            result["conflict_memory_ids"] = [row[0] for row in conflicts]
        return result

    def search(
        self,
        project_id: str = "",
        query: str = "",
        shot_id: str = "",
        include_candidates: bool = False,
        limit: int = 5,
        max_chars: int = 3000,
        software: Literal["houdini", "nuke", "maya"] = "houdini",
        module: str = "",
        source_project: str = "",
        offset: int = 0,
    ) -> dict:
        """Multilingual substring & relevance-ranked recall, scoped before ranking and expansion."""
        if project_id:
            scope_id.validate_python(project_id)
        if software not in ("houdini", "nuke", "maya"):
            raise ValueError("Invalid software")
        selected_module = normalize_modules([module], software)[0] if module else ""
        if shot_id:
            shot_id = scope_id.validate_python(shot_id)
        if not 1 <= limit <= 20 or not 512 <= max_chars <= 12000 or len(query) > 200 or offset < 0:
            raise ValueError("Invalid search limit, offset, max_chars or query length")
        tokens = query.casefold().split()
        result = {"items": [], "budget_unit": "json_characters", "truncated": False}
        if not tokens and not selected_module:
            return result
        if len(tokens) > 16:
            raise ValueError("Search query has too many terms")
        clauses = [
            "COALESCE(json_extract(payload,'$.software'),'houdini')=?",
            "status IN ('active','candidate','disputed','needs_review')"
            if include_candidates
            else "status='active'",
        ]
        clauses.append(
            "NOT EXISTS (SELECT 1 FROM memory_aliases a JOIN memories c ON c.memory_id=a.canonical_id WHERE a.duplicate_id=memories.memory_id AND c.status='active' AND "
            + usable_sql("c")
            + ")"
        )
        clauses.append(usable_sql("memories"))
        clauses.append(
            "NOT EXISTS (SELECT 1 FROM knowledge_sources ks JOIN memories k ON k.memory_id=ks.memory_id WHERE ks.source_id=memories.memory_id AND k.status='active' AND "
            + usable_sql("k")
            + (
                " AND EXISTS (SELECT 1 FROM json_each(COALESCE(json_extract(k.payload,'$.modules'),'[\"unclassified\"]')) replacement_tags WHERE replacement_tags.value=?)"
                if selected_module
                else ""
            )
            + ")"
        )
        # Legacy shot-only records retain their original scope; new knowledge defaults global.
        clauses.append(
            "(COALESCE(json_extract(payload,'$.reuse_scope'),CASE WHEN shot_id='' THEN 'global' ELSE 'shot' END)='global' OR (project_id=? AND (?='' OR shot_id=?)))"
        )
        base_params = [
            software,
            *((selected_module,) if selected_module else ()),
            project_id,
            shot_id,
            shot_id,
        ]
        if source_project:
            clauses.append("project_id=?")
            base_params.append(scope_id.validate_python(source_project))
        if selected_module:
            clauses.append(MODULE_MATCH_SQL)
            base_params.append(selected_module)
        token_clauses = ["instr(search_text,?)>0" for _ in tokens]
        conflict_expr = (
            "EXISTS (SELECT 1 FROM knowledge_sources cf JOIN memories note ON note.memory_id=cf.memory_id "
            "WHERE note.status='disputed' AND (cf.source_id=memories.memory_id "
            "OR cf.source_id IN (SELECT source_id FROM knowledge_sources WHERE memory_id=memories.memory_id))) AS has_conflict"
        )
        with self.connection() as db:
            rows = db.execute(
                f"SELECT rowid,payload,status,{conflict_expr} FROM memories WHERE "
                + " AND ".join(clauses + token_clauses)
                + " ORDER BY rowid DESC",
                [*base_params, *tokens],
            ).fetchall()
            # CJK bigram fallback when an unspaced Chinese phrase misses literal instr().
            if not rows and tokens and any(
                any("\u4e00" <= ch <= "\u9fff" for ch in tok) and len(tok) >= 3 for tok in tokens
            ):
                candidates = db.execute(
                    f"SELECT rowid,payload,status,search_text,{conflict_expr} FROM memories WHERE "
                    + " AND ".join(clauses)
                    + " ORDER BY rowid DESC LIMIT 200",
                    base_params,
                ).fetchall()
                matched = []
                for row in candidates:
                    text = row["search_text"]
                    all_ok = True
                    score = 0.0
                    for tok in tokens:
                        if tok in text:
                            score += 2.0
                            continue
                        cjk = [ch for ch in tok if "\u4e00" <= ch <= "\u9fff"]
                        if len(cjk) >= 3:
                            bigrams = ["".join(cjk[i : i + 2]) for i in range(len(cjk) - 1)]
                            hits = sum(1 for bg in bigrams if bg in text)
                            if hits / len(bigrams) >= 0.75:
                                score += hits / len(bigrams)
                                continue
                        all_ok = False
                        break
                    if all_ok:
                        matched.append((score, row["rowid"], row))
                matched.sort(key=lambda item: (item[0], item[1]), reverse=True)
                rows = [item[2] for item in matched]
            # Optional hybrid semantic search when an embedding provider is configured.
            from fx_agent.embeddings import embedding_enabled, semantic_scores

            sem_scores: dict[str, float] = {}
            if query.strip() and embedding_enabled():
                all_scoped = db.execute(
                    f"SELECT rowid,payload,status,{conflict_expr} FROM memories WHERE "
                    + " AND ".join(clauses)
                    + " ORDER BY rowid DESC LIMIT 200",
                    base_params,
                ).fetchall()
                sem_items: dict[str, str] = {}
                scoped_by_id: dict[str, object] = {}
                for r in all_scoped:
                    rec = json.loads(r["payload"])
                    mid = rec["memory_id"]
                    scoped_by_id[mid] = r
                    sem_items[mid] = " ".join(
                        [
                            rec.get("title", ""),
                            rec.get("applies_when", ""),
                            rec.get("method", ""),
                            rec.get("reason", ""),
                        ]
                    )
                sem_scores = semantic_scores(
                    self.root,
                    item_type="memory",
                    software=software,
                    query=query,
                    items=sem_items,
                )
                existing_ids = {json.loads(r["payload"])["memory_id"] for r in rows}
                for mid, sim in sem_scores.items():
                    if sim >= 0.35 and mid not in existing_ids and mid in scoped_by_id:
                        rows.append(scoped_by_id[mid])
        # Relevance ranking: prioritize title/applies_when hits (fused with semantic similarity if enabled).
        if tokens and len(rows) > 1 or sem_scores:
            lex_scores: list[int] = []
            parsed_ids: list[str] = []
            for row in rows:
                rec = json.loads(row["payload"])
                parsed_ids.append(rec["memory_id"])
                title = rec.get("title", "").casefold()
                cond = rec.get("applies_when", "").casefold()
                method = rec.get("method", "").casefold()
                score = 0
                for tok in tokens:
                    if tok in title:
                        score += 5
                    if tok in cond:
                        score += 2
                    if tok in method:
                        score += 1
                lex_scores.append(score)
            max_lex = max(lex_scores, default=0)
            ranked = []
            for row, lex, mid in zip(rows, lex_scores, parsed_ids, strict=True):
                if sem_scores:
                    lex_norm = (lex / max_lex) if max_lex > 0 else 0.0
                    fused = 0.55 * lex_norm + 0.45 * sem_scores.get(mid, 0.0)
                    ranked.append((fused, row["rowid"], row))
                else:
                    ranked.append((float(lex), row["rowid"], row))
            ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
            rows = [item[2] for item in ranked]
        paged = rows[offset : offset + limit + 1]
        for row in paged[:limit]:
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
                    "project_id",
                )
            } | {"summary": record["method"][:240]}
            if row["has_conflict"]:
                card["has_conflict"] = True
            result["items"].append(card)
            if len(compact(result)) > max_chars:
                result["items"].pop()
                result["truncated"] = True
                break
        result["truncated"] = result["truncated"] or len(paged) > limit
        if offset > 0 or result["truncated"]:
            next_off = offset + len(result["items"])
            if len(compact(result | {"next_offset": next_off})) <= max_chars:
                result["next_offset"] = next_off
        if not include_candidates and not result["items"] and not result["truncated"] and offset == 0:
            references = self.search(
                project_id, query, shot_id, True, 1, 12000, software, module, source_project
            )
            if references["items"]:
                result["reference_hint"] = {
                    "has_matches": True,
                    "knowledge_status": "reference_not_verified_success",
                    "message": "无匹配正式经验，但存在候选参考；展开后判断适用性，不自动升级为成功经验。",
                    "next_call": {
                        "tool": "fx_search_memory",
                        "include_candidates": True,
                    },
                    "repeat_same_arguments": True,
                }
        return result


def usable_sql(alias: str) -> str:
    """A derived recipe is usable only while every recorded source is unchanged."""
    return (
        feedback_current_sql(alias)
        + " AND "
        + (
            f"NOT EXISTS (SELECT 1 FROM knowledge_sources dep LEFT JOIN memories src ON src.memory_id=dep.source_id "
            f"WHERE dep.memory_id={alias}.memory_id AND (src.memory_id IS NULL OR src.status!='active' OR src.payload!=dep.source_payload OR NOT ({feedback_current_sql('src')})))"
        )
    )


def feedback_current_sql(alias: str) -> str:
    return (
        f"(json_extract({alias}.payload,'$.origin_feedback_id') IS NULL OR EXISTS ("
        f"SELECT 1 FROM learning_feedback f WHERE f.feedback_id=json_extract({alias}.payload,'$.origin_feedback_id') "
        "AND json_extract(f.payload,'$.outcome')='accepted' AND f.revision=(SELECT MAX(newer.revision) "
        "FROM learning_feedback newer WHERE newer.project_id=f.project_id AND newer.shot_id=f.shot_id "
        "AND newer.attempt_id=f.attempt_id AND newer.software=f.software)))"
    )
