"""MySQL 5.7+ persistence adapter for the shared, parameterized knowledge SQL.

The workstation keeps its native SQLite backend. Only this independent service
contains the driver, dialect adaptation and one-time SQLite migration.
"""

import gzip
import json
import re
import secrets
import sqlite3
import tempfile
from contextlib import closing, contextmanager
from pathlib import Path


def identifier(value):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError("Invalid SQL identifier")
    return "`" + value + "`"


def mysql_sql(sql, columns):
    """Translate the bounded domain dialect, retaining parameterized values."""
    sql = re.sub(r"\bBEGIN(?: IMMEDIATE)?\b", "START TRANSACTION", sql, flags=re.I)
    sql = re.sub(r"INSERT OR IGNORE", "INSERT IGNORE", sql, flags=re.I)
    sql = re.sub(r"INSERT OR REPLACE", "REPLACE", sql, flags=re.I)
    sql = re.sub(r"ON CONFLICT\([^)]*\) DO UPDATE SET", "ON DUPLICATE KEY UPDATE", sql, flags=re.I)
    sql = re.sub(r"\bexcluded\.(\w+)", r"VALUES(\1)", sql, flags=re.I)
    aliases = {}
    while match := re.search(r"json_each\(", sql, re.I):
        depth, end, quoted = 1, match.end(), False
        while end < len(sql) and depth:
            char = sql[end]
            if char == "'":
                quoted = not quoted
            if not quoted:
                depth += (char == "(") - (char == ")")
            end += 1
        expression = sql[match.end() : end - 1]
        alias = re.match(r"\s+(?:AS\s+)?([A-Za-z_]\w*)", sql[end:], re.I)
        if not alias:
            raise ValueError("JSON array access requires an explicit alias")
        name = alias[1]
        aliases[name] = expression
        replacement = "fx_json_numbers AS " + name
        if re.search(r"JOIN\s*$", sql[: match.start()], re.I):
            replacement += f" ON {name}.n < JSON_LENGTH({expression})"
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
        r"json_extract\(([^,()]+),\s*('[^']+')\)",
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
    return sql.replace("%", "%%").replace("?", "%s")


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
    def __init__(self, cursor):
        self.cursor = cursor
        self.names = [item[0] for item in cursor.description] if cursor.description else []
        self.rowcount = cursor.rowcount

    def fetchone(self):
        row = self.cursor.fetchone()
        return Row(self.names, row) if row is not None else None

    def fetchall(self):
        return [Row(self.names, row) for row in self.cursor.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


class Connection:
    def __init__(self, backend, raw):
        self.backend, self.raw = backend, raw

    def execute(self, sql, parameters=()):
        if re.match(r"CREATE TABLE", sql, re.I):
            self.backend.ensure_table(sql)
            return self.execute("SELECT 1 FROM DUAL WHERE 0")
        cursor = self.raw.cursor()
        try:
            cursor.execute(mysql_sql(sql, self.backend.columns), parameters)
        except self.backend.driver.IntegrityError as exc:
            raise sqlite3.IntegrityError(str(exc.args[0])) from exc
        if (
            re.match(r"\s*(INSERT|REPLACE|UPDATE|DELETE)\b", sql, re.I)
            and "knowledge_state" not in sql
        ):
            affected = re.search(r"(?:INTO|UPDATE|FROM)\s+`?(\w+)", sql, re.I)
            if affected and affected[1] in self.backend.knowledge_tables:
                with self.raw.cursor() as update:
                    update.execute(
                        "UPDATE knowledge_state SET token=%s WHERE id=1", (secrets.token_hex(32),)
                    )
        return Cursor(cursor)

    def executemany(self, sql, rows):
        for row in rows:
            self.execute(sql, row)


class MySQLBackend:
    def __init__(self, *, host, port, user, password, database):
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
        self.columns = {}
        self.knowledge_tables = {
            "memories",
            "memory_aliases",
            "knowledge_sources",
            "user_rules",
            "learning_feedback",
            "learning_results",
            "memory_conflicts",
        }

    def raw_connection(self):
        connection = self.driver.connect(**self.options)
        with connection.cursor() as cursor:
            cursor.execute("SET SESSION TRANSACTION ISOLATION LEVEL SERIALIZABLE")
        return connection

    @contextmanager
    def connection(self):
        with closing(self.raw_connection()) as raw:
            try:
                yield Connection(self, raw)
                raw.commit()
            except BaseException:
                raw.rollback()
                raise

    def ensure_table(self, sql):
        name = re.search(r"CREATE TABLE(?: IF NOT EXISTS)?\s+(\w+)", sql, re.I)[1]
        if name in self.columns:
            return
        with closing(sqlite3.connect(":memory:")) as model:
            model.execute(sql)
            fields = model.execute(f"PRAGMA table_info({name})").fetchall()
            indexes = [
                model.execute(f"PRAGMA index_info({row[1]})").fetchall()
                for row in model.execute(f"PRAGMA index_list({name})")
                if row[2]
            ]
        names = [field[1] for field in fields]
        indexed = {item[2] for index in indexes for item in index} | {
            field[1] for field in fields if field[5]
        }
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
            unique = {tuple(item[2] for item in index) for index in indexes}
            primary = tuple(
                field[1] for field in sorted(fields, key=lambda field: field[5]) if field[5]
            )
            if primary:
                unique.add(primary)
            for keys in unique:
                definitions.append("UNIQUE KEY (" + ",".join(identifier(key) for key in keys) + ")")
        with self.connection() as db, db.raw.cursor() as cursor:
            cursor.execute(
                "CREATE TABLE IF NOT EXISTS "
                + identifier(name)
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
                for (sql,) in source.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ):
                    self.ensure_table(sql)
        with self.connection() as db, db.raw.cursor() as cursor:
            cursor.execute("CREATE TABLE IF NOT EXISTS fx_json_numbers (n INT PRIMARY KEY)")
            cursor.executemany(
                "INSERT IGNORE INTO fx_json_numbers VALUES (%s)", [(n,) for n in range(256)]
            )
            cursor.execute(
                "CREATE TABLE IF NOT EXISTS fx_metadata (name VARCHAR(191) PRIMARY KEY,value LONGTEXT)"
            )
        self.columns["fx_metadata"] = ["name", "value"]

    def migrate(self, source_path):
        """Copy rows once in one transaction, preserving row order and source data."""
        self.initialize()
        with self.connection() as db:
            if db.execute("SELECT value FROM fx_metadata WHERE name='initial_import'").fetchone():
                return {"already_imported": True}
        if not Path(source_path).is_file():
            raise RuntimeError("Upload the original memory.sqlite3 before the first MySQL association")
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
                for row in rows:
                    db.execute(
                        "INSERT INTO "
                        + identifier(table)
                        + " (`rowid`,"
                        + ",".join(identifier(name) for name in names)
                        + ") VALUES ("
                        + ",".join("?" for _ in row)
                        + ")",
                        row,
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
        """Logical backup includes knowledge and API key hashes in one transaction."""
        with self.connection() as db, gzip.open(target, "xt", encoding="utf-8") as stream:
            result = {}
            for table, columns in self.columns.items():
                columns = ["rowid", *columns] if table != "fx_metadata" else columns
                rows = db.execute(
                    "SELECT "
                    + ",".join(identifier(column) for column in columns)
                    + " FROM "
                    + identifier(table)
                    + (" ORDER BY rowid" if table != "fx_metadata" else "")
                ).fetchall()
                result[table] = {"columns": columns, "rows": [list(row) for row in rows]}
            json.dump(result, stream, ensure_ascii=False)
