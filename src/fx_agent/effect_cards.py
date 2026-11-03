"""Versioned effect variants: one scheme may describe many separately searchable looks."""

import hashlib
import json
import math
import re
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from fx_agent.archive import CaseArchive, contained
from fx_agent.modules import normalize_modules
from fx_agent.store import Identifier, Step, Store, compact
from fx_agent.templates import TemplateLibrary, _tokens

CardId = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{32}$")]
Tag = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
NodePath = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4096)]


class VisualEvidence(BaseModel):
    """Host observations with replay locations and archived representative samples."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    kind: Literal["movie", "sequence", "image"]
    source_path: NodePath
    render_node_path: NodePath | None = None
    frame_range: str = Field(default="", max_length=200)
    time_range: str = Field(default="", max_length=200)
    observation: str = Field(min_length=1, max_length=3000)
    sample_paths: list[NodePath] = Field(min_length=1, max_length=20)
    case_id: CardId | None = None

    @model_validator(mode="after")
    def replay_location(self):
        if self.kind == "sequence" and not self.frame_range:
            raise ValueError("Sequence evidence requires a frame range")
        if self.kind == "movie" and not self.time_range:
            raise ValueError("Movie evidence requires a time range")
        return self


def identity_status(record: dict) -> str:
    if record.get("questions"):
        return "needs_confirmation"
    if (
        not record.get("effect_family")
        or not record.get("effect_kind")
        or not record.get("behaviors")
    ):
        return "needs_relearning"
    if record.get("naming_basis", "provisional") == "provisional":
        return "needs_confirmation"
    if record.get("basis") == "user" and record.get("user_quote"):
        return "identified"
    if record.get("visual_evidence"):
        return "identified"
    return "needs_visual_evidence"


CONCEPTS = {
    "粒子": ("粒子", "particle", "particles"),
    "烟雾": ("烟", "雾", "smoke", "fog", "mist"),
    "爆炸": ("爆炸", "爆破", "explosion", "explosive"),
    "环境": ("环境", "ambient", "environment"),
    "能量": ("能量", "energy"),
    "环绕": ("环绕", "绕转", "绕圈", "orbit", "orbiting"),
    "吸引": ("吸引", "吸入", "向心", "收拢", "汇聚", "attract", "attraction", "attracting"),
    "外扩": ("外扩", "扩散", "向外", "expansion", "expanding"),
    "消散": ("消散", "dissipate", "dissipation"),
    "火焰": ("火焰", "fire", "flame"),
}


def _concepts(text: str) -> set[str]:
    lowered = text.lower()
    return {
        key
        for key, aliases in CONCEPTS.items()
        if any(
            re.search(r"\b" + re.escape(alias) + r"\b", lowered)
            if alias.isascii()
            else alias in lowered
            for alias in aliases
        )
    }


class EffectCardInput(BaseModel):
    """The host describes an actual source variant, not every possible parameter combination."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    effect_key: Identifier
    title: str = Field(min_length=1, max_length=200)
    scheme_key: Identifier
    scheme_name: str = Field(min_length=1, max_length=200)
    method: str = Field(min_length=1, max_length=3000)
    subject: str = Field(min_length=1, max_length=500)
    appearance: str = Field(min_length=1, max_length=3000)
    motion: str = Field(min_length=1, max_length=3000)
    aliases: list[Tag] = Field(default_factory=list, max_length=30)
    tags: list[Tag] = Field(default_factory=list, max_length=30)
    node_paths: list[NodePath] = Field(default_factory=list, max_length=5000)
    parameter_notes: str = Field(default="", max_length=3000)
    animation_notes: str = Field(default="", max_length=3000)
    render_notes: str = Field(default="", max_length=3000)
    compositing_notes: str = Field(default="", max_length=3000)
    variant_notes: str = Field(min_length=1, max_length=3000)
    scope: str = Field(min_length=1, max_length=1000)
    reuse_steps: list[Step] = Field(min_length=1, max_length=20)
    inputs: list[Step] = Field(default_factory=list, max_length=30)
    limitations: str = Field(min_length=1, max_length=3000)
    basis: Literal["inferred", "user", "preview"] = "inferred"
    evidence_note: str = Field(min_length=1, max_length=3000)
    user_quote: str = Field(default="", max_length=2000)
    preview_paths: list[NodePath] = Field(default_factory=list, max_length=20)
    modules: list[str] = Field(min_length=1, max_length=4)
    supersedes: list[CardId] = Field(default_factory=list, max_length=30)
    effect_family: list[Tag] = Field(default_factory=list, max_length=10)
    effect_kind: str = Field(default="", max_length=200)
    behaviors: list[Tag] = Field(default_factory=list, max_length=20)
    naming_basis: Literal["author", "preview", "user", "provisional"] = "provisional"
    author_labels: list[Tag] = Field(default_factory=list, max_length=30)
    questions: list[Step] = Field(default_factory=list, max_length=30)
    visual_evidence: list[VisualEvidence] = Field(default_factory=list, max_length=30)
    reuse_mode: Literal["rebuild", "copy_nodes", "hybrid"] = "rebuild"
    reuse_node_paths: list[NodePath] = Field(default_factory=list, max_length=5000)
    reuse_dependency_notes: str = Field(default="", max_length=3000)
    refinement_sources: list[Tag] = Field(default_factory=list, max_length=30)
    refinement_reason: str = Field(default="", max_length=3000)

    @model_validator(mode="after")
    def evidence_requirements(self):
        if self.basis == "user" and not self.user_quote:
            raise ValueError("User-based descriptions require a user quote")
        if self.basis == "preview" and not (self.preview_paths or self.visual_evidence):
            raise ValueError("Preview-based descriptions require archived preview references")
        if self.naming_basis == "user" and (self.basis != "user" or not self.user_quote):
            raise ValueError("User naming requires an actual user quote and user basis")
        if self.naming_basis == "author" and not self.author_labels:
            raise ValueError("Author naming requires original author labels")
        if self.reuse_mode in {"copy_nodes", "hybrid"} and (
            not self.reuse_node_paths or not self.reuse_dependency_notes
        ):
            raise ValueError("Node reuse requires explicit nodes and dependency boundaries")
        if self.refinement_sources and not self.refinement_reason:
            raise ValueError("Effect refinement sources require an explanation")
        return self


class EffectExclusion(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    node_path: NodePath
    reason: str = Field(min_length=1, max_length=1000)


class MediaReview(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    path: NodePath
    status: Literal["viewed", "unavailable", "unassigned", "excluded"]
    note: str = Field(min_length=1, max_length=3000)
    card_ids: list[CardId] = Field(default_factory=list, max_length=100)


def _heads(records: list[dict]) -> list[dict]:
    parents = {parent for r in records for parent in r["parent_ids"]}
    return [r for r in records if r["card_id"] not in parents]


def _equivalent_heads(records: list[dict]) -> list[dict]:
    """Fold only exactly equal definitions of the same source variant, retaining IDs."""
    grouped = {}
    for record in records:
        key = (record["study_id"], record["effect_key"], record["content_hash"])
        if key not in grouped:
            grouped[key] = record | {"equivalent_card_ids": []}
        grouped[key]["equivalent_card_ids"].append(record["card_id"])
    return list(grouped.values())


def _markdown(record: dict) -> str:
    blocks = [
        "# " + record["title"],
        f"效果ID：{record['card_id']}\n工程学习ID：{record['study_id']}\n软件：{record['software']}\n案例：{record['case_id']}",
        f"共用制作方案：{record['scheme_name']}（{record['scheme_key']}）\n效果变体：{record['effect_key']}",
    ]
    for label, key in (
        ("作用对象", "subject"),
        ("外观描述", "appearance"),
        ("动态描述", "motion"),
        ("核心制作方案", "method"),
        ("本卡与同方案其他变体的区别", "variant_notes"),
        ("帧段、Take或版本范围", "scope"),
        ("参数差异", "parameter_notes"),
        ("动画差异", "animation_notes"),
        ("渲染差异", "render_notes"),
        ("合成差异", "compositing_notes"),
        ("证据及推断边界", "evidence_note"),
        ("用户原话", "user_quote"),
        ("适用限制与缺失项", "limitations"),
    ):
        blocks.append("## " + label + "\n\n" + (record[key] or "未提供；不能据此认定没有差异。"))
    for label, key in (
        ("别名", "aliases"),
        ("标签", "tags"),
        ("来源节点", "node_paths"),
        ("输入依赖", "inputs"),
        ("复用步骤", "reuse_steps"),
        ("预览证据", "preview_paths"),
    ):
        blocks.append(
            "## " + label + "\n\n" + ("\n".join("- " + x for x in record[key]) or "未提供。")
        )
    blocks.append(
        f"描述来源：{record['basis']}。这是工程参考卡，不是已验证的成功经验；导出工作副本后核对节点、依赖、尺度与版本。"
    )
    blocks.append(
        "## 效果识别与待确认问题\n\n"
        + compact(
            {
                k: record.get(k)
                for k in (
                    "effect_family",
                    "effect_kind",
                    "behaviors",
                    "naming_basis",
                    "author_labels",
                    "questions",
                )
            }
        )
    )
    blocks.append(
        "## 画面证据与复用范围\n\n"
        + compact(
            {
                k: record.get(k)
                for k in (
                    "visual_evidence",
                    "reuse_mode",
                    "reuse_node_paths",
                    "reuse_dependency_notes",
                    "refinement_sources",
                    "refinement_reason",
                )
            }
        )
    )
    return "\n\n".join(blocks) + "\n"


class EffectLibrary:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.templates = TemplateLibrary(self.root)

    def _source(self, study_id: str) -> tuple[dict, dict, set[str], set[str]]:
        study = self.templates._record(study_id)
        if not self.templates.validate(study_id)["valid"]:
            raise ValueError("Source study checksum validation failed")
        path = contained(self.root / "cases" / study["case_id"], study["snapshot_path"])
        data = path.read_bytes()
        expected = next(
            a["sha256"] for a in study["artifacts"] if a["archive_path"] == study["snapshot_path"]
        )
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError("Source snapshot checksum changed during reading")
        snapshot = json.loads(data)
        case = CaseArchive(self.root).get(study["case_id"], study["project_id"])
        previews = {
            f["archive_path"] for f in case["files"] if f["role"] in {"preview", "reference"}
        }
        return study, snapshot, {n["path"] for n in snapshot["nodes"]}, previews

    def _rows(self, study_id: str = "", db=None) -> list[dict]:
        def read(connection):
            rows = connection.execute(
                "SELECT payload FROM template_effects"
                + (" WHERE study_id=?" if study_id else "")
                + " ORDER BY rowid DESC",
                (study_id,) if study_id else (),
            ).fetchall()
            return [json.loads(row[0]) for row in rows]

        if db is not None:
            return read(db)
        with Store(self.root).connection() as connection:
            return read(connection)

    def _record_card(self, card_id: str) -> dict:
        if not re.fullmatch(r"[a-f0-9]{32}", card_id):
            raise ValueError("Invalid card_id")
        with Store(self.root).connection() as db:
            row = db.execute(
                "SELECT payload FROM template_effects WHERE card_id=?", (card_id,)
            ).fetchone()
        if not row:
            raise ValueError("Effect card not found")
        record = json.loads(row[0])
        study = self.templates._record(record["study_id"])
        if any(record[k] != study[k] for k in ("case_id", "project_id", "software")):
            raise ValueError("Effect card source binding mismatch")
        return record

    def _document(self, record: dict) -> dict:
        """Generated text is a view of SQLite; divergent local text is preserved."""
        temporary = None
        try:
            base = self.root / "effects"
            path = base / (record["card_id"] + ".md")
            if contained(self.root, "effects") != base or contained(base, path.name) != path:
                raise ValueError("Effect document paths must not be links")
            base.mkdir(parents=True, exist_ok=True)
            if path.exists():
                state = (
                    "ready"
                    if path.read_text(encoding="utf-8") == record["text"]
                    else "manual_conflict"
                )
                return {"state": state, "path": str(path)}
            temporary = base / ("." + uuid.uuid4().hex + ".tmp")
            temporary.write_text(record["text"], encoding="utf-8", newline="\n")
            # A concurrent writer can only generate this same immutable card.
            if path.exists():
                return {
                    "state": "ready"
                    if path.read_text(encoding="utf-8") == record["text"]
                    else "manual_conflict",
                    "path": str(path),
                }
            temporary.replace(path)
            return {"state": "ready", "path": str(path)}
        except (OSError, ValueError) as exc:
            return {"state": "pending_rebuild", "error": str(exc)}
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def annotate(self, study_id: str, cards: list[dict]) -> dict:
        if not 1 <= len(cards) <= 50:
            raise ValueError("Supply between 1 and 50 effect cards per call")
        definitions = [EffectCardInput(**card) for card in cards]
        if len({d.effect_key for d in definitions}) != len(definitions):
            raise ValueError("Each effect_key must be unique in the batch")
        study, snapshot, nodes, previews = self._source(study_id)
        author_texts = {x.get("label", "") for x in snapshot.get("netboxes", [])}
        author_texts.update(x.get("text", "") for x in snapshot.get("sticky_notes", []))
        author_texts.update(n.get("comment", "") for n in snapshot["nodes"])
        author_texts.update(n.get("parameters", {}).get("label", "") for n in snapshot["nodes"])
        for definition in definitions:
            definition.modules = normalize_modules(definition.modules, study["software"])
            if set(definition.node_paths) - nodes:
                raise ValueError("Effect card references a node absent from the source snapshot")
            if set(definition.preview_paths) - previews:
                raise ValueError("Effect card preview must reference an archived preview/reference")
            if set(definition.reuse_node_paths) - nodes:
                raise ValueError("Reuse node references a node absent from the source snapshot")
            if set(definition.author_labels) - author_texts:
                raise ValueError("Author labels must preserve actual source annotations")
            for evidence in definition.visual_evidence:
                allowed = previews
                if evidence.case_id:
                    archive = CaseArchive(self.root)
                    if not archive.verify(evidence.case_id, study["project_id"])["valid"]:
                        raise ValueError("Visual evidence archive checksum validation failed")
                    case = archive.get(evidence.case_id, study["project_id"])
                    allowed = {
                        f["archive_path"]
                        for f in case["files"]
                        if f["role"] in {"preview", "reference"}
                    }
                if set(evidence.sample_paths) - allowed:
                    raise ValueError(
                        "Visual evidence samples must reference archived preview/reference files"
                    )
                if evidence.render_node_path and evidence.render_node_path not in nodes:
                    raise ValueError("Visual evidence render node is absent from source snapshot")
        results, saved = [], []
        with Store(self.root).connection() as db:
            db.execute("BEGIN IMMEDIATE")
            records = self._rows(study_id, db)
            for definition in definitions:
                provenance = []
                for source in definition.refinement_sources:
                    kind, _, identifier = source.partition(":")
                    table = {
                        "dream": ("dream_runs", "run_id"),
                        "memory": ("memories", "memory_id"),
                        "feedback": ("learning_feedback", "feedback_id"),
                        "effect_card": ("template_effects", "card_id"),
                    }.get(kind)
                    if not table or not re.fullmatch(r"[a-f0-9]{32}", identifier):
                        raise ValueError("Invalid effect refinement source")
                    row = db.execute(
                        f"SELECT payload FROM {table[0]} WHERE {table[1]}=?", (identifier,)
                    ).fetchone()
                    if not row or json.loads(row[0]).get("undone"):
                        raise ValueError("Effect refinement source is missing or undone")
                    if kind == "effect_card" and json.loads(row[0])["study_id"] != study_id:
                        raise ValueError("Effect refinement source belongs to a different study")
                    value = json.loads(row[0])
                    provenance.append(
                        {
                            "source_ref": source,
                            "payload": {
                                k: value[k]
                                for k in ("card_id", "study_id", "content_hash", "title")
                                if k in value
                            }
                            if kind == "effect_card"
                            else value,
                        }
                    )
                content = definition.model_dump(exclude={"supersedes"})
                if provenance:
                    content["refinement_provenance"] = provenance
                content_hash = hashlib.sha256(compact(content).encode()).hexdigest()
                current = _heads([r for r in records if r["effect_key"] == definition.effect_key])
                if current and all(r["content_hash"] == content_hash for r in current):
                    record, state = current[0], "unchanged"
                else:
                    if set(definition.supersedes) != {r["card_id"] for r in current}:
                        raise ValueError(
                            "Provide all current card IDs in supersedes; stale or conflicting revision"
                        )
                    record = content | {
                        "schema_version": 1,
                        "card_id": uuid.uuid4().hex,
                        "study_id": study_id,
                        "case_id": study["case_id"],
                        "project_id": study["project_id"],
                        "software": study["software"],
                        "parent_ids": sorted(definition.supersedes),
                        "created_at": datetime.now(UTC).isoformat(),
                        "content_hash": content_hash,
                        "knowledge_status": "reference",
                        "visual_verified": False,
                    }
                    record["text"] = _markdown(record)
                    db.execute(
                        "INSERT INTO template_effects VALUES (?,?,?,?,?)",
                        (
                            record["card_id"],
                            study_id,
                            definition.effect_key,
                            compact(record),
                            compact(content),
                        ),
                    )
                    records.append(record)
                    state = "revised" if current else "added"
                saved.append(record)
                results.append(
                    {
                        "card_id": record["card_id"],
                        "effect_key": record["effect_key"],
                        "state": state,
                    }
                )
        for result, record in zip(results, saved, strict=True):
            result["document"] = self._document(record)
        return {"study_id": study_id, "items": results, "effect_catalog": self.catalog(study_id)}

    def refine(self, card_id: str, updates: dict, sources: list[str], reason: str) -> dict:
        """Fill newly understood details without discarding earlier card content."""
        record = self._record_card(card_id)
        protected = {
            "effect_key",
            "scheme_key",
            "supersedes",
            "refinement_sources",
            "refinement_reason",
        }
        if not updates or set(updates) - (set(EffectCardInput.model_fields) - protected):
            raise ValueError("Refinement must update descriptive fields of the same effect variant")
        if not sources or not reason.strip():
            raise ValueError("Refinement requires evidence sources and a reason")
        definition = {k: v for k, v in record.items() if k in EffectCardInput.model_fields}
        definition.update(updates)
        definition.update(
            supersedes=[card_id], refinement_sources=sources, refinement_reason=reason
        )
        return self.annotate(record["study_id"], [definition])

    @staticmethod
    def _summary(record: dict) -> dict:
        return {
            "card_id": record["card_id"],
            "study_id": record["study_id"],
            "effect_key": record["effect_key"],
            "title": record["title"],
            "scheme_key": record["scheme_key"],
            "scheme_name": record["scheme_name"],
            "subject": record["subject"][:120],
            "motion": record["motion"][:120],
            "software": record["software"],
            "modules": record["modules"],
            "basis": record["basis"],
            "identity_status": identity_status(record),
            "effect_family": record.get("effect_family", []),
            "effect_kind": record.get("effect_kind", ""),
            "behaviors": record.get("behaviors", []),
            "questions": record.get("questions", []),
        }

    def catalog(self, study_id: str) -> dict:
        self.templates._record(study_id)
        current = _heads(self._rows(study_id))
        ids = {r["card_id"] for r in current}
        with Store(self.root).connection() as db:
            reviews = [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT payload FROM template_effect_reviews WHERE study_id=? ORDER BY rowid",
                    (study_id,),
                )
            ]
        relevant_reviews = [
            r for r in reviews if set(r["card_ids"]) == ids and r.get("review_version") == 2
        ]
        review_parents = {p for r in relevant_reviews for p in r.get("parent_review_ids", [])}
        review_heads = [r for r in relevant_reviews if r["review_id"] not in review_parents]
        reviewed = (
            bool(ids) and len(review_heads) == 1 and review_heads[0].get("media_complete", False)
        )
        media_issues = [issue for r in review_heads for issue in r.get("media_issues", [])]
        unique = _equivalent_heads(current)
        conflicts = any(n > 1 for n in Counter(r["effect_key"] for r in unique).values())
        statuses = Counter(identity_status(r) for r in unique)
        learning_state = next(
            (
                s
                for s in ("needs_confirmation", "needs_relearning", "needs_visual_evidence")
                if statuses[s]
            ),
            "reviewed" if reviewed else "needs_review" if current else "not_annotated",
        )
        if media_issues and not reviewed:
            learning_state = "needs_confirmation"
        return {
            "card_count": len(unique),
            "scheme_count": len({r["scheme_key"] for r in current}),
            "state": "conflict"
            if conflicts
            else "reviewed"
            if reviewed
            else "needs_review"
            if current
            else "not_annotated",
            "has_conflict": conflicts,
            "coverage_basis": "host_review_not_automatic_effect_detection",
            "learning_state": "conflict" if conflicts else learning_state,
            "identity_counts": dict(statuses),
            "relearning_required": any(s != "identified" for s in statuses),
            "media_issues": media_issues,
            "review_conflict": len(review_heads) > 1,
        }

    def review(
        self,
        study_id: str,
        *,
        reviewed_node_paths: list[str] | None = None,
        expected_card_ids: list[str] | None = None,
        exclusions: list[dict] | None = None,
        note: str = "",
        media_source: str = "",
        media_reviews: list[dict] | None = None,
    ) -> dict:
        study, snapshot, nodes, _ = self._source(study_id)
        with Store(self.root).connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = _heads(self._rows(study_id, db))
            ids = {r["card_id"] for r in current}
            mapped = {path for r in current for path in r["node_paths"]}
            if reviewed_node_paths is not None:
                if not ids or snapshot["parse_status"] != "parsed":
                    raise ValueError("Annotate a parsed study before marking its effects reviewed")
                if set(reviewed_node_paths) != nodes:
                    raise ValueError("All snapshot nodes must be explicitly reviewed")
                if any(identity_status(r) != "identified" for r in current):
                    raise ValueError(
                        "Effect identity, visual evidence and unresolved questions require second-pass learning before review"
                    )
                if set(expected_card_ids or []) != ids or any(
                    n > 1
                    for n in Counter(r["effect_key"] for r in _equivalent_heads(current)).values()
                ):
                    raise ValueError("Review requires all current non-conflicting cards")
                excluded = [EffectExclusion(**x).model_dump() for x in exclusions or []]
                excluded_paths = {x["node_path"] for x in excluded}
                if excluded_paths != nodes - mapped:
                    raise ValueError("Unmapped technical nodes need explicit exclusion reasons")
                if len(note) > 3000:
                    raise ValueError("Review note too long")
                if not media_source:
                    raise ValueError(
                        "Explicit media_source is required to account for supplied videos and sequences"
                    )
                from fx_agent.effect_study import media_inventory, render_outputs
                from fx_agent.templates import TemplatePolicy

                _, entries = self.templates._inventory(media_source, TemplatePolicy())
                inventory = media_inventory(
                    entries, render_outputs(snapshot), Path(study["source_path"])
                )
                media = [MediaReview(**x).model_dump() for x in media_reviews or []]
                if len({x["path"] for x in media}) != len(media) or {x["path"] for x in media} != {
                    x["path"] for x in inventory
                }:
                    raise ValueError(
                        "Every supplied media item needs one explicit review; paginate the study plan first"
                    )
                for item in media:
                    if set(item["card_ids"]) - ids or (
                        item["status"] == "viewed" and not item["card_ids"]
                    ):
                        raise ValueError("Viewed media must identify current effect cards")
                media_issues = [x for x in media if x["status"] in {"unavailable", "unassigned"}]
                prior_reviews = [
                    json.loads(r[0])
                    for r in db.execute(
                        "SELECT payload FROM template_effect_reviews WHERE study_id=?", (study_id,)
                    )
                ]
                prior_reviews = [r for r in prior_reviews if set(r["card_ids"]) == ids]
                review_parents = {p for r in prior_reviews for p in r.get("parent_review_ids", [])}
                review = {
                    "review_id": uuid.uuid4().hex,
                    "study_id": study_id,
                    "case_id": study["case_id"],
                    "project_id": study["project_id"],
                    "card_ids": sorted(ids),
                    "reviewed_node_paths": sorted(nodes),
                    "exclusions": excluded,
                    "note": note,
                    "created_at": datetime.now(UTC).isoformat(),
                    "coverage_basis": "host_review_not_automatic_effect_detection",
                    "review_version": 2,
                    "media_source": media_source,
                    "media_inventory": inventory,
                    "media_reviews": media,
                    "media_issues": media_issues,
                    "media_complete": not media_issues,
                    "parent_review_ids": sorted(
                        r["review_id"]
                        for r in prior_reviews
                        if r["review_id"] not in review_parents
                    ),
                }
                db.execute(
                    "INSERT INTO template_effect_reviews VALUES (?,?,?)",
                    (review["review_id"], study_id, compact(review)),
                )
        return self.catalog(study_id) | {
            "study_id": study_id,
            "node_count": len(nodes),
            "unmapped_node_count": len(nodes - mapped),
            "unmapped_node_paths": sorted(nodes - mapped)[:30],
        }

    def get(
        self, card_id: str, *, section: str = "card", offset: int = 0, max_chars: int = 12000
    ) -> dict:
        if offset < 0 or not 1024 <= max_chars <= 200000:
            raise ValueError("Invalid offset or max_chars")
        record = self._record_card(card_id)
        document = self._document(record)
        if section == "text":
            chunk = record["text"][offset : offset + max_chars - 400]
            while len(compact(chunk)) > max_chars - 300:
                chunk = chunk[: len(chunk) // 2]
            return {
                "card_id": card_id,
                "content": chunk,
                "next_offset": offset + len(chunk),
                "truncated": offset + len(chunk) < len(record["text"]),
            }
        if section != "card":
            raise ValueError("Unknown effect card section")
        result = {
            "card": {k: v for k, v in record.items() if k != "text"},
            "markdown_path": document.get("path"),
            "document": document,
            "truncated": False,
            "identity_status": identity_status(record),
        }
        related = [
            r for r in self._rows(record["study_id"]) if r["effect_key"] == record["effect_key"]
        ]
        result["current_card_ids"] = [r["card_id"] for r in _heads(related)]
        result["equivalent_card_ids"] = [
            r["card_id"] for r in related if r["content_hash"] == record["content_hash"]
        ]
        if len(compact(result)) > max_chars:
            return {
                "card_id": card_id,
                "title": record["title"][:100],
                "study_id": record["study_id"],
                "detail_required": True,
                "next_section": "text",
                "truncated": True,
            }
        return result

    def search(
        self,
        query: str = "",
        *,
        software: str = "",
        module: str = "",
        project_id: str = "",
        scheme_key: str = "",
        study_id: str = "",
        include_history: bool = False,
        offset: int = 0,
        limit: int = 10,
        max_chars: int = 6000,
    ) -> dict:
        if (
            len(query) > 1000
            or offset < 0
            or not 1 <= limit <= 100
            or not 1024 <= max_chars <= 200000
        ):
            raise ValueError("Invalid effect search parameters")
        if software and software not in {"houdini", "nuke"}:
            raise ValueError("Invalid software")
        records = self._rows(study_id)
        current = _equivalent_heads(_heads(records))
        branches = Counter((r["study_id"], r["effect_key"]) for r in current)
        if not include_history:
            records = current
        with Store(self.root).connection() as db:
            studies = {
                r[0]: json.loads(r[1])
                for r in db.execute("SELECT study_id,payload FROM template_studies")
            }
        source_parents = {p for s in studies.values() for p in s["parent_ids"]}
        normalized = re.sub(
            r"我想要|我想|想要|我要|需要|帮我|做一个|一个|制作|实现|请", " ", query.lower()
        ).strip()
        wanted = set(_tokens(normalized))
        wanted_concepts = _concepts(normalized)
        residual = normalized
        for alias in sorted(
            {a for values in CONCEPTS.values() for a in values}, key=len, reverse=True
        ):
            residual = (
                re.sub(r"\b" + re.escape(alias) + r"\b", " ", residual)
                if alias.isascii()
                else residual.replace(alias, " ")
            )
        residual = re.sub(r"效果|特效|并且|以及|的|与|和|并", " ", residual)
        extra_terms = re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]+", residual)
        docs = []
        weighted = {
            "title": 4,
            "aliases": 4,
            "subject": 3,
            "tags": 3,
            "appearance": 2,
            "motion": 2,
            "variant_notes": 2,
            "scheme_name": 1,
            "method": 1,
            "parameter_notes": 1,
            "animation_notes": 1,
            "render_notes": 1,
            "compositing_notes": 1,
            "reuse_steps": 1,
            "effect_family": 5,
            "effect_kind": 5,
            "behaviors": 5,
            "author_labels": 2,
        }
        for r in records:
            if (
                software
                and r["software"] != software
                or module
                and module not in r["modules"]
                or project_id
                and r["project_id"] != project_id
                or scheme_key
                and r["scheme_key"] != scheme_key
            ):
                continue
            source = studies.get(r["study_id"])
            if source is None or any(
                r[k] != source[k] for k in ("case_id", "project_id", "software")
            ):
                continue
            counts, fields = Counter(), []
            for field, weight in weighted.items():
                value = r.get(field, "")
                tokens = _tokens(" ".join(value) if isinstance(value, list) else value)
                counts.update(tokens * weight)
                if wanted.intersection(tokens):
                    fields.append(field)
            docs.append((r, counts, fields, sum(counts.values())))
        average = sum(d[3] for d in docs) / max(len(docs), 1) or 1
        frequencies = {t: sum(t in d[1] for d in docs) for t in wanted}
        scored = []
        for r, counts, fields, length in docs:
            score = sum(
                math.log(1 + (len(docs) - frequencies[t] + 0.5) / (frequencies[t] + 0.5))
                * counts[t]
                * 2.2
                / (counts[t] + 1.2 * (0.25 + 0.75 * length / average))
                for t in wanted
                if counts[t]
            )
            explicit = " ".join(
                r.get("effect_family", []) + r.get("behaviors", []) + [r.get("effect_kind", "")]
            )
            present = _concepts(explicit)
            missing = sorted(wanted_concepts - present)
            description = " ".join(
                [r["title"], r["subject"], r["appearance"], r["motion"], *r["aliases"]]
            ).lower()
            unverified = [term for term in extra_terms if term not in description]
            kind = (
                "complete"
                if wanted_concepts
                and not missing
                and not unverified
                and identity_status(r) == "identified"
                else "partial"
                if present & wanted_concepts
                else "candidate"
            )
            if not query or score > 0 or present & wanted_concepts:
                scored.append((score, r, fields, kind, missing, unverified))
        scored.sort(
            key=lambda d: ({"complete": 2, "partial": 1, "candidate": 0}[d[3]], d[0]), reverse=True
        )
        items = [
            self._summary(r)
            | {
                "score": round(score, 5),
                "matched_fields": fields,
                "has_conflict": branches[(r["study_id"], r["effect_key"])] > 1,
                "equivalent_count": len(r.get("equivalent_card_ids", [r["card_id"]])),
                "source_is_current": r["study_id"] not in source_parents,
                "match_kind": kind,
                "missing_concepts": missing,
                "unverified_terms": unverified,
            }
            for score, r, fields, kind, missing, unverified in scored[offset : offset + limit]
        ]
        while items and len(compact(items)) > max_chars - 300:
            items.pop()
        if not items and offset < len(scored):
            r = scored[offset][1]
            items = [
                {
                    "card_id": r["card_id"],
                    "study_id": r["study_id"],
                    "title": r["title"][:100],
                    "detail_required": True,
                }
            ]
        return {
            "items": items,
            "total": len(scored),
            "next_offset": offset + len(items),
            "truncated": offset + len(items) < len(scored),
            "retrieval": "weighted_local_bm25_and_host_supplied_aliases",
            "knowledge_status": "reference_not_verified_success",
        }


def refresh_effect_documents(root: Path) -> dict:
    library = EffectLibrary(root)
    results = [library._document(r) for r in library._rows()]
    return {
        "counts": dict(Counter(r["state"] for r in results)),
        "issues": [r for r in results if r["state"] != "ready"][:20],
    }
