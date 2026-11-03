"""Copy a verified local data snapshot without replacing existing destinations."""

import json
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from fx_agent.archive import CaseArchive, copy_verified, publish_directory


def migrate_data(source: Path, target: Path, *, exclude_import_history: bool = False) -> dict:
    """Keep the source intact; stop archive writers before calling this operation."""
    source, target = source.resolve(strict=True), target.resolve()
    if source.is_relative_to(target) or target.is_relative_to(source):
        raise ValueError("Migration roots must not overlap")
    if target.exists():
        raise FileExistsError("Migration target already exists; refusing to merge")
    database = source / "memory.sqlite3"
    if not database.is_file():
        raise FileNotFoundError(database)
    files = [
        path
        for path in source.rglob("*")
        if not (exclude_import_history and path.relative_to(source).parts[0] == "imports")
    ]
    if any(path.is_symlink() or path.is_junction() for path in files):
        raise ValueError("Migration source contains links; refusing to copy external data")
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".fx-data-", dir=target.parent)).resolve()
    count = 0
    try:
        with closing(sqlite3.connect(database, timeout=10)) as guard:
            guard.execute("BEGIN IMMEDIATE")  # Block database writes while snapshotting.
            with (
                closing(sqlite3.connect(database)) as reader,
                closing(sqlite3.connect(stage / database.name)) as writer,
            ):
                reader.backup(writer)
                if writer.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Migrated database failed integrity validation")
            for path in files:
                if not path.is_file() or path in {
                    database,
                    source / "memory.sqlite3-wal",
                    source / "memory.sqlite3-shm",
                    source / "memory.sqlite3-journal",
                }:
                    continue
                destination = stage / path.relative_to(source)
                destination.parent.mkdir(parents=True, exist_ok=True)
                copy_verified(path, destination)
                count += 1
            archive = CaseArchive(stage)
            cases = stage / "cases"
            for folder in cases.iterdir() if cases.exists() else []:
                if folder.name == ".staging" and folder.is_dir() and not any(folder.iterdir()):
                    continue
                manifest = folder / "manifest.json"
                case = json.loads(manifest.read_text(encoding="utf-8"))
                if not archive.verify(folder.name, case["project_id"])["valid"]:
                    raise ValueError("Migrated case failed checksum validation")
            with closing(sqlite3.connect(stage / database.name)) as snapshot:
                tables = {
                    row[0]
                    for row in snapshot.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
                for table in ("memories", "learning_feedback", "template_studies"):
                    if table not in tables:
                        continue
                    for (payload,) in snapshot.execute(f"SELECT payload FROM {table}"):
                        record = json.loads(payload)
                        if (
                            record.get("case_id")
                            and not archive.verify(record["case_id"], record["project_id"])["valid"]
                        ):
                            raise ValueError("Database references an invalid case")
            publish_directory(stage, target)
    finally:
        if stage.exists():
            if stage.parent != target.parent or not stage.name.startswith(".fx-data-"):
                raise ValueError("Unsafe migration cleanup path")
            shutil.rmtree(stage)
    return {
        "source": str(source),
        "data_root": str(target),
        "copied_files": count,
        "database_integrity": "ok",
        "original_preserved": True,
    }
