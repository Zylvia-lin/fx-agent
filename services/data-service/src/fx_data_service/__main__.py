"""Docker entrypoint: associate existing data once, then own the local database."""

import os
from pathlib import Path

import uvicorn

from fx_data_service.app import ServiceConfig, create_app, snapshot_database


def secret(name):
    path = os.getenv(name + "_FILE")
    return Path(path).read_text(encoding="utf-8").strip() if path else os.getenv(name, "")


def main():
    key = secret("FX_AGENT_API_KEY")
    settings = ServiceConfig(
        data_root=Path(os.getenv("FX_AGENT_DATA_ROOT", "/data")),
        database_root=Path(os.getenv("FX_AGENT_DATABASE_ROOT", "/database")),
        api_key=key,
        admin_password=secret("FX_AGENT_ADMIN_PASSWORD"),
        backup_root=Path(os.environ["FX_AGENT_BACKUP_ROOT"])
        if os.getenv("FX_AGENT_BACKUP_ROOT")
        else None,
    )
    source = settings.data_root / "memory.sqlite3"
    target = settings.database_root / "memory.sqlite3"
    backend = None
    engine = os.getenv("FX_AGENT_DB_ENGINE", "mysql")
    if engine == "mysql":
        from fx_data_service.mysql import MySQLBackend

        backend = MySQLBackend(
            host=os.getenv("FX_AGENT_MYSQL_HOST", "127.0.0.1"),
            port=os.getenv("FX_AGENT_MYSQL_PORT", "3306"),
            user=os.environ["FX_AGENT_MYSQL_USER"],
            password=secret("FX_AGENT_MYSQL_PASSWORD"),
            database=os.environ["FX_AGENT_MYSQL_DATABASE"],
        )
        print("MySQL knowledge association:", backend.migrate(source))
    elif engine == "sqlite":
        if source.exists() and not target.exists():
            snapshot_database(source, target)
        elif not source.exists() and not target.exists():
            raise RuntimeError(
                "No existing knowledge database found; refusing to silently create an empty deployment"
            )
    else:
        raise ValueError("FX_AGENT_DB_ENGINE must be mysql or sqlite")
    uvicorn.run(
        create_app(settings, backend),
        host=os.getenv("FX_AGENT_HOST", "0.0.0.0"),
        port=int(os.getenv("FX_AGENT_PORT", "18790")),
        workers=1,
    )


if __name__ == "__main__":
    main()
