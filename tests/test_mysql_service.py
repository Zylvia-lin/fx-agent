"""Dialect boundary: single-machine storage remains SQLite."""

from fx_data_service.mysql import mysql_sql, project_sql


def test_mysql57_module_json_and_upsert_translation():
    sql = "SELECT payload FROM memories WHERE EXISTS (SELECT 1 FROM json_each(COALESCE(json_extract(payload,'$.modules'),'[\"unclassified\"]')) AS module_tags WHERE module_tags.value=?) ORDER BY rowid LIMIT ?"
    converted = mysql_sql(sql, {})
    assert "json_each" not in converted.lower()
    assert "fx_json_numbers" in converted
    assert "JSON_UNQUOTE" in converted
    assert converted.count("%s") == 2
    sql = "INSERT INTO task_gates VALUES (?,?) ON CONFLICT(task_id) DO UPDATE SET payload=excluded.payload"
    converted = mysql_sql(sql, {"task_gates": ["task_id", "payload"]})
    assert "ON DUPLICATE KEY UPDATE payload=VALUES(payload)" in converted
    assert "(`task_id`,`payload`)" in converted
    assert "`stored`" in mysql_sql("SELECT original,stored FROM files WHERE device_id=?", {})


def test_project_tables_preserve_literals_aliases_and_json_paths():
    sql = "SELECT memories.payload FROM `memories` WHERE json_extract(payload,'$.memories')='memories' AND title=\"memories\""
    result = project_sql(sql, {"memories"}, "p_123_")
    assert result.startswith("SELECT `p_123_memories`.payload FROM `p_123_memories`")
    assert "'$.memories'" in result and "='memories'" in result
    assert 'title="memories"' in result
    assert project_sql(sql, {"memories"}, "") == sql
