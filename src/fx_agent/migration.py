"""Copy a verified local data snapshot without replacing existing destinations."""

import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from fx_agent.baselines import validate_baselines
from fx_agent.fs_utils import copy_verified, publish_directory


def migrate_data(source: Path, target: Path) -> dict:
    """Keep the source intact; stop local writers before calling this operation."""
    source, target = source.resolve(strict=True), target.resolve()
    if source.is_relative_to(target) or target.is_relative_to(source):
        raise ValueError("Migration roots must not overlap")
    if target.exists():
        raise FileExistsError("Migration target already exists; refusing to merge")
    database = source / "memory.sqlite3"
    if not database.is_file():
        raise FileNotFoundError(database)
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".fx-data-", dir=target.parent)).resolve()
    count = 0
    try:
        with closing(sqlite3.connect(database, timeout=10)) as guard:
            guard.execute("BEGIN IMMEDIATE")  # Block database writes while snapshotting.
            files = list(source.rglob("*"))
            if any(path.is_symlink() or path.is_junction() for path in files):
                raise ValueError("Migration source contains links; refusing to copy external data")
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
                    source / ".baseline.lock",
                }:
                    continue
                destination = stage / path.relative_to(source)
                destination.parent.mkdir(parents=True, exist_ok=True)
                copy_verified(path, destination)
                count += 1
            baseline_check = validate_baselines(stage)
            if not baseline_check["valid"]:
                raise ValueError("Migrated baseline failed validation: " + baseline_check["error"])
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
