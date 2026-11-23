"""MySQL 5.7+ persistence adapter for the shared, parameterized knowledge SQL.

The workstation keeps its native SQLite backend. Only this independent service
contains the driver, dialect adaptation and one-time SQLite migration.
"""

import gzip
import hashlib
import json
import os
import queue
import re
import secrets
import sqlite3
import tempfile
import time
from contextlib import closing, contextmanager, suppress
from pathlib import Path

_LITERAL_RE = re.compile(r"'(?:''|\\.|[^'\\])*'|\"(?:\"\"|\\.|[^\"\\])*\"")
_JSON_NUMBERS_LIMIT = 1024
_RETRYABLE_MYSQL_ERRORS = {1205, 1213}


def identifier(value):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError("Invalid SQL identifier")
    return "`" + value + "`"


def project_sql(sql, tables, prefix):
    """Rename known table identifiers without touching SQL literals or values."""
    if not prefix:
        return sql
    identifier(prefix)
    tokens = r"'(?:''|\\.|[^'\\])*'|\"(?:\"\"|\\.|[^\"\\])*\"|`[^`]+`|[A-Za-z_]\w*"

    def replace(match):
        token = match[0]
        if token[0] in ("'", '"'):
            return token
        name = token.strip("`")
        return identifier(prefix + name) if name in tables else token

    return re.sub(tokens, replace, sql)


def _protect_literals(sql: str) -> tuple[str, list[str]]:
    literals: list[str] = []

    def _stash(match):
        idx = len(literals)
        literals.append(match[0])
        return f"\x01LIT{idx}\x02"

    return _LITERAL_RE.sub(_stash, sql), literals


def _restore_literals(sql: str, literals: list[str], *, escape_percent: bool = False) -> str:
    def _unstash(match):
        text = literals[int(match[1])]
        return text.replace("%", "%%") if escape_percent else text

    return re.sub(r"\x01LIT(\d+)\x02", _unstash, sql)


def mysql_sql(sql, columns):
    """Translate the bounded domain dialect, preserving string literals and parameters."""
    sql, literals = _protect_literals(sql)
    sql = re.sub(r"\bBEGIN(?: IMMEDIATE)?\b", "START TRANSACTION", sql, flags=re.I)
    sql = re.sub(r"INSERT OR IGNORE", "INSERT IGNORE", sql, flags=re.I)
    sql = re.sub(r"INSERT OR REPLACE", "REPLACE", sql, flags=re.I)
    sql = re.sub(r"ON CONFLICT\([^)]*\) DO UPDATE SET", "ON DUPLICATE KEY UPDATE", sql, flags=re.I)
    sql = re.sub(r"\bexcluded\.(\w+)", r"VALUES(\1)", sql, flags=re.I)
    aliases = {}
    while match := re.search(r"json_each\(", sql, re.I):
        depth, end = 1, match.end()
        while end < len(sql) and depth:
            char = sql[end]
            depth += (char == "(") - (char == ")")
            end += 1
        expression = sql[match.end() : end - 1]
        alias = re.match(r"\s+(?:AS\s+)?([A-Za-z_]\w*)", sql[end:], re.I)
        if not alias:
            raise ValueError("JSON array access requires an explicit alias")
        name = alias[1]
        aliases[name] = expression
        if re.search(r"JOIN\s*$", sql[: match.start()], re.I):
            replacement = f"fx_json_numbers AS {name} ON {name}.n < JSON_LENGTH({expression})"
        else:
            # Bound FROM json_each(...) with JSON_LENGTH so phantom NULL rows never appear.
            replacement = f"(SELECT n FROM fx_json_numbers WHERE n < JSON_LENGTH({expression})) AS {name}"
        sql = sql[: match.start()] + replacement + sql[end + alias.end() :]
    for alias, expression in aliases.items():
        sql = re.sub(
            rf"\b{alias}\.value\b",
            lambda m, a=alias, e=expression: (
                f"JSON_UNQUOTE(JSON_EXTRACT({e},CONCAT('$[',{a}.n,']')))"
            ),
            sql,
        )
    sql = re.sub(
        r"json_extract\(([^,()]+),\s*(\x01LIT\d+\x02)\)",
        lambda m: (
            f"(CASE WHEN JSON_TYPE(JSON_EXTRACT({m[1]},{m[2]}))='NULL' THEN NULL ELSE JSON_UNQUOTE(JSON_EXTRACT({m[1]},{m[2]})) END)"
        ),
        sql,
        flags=re.I,
    )
    sql = re.sub(r"\bAS cursor\b", "AS `cursor`", sql, flags=re.I)
    sql = re.sub(r"(?<!`)\bstored\b(?!`)", "`stored`", sql, flags=re.I)
    match = re.match(r"(INSERT(?: IGNORE)? INTO|REPLACE INTO)\s+(\w+)\s+VALUES", sql, re.I)
    if match and match[2] in columns:
        names = ",".join(identifier(c) for c in columns[match[2]])
        sql = (
            sql[: match.start()]
            + f"{match[1]} {identifier(match[2])} ({names}) VALUES"
            + sql[match.end() :]
        )
    match = re.search(r"SELECT\s+(rowid\s*,\s*)?\*\s+FROM\s+(\w+)", sql, re.I)
    if match and match[2] in columns:
        names = ",".join(identifier(c) for c in columns[match[2]])
        sql = (
            sql[: match.start()]
            + "SELECT "
            + (match[1] or "")
            + names
            + " FROM "
            + identifier(match[2])
            + sql[match.end() :]
        )
    sql = sql.replace("%", "%%").replace("?", "%s")
    return _restore_literals(sql, literals, escape_percent=True)


class Row:
    def __init__(self, names, values):
        self.names, self.values = names, values

    def __getitem__(self, key):
        return self.values[key] if isinstance(key, int) else self.values[self.names.index(key)]

    def __iter__(self):
        return iter(self.values)

    def keys(self):
        return self.names


class Cursor:
    """Materialize rows eagerly and close the underlying driver cursor immediately."""

    def __init__(self, cursor):
        try:
            self.names = [item[0] for item in cursor.description] if cursor.description else []
            self.rowcount = cursor.rowcount
            self._rows = [Row(self.names, row) for row in cursor.fetchall()] if cursor.description else []
            self._pos = 0
        finally:
            cursor.close()

    def fetchone(self):
        if self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        return row

    def fetchall(self):
        rest = self._rows[self._pos :]
        self._pos = len(self._rows)
        return rest

    def __iter__(self):
        return iter(self.fetchall())


class Connection:
    def __init__(self, backend, raw):
        self.backend, self.raw = backend, raw

    def _run_with_retry(self, fn):
        for attempt in range(4):
            try:
                return fn()
            except self.backend.driver.IntegrityError as exc:
                raise sqlite3.IntegrityError(str(exc.args[0])) from exc
            except self.backend.driver.OperationalError as exc:
                code = exc.args[0] if exc.args else None
                if code in _RETRYABLE_MYSQL_ERRORS and attempt < 3:
                    time.sleep(0.05 * (2**attempt))
                    continue
                raise

    def execute(self, sql, parameters=()):
        if re.match(r"CREATE TABLE", sql, re.I):
            self.backend.ensure_table(sql)
            return self.execute("SELECT 1 FROM DUAL WHERE 0")
        translated = self.backend.sql(mysql_sql(sql, self.backend.columns))

        def _exec():
            cursor = self.raw.cursor()
            try:
                cursor.execute(translated, parameters)
                return Cursor(cursor)
            except BaseException:
                with suppress(Exception):
                    cursor.close()
                raise

        result = self._run_with_retry(_exec)
        if (
            re.match(r"\s*(INSERT|REPLACE|UPDATE|DELETE)\b", sql, re.I)
            and "knowledge_state" not in sql
        ):
            affected = re.search(r"(?:INTO|UPDATE|FROM)\s+`?(\w+)", sql, re.I)
            if affected and affected[1] in self.backend.knowledge_tables:
                with self.raw.cursor() as update:
                    update.execute(
                        self.backend.sql("UPDATE knowledge_state SET token=%s WHERE id=1"),
                        (secrets.token_hex(32),),
                    )
        return result

    def executemany(self, sql, rows):
        rows = list(rows)
        if not rows:
            return
        translated = self.backend.sql(mysql_sql(sql, self.backend.columns))

        def _exec():
            with self.raw.cursor() as cursor:
                cursor.executemany(translated, rows)

        self._run_with_retry(_exec)
        if (
            re.match(r"\s*(INSERT|REPLACE|UPDATE|DELETE)\b", sql, re.I)
            and "knowledge_state" not in sql
        ):
            affected = re.search(r"(?:INTO|UPDATE|FROM)\s+`?(\w+)", sql, re.I)
            if affected and affected[1] in self.backend.knowledge_tables:
                with self.raw.cursor() as update:
                    update.execute(
                        self.backend.sql("UPDATE knowledge_state SET token=%s WHERE id=1"),
                        (secrets.token_hex(32),),
                    )


class MySQLBackend:
    def __init__(self, *, host, port, user, password, database, prefix="", ssl_ca: str = ""):
        import pymysql

        self.driver = pymysql
        self.options = dict(
            host=host,
            port=int(port),
            user=user,
            password=password,
            database=database,
            charset="utf8mb4",
            autocommit=False,
            connect_timeout=10,
            read_timeout=120,
            write_timeout=120,
        )
        ca_path = ssl_ca or os.getenv("FX_AGENT_MYSQL_SSL_CA", "")
        if ca_path:
            self.options["ssl"] = {"ca": ca_path}
        self.columns = {}
        if prefix:
            identifier(prefix)
        self.prefix = prefix
        self._pool: queue.LifoQueue = queue.LifoQueue(maxsize=16)
        self.knowledge_tables = {
            "memories",
            "memory_aliases",
            "knowledge_sources",
            "user_rules",
            "learning_feedback",
            "learning_results",
            "memory_conflicts",
        }

    def sql(self, sql):
        return project_sql(sql, self.columns, self.prefix)

    def for_project(self, project):
        return MySQLBackend(
            **{key: self.options[key] for key in ("host", "port", "user", "password", "database")},
            prefix="p_" + hashlib.sha256(project.encode()).hexdigest()[:24] + "_",
            ssl_ca=(self.options.get("ssl") or {}).get("ca", ""),
        )

    def raw_connection(self):
        connection = self.driver.connect(**self.options)
        with connection.cursor() as cursor:
            cursor.execute("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        return connection

    def _borrow(self):
        while True:
            try:
                raw = self._pool.get_nowait()
            except queue.Empty:
                return self.raw_connection()
            try:
                raw.ping(reconnect=True)
                return raw
            except Exception:
                with suppress(Exception):
                    raw.close()

    def _release(self, raw, *, healthy: bool):
        if healthy:
            try:
                raw.rollback()
                self._pool.put_nowait(raw)
                return
            except Exception:
                pass
        with suppress(Exception):
            raw.close()

    @contextmanager
    def connection(self):
        raw = self._borrow()
        healthy = True
        try:
            yield Connection(self, raw)
            raw.commit()
        except BaseException:
            try:
                raw.rollback()
            except Exception:
                healthy = False
            raise
        finally:
            self._release(raw, healthy=healthy)

    def ensure_table(self, sql, extra_indexes: list[tuple[str, tuple[str, ...]]] | None = None):
        match = re.search(r"CREATE TABLE(?: IF NOT EXISTS)?\s+([A-Za-z_][A-Za-z0-9_]*)", sql, re.I)
        if not match:
            raise ValueError("Unsupported CREATE TABLE statement")
        name = match[1]
        identifier(name)
        if name in self.columns:
            return
        with closing(sqlite3.connect(":memory:")) as model:
            model.execute(sql)
            fields = model.execute(f"PRAGMA table_info({identifier(name)})").fetchall()
            all_indexes = []
            for row in model.execute(f"PRAGMA index_list({identifier(name)})"):
                idx_name = row[1]
                cols = tuple(
                    item[2]
                    for item in model.execute(
                        f"PRAGMA index_info(\"{idx_name.replace('\"', '\"\"')}\")"
                    ).fetchall()
                    if item[2]
                )
                if cols:
                    all_indexes.append((bool(row[2]), cols))
        non_unique_indexes: list[tuple[str, ...]] = [
            cols for is_unique, cols in all_indexes if not is_unique
        ]
        for _, cols in extra_indexes or []:
            if cols and cols not in non_unique_indexes:
                non_unique_indexes.append(cols)
        unique = {cols for is_unique, cols in all_indexes if is_unique}
        names = [field[1] for field in fields]
        indexed = (
            {col for cols in unique for col in cols}
            | {col for cols in non_unique_indexes for col in cols}
            | {field[1] for field in fields if field[5]}
        )
        definitions = []
        for _, field, kind, required, _default, primary in fields:
            datatype = (
                "BIGINT"
                if kind.upper() == "INTEGER"
                else "DOUBLE"
                if kind.upper() == "REAL"
                else "VARCHAR(191)"
                if field in indexed
                else "LONGTEXT"
            )
            if name == "files" and field == "original":
                datatype = "LONGTEXT"
            definitions.append(
                identifier(field) + " " + datatype + (" NOT NULL" if required or primary else "")
            )
        definitions.append("`rowid` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT UNIQUE")
        if name == "files":
            definitions += [
                "`path_hash` CHAR(64) GENERATED ALWAYS AS (SHA2(original,256)) STORED",
                "UNIQUE KEY (`device_id`,`path_hash`)",
            ]
        else:
            primary = tuple(
                field[1] for field in sorted(fields, key=lambda field: field[5]) if field[5]
            )
            if primary:
                unique.add(primary)
            for keys in sorted(unique):
                definitions.append("UNIQUE KEY (" + ",".join(identifier(key) for key in keys) + ")")
            for keys in non_unique_indexes:
                if keys not in unique and all(k in names for k in keys):
                    definitions.append("KEY (" + ",".join(identifier(key) for key in keys) + ")")
        with self.connection() as db, db.raw.cursor() as cursor:
            cursor.execute(
                "CREATE TABLE IF NOT EXISTS "
                + identifier(self.prefix + name)
                + " ("
                + ",".join(definitions)
                + ") ENGINE=InnoDB ROW_FORMAT=DYNAMIC DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin"
            )
        self.columns[name] = names

    def initialize(self):
        from fx_agent.data_context import service_scope
        from fx_agent.store import Store

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with service_scope(root, root, ""):
                store = Store(root)
            with closing(sqlite3.connect(store.path)) as source:
                # Collect non-unique indexes from SQLite schema so MySQL keeps all query indexes.
                table_indexes: dict[str, list[tuple[str, tuple[str, ...]]]] = {}
                for tbl_name, idx_name in source.execute(
                    "SELECT tbl_name, name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"
                ):
                    cols = tuple(
                        item[2]
                        for item in source.execute(
                            f"PRAGMA index_info(\"{idx_name.replace('\"', '\"\"')}\")"
                        ).fetchall()
                        if item[2]
                    )
                    if cols:
                        table_indexes.setdefault(tbl_name, []).append((idx_name, cols))
                for tbl_name, sql in source.execute(
                    "SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ):
                    self.ensure_table(sql, extra_indexes=table_indexes.get(tbl_name))
        with self.connection() as db, db.raw.cursor() as cursor:
            cursor.execute("CREATE TABLE IF NOT EXISTS fx_json_numbers (n INT PRIMARY KEY)")
            cursor.executemany(
                "INSERT IGNORE INTO fx_json_numbers VALUES (%s)",
                [(n,) for n in range(_JSON_NUMBERS_LIMIT)],
            )
            cursor.execute(
                "CREATE TABLE IF NOT EXISTS "
                + identifier(self.prefix + "fx_metadata")
                + " (name VARCHAR(191) PRIMARY KEY,value LONGTEXT)"
            )
        self.columns["fx_metadata"] = ["name", "value"]
        if self.prefix:
            with self.connection() as db:
                db.execute(
                    "INSERT OR IGNORE INTO knowledge_state VALUES (1,?)", (secrets.token_hex(32),)
                )

    def migrate(self, source_path):
        """Copy rows once in one transaction, preserving row order and source data."""
        self.initialize()
        with self.connection() as db:
            if db.execute("SELECT value FROM fx_metadata WHERE name='initial_import'").fetchone():
                return {"already_imported": True}
        if not Path(source_path).is_file():
            raise RuntimeError(
                "Upload the original memory.sqlite3 before the first MySQL association"
            )
        with (
            self.connection() as db,
            closing(sqlite3.connect(Path(source_path).as_uri() + "?mode=ro", uri=True)) as source,
        ):
            if db.execute("SELECT value FROM fx_metadata WHERE name='initial_import'").fetchone():
                return {"already_imported": True}
            if (
                db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
                or db.execute("SELECT COUNT(*) FROM user_rules").fetchone()[0]
            ):
                raise ValueError(
                    "MySQL knowledge tables are not empty; refusing automatic overwrite"
                )
            counts = {}
            for table in self.columns:
                if not source.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone():
                    continue
                names = self.columns[table]
                rows = source.execute(
                    "SELECT rowid," + ",".join(names) + " FROM " + table + " ORDER BY rowid"
                ).fetchall()
                if rows:
                    db.executemany(
                        "INSERT INTO "
                        + identifier(table)
                        + " (`rowid`,"
                        + ",".join(identifier(name) for name in names)
                        + ") VALUES ("
                        + ",".join("?" for _ in range(len(names) + 1))
                        + ")",
                        rows,
                    )
                counts[table] = len(rows)
                if db.execute("SELECT COUNT(*) FROM " + identifier(table)).fetchone()[0] != len(
                    rows
                ):
                    raise ValueError("Migration count mismatch: " + table)
            db.execute(
                "INSERT INTO fx_metadata (name,value) VALUES (?,?)",
                ("initial_import", json.dumps(counts)),
            )
            return counts

    def backup(self, target):
        """Stream logical backup table-by-table to a temp file and atomically rename."""
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(f".{target.name}.{secrets.token_hex(8)}.tmp")
        try:
            with self.connection() as db, gzip.open(temp, "xt", encoding="utf-8") as stream:
                stream.write("{")
                first_table = True
                for table, cols in self.columns.items():
                    columns = ["rowid", *cols] if table != "fx_metadata" else list(cols)
                    if not first_table:
                        stream.write(",")
                    first_table = False
                    stream.write(json.dumps(table, ensure_ascii=False) + ":{")
                    stream.write('"columns":' + json.dumps(columns, ensure_ascii=False) + ',"rows":[')
                    first_row = True
                    query = (
                        "SELECT "
                        + ",".join(identifier(column) for column in columns)
                        + " FROM "
                        + identifier(table)
                        + (" ORDER BY rowid" if table != "fx_metadata" else "")
                    )
                    with db.raw.cursor() as cursor:
                        cursor.execute(self.sql(query))
                        while batch := cursor.fetchmany(500):
                            for row in batch:
                                if not first_row:
                                    stream.write(",")
                                first_row = False
                                stream.write(json.dumps(list(row), ensure_ascii=False))
                    stream.write("]}")
                stream.write("}")
            temp.replace(target)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
