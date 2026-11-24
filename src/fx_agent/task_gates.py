"""Persistent task evidence; no extra model or repeated knowledge injection."""

import hashlib
import json
import ntpath
import uuid
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fx_agent.archive import CaseArchive, file_hash
from fx_agent.modules import HOUDINI, normalize_modules
from fx_agent.store import Identifier, Step, Store, compact


class TaskSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    project_id: Identifier
    shot_id: Identifier
    software: Literal["houdini", "nuke", "maya"]
    kind: Literal["production", "quick_tweak", "learning"] = "production"
    scene_path: str = Field(min_length=1, max_length=4096)
    endpoint: str = Field(pattern=r"^127\.0\.0\.1:[0-9]{1,5}$")
    goal: str = Field(min_length=1, max_length=2000)
    acceptance: list[Step] = Field(min_length=1, max_length=20)
    budget: str = Field(min_length=1, max_length=1000)
    modules: list[str] = Field(min_length=1, max_length=4)
    input_paths: list[str] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def validate_scope(self):
        self.modules = normalize_modules(self.modules, self.software)
        if not 1 <= int(self.endpoint.rsplit(":", 1)[1]) <= 65535:
            raise ValueError("Invalid endpoint port")
        return self


QUICK_TWEAK_MAX_EDITS = 15
HEAVY_BUILD_TOOLS = frozenset(
    {
        "setup_pyro_sim",
        "setup_rbd_sim",
        "setup_flip_sim",
        "setup_vellum_sim",
        "build_network",
        "build_sop_chain",
        "batch_reconnect",
        "create_hda",
        "setup_basic_comp",
        "setup_keyer",
        "setup_grade_chain",
    }
)


class SourceAdaptation(BaseModel):
    """Host-authored application record, not proof of visual correctness."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    source: str = Field(pattern=r"^(effect_card|memory|template_study):[a-f0-9]{32}$")
    source_method: str = Field(min_length=1, max_length=1500)
    application: str = Field(min_length=1, max_length=1500)
    verification: str = Field(min_length=1, max_length=1500)
    source_node_paths: list[Annotated[str, Field(min_length=1, max_length=4096)]] = Field(
        default_factory=list, max_length=50
    )


def same_path(a, b):
    return ntpath.normcase(ntpath.normpath(a)) == ntpath.normcase(ntpath.normpath(b))


def is_untitled_scene(path: str) -> bool:
    if not path or not str(path).strip():
        return True
    name = ntpath.basename(str(path).strip()).lower()
    return name in {
        "untitled",
        "untitled.hip",
        "untitled.hipnc",
        "untitled.hiplc",
        "untitled.nk",
        "untitled.ma",
        "untitled.mb",
    }


EVIDENCE_TOOLS = frozenset(
    {
        "fx_get_production_rules",
        "fx_get_user_rule",
        "fx_get_module_knowledge",
        "get_workflow_guide",
        "fx_search_effects",
        "fx_search_memory",
        "fx_search_templates",
        "fx_get_effect",
        "fx_get_memory",
        "fx_get_template",
        "fx_learn_templates",
        "fx_review_template_effects",
        "fx_annotate_template_effects",
        "fx_merge_effect_cards",
        "fx_refine_effect",
        "save_scene",
        "save_script",
        "verify_network",
        "find_broken_reads",
    }
)


def _normalize_search_module(raw: str, software: str) -> str:
    if not raw or not str(raw).strip():
        return ""
    try:
        return normalize_modules([raw], software)[0]
    except ValueError:
        return str(raw).strip()


def successful(value):
    if isinstance(value, list):
        return all(successful(v) for v in value)
    if not isinstance(value, dict):
        return True
    if (
        value.get("error")
        or value.get("errors")
        or value.get("error_nodes")
        or value.get("license_error")
        or value.get("isError")
        or value.get("interrupted")
    ):
        return False
    if (
        value.get("success") is False
        or value.get("healthy") is False
        or value.get("valid") is False
        or value.get("status") == "error"
    ):
        return False
    return not any(not successful(v) for v in value.values() if isinstance(v, (dict, list)))


@contextmanager
def endpoint_lock(root, endpoint):
    """Nonblocking OS lock, shared across processes and released even on process exit."""
    import os

    folder = Path(root) / "task-locks"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (hashlib.sha256(endpoint.encode()).hexdigest() + ".lock")
    with path.open("a+b") as handle:
        if not handle.tell():
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError("Task endpoint busy; wait for its running operation") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


KNOWLEDGE_WRITES = frozenset(
    {
        "fx_learn_templates",
        "fx_annotate_template_effects",
        "fx_refine_effect",
        "fx_merge_effect_cards",
        "fx_learn_template_experience",
        "fx_export_template",
    }
)


class TaskGates:
    def __init__(self, root):
        self.root = Path(root)
        self.store = Store(self.root)

    def _read(self, db, task_id):
        row = db.execute("SELECT payload FROM task_gates WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            raise ValueError("Unknown task_id; use fx_begin_task")
        return json.loads(row[0])

    def _write(self, db, task):
        db.execute(
            "INSERT INTO task_gates VALUES (?,?) ON CONFLICT(task_id) DO UPDATE SET payload=excluded.payload",
            (task["task_id"], compact(task)),
        )

    def _rules_hash(self, project_id):
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT scope,rule_key,MAX(revision) FROM user_rules WHERE scope IN ('global',?) GROUP BY scope,rule_key ORDER BY scope,rule_key",
                ("project:" + project_id,),
            ).fetchall()
        return hashlib.sha256(compact([list(r) for r in rows]).encode()).hexdigest()

    @staticmethod
    def _assets(spec):
        result = []
        for value in spec.get("input_paths", []):
            path = Path(value)
            stat = path.stat() if path.is_file() else None
            result.append(
                [value, stat.st_size if stat else None, stat.st_mtime_ns if stat else None]
            )
        return result

    def begin(self, spec: TaskSpec):
        task = spec.model_dump() | {
            "task_id": uuid.uuid4().hex,
            "revision": 1,
            "state": "needs_knowledge",
            "generation": 1,
            "edit_seq": 0,
            "saved_seq": -1,
            "verified_seq": -1,
            "receipts": {},
            "questions": [],
            "plan": None,
            "case_id": "",
        }
        task["assets"] = self._assets(task)
        task["rules_hash"] = self._rules_hash(task["project_id"])
        with self.store.connection() as db:
            self._write(db, task)
        return self._summary(task)

    def get(self, task_id, detail=False, max_chars: int = 0):
        with self.store.connection() as db:
            task = self._read(db, task_id)
        if not detail:
            return self._summary(task)
        if max_chars and max_chars > 0 and len(compact(task)) > max_chars:
            trimmed = dict(task)
            receipts = dict(trimmed.get("receipts") or {})
            keys = list(receipts.keys())
            dropped = 0
            while keys and len(compact(trimmed | {"receipts": receipts, "omitted_receipts": dropped})) > max_chars:
                receipts.pop(keys.pop(0), None)
                dropped += 1
            trimmed["receipts"] = receipts
            if dropped:
                trimmed["omitted_receipts"] = dropped
                trimmed["truncated"] = True
            return trimmed
        return task

    @staticmethod
    def _available_sources(task) -> set[str]:
        receipts = list(task["receipts"].values())
        available = {s for r in receipts for s in r.get("sources", [])}
        sections_by_study: dict[str, set[str]] = {}
        for r in receipts:
            if r.get("tool") == "fx_get_template" and r.get("template_section"):
                for study_id in r.get("study_ids", []):
                    sections_by_study.setdefault(study_id, set()).add(r["template_section"])
        for study_id, sections in sections_by_study.items():
            if "snapshot" in sections or {"nodes", "connections"} <= sections:
                available.add("template_study:" + study_id)
        return available

    def _missing(self, task):
        receipts = list(task["receipts"].values())
        expected, complete, detail_keys = 0, False, set()
        current_rules_hash = self._rules_hash(task["project_id"])
        rules = [
            r
            for r in receipts
            if r["tool"] == "fx_get_production_rules" and r.get("rules_hash") == current_rules_hash
        ]
        # Deduplicate repeated reads at the same offset, preferring complete / furthest-advancing reads.
        best_by_offset: dict[int, dict] = {}
        for r in rules:
            off = r.get("offset", 0)
            prev = best_by_offset.get(off)
            if prev is None or (
                (r.get("omitted", 1) == 0, r.get("next_offset", 0))
                > (prev.get("omitted", 1) == 0, prev.get("next_offset", 0))
            ):
                best_by_offset[off] = r
        for off in sorted(best_by_offset):
            r = best_by_offset[off]
            if off != expected:
                continue
            expected = r["next_offset"]
            detail_keys.update(r["detail_keys"])
            complete = r["omitted"] == 0
        expanded = {
            r["rule_key"]
            for r in receipts
            if r["tool"] == "fx_get_user_rule"
            and r.get("project_id", "") in {"", task["project_id"]}
        }
        missing = [] if complete and detail_keys <= expanded else ["rules"]
        if task.get("kind") == "quick_tweak":
            return missing
        for module in task["modules"]:
            selected = set((task.get("plan") or {}).get("selected_sources", []))
            expanded_module = any(
                selected.intersection(r.get("sources", []))
                and module in r.get("source_modules", [])
                for r in receipts
            )
            scoped_searched = bool((task.get("plan") or {}).get("no_match_reason")) and any(
                r["tool"] == "fx_search_memory"
                and _normalize_search_module(r.get("module", ""), task["software"]) == module
                and r.get("match_count") is not None
                for r in receipts
            )
            mod_receipts = [
                r
                for r in receipts
                if r["tool"] == "fx_get_module_knowledge"
                and _normalize_search_module(r.get("module", ""), task["software"]) == module
            ]
            # Check if paginated module reads cover the full guide from offset 0.
            mod_complete = any(not r.get("truncated") and r.get("offset", 0) == 0 for r in mod_receipts)
            if not mod_complete and mod_receipts:
                mod_by_off: dict[int, dict] = {}
                for r in mod_receipts:
                    off = r.get("offset", 0)
                    prev = mod_by_off.get(off)
                    if prev is None or (not r.get("truncated"), r.get("next_offset", 0)) > (
                        not prev.get("truncated"),
                        prev.get("next_offset", 0),
                    ):
                        mod_by_off[off] = r
                mod_expected = 0
                for off in sorted(mod_by_off):
                    r = mod_by_off[off]
                    if off != mod_expected:
                        continue
                    if not r.get("truncated"):
                        mod_complete = True
                        break
                    nxt = r.get("next_offset", 0)
                    if nxt > mod_expected:
                        mod_expected = nxt
            if not (mod_complete or (mod_receipts and (expanded_module or scoped_searched))):
                missing.append("module:" + module)
        if task["software"] == "houdini":
            topics = {"discipline"} | {HOUDINI[m][1] for m in task["modules"] if HOUDINI[m][1]}
            read_topics = {r.get("topic") for r in receipts if r["tool"] == "get_workflow_guide"}
            missing += ["guide:" + topic for topic in sorted(topics - read_topics)]
        for name in ("fx_search_memory",):
            if not any(
                r["tool"] == name
                and (
                    r.get("query", "").strip()
                    or _normalize_search_module(r.get("module", ""), task["software"]) in task["modules"]
                )
                for r in receipts
            ):
                missing.append(name)
        for receipt in receipts:
            if receipt.get("reference_matches"):
                rec_mod = _normalize_search_module(receipt.get("module", ""), task["software"])
                rec_query = receipt.get("query", "").strip()
                if not any(
                    r["tool"] == "fx_search_memory"
                    and r.get("include_candidates")
                    and _normalize_search_module(r.get("module", ""), task["software"]) == rec_mod
                    and r.get("query", "").strip() == rec_query
                    and r.get("search_scope", {}) == receipt.get("search_scope", {})
                    for r in receipts
                ):
                    missing.append("candidate_search:" + rec_mod)
        return missing

    def _summary(self, task):
        return {
            k: task[k]
            for k in (
                "task_id",
                "revision",
                "generation",
                "state",
                "kind",
                "software",
                "endpoint",
                "scene_path",
            )
        } | {
            "missing": self._missing(task),
            "open_questions": sum(not q.get("answer") for q in task["questions"]),
            "edit_seq": task["edit_seq"],
            "case_id": task["case_id"],
        }

    @staticmethod
    def _revision(task, expected):
        if task["revision"] != expected:
            raise ValueError("Task revision conflict; read fx_get_task")

    def update(self, task_id, expected_revision, spec):
        old = self.get(task_id, True)
        with endpoint_lock(self.root, old["endpoint"]), self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            task = self._read(db, task_id)
            self._revision(task, expected_revision)
            previous_scene_path = task["scene_path"]
            new_spec = spec.model_dump()
            spec_keys = (
                "project_id",
                "shot_id",
                "software",
                "kind",
                "scene_path",
                "endpoint",
                "goal",
                "acceptance",
                "budget",
                "modules",
                "input_paths",
            )
            old_spec = {k: task.get(k) for k in spec_keys}
            same_spec = old_spec == new_spec
            quick_tweak_upgrade = (
                task["kind"] == "quick_tweak" and new_spec["kind"] == "production"
            )
            scope_changed = (
                task["project_id"] != new_spec["project_id"]
                or task["software"] != new_spec["software"]
                or (task["kind"] != new_spec["kind"] and not quick_tweak_upgrade)
            )
            current_rules_hash = self._rules_hash(new_spec["project_id"])
            rules_changed = task.get("rules_hash") and task.get("rules_hash") != current_rules_hash
            # Explicit re-check (identical spec) or scope change clears all receipts;
            # targeted updates preserve unaffected knowledge receipts.
            if same_spec or scope_changed:
                kept_receipts = {}
            else:
                new_modules = set(new_spec["modules"])
                kept_receipts = {}
                for key, receipt in task.get("receipts", {}).items():
                    tool = receipt.get("tool", "")
                    if tool in {"save_scene", "save_script", "verify_network", "find_broken_reads"}:
                        continue
                    if rules_changed and tool in {"fx_get_production_rules", "fx_get_user_rule"}:
                        continue
                    if tool == "fx_get_module_knowledge":
                        mod = _normalize_search_module(receipt.get("module", ""), new_spec["software"])
                        if mod not in new_modules:
                            continue
                    kept_receipts[key] = receipt
            task.update(new_spec)
            task.update(
                revision=task["revision"] + 1,
                generation=task["generation"] + 1,
                state="needs_knowledge",
                receipts=kept_receipts,
                plan=None,
                saved_seq=-1,
                verified_seq=-1,
                case_id="",
            )
            task["assets"] = self._assets(task)
            task["previous_scene_path"] = previous_scene_path
            task.pop("scene_context", None)
            self._write(db, task)
        return self._summary(task)

    def plan(
        self,
        task_id,
        expected_revision,
        method,
        applies_when,
        changes,
        selected_sources,
        no_match_reason="",
        source_adaptations=None,
    ):
        old = self.get(task_id, True)
        with endpoint_lock(self.root, old["endpoint"]), self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            task = self._read(db, task_id)
            self._revision(task, expected_revision)
            task["plan"] = {
                "selected_sources": selected_sources,
                "no_match_reason": no_match_reason,
            }
            if self._missing(task):
                raise ValueError("Missing actual knowledge reads: " + ",".join(self._missing(task)))
            available = self._available_sources(task)
            if set(selected_sources) - available:
                raise ValueError("Selected knowledge must have been read in full for this task")
            if not selected_sources and not no_match_reason.strip():
                raise ValueError("Explain why retrieved knowledge does not apply")
            adaptations = self._source_adaptations(task, selected_sources, source_adaptations)
            if not all(v.strip() for v in (method, applies_when, changes)):
                raise ValueError("Plan needs method, applicability and adaptation")
            if (
                any(len(v) > 6000 for v in (method, applies_when, changes, no_match_reason))
                or len(selected_sources) > 50
            ):
                raise ValueError("Task plan exceeds its evidence/text budget")
            task.update(
                plan={
                    "method": method,
                    "applies_when": applies_when,
                    "changes": changes,
                    "selected_sources": selected_sources,
                    "no_match_reason": no_match_reason,
                    "source_adaptations": adaptations,
                },
                revision=task["revision"] + 1,
                state="ready",
                rules_hash=self._rules_hash(task["project_id"]),
            )
            self._write(db, task)
        return self._summary(task)

    @staticmethod
    def _source_adaptations(task, selected_sources, adaptations):
        if len(adaptations or []) > 50:
            raise ValueError("Too many source adaptations")
        records = [SourceAdaptation.model_validate(a).model_dump() for a in adaptations or []]
        if len({a["source"] for a in records}) != len(records) or {
            a["source"] for a in records
        } != set(selected_sources):
            raise ValueError("Each selected source requires one source adaptation")
        receipts = list(task["receipts"].values())
        expanded = {s for r in receipts for s in r.get("sources", [])}
        pending, visited, studies = list(selected_sources), set(), set()
        while pending:
            source = pending.pop()
            if source in visited:
                continue
            visited.add(source)
            for receipt in receipts:
                if source not in receipt.get("sources", []):
                    continue
                if source.startswith("effect_card:") and receipt.get("card_read_version") != 1:
                    raise ValueError("Read selected effect card again for source node evidence")
                for card_id in receipt.get("component_card_ids", []):
                    component = "effect_card:" + card_id
                    if component not in expanded:
                        raise ValueError("Read selected effect component in full: " + component)
                    pending.append(component)
                if source.startswith("effect_card:"):
                    studies.update(receipt.get("study_ids", []))
        for study_id in studies:
            sections = {
                r.get("template_section") for r in receipts if study_id in r.get("study_ids", [])
            }
            if "snapshot" not in sections and not {"nodes", "connections"} <= sections:
                raise ValueError("Read relevant template nodes and connections: " + study_id)
        for adaptation in records:
            if not adaptation["source"].startswith("effect_card:"):
                continue
            related, queue = set(), [adaptation["source"]]
            while queue:
                source = queue.pop()
                if source in related:
                    continue
                related.add(source)
                queue.extend("effect_card:" + card_id for r in receipts
                             if source in r.get("sources", [])
                             for card_id in r.get("component_card_ids", []))
            card_receipts = [r for r in receipts if related.intersection(r.get("sources", []))]
            allowed = {p for r in card_receipts for p in r.get("source_node_paths", [])}
            declared = set(adaptation["source_node_paths"])
            if allowed and (not declared or not declared <= allowed):
                raise ValueError("Source adaptation must identify source_node_paths from its read cards")
            for path in declared:
                source_studies = {s for r in card_receipts if path in r.get("source_node_paths", [])
                                  for s in r.get("study_ids", [])}
                node_studies = {s for r in receipts if path in r.get("template_nodes", [])
                                for s in r.get("study_ids", [])} & source_studies
                if not node_studies:
                    raise ValueError("Read declared source node in template nodes: " + path)
                if not any(
                    set(r.get("study_ids", [])) & node_studies
                    and (path in r.get("template_edge_nodes", []) or r.get("template_connections_complete"))
                    for r in receipts
                ):
                    raise ValueError("Read related template connections for source node: " + path)
        return records

    def question(
        self,
        task_id,
        expected_revision,
        question="",
        question_id="",
        answer="",
        user_quote="",
        source_ref="",
    ):
        old = self.get(task_id, True)
        with endpoint_lock(self.root, old["endpoint"]), self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            task = self._read(db, task_id)
            self._revision(task, expected_revision)
            if question_id:
                found = next(
                    (q for q in task["questions"] if q["question_id"] == question_id), None
                )
                if not found or not all(v.strip() for v in (answer, user_quote, source_ref)):
                    raise ValueError("Answer needs a known question and actual user quote/source")
                found.update(answer=answer, user_quote=user_quote, source_ref=source_ref)
            else:
                if not question.strip():
                    raise ValueError("A concrete question is required")
                found = {"question_id": uuid.uuid4().hex, "question": question, "answer": ""}
                task["questions"].append(found)
            task["revision"] += 1
            task.update(verified_seq=-1, case_id="")
            if task["state"] == "verified":
                task["state"] = "ready" if task["plan"] else "needs_knowledge"
            self._write(db, task)
        return (
            self._summary(task) | found | {"action_required": "continue" if answer else "ask_user"}
        )

    def authorize(
        self, task_id, software, endpoint, scene_path, scene_context=None, *, tool_name: str = ""
    ):
        task = self.get(task_id, True)
        kind = task["kind"]
        if kind == "quick_tweak":
            if task["state"] not in {"needs_knowledge", "ready", "verified"}:
                raise ValueError("Task is not active; check fx_get_task")
            if tool_name and tool_name in HEAVY_BUILD_TOOLS:
                raise ValueError(
                    f"Tool '{tool_name}' is blocked in quick_tweak mode; upgrade kind to 'production' via fx_update_task"
                )
            if (
                tool_name
                and tool_name not in {"save_scene", "save_script", "render_viewport", "render_quad_view", "render_sheet", "render_node_network", "fx_render_view_start", "fx_render_view_save", "fx_render_view_stop", "render_maya_frame", "render_frames", "write_cache", "cook_frame_range", "cook_top_node", "start_render"}
                and task["edit_seq"] >= QUICK_TWEAK_MAX_EDITS
            ):
                raise ValueError(
                    f"quick_tweak edit limit ({QUICK_TWEAK_MAX_EDITS}) reached; upgrade kind to 'production' via fx_update_task"
                )
        elif (
            kind != "production"
            or task["state"] not in {"ready", "verified"}
            or not task["plan"]
        ):
            raise ValueError(
                "Task is not ready; call fx_prepare_task_brief then fx_set_task_plan"
            )
        if (
            task["software"] != software
            or task["endpoint"] != endpoint
            or not same_path(task["scene_path"], scene_path)
        ):
            raise ValueError("Task target differs from the live software/endpoint/scene")
        if task["assets"] != self._assets(task) or task.get("rules_hash") != self._rules_hash(
            task["project_id"]
        ):
            raise ValueError("Task environment/rules changed; update task and recheck knowledge")
        if self._missing(task):
            raise ValueError(
                "Task knowledge missing ("
                + ",".join(self._missing(task))
                + "); call fx_prepare_task_brief or read missing items"
            )
        if kind == "production":
            self._source_adaptations(
                task,
                task["plan"].get("selected_sources", []),
                task["plan"].get("source_adaptations", []),
            )
        if any(not q.get("answer") for q in task["questions"]):
            raise ValueError("Unresolved task questions; ask the user before modifying")
        if scene_context is not None:
            if "scene_context" in task and task["scene_context"] != scene_context:
                raise ValueError("Task scene settings changed; update task and recheck knowledge")
            if "scene_context" not in task:
                with self.store.connection() as db:
                    db.execute("BEGIN IMMEDIATE")
                    latest = self._read(db, task_id)
                    if "scene_context" in latest and latest["scene_context"] != scene_context:
                        raise ValueError("Task scene settings changed; update task and recheck knowledge")
                    latest["scene_context"] = scene_context
                    self._write(db, latest)
        return task

    def edit(self, task_id):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            task = self._read(db, task_id)
            task.update(edit_seq=task["edit_seq"] + 1, state="ready", verified_seq=-1, case_id="")
            self._write(db, task)
            device_id = task.get("device_id", "")
            for row in db.execute(
                "SELECT payload FROM task_gates WHERE task_id<>? "
                "AND json_extract(payload,'$.endpoint')=? "
                "AND COALESCE(json_extract(payload,'$.device_id'),'')=?",
                (task_id, task["endpoint"], device_id),
            ).fetchall():
                other = json.loads(row[0])
                other.update(saved_seq=-1, verified_seq=-1, case_id="")
                if other["state"] == "verified":
                    other["state"] = "ready" if other["plan"] else "needs_knowledge"
                self._write(db, other)

    def invalidate(self, task_id, *, saved=False, verified=False):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            task = self._read(db, task_id)
            if saved:
                task["saved_seq"] = -1
            if verified:
                task["verified_seq"] = -1
            task["case_id"] = ""
            if task["state"] == "verified":
                task["state"] = "ready" if task["plan"] else "needs_knowledge"
            self._write(db, task)

    def observe(self, task_id, tool, arguments, result):
        if tool == "fx_search_published_templates":
            return
        # The Houdini/shared adapter compact-serializes dicts before the SDK returns.
        if isinstance(result, str):
            with suppress(ValueError):
                result = json.loads(result)
        if not successful(result):
            return
        if (
            isinstance(result, dict)
            and result.get("status") == "ok"
            and isinstance(result.get("result"), dict)
        ):
            result = result["result"]
        value = result if isinstance(result, dict) else {}
        if value.get("action") and value.get("message"):
            return
        if tool not in EVIDENCE_TOOLS:
            return
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            task = self._read(db, task_id)
            if tool == "fx_get_production_rules":
                requested_software = (
                    arguments.get("software")
                    or (value.get("fixed_rules") or {}).get("software")
                    or value.get("software", "")
                )
            else:
                requested_software = arguments.get(
                    "software",
                    "houdini" if tool in {"fx_get_module_knowledge", "fx_search_memory"} else "",
                )
            if requested_software and requested_software != task["software"]:
                return
            receipt = {"tool": tool, "seq": task["edit_seq"], "sources": []}
            if tool == "fx_get_production_rules":
                if arguments.get("project_id", "") != task["project_id"]:
                    return
                current_hash = self._rules_hash(task["project_id"])
                receipt.update(
                    offset=arguments.get("offset", 0),
                    next_offset=value.get("next_offset", 0),
                    omitted=value.get("omitted_rules", 1),
                    detail_keys=[
                        compact([r.get("scope", "global"), r["rule_key"], r.get("revision", 0)])
                        for r in value.get("user_rules", [])
                        if r.get("detail_required")
                    ],
                    rules_hash=current_hash,
                )
                if task.get("kind") == "quick_tweak":
                    task["rules_hash"] = current_hash
            if tool == "fx_get_user_rule":
                rule_scope = value.get("scope", "global")
                rule_project = value.get("project_id", arguments.get("project_id", ""))
                if rule_scope == "project" and rule_project and rule_project != task["project_id"]:
                    return
                receipt["rule_key"] = compact(
                    [rule_scope, value.get("rule_key"), value.get("revision", 0)]
                )
                receipt["project_id"] = rule_project if rule_scope == "project" else ""
            if tool == "fx_get_module_knowledge":
                receipt["module"] = _normalize_search_module(
                    arguments.get("module", ""), task["software"]
                )
                receipt["truncated"] = bool(value.get("truncated"))
                receipt["offset"] = arguments.get("offset", 0)
                if "next_offset" in value:
                    receipt["next_offset"] = value["next_offset"]
            if tool == "get_workflow_guide":
                receipt["topic"] = arguments.get("topic")
            if tool.startswith("fx_search_"):
                receipt["query"] = arguments.get("query", "")
                receipt["module"] = _normalize_search_module(
                    arguments.get("module", ""), task["software"]
                )
                receipt["include_candidates"] = bool(arguments.get("include_candidates"))
                receipt["reference_matches"] = bool(value.get("reference_hint", {}).get("has_matches"))
                receipt["search_scope"] = {
                    key: arguments.get(key, "") for key in ("project_id", "shot_id", "source_project")
                }
                receipt["match_count"] = (
                    None
                    if value.get("truncated")
                    else value.get("total", len(value.get("items", [])))
                )
            if not value.get("detail_required") and not value.get("truncated"):
                for name, key in (
                    ("fx_get_effect", "card_id"),
                    ("fx_get_memory", "memory_id"),
                    ("fx_get_template", "study_id"),
                ):
                    section = arguments.get(
                        "section", "card" if tool == "fx_get_effect" else "summary"
                    )
                    full = (
                        (
                            tool == "fx_get_effect"
                            and section == "card"
                            and value.get("card", {}).get("card_id") == arguments.get(key)
                        )
                        or (
                            tool == "fx_get_memory" and value.get("memory_id") == arguments.get(key)
                        )
                        or (
                            tool == "fx_get_template"
                            and section == "snapshot"
                            and arguments.get("offset", 0) == 0
                            and isinstance(value.get("content"), dict)
                        )
                    )
                    source = value.get("card", value.get("content", value))
                    source_software = (
                        source.get("software", "houdini") if isinstance(source, dict) else ""
                    )
                    if (
                        tool == name
                        and arguments.get(key)
                        and full
                        and source_software == task["software"]
                    ):
                        prefix = {
                            "card_id": "effect_card",
                            "memory_id": "memory",
                            "study_id": "template_study",
                        }[key]
                        receipt["sources"] = [prefix + ":" + arguments[key]]
                        receipt["source_modules"] = source.get("modules", []) + source.get(
                            "component_modules", []
                        )
                        receipt["component_card_ids"] = source.get("component_card_ids", [])
                        if tool == "fx_get_effect":
                            receipt["card_read_version"] = 1
                            receipt["source_node_paths"] = source.get("node_paths", [])
            if tool == "fx_get_template" and not value.get("detail_required"):
                section = arguments.get("section", "summary")
                if section in {"nodes", "connections"} and (
                    value.get("content") or section == "connections" and not value.get("truncated")
                ):
                    receipt["template_section"] = section
                elif (
                    section == "snapshot"
                    and not value.get("truncated")
                    and isinstance(value.get("content"), dict)
                ):
                    receipt["template_section"] = "snapshot"
                content = value.get("content")
                nodes = content.get("nodes", []) if isinstance(content, dict) else content
                edges = content.get("connections", []) if isinstance(content, dict) else content
                if section in {"nodes", "snapshot"} and isinstance(nodes, list):
                    receipt["template_nodes"] = [n["path"] for n in nodes if n.get("path")]
                if section in {"connections", "snapshot"} and isinstance(edges, list):
                    receipt["template_edge_nodes"] = sorted(
                        {e[key] for e in edges for key in ("src_path", "dst_path") if e.get(key)}
                    )
                    receipt["template_connections_complete"] = (
                        arguments.get("offset", 0) == 0 and not value.get("truncated")
                    )
            questions = list(value.get("card", {}).get("questions", []))
            current_heads = set(value.get("current_card_ids", []))
            equivalent = set(value.get("equivalent_card_ids", []))
            # Module-wide guides may contain unrelated historical disputes; only surface
            # blocking questions when reading an individual memory or effect card.
            if tool != "fx_get_module_knowledge" and (
                value.get("has_conflict")
                or value.get("conflict_memory_ids")
                or value.get("status") in {"disputed", "needs_review"}
                or len(current_heads) > 1
                and not current_heads <= equivalent
            ):
                conflict_ids = (
                    value.get("conflict_memory_ids")
                    or value.get("current_card_ids")
                    or ([value["memory_id"]] if value.get("memory_id") else [])
                    or ([arguments["memory_id"]] if arguments.get("memory_id") else [])
                    or ([arguments["card_id"]] if arguments.get("card_id") else [])
                )
                questions.append(
                    "知识来源存在未解决争议，请核对来源并确认适用方案：" + ",".join(conflict_ids)
                )
            if tool == "fx_get_memory":
                source_ref = "memory:" + str(value.get("memory_id") or arguments.get("memory_id", ""))
            elif tool == "fx_get_effect":
                source_ref = "effect_card:" + str(
                    value.get("card", {}).get("card_id") or arguments.get("card_id", "")
                )
            else:
                source_ref = tool
            for question in questions:
                if not any(q["question"] == question for q in task["questions"]):
                    task["questions"].append(
                        {
                            "question_id": uuid.uuid4().hex,
                            "question": question,
                            "answer": "",
                            "source_ref": source_ref,
                        }
                    )
            if tool in {
                "fx_learn_templates",
                "fx_get_template",
                "fx_review_template_effects",
                "fx_annotate_template_effects",
                "fx_merge_effect_cards",
                "fx_refine_effect",
                "fx_get_effect",
            }:
                receipt["study_ids"] = sorted(
                    {r["study_id"] for r in value.get("items", []) if r.get("study_id")}
                    | ({value["study_id"]} if value.get("study_id") else set())
                    | (
                        {value["card"]["study_id"]}
                        if value.get("card", {}).get("study_id")
                        else set()
                    )
                )
            if tool in {"save_scene", "save_script"}:
                if tool == "save_script" and not arguments.get("confirm"):
                    return
                task["saved_seq"] = task["edit_seq"]
            if (
                tool == "verify_network"
                and (value.get("healthy") is True or value.get("valid") is True)
                or tool == "find_broken_reads"
                and "broken_reads" in value
                and not value["broken_reads"]
            ):
                task["verified_seq"] = task["edit_seq"]
            key = hashlib.sha256(compact([tool, arguments]).encode()).hexdigest()
            if len(task["receipts"]) >= 500 and key not in task["receipts"]:
                raise ValueError("Task evidence budget reached; split the work into another task")
            task["receipts"][key] = receipt
            if questions and task["state"] == "verified":
                task.update(state="ready", case_id="", verified_seq=-1)
            self._write(db, task)

    def finish(
        self,
        task_id,
        expected_revision,
        case_id,
        verification_note,
        *,
        live_endpoint=None,
        live_scene_path=None,
        live_scene_context=None,
    ):
        old = self.get(task_id, True)
        with endpoint_lock(self.root, old["endpoint"]):
            task = self.get(task_id, True)
            self._revision(task, expected_revision)
            if any(not q.get("answer") for q in task["questions"]):
                raise ValueError("Unresolved question; ask the user and save the answer")
            if not verification_note.strip():
                raise ValueError("Actual verification observation is required")
            if task["kind"] == "quick_tweak":
                if self._missing(task):
                    raise ValueError("Task requires production rules check before completion")
                self.authorize(
                    task_id,
                    task["software"],
                    live_endpoint if live_endpoint is not None else task["endpoint"],
                    live_scene_path if live_scene_path is not None else task["scene_path"],
                    live_scene_context,
                )
                if task["saved_seq"] != task["edit_seq"]:
                    raise ValueError("Current scene must be saved after the last edit")
            else:
                if not task["plan"] or self._missing(task):
                    raise ValueError("Task needs a plan backed by current knowledge before completion")
                if task["kind"] == "production":
                    self.authorize(
                        task_id,
                        task["software"],
                        live_endpoint if live_endpoint is not None else task["endpoint"],
                        live_scene_path if live_scene_path is not None else task["scene_path"],
                        live_scene_context,
                    )
                    if task["saved_seq"] != task["edit_seq"]:
                        raise ValueError("Current scene must be saved after the last edit")
                    if task["verified_seq"] != task["edit_seq"]:
                        raise ValueError("Current scene requires actual structural verification")
                    if case_id:
                        archive = CaseArchive(self.root)
                        case = archive.get(case_id, task["project_id"])
                        if (
                            case["shot_id"] != task["shot_id"]
                            or not archive.verify(case_id, task["project_id"])["valid"]
                        ):
                            raise ValueError("Final archive scope/checksum mismatch")
                        projects = [
                            f
                            for f in case["files"]
                            if f["role"] == "project"
                            and same_path(f["source_path"], task["scene_path"])
                        ]
                        if not projects or not any(f["role"] == "preview" for f in case["files"]):
                            raise ValueError("Final archive needs the saved target scene and a preview")
                        if file_hash(Path(task["scene_path"])) != projects[0]["sha256"]:
                            raise ValueError("Final archive differs from the saved target scene")
                else:
                    from fx_agent.effect_cards import EffectLibrary

                    studies = {i for r in task["receipts"].values() for i in r.get("study_ids", [])}
                    if not studies or any(
                        EffectLibrary(self.root).catalog(i)["learning_state"] != "reviewed"
                        for i in studies
                    ):
                        raise ValueError(
                            "Learning incomplete; investigate evidence, record concrete questions and ask the user"
                        )
            with self.store.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                current = self._read(db, task_id)
                self._revision(current, expected_revision)
                current.update(
                    state="verified",
                    revision=current["revision"] + 1,
                    case_id=case_id,
                    verification_note=verification_note,
                )
                self._write(db, current)
        return self._summary(current)


def check_knowledge_gate(
    gates: TaskGates,
    task_id: str,
    *,
    tool_name: str,
    arguments: dict,
    feedback_loader=None,
) -> None:
    """Shared admission check for knowledge writes, accepted feedback, and finalization."""
    acceptance = (
        tool_name == "fx_record_feedback"
        and isinstance(arguments.get("feedback"), dict)
        and arguments["feedback"].get("outcome") == "accepted"
    )
    knowledge_write = (
        tool_name in KNOWLEDGE_WRITES
        or tool_name == "fx_review_template_effects"
        and arguments.get("reviewed_node_paths") is not None
    )
    if not (knowledge_write or tool_name == "fx_finalize_learning" or acceptance):
        return
    if not task_id:
        raise ValueError(
            "Task gate requires fx_task_id; use fx_begin_task then read knowledge and save a plan"
        )
    task = gates.get(task_id, True)
    if knowledge_write:
        if not task["plan"] or gates.get(task_id)["missing"]:
            raise ValueError(
                "Learning task needs current knowledge and a plan before writing studies"
            )
        if task["kind"] == "learning":
            gates.invalidate(task_id, verified=True)
    if tool_name == "fx_finalize_learning" or acceptance:
        task = gates.get(task_id, True)
        if task["state"] != "verified" or task["kind"] != "production":
            raise ValueError(
                "Successful learning requires a verified production task and user acceptance"
            )
        if any(not q.get("answer") for q in task["questions"]):
            raise ValueError("Unresolved task questions; ask the user before finalizing")
        if task.get("case_id"):
            archive = CaseArchive(gates.root)
            if not archive.verify(task["case_id"], task["project_id"])["valid"]:
                raise ValueError("Verified task archive failed checksum validation")
        if acceptance:
            feedback = arguments["feedback"]
        elif feedback_loader is not None:
            feedback = feedback_loader(arguments["feedback_id"], arguments["project_id"])
        else:
            from fx_agent.learning import LearningStore

            feedback = LearningStore(gates.root).get_feedback(
                arguments["feedback_id"], arguments["project_id"]
            )
        if any(
            feedback.get(k, "houdini" if k == "software" else "") != task[k]
            for k in ("project_id", "shot_id", "software", "case_id")
        ):
            raise ValueError("Accepted feedback/archive must belong to the verified task")


def install_task_gates(*args, **kwargs):
    from fx_agent.task_tools import install_task_gates as install

    return install(*args, **kwargs)
