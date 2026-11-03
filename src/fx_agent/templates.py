"""Portable, bounded source studies; extracted facts never become verified lessons."""

import hashlib
import json
import math
import os
import re
import tempfile
import threading
import uuid
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from fx_agent.archive import (
    ArtifactInput,
    CaseArchive,
    check_cancelled,
    contained,
    copy_verified,
    file_hash,
    publish_directory,
)
from fx_agent.memory import MemoryStore
from fx_agent.modules import normalize_modules
from fx_agent.store import Store, compact, scope_id
from fx_agent.template_extract import EXTRACTOR_VERSION, extract_patterns, extract_project

PROJECTS = {".hip": "houdini", ".hiplc": "houdini", ".hipnc": "houdini", ".nk": "nuke"}
IMAGES = {".hdr", ".exr", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".tx", ".rat", ".tga", ".bmp"}
MOVIES = {".mov", ".mp4", ".webm"}
CACHES = {".abc", ".bgeo", ".vdb", ".sim", ".geo", ".usd", ".usdc", ".usda"}
ASSETS = IMAGES | {".hda", ".hdalc", ".hdanc", ".otl", ".gizmo", ".cube", ".obj", ".fbx"}
SKIP_DIRS = {"backup", ".git", ".venv", "__pycache__", ".tmp"}
SEQUENCE = re.compile(r"\$F\d*|#+|%0?\d*d|<UDIM>", re.I)


class TemplatePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_total_bytes: int = Field(default=256 * 1024 * 1024, gt=0, le=2 * 1024**3)
    max_batch_bytes: int = Field(default=512 * 1024 * 1024, gt=0, le=10 * 1024**3)
    max_asset_bytes: int = Field(default=32 * 1024 * 1024, gt=0)
    max_preview_files: int = Field(default=3, ge=0, le=20)
    max_scan_files: int = Field(default=50000, ge=1, le=500000)
    max_projects: int = Field(default=200, ge=1, le=2000)
    archive_caches: bool = False
    auto_dependencies: bool = True


def _cache(path: Path) -> bool:
    return path.suffix.lower() in CACHES or path.name.lower().endswith((".bgeo.sc", ".geo.sc"))


def _tokens(text):
    """English identifiers plus Chinese bigrams, without an external search service."""
    lowered = text.lower()
    words = re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]+", lowered)
    return [
        part
        for word in words
        for part in (
            [word]
            if word.isascii() or len(word) < 2
            else [word[i : i + 2] for i in range(len(word) - 1)]
        )
    ]


class TemplateLibrary:
    """Immutable studies reference only relative archive paths inside the data root."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()

    def _source(self, source: str) -> Path:
        path = Path(source).expanduser()
        if not path.is_absolute():
            raise ValueError("Source path must be absolute")
        if path.is_symlink() or path.is_junction():
            raise ValueError("Source links are not followed")
        path = path.resolve(strict=True)
        if path.is_relative_to(self.root) or self.root.is_relative_to(path):
            raise ValueError("Source and data roots must not overlap")
        return path

    def _inventory(self, source: str, policy: TemplatePolicy) -> tuple[Path, list[dict]]:
        path = self._source(source)
        base = path if path.is_dir() else path.parent
        entries = []

        def add(file):
            if file.is_symlink() or file.is_junction() or not file.is_file():
                return
            if file.name.lower().endswith(("~", ".bak", ".autosave")):
                return
            if len(entries) >= policy.max_scan_files:
                raise ValueError("max_scan_files exceeded; select a narrower source directory")
            stat = file.stat()
            entries.append(
                {
                    "path": str(file),
                    "relative_path": file.relative_to(base).as_posix(),
                    "size_bytes": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                }
            )

        if path.is_file():
            add(path)
        else:
            for folder, dirs, files in os.walk(path, followlinks=False):
                dirs[:] = sorted(
                    d
                    for d in dirs
                    if d.lower() not in SKIP_DIRS
                    and not (Path(folder) / d).is_symlink()
                    and not (Path(folder) / d).is_junction()
                )
                for name in sorted(files):
                    add(Path(folder) / name)
        return base, entries

    def plan(self, source: str, policy: dict | None = None) -> dict:
        """Inventory metadata only, grouping render sequences without hashing every frame."""
        options = TemplatePolicy(**(policy or {}))
        _, entries = self._inventory(source, options)
        projects, grouped = [], defaultdict(list)
        for item in entries:
            path = Path(item["path"])
            if path.suffix.lower() in PROJECTS:
                projects.append(item | {"software": PROJECTS[path.suffix.lower()]})
            if path.suffix.lower() in IMAGES:
                key = re.sub(r"\d+(?=\.[^.]+$)", "#", item["relative_path"])
                grouped[key].append(item)
        if len(projects) > options.max_projects:
            raise ValueError("max_projects exceeded; select a narrower source directory")
        sequences = [
            {
                "pattern": key,
                "count": len(items),
                "size_bytes": sum(x["size_bytes"] for x in items),
                "fingerprint_kind": "metadata_only",
            }
            for key, items in grouped.items()
            if "#" in key and len(items) > 1
        ]
        return {
            "source": source,
            "projects": projects,
            "file_count": len(entries),
            "sequences": sequences[:100],
            "sequence_group_count": len(sequences),
            "cache_files": sum(_cache(Path(x["path"])) for x in entries),
            "project_bytes": sum(x["size_bytes"] for x in projects),
            "copy_policy": options.model_dump(),
            "source_modified": False,
            "pairing": "HIP/NK/成片仅按路径提供线索，不自动认定版本匹配。",
        }

    def _dependencies(self, snapshot, project, base):
        dependencies = []
        seen = set()
        for node in snapshot["nodes"]:
            for parm, raw in node["parameters"].items():
                if not re.search(r"file|texture|map|filename|font", parm, re.I) or re.search(
                    r"output|sopoutput|picture|snippet|script|callback", parm, re.I
                ):
                    continue
                if not isinstance(raw, str):
                    continue
                value = raw.strip().strip('"')
                value = re.sub(r"\s+\d+-\d+$", "", value)
                if not value or value in seen:
                    continue
                seen.add(value)
                item = {
                    "node_path": node["path"],
                    "parameter": parm,
                    "raw_path": value,
                    "resolved_path": None,
                    "status": "unresolved_expression",
                    "fingerprint_kind": None,
                }
                if snapshot["software"] == "nuke" and node["type"] in {
                    "Write",
                    "DeepWrite",
                    "WriteGeo",
                    "WriteGeo2",
                    "WriteTank",
                }:
                    item["status"] = "output_not_archived"
                    dependencies.append(item)
                    continue
                value = value.replace("[file dirname [value root.name]]", str(project.parent))
                value = value.replace("${HIPNAME}", project.stem).replace("$HIPNAME", project.stem)
                value = value.replace("${HIP}", str(project.parent)).replace(
                    "$HIP", str(project.parent)
                )
                if SEQUENCE.search(value):
                    item["status"] = "sequence_not_archived"
                elif not re.search(r"\$|\[|`|\n", value):
                    candidate = Path(value)
                    candidate = candidate if candidate.is_absolute() else project.parent / candidate
                    # Do not probe source-supplied UNC/external paths outside the
                    # user's input scope just to discover whether they exist.
                    lexical = Path(os.path.abspath(candidate))
                    if not lexical.is_relative_to(base):
                        item["resolved_path"] = str(lexical)
                        item["status"] = "external_not_archived"
                        dependencies.append(item)
                        continue
                    resolved = candidate.resolve()
                    item["resolved_path"] = str(resolved)
                    if (
                        candidate.is_symlink()
                        or candidate.is_junction()
                        or not resolved.is_relative_to(base)
                    ):
                        item["status"] = "external_not_archived"
                    elif not candidate.exists():
                        item["status"] = "missing"
                    elif _cache(resolved):
                        stat = resolved.stat()
                        item.update(
                            status="cache_not_archived",
                            size_bytes=stat.st_size,
                            mtime_ns=stat.st_mtime_ns,
                            fingerprint_kind="metadata_only",
                        )
                    elif resolved.is_file() and resolved.suffix.lower() in ASSETS:
                        item["status"] = "available"
                    else:
                        item["status"] = "reference_only"
                dependencies.append(item)
        return dependencies

    def ingest(
        self,
        source: str,
        *,
        project_id: str,
        shot_id: str,
        template_key: str = "",
        modules: list[str] | None = None,
        files: list[dict] | None = None,
        policy: dict | None = None,
        cancellation: threading.Event | None = None,
    ) -> dict:
        """Re-running a batch resumes unchanged studies; failure is isolated per project."""
        options = TemplatePolicy(**(policy or {}))
        project_id, shot_id = (
            scope_id.validate_python(project_id),
            scope_id.validate_python(shot_id),
        )
        base, entries = self._inventory(source, options)
        projects = [x for x in entries if Path(x["path"]).suffix.lower() in PROJECTS]
        if len(projects) > options.max_projects or (template_key and len(projects) != 1):
            raise ValueError("max_projects exceeded or template_key used for multiple projects")
        extras = [ArtifactInput(**x).model_dump() for x in files or []]
        if len(extras) > 496:
            raise ValueError("Too many selected files; maximum 496 per study")
        if extras and len(projects) > 1:
            raise ValueError(
                "Select a single project to attach explicit previews/dependencies; batch pairing is ambiguous"
            )
        items, batch_bytes = [], 0
        for item in projects:
            check_cancelled(cancellation)
            try:
                result = self._ingest_one(
                    base,
                    item,
                    project_id,
                    shot_id,
                    template_key,
                    modules,
                    extras,
                    options,
                    cancellation,
                    policy_budget=options.max_batch_bytes - batch_bytes,
                )
            except (OSError, ValueError) as exc:
                result = {"state": "failed", "error": str(exc)}
            items.append({"source": item["path"]} | result)
            if result["state"] in {"imported", "partial"}:
                batch_bytes += result["archive_bytes"]
        return {
            "items": items,
            "counts": dict(Counter(x["state"] for x in items)),
            "source_modified": False,
            "data_root": str(self.root),
            "new_archive_bytes": batch_bytes,
        }

    def _ingest_one(
        self,
        base,
        item,
        project_id,
        shot_id,
        template_key,
        modules,
        extras,
        policy,
        cancellation,
        *,
        policy_budget,
    ):
        project = Path(item["path"])
        max_bytes = min(policy.max_total_bytes, policy_budget)
        if project.stat().st_size > max_bytes:
            raise ValueError("Project exceeds max_total_bytes")
        software = PROJECTS[project.suffix.lower()]
        tags = normalize_modules(
            modules or ["general" if software == "houdini" else "compositing"], software
        )
        with project.open("rb") as reader:
            data = reader.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError("Project grew beyond max_total_bytes")
        source_hash = hashlib.sha256(data).hexdigest()
        snapshot = extract_project(data, software)
        dependencies = self._dependencies(snapshot, project, base)
        patterns = extract_patterns(snapshot)
        generated_text = {"snapshot.json": compact(snapshot), "patterns.json": compact(patterns)}
        metadata_bytes = sum(len(text.encode("utf-8")) for text in generated_text.values())
        if len(data) + metadata_bytes > max_bytes:
            raise ValueError("Project and extracted metadata exceed archive byte budget")
        selected = [{"path": str(project), "role": "project"}]
        omitted, previews, total = [], 0, len(data) + metadata_bytes
        candidates = list(extras)
        if policy.auto_dependencies:
            candidates += [
                {"path": d["resolved_path"], "role": "dependency"}
                for d in dependencies
                if d["status"] == "available"
            ]
        selected_paths = {str(project)}
        for entry in candidates:
            candidate = Path(entry["path"]).expanduser()
            reason = ""
            if not candidate.is_absolute():
                raise ValueError("Artifact paths must be absolute")
            if candidate.is_symlink() or candidate.is_junction():
                reason = "link_not_archived"
            candidate = candidate.resolve()
            if str(candidate) in selected_paths:
                continue
            if not candidate.is_file():
                reason = "missing"
            elif candidate.is_relative_to(self.root):
                reason = "data_not_reimported"
            elif _cache(candidate) and not policy.archive_caches:
                reason = "cache_not_archived"
            elif entry["role"] == "project":
                reason = "import_companion_project_separately"
            elif entry["role"] == "preview" and previews >= policy.max_preview_files:
                reason = "preview_limit"
            elif candidate.stat().st_size > policy.max_asset_bytes:
                reason = "max_asset_bytes"
            elif total + candidate.stat().st_size > max_bytes:
                reason = "max_total_bytes"
            elif (
                candidate.suffix.lower() in IMAGES
                and entry["role"] != "preview"
                and re.search(r"[._]\d{3,}\.[^.]+$", candidate.name)
            ):
                reason = "sequence_requires_explicit_preview"
            if reason:
                omitted.append({"path": str(candidate), "reason": reason})
                continue
            size = candidate.stat().st_size
            total += size
            previews += entry["role"] == "preview"
            selected.append({"path": str(candidate), "role": entry["role"]})
            selected_paths.add(str(candidate))
        hashes = [
            {
                "role": f["role"],
                "name": Path(f["path"]).name,
                "logical_path": Path(f["path"]).relative_to(project.parent).as_posix()
                if Path(f["path"]).is_relative_to(project.parent)
                else "external/" + Path(f["path"]).name,
                "sha256": source_hash if f["role"] == "project" else file_hash(Path(f["path"])),
            }
            for f in selected
        ]
        key = template_key or item["relative_path"]
        # An explicit key follows a moved project. Default origins must distinguish
        # separate directories containing the same relative filename.
        identity = key if template_key else [str(base), key]
        source_id = hashlib.sha256(compact([project_id, shot_id, identity]).encode()).hexdigest()
        fingerprint = hashlib.sha256(
            compact(
                [
                    EXTRACTOR_VERSION,
                    source_hash,
                    hashes,
                    tags,
                    [
                        {k: v for k, v in d.items() if k not in {"resolved_path", "mtime_ns"}}
                        for d in dependencies
                    ],
                    [{"name": Path(f["path"]).name, "reason": f["reason"]} for f in omitted],
                ]
            ).encode()
        ).hexdigest()
        store = Store(self.root)
        with store.connection() as db:
            existing = db.execute(
                "SELECT study_id FROM template_studies WHERE source_id=? AND fingerprint=? ORDER BY rowid LIMIT 1",
                (source_id, fingerprint),
            ).fetchone()
        if existing:
            if not self.validate(existing[0])["valid"]:
                raise ValueError("Existing study checksum validation failed")
            return {"state": "unchanged", "study_id": existing[0]}
        check_cancelled(cancellation)
        work_root = self.root / ".template-work"
        work_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="study-", dir=work_root) as work:
            work = Path(work)
            for filename, text in generated_text.items():
                path = work / filename
                path.write_text(text, encoding="utf-8")
                selected.append({"path": str(path), "role": "dependency"})
            with store.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                # Re-check after acquiring the write lock; concurrent imports do not duplicate.
                existing = db.execute(
                    "SELECT study_id FROM template_studies WHERE source_id=? AND fingerprint=?",
                    (source_id, fingerprint),
                ).fetchone()
                if existing:
                    return {"state": "unchanged", "study_id": existing[0]}
                history = db.execute(
                    "SELECT study_id,payload FROM template_studies WHERE source_id=? ORDER BY rowid",
                    (source_id,),
                ).fetchall()
                ancestors = {p for row in history for p in json.loads(row[1])["parent_ids"]}
                parents = [row[0] for row in history if row[0] not in ancestors]
                case = CaseArchive(self.root).create(
                    project_id=project_id,
                    shot_id=shot_id,
                    title=project.name,
                    files=selected,
                    max_total_bytes=max_bytes,
                    cancellation=cancellation,
                    expected_hashes={
                        f["path"]: h["sha256"] for f, h in zip(selected, hashes, strict=False)
                    },
                )
                study_id = uuid.uuid4().hex
                metadata_dir = ".fx-template-" + study_id
                artifacts = []
                for f in case["files"]:
                    original = Path(f["source_path"])
                    generated = original.parent == work
                    destination = (
                        metadata_dir + "/" + original.name
                        if generated
                        else original.relative_to(project.parent).as_posix()
                        if original.is_relative_to(project.parent)
                        else "external/" + f["sha256"][:12] + "/" + original.name
                    )
                    artifacts.append(
                        {
                            "role": f["role"],
                            "archive_path": f["archive_path"],
                            "export_path": destination,
                            "sha256": f["sha256"],
                        }
                    )
                snapshot_member = next(
                    a["archive_path"]
                    for a in artifacts
                    if a["export_path"] == metadata_dir + "/snapshot.json"
                )
                patterns_member = next(
                    a["archive_path"]
                    for a in artifacts
                    if a["export_path"] == metadata_dir + "/patterns.json"
                )
                record = {
                    "schema_version": 1,
                    "study_id": study_id,
                    "source_id": source_id,
                    "template_key": key,
                    "fingerprint": fingerprint,
                    "source_sha256": source_hash,
                    "source_path": str(project),
                    "project_id": project_id,
                    "shot_id": shot_id,
                    "software": software,
                    "modules": tags,
                    "title": project.name,
                    "case_id": case["case_id"],
                    "parent_ids": parents,
                    "created_at": datetime.now(UTC).isoformat(),
                    "snapshot_path": snapshot_member,
                    "patterns_path": patterns_member,
                    "artifacts": artifacts,
                    "export_manifest_path": metadata_dir + "/template-manifest.json",
                    "dependencies": dependencies,
                    "omitted_files": omitted,
                    "limitations": snapshot["limitations"],
                    "parse_status": snapshot["parse_status"],
                    "node_count": len(snapshot["nodes"]),
                    "pattern_count": len(patterns),
                    "archive_bytes": sum(f["size_bytes"] for f in case["files"]),
                    "visual_validation": "not_performed",
                    "extractor_version": EXTRACTOR_VERSION,
                }
                search_text = compact(
                    {
                        "title": record["title"],
                        "modules": tags,
                        "nodes": [
                            {k: v for k, v in n.items() if k not in {"raw_sections", "raw_script"}}
                            for n in snapshot["nodes"]
                        ],
                        "notes": snapshot["sticky_notes"],
                        "boxes": snapshot["netboxes"],
                    }
                )
                db.execute(
                    "INSERT INTO template_studies VALUES (?,?,?,?,?,?)",
                    (study_id, source_id, fingerprint, project_id, compact(record), search_text),
                )
        return {
            "state": "imported" if snapshot["parse_status"] == "parsed" else "partial",
            **self._card(record),
        }

    @staticmethod
    def _card(record):
        keys = (
            "study_id",
            "source_id",
            "title",
            "software",
            "modules",
            "project_id",
            "shot_id",
            "case_id",
            "node_count",
            "pattern_count",
            "archive_bytes",
            "parse_status",
        )
        return {k: record[k] for k in keys} | {"parent_count": len(record["parent_ids"])}

    def _record(self, study_id):
        if not re.fullmatch(r"[a-f0-9]{32}", study_id):
            raise ValueError("Invalid study_id")
        with Store(self.root).connection() as db:
            row = db.execute(
                "SELECT payload FROM template_studies WHERE study_id=?", (study_id,)
            ).fetchone()
        if not row:
            raise ValueError("Template study not found")
        return json.loads(row[0])

    def validate(self, study_id):
        record = self._record(study_id)
        return CaseArchive(self.root).verify(record["case_id"], record["project_id"]) | {
            "study_id": study_id
        }

    def get(
        self,
        study_id: str,
        *,
        section: str = "summary",
        offset: int = 0,
        limit: int = 50,
        max_chars: int = 12000,
    ) -> dict:
        if offset < 0 or not 1 <= limit <= 500 or not 1024 <= max_chars <= 200000:
            raise ValueError("Invalid pagination or max_chars")
        record = self._record(study_id)
        if section == "summary":
            result = record | {"archive_root": str(self.root / "cases" / record["case_id"])}
            if len(compact(result)) <= max_chars:
                return result
            return self._card(record) | {
                "detail_required": True,
                "truncated": True,
                "next_sections": [
                    "dependencies",
                    "omitted_files",
                    "artifacts",
                    "parent_ids",
                    "history",
                    "limitations",
                ],
            }
        if section == "history":
            with Store(self.root).connection() as db:
                rows = db.execute(
                    "SELECT payload FROM template_studies WHERE source_id=? ORDER BY rowid DESC",
                    (record["source_id"],),
                ).fetchall()
            content = [
                {
                    k: value[k]
                    for k in ("study_id", "source_path", "fingerprint", "created_at", "parent_ids")
                }
                for value in (json.loads(row[0]) for row in rows)
            ]
        elif section in {"dependencies", "omitted_files", "artifacts", "parent_ids", "limitations"}:
            content = record[section]
        elif section not in {"snapshot", "patterns", "nodes", "connections", "snapshot_text"}:
            raise ValueError("Unknown section")
        else:
            if not self.validate(study_id)["valid"]:
                raise ValueError("Template study checksum validation failed")
            case_root = self.root / "cases" / record["case_id"]
            filename = record["patterns_path"] if section == "patterns" else record["snapshot_path"]
            text = contained(case_root, filename).read_text(encoding="utf-8")
            expected_hash = next(
                a["sha256"] for a in record["artifacts"] if a["archive_path"] == filename
            )
            if hashlib.sha256(text.encode("utf-8")).hexdigest() != expected_hash:
                raise ValueError("Template metadata checksum changed during reading")
            if section == "snapshot_text":
                chunk = text[offset : offset + limit]
                while len(compact(chunk)) > max_chars - 300:
                    chunk = chunk[: len(chunk) // 2]
                return {
                    "study_id": study_id,
                    "content": chunk,
                    "next_offset": offset + len(chunk),
                    "truncated": offset + len(chunk) < len(text),
                }
            content = json.loads(text)
            if section in {"nodes", "connections"}:
                content = content[section]
        if isinstance(content, list):
            page = content[offset : offset + limit]
            while page and len(compact(page)) > max_chars - 300:
                page.pop()
            if not page and offset < len(content):
                return {
                    "study_id": study_id,
                    "content": [],
                    "next_offset": offset,
                    "truncated": True,
                    "required_chars": len(compact(content[offset])) + 2,
                    "detail_required": True,
                    "next_sections": ["snapshot_text"] if section in {"nodes", "patterns"} else [],
                }
            return {
                "study_id": study_id,
                "content": page,
                "next_offset": offset + len(page),
                "truncated": offset + len(page) < len(content),
            }
        if len(compact(content)) > max_chars - 300:
            return {
                "study_id": study_id,
                "content": {
                    "software": content["software"],
                    "parse_status": content["parse_status"],
                    "node_count": len(content["nodes"]),
                },
                "truncated": True,
                "detail_required": True,
                "next_sections": ["nodes", "connections", "snapshot_text"],
                "snapshot_file": str(contained(case_root, filename)),
            }
        return {"study_id": study_id, "content": content, "truncated": False}

    def search(
        self,
        query: str = "",
        *,
        software: str = "",
        module: str = "",
        project_id: str = "",
        include_history: bool = False,
        offset: int = 0,
        limit: int = 10,
        max_chars: int = 6000,
    ) -> dict:
        if (
            offset < 0
            or not 1 <= limit <= 100
            or not 1024 <= max_chars <= 200000
            or len(query) > 1000
        ):
            raise ValueError("Invalid search parameters")
        if software and software not in {"houdini", "nuke"}:
            raise ValueError("Invalid software")
        with Store(self.root).connection() as db:
            rows = db.execute(
                "SELECT payload, search_text FROM template_studies ORDER BY rowid DESC"
            ).fetchall()
        docs = []
        ancestors = {p for row in rows for p in json.loads(row[0])["parent_ids"]}
        equivalents = {}
        for row in rows:
            record = json.loads(row[0])
            if not include_history and record["study_id"] in ancestors:
                continue
            if (
                software
                and record["software"] != software
                or module
                and module not in record["modules"]
                or project_id
                and record["project_id"] != project_id
            ):
                continue
            equivalent_key = (record["source_id"], record["fingerprint"])
            if equivalent_key in equivalents:
                equivalents[equivalent_key]["equivalent_study_ids"].append(record["study_id"])
                continue
            record["equivalent_study_ids"] = [record["study_id"]]
            equivalents[equivalent_key] = record
            tokens = Counter(_tokens(row[1]))
            docs.append((record, tokens, sum(tokens.values())))
        wanted = set(_tokens(query))
        average = sum(d[2] for d in docs) / max(len(docs), 1) or 1
        frequencies = {t: sum(t in d[1] for d in docs) for t in wanted}
        scored = []
        for record, counts, length in docs:
            score = sum(
                math.log(1 + (len(docs) - frequencies[t] + 0.5) / (frequencies[t] + 0.5))
                * counts[t]
                * 2.2
                / (counts[t] + 1.2 * (0.25 + 0.75 * length / average))
                for t in wanted
                if counts[t]
            )
            if not query or score > 0:
                scored.append((score, record))
        scored.sort(key=lambda x: x[0], reverse=True)
        branch_counts = Counter(
            r["source_id"] for r, _, _ in docs if r["study_id"] not in ancestors
        )
        cards = [
            self._card(r)
            | {
                "score": round(score, 5),
                "equivalent_count": len(r["equivalent_study_ids"]),
                "branch_count": branch_counts[r["source_id"]],
            }
            for score, r in scored[offset : offset + limit]
        ]
        while cards and len(compact(cards)) > max_chars - 300:
            cards.pop()
        if not cards and offset < len(scored):
            # Minimal discovery cards must still move the cursor at tiny budgets.
            record = scored[offset][1]
            cards = [
                {
                    "study_id": record["study_id"],
                    "title": record["title"][:80],
                    "detail_required": True,
                }
            ]
        return {
            "items": cards,
            "next_offset": offset + len(cards),
            "truncated": offset + len(cards) < len(scored),
            "total": len(scored),
            "evidence": "工程提取资料，不代表已验证的成功经验。",
        }

    def submit_experience(
        self,
        study_id: str,
        *,
        title: str,
        method: str,
        reason: str,
        applies_when: str,
        modules: list[str],
    ) -> dict:
        record = self._record(study_id)
        if not self.validate(study_id)["valid"]:
            raise ValueError("Template study checksum validation failed")
        return MemoryStore(self.root).submit(
            project_id=record["project_id"],
            software=record["software"],
            modules=modules,
            kind="workflow",
            title=title,
            method=method,
            reason=reason,
            applies_when=applies_when,
            source_type="inferred",
            source_ref="template_study:" + study_id,
            project_dependent=True,
            case_id=record["case_id"],
        )

    def export(self, study_id: str, destination: str) -> dict:
        """Export an independent working copy; original parameter/path text is unchanged."""
        record = self._record(study_id)
        target = Path(destination)
        if not target.is_absolute():
            raise ValueError("Destination must be absolute")
        target = target.resolve()
        if target.exists():
            raise FileExistsError("Destination exists; refusing to overwrite")
        if target.is_relative_to(self.root) or self.root.is_relative_to(target):
            raise ValueError("Export must not overlap data or the original project directory")
        if not self.validate(study_id)["valid"]:
            raise ValueError("Template study checksum validation failed")
        target.parent.mkdir(parents=True, exist_ok=True)
        case_root = self.root / "cases" / record["case_id"]
        with tempfile.TemporaryDirectory(
            prefix=".fx-template-export-", dir=target.parent
        ) as temporary:
            stage = Path(temporary) / "complete"
            stage.mkdir()
            for member in record["artifacts"]:
                dest = contained(stage, member["export_path"])
                dest.parent.mkdir(parents=True, exist_ok=True)
                if dest.exists() and file_hash(dest) == member["sha256"]:
                    continue  # Equivalent external sources may share one exported file.
                info = copy_verified(contained(case_root, member["archive_path"]), dest)
                if info["sha256"] != member["sha256"]:
                    raise ValueError("Archive checksum changed during export")
            manifest_path = contained(stage, record["export_manifest_path"])
            manifest_path.write_text(compact(record), encoding="utf-8")
            publish_directory(stage, target)
        return {
            "study_id": study_id,
            "destination": str(target),
            "source_modified": False,
            "paths_rewritten": False,
            "manifest_path": str(target / record["export_manifest_path"]),
            "reproduction_status": "not_tested",
            "limitations": "缓存和序列默认未复制；绝对路径及外部依赖需在工作副本中核对。",
        }
