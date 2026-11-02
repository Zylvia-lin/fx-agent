"""Project-scoped Markdown supplements derived from accepted learning results."""

import hashlib
import json
import logging
import os
import sqlite3
import uuid
from pathlib import Path

from fx_agent.archive import contained
from fx_agent.modules import HOUDINI, NUKE, normalize_modules
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
        key = hashlib.sha256(project_id.encode("utf-8")).hexdigest()
        expected = self.root / "modules" / key
        resolved = contained(self.root, str(Path("modules") / key))
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
            lines.append("当前无有效的项目范围模块总结；旧方案已撤销或尚未完成提炼。")
        for (title, method, reason, conditions), sources in sorted(groups.items()):
            lines += [
                f"### {safe_text(title)}",
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

    def refresh(self, project_id: str) -> dict:
        project_id = scope_id.validate_python(project_id)
        base = self._base(project_id)
        updated, counts = [], {}
        # Serialize cooperative writers so an older DB snapshot cannot overwrite a newer view.
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT m.payload,r.feedback_id FROM memories m JOIN learning_results r ON r.memory_id=m.memory_id "
                "JOIN learning_feedback f ON f.feedback_id=r.feedback_id "
                "WHERE m.project_id=? AND f.project_id=m.project_id AND m.status='active' AND m.shot_id='' "
                "AND json_extract(f.payload,'$.outcome')='accepted' "
                "AND f.revision=(SELECT MAX(x.revision) FROM learning_feedback x WHERE x.project_id=f.project_id AND x.shot_id=f.shot_id AND x.attempt_id=f.attempt_id AND x.software=f.software)",
                (project_id,),
            ).fetchall()
            groups = {}
            for row in rows:
                record = json.loads(row["payload"])
                record["origin_feedback_id"] = row["feedback_id"]
                software = record.get("software", "houdini")
                modules = normalize_modules(record.get("modules", ["unclassified"]), software)
                for module in modules:
                    if module != "unclassified":
                        groups.setdefault((software, module), []).append(record)
            known = set(groups)
            if base.exists():
                for path in base.glob("*/*.md"):
                    software, module = path.parent.name, path.stem
                    catalog = (
                        HOUDINI if software == "houdini" else NUKE if software == "nuke" else {}
                    )
                    if module in catalog and module != "unclassified":
                        known.add((software, module))
            for software, module in sorted(known):
                label, topic = (HOUDINI if software == "houdini" else NUKE)[module]
                section, count = self._section(groups.get((software, module), []))
                header = (
                    f"# {label}：项目工作流补充\n\n项目：{safe_text(project_id)}\n\n"
                    f"上游准则：get_workflow_guide({topic!r})\n\n"
                    if topic
                    else f"# {label}：项目工作流补充\n\n项目：{safe_text(project_id)}\n\n基础工作流：nuke-workflow\n\n"
                )
                header += "只适用于本项目与条目中的条件。归档在提炼时校验，复用前重新核验案例；不代表因果解释全部证实。\n\n自动区块内不要手改；手写补充放在区块外，其内容需另行核实。\n\n"
                path = self._path(project_id, software, module)
                if self._write(path, section, header):
                    updated.append(str(path.relative_to(self.root)))
                counts[f"{software}/{module}"] = count
        return {
            "state": "updated" if updated else "current",
            "updated_paths": updated[:8],
            "omitted_paths": max(0, len(updated) - 8),
            "document_count": len(counts),
            "lesson_counts": counts,
        }

    def get(
        self, project_id: str, module: str, software: str = "houdini", max_chars: int = 4000
    ) -> dict:
        project_id = scope_id.validate_python(project_id)
        module = normalize_modules([module], software)[0]
        if not 1024 <= max_chars <= 12000:
            raise ValueError("max_chars must be 1024..12000")
        refreshed = self.refresh(project_id)
        path = self._path(project_id, software, module)
        topic = (HOUDINI if software == "houdini" else NUKE)[module][1]
        result = {
            "project_id": project_id,
            "software": software,
            "module": module,
            "guide_topic": topic,
            "available": path.is_file(),
            "path": str(path),
            "lesson_count": refreshed["lesson_counts"].get(f"{software}/{module}", 0),
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


def refresh_guides(root: Path, project_id: str) -> dict:
    """A Markdown failure must not masquerade as a rolled-back learning transaction."""
    try:
        result = ModuleGuides(root).refresh(project_id)
        return {key: value for key, value in result.items() if key != "lesson_counts"}
    except (OSError, ValueError, sqlite3.Error) as error:
        logging.getLogger(__name__).warning("Module guide rebuild required: %s", error)
        return {
            "state": "pending_rebuild",
            "error": str(error),
            "repair_tool": "fx_refresh_module_guides",
        }
