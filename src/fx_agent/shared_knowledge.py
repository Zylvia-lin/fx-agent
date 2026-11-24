"""Import reviewed, portable experience summaries into the local cache."""

import json
from pathlib import Path

from fx_agent.config import CHECKOUT_DATA, data_root, database_path, local_data_root
from fx_agent.memory import Experience, MemoryStore, usable_sql, validate_text_only_hygiene
from fx_agent.migration import migrate_data
from fx_agent.store import compact

SHARED_PATH = Path(__file__).with_name("shared_experiences.json")
FIELDS = {"software", "modules", "kind", "title", "method", "reason", "applies_when"}
SHARED_PROJECT = "__shared_experience__"
SHARED_SOURCE = "reviewed shared experience"


def summary(payload: dict) -> dict:
    experience = Experience(
        **{field: payload[field] for field in FIELDS if field in payload},
        source_ref=SHARED_SOURCE,
    )
    validate_text_only_hygiene(
        experience.title, experience.method, experience.reason, experience.applies_when
    )
    if experience.kind != "preference" and not (experience.reason and experience.applies_when):
        raise ValueError("Shared experience requires a reason and applicable conditions")
    return experience.model_dump(include=FIELDS)


def prepare_local_data() -> Path:
    root = data_root()
    if root == local_data_root() and not database_path(root).is_file():
        for legacy in (CHECKOUT_DATA, root.parent):
            if not database_path(legacy).is_file():
                continue
            try:
                migrate_data(legacy, root, database_only=True)
            except OSError:
                if not database_path(root).is_file():
                    raise
            break
    load_shared_experiences(root)
    return root


def load_shared_experiences(root: Path) -> None:
    entries = json.loads(SHARED_PATH.read_text(encoding="utf-8"))
    if not isinstance(entries, list) or any(
        not isinstance(item, dict) or item.keys() != FIELDS for item in entries
    ):
        raise ValueError("Shared experience file must contain only portable summary fields")
    normalized = [summary(item) for item in entries]
    entries = {compact(item): item for item in normalized}
    store = MemoryStore(root)
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        imported = db.execute(
            "SELECT memory_id,payload FROM memories WHERE project_id=? AND status='active' AND "
            "json_extract(payload,'$.source_ref')=?",
            (SHARED_PROJECT, SHARED_SOURCE),
        ).fetchall()
        db.executemany(
            "UPDATE memories SET status='shared_withdrawn' WHERE memory_id=?",
            [(row["memory_id"],) for row in imported
             if compact(summary(json.loads(row["payload"]))) not in entries],
        )
        for entry in entries.values():
            existing = db.execute(
                "SELECT m.payload FROM memories m WHERE m.shot_id='' AND m.status='active' AND "
                "COALESCE(json_extract(m.payload,'$.reuse_scope'),'global')='global' AND "
                "json_extract(m.payload,'$.title')=? AND " + usable_sql("m"),
                (entry["title"],),
            )
            if any(summary(json.loads(row["payload"])) == entry for row in existing):
                continue
            receipt = store.submit(
                _db=db, _shared=True, project_id=SHARED_PROJECT,
                source_ref=SHARED_SOURCE, **entry,
            )
            if receipt["status"] == "shared_withdrawn":
                db.execute(
                    "UPDATE memories SET status='active' WHERE memory_id=?",
                    (receipt["memory_id"],),
                )


if __name__ == "__main__":
    prepare_local_data()
