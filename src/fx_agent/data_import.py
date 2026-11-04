"""Merge verified knowledge with conflict evidence; synchronize current baseline documents."""

import hashlib
import json
import shutil
import sqlite3
import tempfile
from contextlib import closing, nullcontext
from pathlib import Path

from fx_agent.archive import CaseArchive, contained, copy_verified, file_hash, publish_directory
from fx_agent.baselines import (
    baseline_destination,
    baseline_lock,
    current_baseline_sources,
    prune_baseline_copies,
    publish_baseline,
    validate_baseline,
    write_baseline_indexes,
)
from fx_agent.memory import usable_sql
from fx_agent.migration import migrate_data
from fx_agent.module_guides import refresh_guides
from fx_agent.store import Store, compact

TABLES = (
    "checkpoints",
    "user_rules",
    "learning_feedback",
    "memories",
    "learning_results",
    "knowledge_sources",
    "render_issues",
    "template_studies",
    "template_effects",
    "template_effect_reviews",
)
SNAPSHOT_TABLES = (*TABLES, "memory_conflicts", "memory_aliases", "dream_runs", "import_runs")


def _connect(path):
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def _blocked_effect_components(incoming, db, blocked, blocked_cases):
    """Preflight component bindings before ID-sorted imports can publish a total card."""
    rows = {r["card_id"]: dict(r) for r in incoming.execute("SELECT * FROM template_effects")}
    payloads = {key: json.loads(row["payload"]) for key, row in rows.items()}
    invalid = set()
    for key, row in rows.items():
        local = db.execute("SELECT * FROM template_effects WHERE card_id=?", (key,)).fetchone()
        payload = payloads[key]
        if (
            local is not None
            and dict(local) != row
            or row["study_id"] in blocked["template_studies"]
            or payload.get("case_id") in blocked_cases
            or any(e.get("case_id") in blocked_cases for e in payload.get("visual_evidence", []))
        ):
            invalid.add(key)
    component_conflicts = set()
    while True:
        newly_blocked = set()
        for key, payload in payloads.items():
            for component_id in payload.get("component_card_ids", []):
                source = payloads.get(component_id)
                if source is None:
                    local = db.execute(
                        "SELECT payload FROM template_effects WHERE card_id=?", (component_id,)
                    ).fetchone()
                    source = json.loads(local[0]) if local else None
                if (
                    component_id in invalid
                    or not source
                    or source["study_id"] != payload["study_id"]
                ):
                    newly_blocked.add(key)
        component_conflicts.update(newly_blocked)
        if newly_blocked <= invalid:
            return component_conflicts
        invalid.update(newly_blocked)


def _fingerprint(db, root):
    digest = hashlib.sha256()
    for table in SNAPSHOT_TABLES:
        digest.update(table.encode())
        for row in db.execute(f"SELECT * FROM {table} ORDER BY 1,2"):
            digest.update((compact(dict(row)) + "\n").encode())
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name not in {
            "memory.sqlite3",
            "memory.sqlite3-wal",
            "memory.sqlite3-shm",
            "local-before.sqlite3",
        }:
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(file_hash(path).encode())
    return digest.hexdigest()


def _publish_tree(source, destination):
    """A failed copy never publishes a partially populated final directory."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".fx-publish-", dir=destination.parent) as temporary:
        copied = Path(temporary) / "complete"
        shutil.copytree(
            source, copied, copy_function=lambda src, dst: copy_verified(Path(src), Path(dst))
        )
        publish_directory(copied, destination)


def _feedback_branches(incoming, local):
    """Same project labels do not establish that two attempt histories are related."""
    groups = {}
    for row in incoming.execute("SELECT * FROM learning_feedback"):
        key = tuple(row[k] for k in ("project_id", "shot_id", "attempt_id", "software"))
        groups.setdefault(key, []).append(dict(row))
    blocked = set()
    for key, rows in groups.items():
        existing = local.execute(
            "SELECT * FROM learning_feedback WHERE project_id=? AND shot_id=? AND attempt_id=? AND software=?",
            key,
        ).fetchall()
        if not existing:
            continue
        by_revision = {row["revision"]: dict(row) for row in existing}
        shared = any(row["feedback_id"] == old["feedback_id"] for row in rows for old in existing)
        diverged = any(
            row["revision"] in by_revision and row != by_revision[row["revision"]] for row in rows
        )
        if not shared or diverged:
            blocked.update(row["feedback_id"] for row in rows)
    return blocked


def import_data(source: Path, target: Path, *, apply: bool = False) -> dict:
    """Stop archive writers first. Preview creates no target database or archive."""
    source, target = Path(source).resolve(strict=True), Path(target).resolve()
    if source.is_relative_to(target) or target.is_relative_to(source):
        raise ValueError("Import roots must not overlap")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".fx-import-", dir=target.parent) as temporary:
        stage = Path(temporary) / "snapshot"
        migrate_data(source, stage, exclude_import_history=True)
        staged = Store(stage)  # Upgrade the copy, never the original device's database.
        # Upstream unresolved conflicts must not regain active status on a third device.
        # Freeze the effective state in the export copy; keep original payloads intact.
        with staged.connection() as db:
            frozen_count = db.execute(
                "UPDATE memories SET status='needs_review' WHERE status='active' AND NOT ("
                + usable_sql("memories")
                + ")"
            ).rowcount
        with closing(_connect(stage / "memory.sqlite3")) as incoming:
            import_id = _fingerprint(incoming, stage)
            local_path = target / "memory.sqlite3"
            if local_path.exists():
                local_copy = Path(temporary) / "local"
                local_copy.mkdir()
                with (
                    closing(_connect(local_path)) as reader,
                    closing(sqlite3.connect(local_copy / "memory.sqlite3")) as writer,
                ):
                    reader.backup(writer)
                local = Store(local_copy)
            else:
                local = Store(Path(temporary) / "empty")
            report = {
                "import_id": import_id,
                "source": str(source),
                "applied": apply,
                "counts": {},
                "conflicts": [],
                "new_memory_ids": [],
                "records": [],
                "upstream_needs_review": frozen_count,
                "baseline_snapshots": {"added": 0, "skipped": 0},
            }
            if apply:
                archive_path = contained(target, f"imports/{import_id}")
                if not archive_path.exists():
                    _publish_tree(stage, archive_path)
                else:
                    if any(p.is_symlink() or p.is_junction() for p in archive_path.rglob("*")):
                        raise ValueError("Import snapshot contains links")
                    with closing(_connect(archive_path / "memory.sqlite3")) as saved:
                        if _fingerprint(saved, archive_path) != import_id:
                            raise ValueError("Existing import snapshot failed validation")
                report["snapshot_path"] = str(archive_path)
                backup_path = archive_path / "local-before.sqlite3"
                if local_path.exists() and not backup_path.exists():
                    with (
                        closing(_connect(local_path)) as reader,
                        closing(sqlite3.connect(backup_path)) as writer,
                    ):
                        reader.backup(writer)
                destination = Store(target)
            else:
                destination = local
            with destination.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                with baseline_lock(target) if apply else nullcontext():
                    reserved = set()
                    for folder in current_baseline_sources(stage):
                        manifest = validate_baseline(folder)
                        current, exists = baseline_destination(target, manifest, reserved)
                        if exists:
                            report["baseline_snapshots"]["skipped"] += 1
                        else:
                            report["baseline_snapshots"]["added"] += 1
                            if apply:
                                publish_baseline(folder, current)
                        if apply:
                            prune_baseline_copies(target, manifest)
                    if apply and sum(report["baseline_snapshots"].values()):
                        write_baseline_indexes(target)
                blocked_cases = set()
                for folder in sorted((stage if stage.exists() else archive_path).glob("cases/*")):
                    if folder.name == ".staging":
                        continue
                    current = contained(target, f"cases/{folder.name}")
                    if current.exists():
                        if (current / "manifest.json").read_bytes() != (
                            folder / "manifest.json"
                        ).read_bytes():
                            blocked_cases.add(folder.name)
                            report["conflicts"].append(
                                {"table": "cases", "key": folder.name, "reason": "case_id_diverged"}
                            )
                        else:
                            case = json.loads(
                                (folder / "manifest.json").read_text(encoding="utf-8")
                            )
                            if not CaseArchive(target).verify(folder.name, case["project_id"])[
                                "valid"
                            ]:
                                raise ValueError("Existing case checksum validation failed")
                    elif apply:
                        _publish_tree(folder, current)
                blocked = {
                    "memories": set(),
                    "learning_feedback": set(),
                    "template_studies": set(),
                    "template_effects": set(),
                }
                feedback_branches = _feedback_branches(incoming, db)
                for table in TABLES:
                    counts = {"added": 0, "skipped": 0, "conflicts": 0}
                    report["counts"][table] = counts
                    info = incoming.execute(f"PRAGMA table_info({table})").fetchall()
                    keys = [row["name"] for row in sorted(info, key=lambda r: r["pk"]) if row["pk"]]
                    component_conflicts = (
                        _blocked_effect_components(incoming, db, blocked, blocked_cases)
                        if table == "template_effects"
                        else set()
                    )
                    for row in incoming.execute(f"SELECT * FROM {table} ORDER BY 1,2"):
                        values = dict(row)
                        where = " AND ".join(f"{key}=?" for key in keys)
                        existing = db.execute(
                            f"SELECT * FROM {table} WHERE {where}", [values[k] for k in keys]
                        ).fetchone()
                        if existing is not None and dict(existing) == values:
                            counts["skipped"] += 1
                            continue
                        problem = "record_diverged" if existing is not None else ""
                        if table == "template_effects" and values["card_id"] in component_conflicts:
                            problem = "component_card_conflict"
                        if (
                            table == "learning_feedback"
                            and values["feedback_id"] in feedback_branches
                        ):
                            problem = "attempt_branch_conflict"
                        payload = json.loads(values["payload"]) if "payload" in values else {}
                        if (
                            table in {"template_effects", "template_effect_reviews"}
                            and values["study_id"] in blocked["template_studies"]
                        ):
                            problem = "study_conflict"
                        if (
                            table == "template_effect_reviews"
                            and set(payload.get("card_ids", [])) & blocked["template_effects"]
                        ):
                            problem = "effect_card_conflict"
                        if payload.get("case_id") in blocked_cases:
                            problem = "case_conflict"
                        if table == "template_effects" and any(
                            evidence.get("case_id") in blocked_cases
                            for evidence in payload.get("visual_evidence", [])
                        ):
                            problem = "visual_evidence_case_conflict"
                        if table == "learning_results" and (
                            values["feedback_id"] in blocked["learning_feedback"]
                            or values["memory_id"] in blocked["memories"]
                        ):
                            problem = "source_conflict"
                        if table == "knowledge_sources" and (
                            values["memory_id"] in blocked["memories"]
                            or values["source_id"] in blocked["memories"]
                        ):
                            problem = "source_conflict"
                        if (
                            table == "memories"
                            and payload.get("origin_feedback_id") in blocked["learning_feedback"]
                        ):
                            problem = "feedback_conflict"
                        if not problem:
                            try:
                                db.execute(
                                    f"INSERT INTO {table} ({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
                                    list(values.values()),
                                )
                            except sqlite3.IntegrityError:
                                problem = "unique_key_conflict"
                        if problem:
                            counts["conflicts"] += 1
                            if table in blocked:
                                blocked[table].add(values[keys[0]])
                            entry = {
                                "table": table,
                                "key": {k: values[k] for k in keys},
                                "reason": problem,
                                "incoming": values,
                                "local": dict(existing) if existing else None,
                            }
                            report["records"].append(entry)
                        else:
                            counts["added"] += 1
                            if table == "memories":
                                report["new_memory_ids"].append(values["memory_id"])
                # Do not activate a synthesis whose source bindings were quarantined.
                for entry in report["records"]:
                    if entry["table"] == "knowledge_sources":
                        db.execute(
                            "UPDATE memories SET status='needs_review' WHERE memory_id=? AND project_id='__knowledge__'",
                            (entry["incoming"]["memory_id"],),
                        )
                if apply:
                    previous = db.execute(
                        "SELECT payload FROM import_runs WHERE import_id=?", (import_id,)
                    ).fetchone()
                    if previous:
                        resolved = [
                            r for r in json.loads(previous[0])["records"] if r.get("resolution")
                        ]
                        for entry in report["records"]:
                            old = next(
                                (
                                    r
                                    for r in resolved
                                    if r["table"] == entry["table"]
                                    and r["incoming"] == entry["incoming"]
                                ),
                                None,
                            )
                            if old:
                                entry["resolution"] = old["resolution"]
                    for entry in report["records"]:
                        if (
                            entry["table"] == "memories"
                            and entry["local"]
                            and not entry.get("resolution")
                        ):
                            db.execute(
                                "INSERT OR IGNORE INTO memory_conflicts VALUES (?,?)",
                                (entry["incoming"]["memory_id"], import_id),
                            )
                    db.execute(
                        "INSERT INTO import_runs VALUES (?,?) ON CONFLICT(import_id) DO UPDATE SET payload=excluded.payload",
                        (import_id, compact(report)),
                    )
                else:
                    db.rollback()
            if apply:
                report["module_guides"] = refresh_guides(target)
                from fx_agent.effect_cards import refresh_effect_documents

                report["effect_documents"] = refresh_effect_documents(target)
            return {k: v for k, v in report.items() if k != "records"} | {
                "conflict_records": len(report["records"]),
                "dream_required": bool(report["new_memory_ids"] or report["records"]),
            }


def import_records(
    root: Path, import_id: str = "", *, table: str = "", offset: int = 0, limit: int = 20
) -> dict:
    """List batches or expand paginated divergent rows with both full versions."""
    if offset < 0 or not 1 <= limit <= 100:
        raise ValueError("Invalid offset or limit")
    with Store(Path(root)).connection() as db:
        if not import_id:
            rows = db.execute(
                "SELECT payload FROM import_runs ORDER BY rowid DESC LIMIT ? OFFSET ?",
                (limit + 1, offset),
            ).fetchall()
            items = [
                {
                    k: v
                    for k, v in json.loads(row[0]).items()
                    if k not in ("records", "new_memory_ids")
                }
                for row in rows[:limit]
            ]
            more = len(rows) > limit
        else:
            row = db.execute(
                "SELECT payload FROM import_runs WHERE import_id=?", (import_id,)
            ).fetchone()
            if not row:
                raise ValueError("Import batch not found")
            values = json.loads(row[0])["records"]
            values = [v for v in values if not table or v["table"] == table]
            items, more = values[offset : offset + limit], len(values) > offset + limit
    return {"items": items, "next_offset": offset + len(items), "truncated": more}


def resolve_memory(
    root: Path,
    import_id: str,
    memory_id: str,
    decision: str,
    expected_snapshot: str,
    rationale: str,
) -> dict:
    """Resolve a divergent memory without replacing either version's source text."""
    import uuid

    from fx_agent.dream import snapshot

    if decision not in ("keep_local", "keep_incoming", "keep_both") or not rationale.strip():
        raise ValueError("Choose keep_local, keep_incoming or keep_both and explain why")
    store = Store(Path(root))
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        if snapshot(db) != expected_snapshot:
            raise ValueError("Knowledge snapshot changed; read again")
        row = db.execute(
            "SELECT payload FROM import_runs WHERE import_id=?", (import_id,)
        ).fetchone()
        if not row:
            raise ValueError("Import batch not found")
        report = json.loads(row[0])
        entry = next(
            (
                r
                for r in report["records"]
                if r["table"] == "memories" and r["incoming"]["memory_id"] == memory_id
            ),
            None,
        )
        if not entry or entry.get("resolution"):
            raise ValueError("Unresolved memory conflict not found")
        current = db.execute("SELECT * FROM memories WHERE memory_id=?", (memory_id,)).fetchone()
        if (dict(current) if current else None) != entry["local"]:
            raise ValueError("Local memory changed since import; review a new import preview")
        branch_id = ""
        if decision != "keep_local":
            incoming = entry["incoming"]
            payload = json.loads(incoming["payload"])
            archive_path = contained(store.root, f"imports/{import_id}")
            case_id = payload.get("case_id")
            if case_id:
                saved = contained(archive_path, f"cases/{case_id}/manifest.json")
                live = contained(store.root, f"cases/{case_id}/manifest.json")
                if (
                    not live.exists()
                    or saved.read_bytes() != live.read_bytes()
                    or not CaseArchive(store.root).verify(case_id, payload["project_id"])["valid"]
                ):
                    raise ValueError(
                        "Resolve the case archive conflict before selecting this memory"
                    )
            origin = payload.get("origin_feedback_id")
            with closing(_connect(archive_path / "memory.sqlite3")) as saved:
                if origin:
                    original = saved.execute(
                        "SELECT * FROM learning_feedback WHERE feedback_id=?", (origin,)
                    ).fetchone()
                    local = db.execute(
                        "SELECT * FROM learning_feedback WHERE feedback_id=?", (origin,)
                    ).fetchone()
                    if not original or not local or dict(original) != dict(local):
                        raise ValueError("Resolve feedback provenance before selecting this memory")
                dependencies = saved.execute(
                    "SELECT * FROM knowledge_sources WHERE memory_id=?", (memory_id,)
                ).fetchall()
                for dependency in dependencies:
                    source = db.execute(
                        "SELECT payload,status FROM memories WHERE memory_id=?",
                        (dependency["source_id"],),
                    ).fetchone()
                    if (
                        not source
                        or source["status"] != "active"
                        or source["payload"] != dependency["source_payload"]
                    ):
                        raise ValueError("Resolve source provenance before selecting this memory")
            branch_id = uuid.uuid5(uuid.NAMESPACE_URL, f"fx-import:{import_id}:{memory_id}").hex
            payload.update(
                memory_id=branch_id, origin_import_id=import_id, import_source_memory_id=memory_id
            )
            text = compact(payload)
            db.execute(
                "INSERT INTO memories VALUES (?,?,?,?,?,?,?)",
                (
                    branch_id,
                    incoming["project_id"],
                    incoming["shot_id"],
                    incoming["status"],
                    hashlib.sha256(text.encode()).hexdigest(),
                    text,
                    incoming["search_text"],
                ),
            )
            db.executemany(
                "INSERT INTO knowledge_sources VALUES (?,?,?)",
                [(branch_id, dep["source_id"], dep["source_payload"]) for dep in dependencies],
            )
            if decision == "keep_incoming" and current:
                db.execute(
                    "UPDATE memories SET status='superseded' WHERE memory_id=?", (memory_id,)
                )
        entry["resolution"] = {"decision": decision, "rationale": rationale, "branch_id": branch_id}
        db.execute(
            "DELETE FROM memory_conflicts WHERE memory_id=? AND import_id=?", (memory_id, import_id)
        )
        db.execute(
            "UPDATE import_runs SET payload=? WHERE import_id=?", (compact(report), import_id)
        )
    return entry["resolution"] | {"module_guides": refresh_guides(store.root)}
