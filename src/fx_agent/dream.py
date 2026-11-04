"""Host-authored, globally scoped knowledge revisions with source guards."""

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from fx_agent.memory import Experience, usable_sql
from fx_agent.module_guides import refresh_guides
from fx_agent.modules import normalize_modules
from fx_agent.store import compact


class DreamAction(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    operation: str = Field(pattern="^(merge|refine|split|conflict|supersede)$")
    source_ids: list[str] = Field(min_length=1, max_length=50)
    title: str = Field(min_length=1, max_length=200)
    method: str = Field(min_length=1, max_length=6000)
    reason: str = Field(min_length=1, max_length=3000)
    applies_when: str = Field(min_length=1, max_length=2000)
    rationale: str = Field(min_length=1, max_length=3000)
    modules: list[str] | None = Field(default=None, min_length=1, max_length=4)


def snapshot(db):
    """Read a transactional change token without rescanning knowledge payloads."""
    return db.execute("SELECT token FROM knowledge_state WHERE id=1").fetchone()[0]


def _plan(db, cursor, limit, software, module):
    if cursor < 0 or not 1 <= limit <= 100:
        raise ValueError("Invalid cursor or limit")
    if software and software not in ("houdini", "nuke"):
        raise ValueError("Invalid software")
    clauses = ["status='active'", "rowid>?", usable_sql("memories")]
    params = [cursor]
    if software:
        clauses.append("COALESCE(json_extract(payload,'$.software'),'houdini')=?")
        params.append(software)
    if module:
        from fx_agent.modules import MODULE_MATCH_SQL

        if not software:
            raise ValueError("Module requires software")
        clauses.append(MODULE_MATCH_SQL)
        params.append(normalize_modules([module], software)[0])
    rows = db.execute(
        "SELECT rowid,* FROM memories WHERE " + " AND ".join(clauses) + " ORDER BY rowid LIMIT ?",
        [*params, limit + 1],
    ).fetchall()

    def group_key(row):
        record = json.loads(row["payload"])
        record.setdefault("software", "houdini")
        record.setdefault("modules", ["unclassified"])
        scope = record.get("reuse_scope", "shot" if row["shot_id"] else "global")
        return compact(
            [
                record.get(k)
                for k in (
                    "software",
                    "modules",
                    "kind",
                    "title",
                    "method",
                    "reason",
                    "applies_when",
                    "project_dependent",
                )
            ]
            + [scope, (row["project_id"], row["shot_id"]) if scope == "shot" else ""]
        )

    groups, items, library = {}, [], {}
    # Exact aliases only need candidate groups represented on the current page.
    titles = sorted({json.loads(row["payload"])["title"] for row in rows[:limit]})
    for entry in db.execute(
        "SELECT * FROM memories WHERE json_extract(payload,'$.title') IN ("
        + ",".join("?" for _ in titles)
        + ") AND status='active' AND "
        + usable_sql("memories")
        + " AND NOT EXISTS (SELECT 1 FROM knowledge_sources ks JOIN memories k ON k.memory_id=ks.memory_id WHERE ks.source_id=memories.memory_id AND k.status='active' AND "
        + usable_sql("k")
        + ") ORDER BY memory_id",
        titles,
    ):
        library.setdefault(group_key(entry), []).append(entry["memory_id"])
    for row in rows[:limit]:
        record = json.loads(row["payload"])
        source_texts = [record.get("source_ref", "")]
        source_texts.extend(
            json.loads(r[0]).get("source_ref", "")
            for r in db.execute(
                "SELECT source_payload FROM knowledge_sources WHERE memory_id=?",
                (row["memory_id"],),
            )
        )
        effect_ids = sorted(
            {m for text in source_texts for m in re.findall(r"effect_card:([a-f0-9]{32})", text)}
        )
        key = group_key(row)
        if key in library:
            groups[key] = library[key]
        items.append(
            {
                "cursor": row["rowid"],
                "memory_id": row["memory_id"],
                "software": record.get("software", "houdini"),
                "modules": record.get("modules", ["unclassified"]),
                "title": record["title"],
                "summary": record["method"][:240],
                "source_project": row["project_id"],
                "detail_required": True,
                "effect_card_ids": effect_ids,
            }
        )
    actions = []
    for group in groups.values():
        for duplicate in group[1:]:
            existing = db.execute(
                "SELECT canonical_id FROM memory_aliases WHERE duplicate_id=?", (duplicate,)
            ).fetchone()
            if not existing or existing[0] != group[0]:
                actions.append({"duplicate_id": duplicate, "canonical_id": group[0]})
    return {
        "snapshot": snapshot(db),
        "items": items,
        "next_cursor": items[-1]["cursor"] if items else cursor,
        "truncated": len(rows) > limit,
        "actions": actions[:20],
        "deferred_actions": max(0, len(actions) - 20),
        "active_memories": db.execute(
            "SELECT COUNT(*) FROM memories WHERE status='active'"
        ).fetchone()[0],
        "scope": "global_host_authored",
        "software": software,
        "module": module,
        "cursor": cursor,
        "limit": limit,
        "effect_learning": {
            "queue_tool": "fx_get_effect_learning_queue",
            "inspect_tool": "fx_plan_effect_study",
            "revision_tool": "fx_refine_effect",
            "merge_tool": "fx_merge_effect_cards",
            "required": "迭代中理解效果后，读取关联效果卡及新证据，补充明确效果名、类别、运动与其他信息。检查同一效果是否被对象编号或组成渲染层拆碎；核对完整来源、外观、运动和用途后，用fx_merge_effect_cards先预览再合并，保留各层节点、输入、输出差异和证据，旧卡保留历史。不可只凭名称相似或节点相同合并；有独立外观、动态或用途的变体保持独立。疑问先查渲染序列，仍不明确则询问用户。",
            "automatic_promotion": False,
        },
    }


def dream(store, mode, expected_snapshot, actions, cursor, limit, software, module):
    if mode not in ("preview", "apply", "reset"):
        raise ValueError("Dream mode must be preview, apply or reset")
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE" if mode != "preview" else "BEGIN")
        plan = _plan(db, cursor, limit, software, module)
        if mode == "preview":
            return plan
        if plan["snapshot"] != expected_snapshot:
            raise ValueError("Dream snapshot changed; preview again")
        if actions is not None and len(actions) > 20:
            raise ValueError("At most 20 semantic actions per pass")
        run_id, created, changes = uuid.uuid4().hex, [], []
        touched = set()
        if mode == "reset":
            # Compatibility: reset exact aliases only. Semantic runs use guarded undo.
            for row in db.execute("SELECT * FROM memory_aliases").fetchall():
                changes.append({"duplicate_id": row[0], "before": list(row), "after": None})
            db.execute("DELETE FROM memory_aliases")
        elif actions is None:
            for action in plan["actions"]:
                before = db.execute(
                    "SELECT * FROM memory_aliases WHERE duplicate_id=?", (action["duplicate_id"],)
                ).fetchone()
                after = [action["duplicate_id"], action["canonical_id"], "global"]
                db.execute(
                    "INSERT INTO memory_aliases VALUES (?,?,?) ON CONFLICT(duplicate_id) DO UPDATE SET canonical_id=excluded.canonical_id,project_id=excluded.project_id",
                    after,
                )
                changes.append(
                    {
                        "duplicate_id": action["duplicate_id"],
                        "before": list(before) if before else None,
                        "after": after,
                    }
                )
        else:
            for raw in actions:
                action = DreamAction(**raw)
                source_ids = set(action.source_ids)
                if len(source_ids) != len(action.source_ids):
                    raise ValueError("Duplicate source IDs")
                if action.operation == "merge" and len(source_ids) < 2:
                    raise ValueError("Merge requires at least two sources")
                # Include ancestry so invalidating an original also invalidates later refinements.
                for source in list(source_ids):
                    source_ids.update(
                        row[0]
                        for row in db.execute(
                            "SELECT source_id FROM knowledge_sources WHERE memory_id=?", (source,)
                        )
                    )
                records, sources = [], []
                for source in sorted(source_ids):
                    row = db.execute(
                        "SELECT * FROM memories WHERE memory_id=? AND status='active' AND "
                        + usable_sql("memories"),
                        (source,),
                    ).fetchone()
                    if not row:
                        raise ValueError("Dream sources must be unchanged active memories")
                    value = json.loads(row["payload"])
                    value.setdefault("software", "houdini")
                    value.setdefault("modules", ["unclassified"])
                    if value.get("reuse_scope", "shot" if row["shot_id"] else "global") != "global":
                        raise ValueError(
                            "Shot-only sources need a separately evidenced global lesson"
                        )
                    sources.append((source, row["payload"]))
                    records.append(value)
                if len({r["software"] for r in records}) != 1:
                    raise ValueError("Cannot mix software")
                touched.update(source_ids)
                modules = action.modules or sorted({m for r in records for m in r["modules"]})
                request = Experience(
                    project_id="__knowledge__",
                    software=records[0]["software"],
                    modules=modules,
                    kind="workflow",
                    title=action.title,
                    method=action.method,
                    reason=action.reason,
                    applies_when=action.applies_when,
                    source_ref=f"dream:{run_id}",
                    project_dependent=False,
                )
                memory_id = uuid.uuid4().hex
                record = request.model_dump() | {
                    "memory_id": memory_id,
                    "created_at": datetime.now(UTC).isoformat(),
                    "evidence_status": "host_synthesis_from_sources",
                    "visual_verified": False,
                    "reproduction_status": "not_tested",
                    "dream_run_id": run_id,
                    "operation": action.operation,
                    "rationale": action.rationale,
                }
                status = "disputed" if action.operation == "conflict" else "active"
                payload = compact(record)
                db.execute(
                    "INSERT INTO memories VALUES (?,?,?,?,?,?,?)",
                    (
                        memory_id,
                        "__knowledge__",
                        "",
                        status,
                        hashlib.sha256(payload.encode()).hexdigest(),
                        payload,
                        " ".join(
                            [action.title, action.method, action.reason, action.applies_when]
                        ).casefold(),
                    ),
                )
                db.executemany(
                    "INSERT INTO knowledge_sources VALUES (?,?,?)",
                    [(memory_id, s, p) for s, p in sources],
                )
                created.append(memory_id)
        applied = len(actions) if actions is not None and mode == "apply" else len(changes)
        touched.update(created)
        for change in changes:
            for value in (change["before"], change["after"]):
                if value:
                    touched.update(value[:2])
        guard_ids = sorted(touched)
        alias_ids = sorted(change["duplicate_id"] for change in changes)
        report = {
            "run_id": run_id,
            "mode": mode,
            "applied": applied,
            "created_ids": created,
            "alias_changes": changes,
            "actions": actions if actions is not None else plan["actions"],
            "before_snapshot": plan["snapshot"],
            "after_snapshot": snapshot(db),
            "undone": False,
            "guard_ids": guard_ids,
            "alias_ids": alias_ids,
            "guard": _guard(db, guard_ids, alias_ids),
            "next_cursor": plan["next_cursor"],
            "truncated": plan["truncated"],
            "software": software,
            "module": module,
        }
        db.execute(
            "INSERT INTO dream_runs VALUES (?,?,?,?,?)",
            (run_id, "global", plan["snapshot"], mode, compact(report)),
        )
    return report | {"module_guides": refresh_guides(store.root)}


def undo(store, run_id):
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT payload FROM dream_runs WHERE run_id=?", (run_id,)).fetchone()
        if not row:
            raise ValueError("Dream run not found")
        report = json.loads(row[0])
        if report.get("undone"):
            raise ValueError("Dream run already undone")
        if (
            "guard" not in report
            or _guard(db, report["guard_ids"], report["alias_ids"]) != report["guard"]
        ):
            raise ValueError(
                "Related knowledge changed after Dream; re-plan instead of overwriting later work"
            )
        for memory_id in report["created_ids"]:
            dependents = db.execute(
                "SELECT dep.memory_id FROM knowledge_sources dep JOIN memories m ON m.memory_id=dep.memory_id WHERE dep.source_id=? AND m.status NOT IN ('disabled','superseded')",
                (memory_id,),
            ).fetchall()
            if any(row[0] not in report["created_ids"] for row in dependents):
                raise ValueError("Related knowledge changed: later refinements depend on this run")
        for memory_id in report["created_ids"]:
            db.execute("UPDATE memories SET status='disabled' WHERE memory_id=?", (memory_id,))
        for change in report["alias_changes"]:
            db.execute("DELETE FROM memory_aliases WHERE duplicate_id=?", (change["duplicate_id"],))
            if change["before"]:
                db.execute("INSERT INTO memory_aliases VALUES (?,?,?)", change["before"])
        report["undone"] = True
        db.execute("UPDATE dream_runs SET payload=? WHERE run_id=?", (compact(report), run_id))
    return {"run_id": run_id, "undone": True, "module_guides": refresh_guides(store.root)}


def _guard(db, ids, aliases):
    records = []
    for memory_id in ids:
        row = db.execute("SELECT * FROM memories WHERE memory_id=?", (memory_id,)).fetchone()
        records.append(list(row) if row else None)
        records.extend(
            list(row)
            for row in db.execute(
                "SELECT * FROM knowledge_sources WHERE memory_id=? ORDER BY source_id", (memory_id,)
            )
        )
        records.extend(
            list(row)
            for row in db.execute(
                "SELECT * FROM memory_conflicts WHERE memory_id=? ORDER BY import_id", (memory_id,)
            )
        )
    for duplicate in aliases:
        row = db.execute(
            "SELECT * FROM memory_aliases WHERE duplicate_id=?", (duplicate,)
        ).fetchone()
        records.append(list(row) if row else None)
    return hashlib.sha256(compact(records).encode()).hexdigest()
