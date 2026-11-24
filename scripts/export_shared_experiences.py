"""Print proposed summaries for explicitly selected local memory IDs."""

import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from fx_agent.config import data_root, database_path
from fx_agent.memory import usable_sql
from fx_agent.shared_knowledge import summary


def export_summaries(database: Path, memory_ids: list[str]) -> list[dict]:
    uri = database.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        result = []
        for memory_id in dict.fromkeys(memory_ids):
            row = db.execute(
                "SELECT m.payload FROM memories m WHERE m.memory_id=? AND m.status='active' "
                "AND m.shot_id='' AND COALESCE(json_extract(m.payload,'$.reuse_scope'),'global')='global' "
                "AND NOT COALESCE(json_extract(m.payload,'$.project_dependent'),0) AND " + usable_sql("m"),
                (memory_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Memory is missing or unsuitable for sharing: {memory_id}")
            result.append(summary(json.loads(row[0])))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("memory_id", nargs="+")
    parser.add_argument("--database", type=Path, default=database_path(data_root()))
    args = parser.parse_args()
    print(json.dumps(export_summaries(args.database, args.memory_id), ensure_ascii=False, indent=2))
