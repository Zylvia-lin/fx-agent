"""Project-scoped Markdown supplements derived from accepted learning results."""

import json
import logging
import os
import sqlite3
import uuid
from pathlib import Path

from fx_agent.archive import contained, file_hash
from fx_agent.modules import CATALOGS, normalize_modules
from fx_agent.store import Store, compact, scope_id

START = "<!-- FX MODULE LEARNING START -->"
END = "<!-- FX MODULE LEARNING END -->"


def safe_text(value: str) -> str:
    return value.replace(START, "&lt;!-- FX MODULE LEARNING START --&gt;").replace(
        END, "&lt;!-- FX MODULE LEARNING END --&gt;"
    )


class ModuleGuides(Store):
    """SQLite remains authoritative; rebuilding never changes the upstream package."""

    def _base(self, project_id: str) -> Path:
        expected = self.root / "modules" / "global"
        resolved = contained(self.root, "modules/global")
        if resolved != expected:
            raise ValueError("Module guide directories must not be links")
        return expected

    def _path(self, project_id: str, software: str, module: str) -> Path:
        base = self._base(project_id)
        expected = base / software / (module + ".md")
        if contained(base, str(Path(software) / (module + ".md"))) != expected:
            raise ValueError("Module guide files must not be links")
        return expected

    def _write(self, path: Path, section: str, header: str) -> bool:
        original = path.read_text(encoding="utf-8") if path.exists() else None
        if original is None:
            content = header + section
        else:
            if (
                original.count(START) != 1
                or original.count(END) != 1
                or original.index(START) > original.index(END)
            ):
                raise ValueError(
                    f"Invalid managed Markdown section: {path.name}; original preserved"
                )
            left, right = original.index(START), original.index(END) + len(END)
            content = original[:left] + section.rstrip() + original[right:]
        if original == content:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            current = path.read_text(encoding="utf-8") if path.exists() else None
            if current != original:
                raise ValueError("Module guide changed during update; original preserved")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return True

    @staticmethod
    def _section(records: list[dict]) -> tuple[str, int]:
        groups = {}
        for record in records:
            key = tuple(record[name] for name in ("title", "method", "reason", "applies_when"))
            groups.setdefault(key, []).append(record)
        lines = [START, "## 从已接受结果提炼的工作流补充", ""]
        if not groups:
            lines.append("当前无有效的全局模块总结；旧方案已撤销或尚未完成提炼。")
        for (title, method, reason, conditions), sources in sorted(groups.items()):
            lines += [
                f"### {safe_text(title)}",
            ]
            conflicts = sorted({cid for source in sources for cid in source.get("conflict_memory_ids", [])})
            if conflicts:
                lines.append(
                    "**存在争议：复用前先核查冲突记录，不应直接作为已确定的方法。** 冲突记录："
                    + "、".join(conflicts)
                )
            lines += [
                f"适用条件：{safe_text(conditions)}",
                "",
                safe_text(method),
                "",
                f"原因与注意：{safe_text(reason)}",
                "",
            ]
            for source in sorted(sources, key=lambda item: item["memory_id"]):
                lines.append(
                    f"来源：memory={source['memory_id']}；feedback={source['origin_feedback_id']}；case={source['case_id']}"
                )
            lines.append("")
        return "\n".join(lines) + "\n" + END, len(groups)

    def refresh(self, project_id: str = "", *, module: str = "", software: str = "houdini") -> dict:
        if project_id:
            scope_id.validate_python(project_id)
        if module:
            module = normalize_modules([module], software)[0]
        selected = (software, module) if module else None
        base = self._base(project_id)
        updated, counts, view_conflicts = [], {}, {}
        # Serialize cooperative writers so an older DB snapshot cannot overwrite a newer view.
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            from fx_agent.memory import usable_sql

            token = db.execute("SELECT token FROM knowledge_state WHERE id=1").fetchone()[0]
            rows = db.execute(
                "SELECT m.payload,m.memory_id,r.feedback_id,tags.value AS module FROM memories m LEFT JOIN learning_results r ON r.memory_id=m.memory_id "
                "LEFT JOIN learning_feedback f ON f.feedback_id=r.feedback_id "
                "JOIN json_each(COALESCE(json_extract(m.payload,'$.modules'),'[\"unclassified\"]')) tags "
                "WHERE m.status='active' AND COALESCE(json_extract(m.payload,'$.reuse_scope'),CASE WHEN m.shot_id='' THEN 'global' ELSE 'shot' END)='global' AND "
                + usable_sql("m")
                + " AND NOT EXISTS (SELECT 1 FROM knowledge_sources ks JOIN memories k ON k.memory_id=ks.memory_id WHERE ks.source_id=m.memory_id AND k.status='active' AND "
                + usable_sql("k")
                + " AND EXISTS (SELECT 1 FROM json_each(COALESCE(json_extract(k.payload,'$.modules'),'[\"unclassified\"]')) replacement_tags WHERE replacement_tags.value=tags.value)) "
                "AND (json_extract(m.payload,'$.dream_run_id') IS NOT NULL OR (json_extract(f.payload,'$.outcome')='accepted' "
                "AND f.revision=(SELECT MAX(x.revision) FROM learning_feedback x WHERE x.project_id=f.project_id AND x.shot_id=f.shot_id AND x.attempt_id=f.attempt_id AND x.software=f.software)))"
                + (" AND tags.value=? AND COALESCE(json_extract(m.payload,'$.software'),'houdini')=?" if module else ""),
                (module, software) if module else (),
            ).fetchall()
            groups, conflicts_by_memory = {}, {}
            for row in rows:
                record = json.loads(row["payload"])
                record["origin_feedback_id"] = row["feedback_id"] or ""
                record_software = record.get("software", "houdini")
                modules = normalize_modules(record.get("modules", ["unclassified"]), record_software)
                if row["memory_id"] not in conflicts_by_memory:
                    conflicts = db.execute(
                        "SELECT DISTINCT note.memory_id FROM knowledge_sources cf JOIN memories note ON note.memory_id=cf.memory_id "
                        "WHERE note.status='disputed' AND (cf.source_id=? OR cf.source_id IN (SELECT source_id FROM knowledge_sources WHERE memory_id=?)) ORDER BY note.memory_id",
                        (row["memory_id"], row["memory_id"]),
                    ).fetchall()
                    conflicts_by_memory[row["memory_id"]] = [conflict[0] for conflict in conflicts]
                record["conflict_memory_ids"] = conflicts_by_memory[row["memory_id"]]
                if row["module"] in modules and row["module"] != "unclassified":
                    groups.setdefault((record_software, row["module"]), []).append(record)
            known = set(groups)
            if base.exists():
                for path in base.glob("*/*.md"):
                    software, module = path.parent.name, path.stem
                    catalog = (
                        CATALOGS.get(software, {})
                    )
                    if module in catalog and module != "unclassified":
                        known.add((software, module))
            if selected:
                known &= {selected}
            for software, module in sorted(known):
                label, topic = CATALOGS[software][module]
                section, count = self._section(groups.get((software, module), []))
                header = (
                    f"# {label}：全局工作流补充\n\n上游准则：get_workflow_guide({topic!r})\n\n"
                    if topic
                    else f"# {label}：全局工作流补充\n\n基础工作流：{software}-workflow\n\n"
                )
                header += "跨项目共享，按条目中的条件复用。归档在提炼时校验，复用前重新核验案例；不代表因果解释全部证实。\n\n自动区块内不要手改；手写补充放在区块外，其内容需另行核实。\n\n"
                path = self._path(project_id, software, module)
                if self._write(path, section, header):
                    updated.append(str(path.relative_to(self.root)))
                counts[f"{software}/{module}"] = count
                conflicts = sorted({cid for record in groups.get((software, module), []) for cid in record["conflict_memory_ids"]})
                view_conflicts[f"{software}/{module}"] = conflicts
                db.execute(
                    "INSERT OR REPLACE INTO module_guide_state VALUES (?,?,?,?,?,?)",
                    (software, module, token, file_hash(path), count, compact(conflicts)),
                )
            if selected and selected not in known:
                db.execute(
                    "INSERT OR REPLACE INTO module_guide_state VALUES (?,?,?,?,?,?)",
                    (*selected, token, "", 0, "[]"),
                )
        return {
            "state": "updated" if updated else "current",
            "updated_paths": updated[:8],
            "omitted_paths": max(0, len(updated) - 8),
            "document_count": len(counts),
            "lesson_counts": counts,
            "conflicts": view_conflicts,
        }

    def _cached(self, project_id: str, software: str, module: str) -> dict | None:
        """Reuse only a view whose DB token and actual file still match."""
        path = self._path(project_id, software, module)
        with self.connection() as db:
            row = db.execute(
                "SELECT view.content_hash,view.lesson_count,view.conflict_ids FROM module_guide_state view "
                "JOIN knowledge_state state ON state.id=1 AND state.token=view.token "
                "WHERE view.software=? AND view.module=?",
                (software, module),
            ).fetchone()
        if row and row["content_hash"] == (file_hash(path) if path.is_file() else ""):
            return {
                "lesson_counts": {f"{software}/{module}": row["lesson_count"]},
                "conflicts": {f"{software}/{module}": json.loads(row["conflict_ids"])},
            }
        return None

    def get(
        self,
        project_id: str = "",
        module: str = "",
        software: str = "houdini",
        max_chars: int = 4000,
    ) -> dict:
        if project_id:
            scope_id.validate_python(project_id)
        module = normalize_modules([module], software)[0]
        if not 1024 <= max_chars <= 12000:
            raise ValueError("max_chars must be 1024..12000")
        refreshed = self._cached(project_id, software, module)
        if refreshed is None:
            refreshed = self.refresh(project_id, module=module, software=software)
        path = self._path(project_id, software, module)
        topic = CATALOGS[software][module][1]
        conflicts = refreshed["conflicts"].get(f"{software}/{module}", [])
        result = {
            "scope": "global",
            "software": software,
            "module": module,
            "guide_topic": topic,
            "available": path.is_file(),
            "path": str(path),
            "lesson_count": refreshed["lesson_counts"].get(f"{software}/{module}", 0),
            "has_conflict": bool(conflicts),
            "conflict_memory_ids": conflicts[:3],
            "omitted_conflicts": max(0, len(conflicts) - 3),
            "budget_unit": "json_characters",
            "truncated": False,
            "content": path.read_text(encoding="utf-8") if path.is_file() else "",
        }
        while len(compact(result)) > max_chars and result["content"]:
            result["content"] = result["content"][: len(result["content"]) // 2]
            result["truncated"] = True
        if len(compact(result)) > max_chars:
            raise ValueError("max_chars too small for the required scope and path")
        return result


def refresh_guides(root: Path, project_id: str = "") -> dict:
    """A Markdown failure must not masquerade as a rolled-back learning transaction."""
    try:
        result = ModuleGuides(root).refresh(project_id)
        return {key: value for key, value in result.items() if key not in ("lesson_counts", "conflicts")}
    except (OSError, ValueError, sqlite3.Error) as error:
        logging.getLogger(__name__).warning("Module guide rebuild required: %s", error)
        return {
            "state": "pending_rebuild",
            "error": str(error),
            "repair_tool": "fx_refresh_module_guides",
        }
