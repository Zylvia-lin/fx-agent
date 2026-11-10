"""Persistent task evidence; no extra model or repeated knowledge injection."""

import hashlib
import json
import ntpath
import uuid
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fx_agent.archive import CaseArchive, file_hash
from fx_agent.modules import HOUDINI, normalize_modules
from fx_agent.store import Identifier, Step, Store, compact


class TaskSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    project_id: Identifier
    shot_id: Identifier
    software: Literal["houdini", "nuke", "maya"]
    kind: Literal["production", "learning"] = "production"
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


def same_path(a, b):
    return ntpath.normcase(ntpath.normpath(a)) == ntpath.normcase(ntpath.normpath(b))


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

    from fx_agent.data_context import current_device

    folder = Path(root) / "task-locks"
    folder.mkdir(parents=True, exist_ok=True)
    device = current_device()
    identity = compact([device, endpoint]) if device else endpoint
    path = folder / (hashlib.sha256(identity.encode()).hexdigest() + ".lock")
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


class TaskGates:
    def __new__(cls, root):
        from fx_agent.data_context import remote_enabled

        if remote_enabled():
            from fx_agent.multidevice import RemoteTaskGates

            return RemoteTaskGates()
        return super().__new__(cls)

    def __init__(self, root):
        self.root = Path(root)
        self.store = Store(self.root)

    def _read(self, db, task_id):
        row = db.execute("SELECT payload FROM task_gates WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            raise ValueError("Unknown task_id; use fx_begin_task")
        from fx_agent.data_context import current_device

        task = json.loads(row[0])
        if current_device() and task.get("device_id") != current_device():
            raise PermissionError("Task belongs to another device; create a local device task")
        return task

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
        from fx_agent.data_context import current_device

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
        if current_device():
            task["device_id"] = current_device()
        with self.store.connection() as db:
            self._write(db, task)
        return self._summary(task)

    def get(self, task_id, detail=False):
        with self.store.connection() as db:
            task = self._read(db, task_id)
        return task if detail else self._summary(task)

    def _missing(self, task):
        receipts = list(task["receipts"].values())
        expected, complete, detail_keys = 0, False, set()
        current_rules_hash = self._rules_hash(task["project_id"])
        rules = [
            r
            for r in receipts
            if r["tool"] == "fx_get_production_rules" and r.get("rules_hash") == current_rules_hash
        ]
        for r in sorted(rules, key=lambda x: x.get("offset", 0)):
            if r.get("offset", 0) != expected:
                continue
            expected = r["next_offset"]
            detail_keys.update(r["detail_keys"])
            complete = r["omitted"] == 0
        expanded = {r["rule_key"] for r in receipts if r["tool"] == "fx_get_user_rule"}
        missing = [] if complete and detail_keys <= expanded else ["rules"]
        for module in task["modules"]:
            selected = set((task.get("plan") or {}).get("selected_sources", []))
            expanded_module = any(
                selected.intersection(r.get("sources", []))
                and module in r.get("source_modules", [])
                for r in receipts
            )
            scoped_empty = bool((task.get("plan") or {}).get("no_match_reason")) and any(
                r["tool"] == "fx_search_memory"
                and r.get("module") == module
                and r.get("match_count") == 0
                for r in receipts
            )
            if not any(
                r["tool"] == "fx_get_module_knowledge"
                and r.get("module") == module
                and (not r.get("truncated") or expanded_module or scoped_empty)
                for r in receipts
            ):
                missing.append("module:" + module)
        if task["software"] == "houdini":
            topics = {"discipline"} | {HOUDINI[m][1] for m in task["modules"] if HOUDINI[m][1]}
            read_topics = {r.get("topic") for r in receipts if r["tool"] == "get_workflow_guide"}
            missing += ["guide:" + topic for topic in sorted(topics - read_topics)]
        for name in ("fx_search_effects", "fx_search_memory"):
            if not any(
                r["tool"] == name
                and (r.get("query", "").strip() or r.get("module") in task["modules"])
                for r in receipts
            ):
                missing.append(name)
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
            task.update(spec.model_dump())
            task.update(
                revision=task["revision"] + 1,
                generation=task["generation"] + 1,
                state="needs_knowledge",
                receipts={},
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
            available = {s for r in task["receipts"].values() for s in r.get("sources", [])}
            if set(selected_sources) - available:
                raise ValueError("Selected knowledge must have been read in full for this task")
            if not selected_sources and not no_match_reason.strip():
                raise ValueError("Explain why retrieved knowledge does not apply")
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
                },
                revision=task["revision"] + 1,
                state="ready",
                rules_hash=self._rules_hash(task["project_id"]),
            )
            self._write(db, task)
        return self._summary(task)

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

    def authorize(self, task_id, software, endpoint, scene_path, scene_context=None):
        task = self.get(task_id, True)
        if (
            task["kind"] != "production"
            or task["state"] not in {"ready", "verified"}
            or not task["plan"]
        ):
            raise ValueError(
                "Task is not ready; understand requirements, read knowledge and save a plan"
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
        if any(not q.get("answer") for q in task["questions"]):
            raise ValueError("Unresolved task questions; ask the user before modifying")
        if scene_context is not None:
            if "scene_context" in task and task["scene_context"] != scene_context:
                raise ValueError("Task scene settings changed; update task and recheck knowledge")
            if "scene_context" not in task:
                with self.store.connection() as db:
                    latest = self._read(db, task_id)
                    latest["scene_context"] = scene_context
                    self._write(db, latest)
        return task

    def edit(self, task_id):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            task = self._read(db, task_id)
            task.update(edit_seq=task["edit_seq"] + 1, state="ready", verified_seq=-1, case_id="")
            self._write(db, task)
            for row in db.execute(
                "SELECT payload FROM task_gates WHERE task_id<>?", (task_id,)
            ).fetchall():
                other = json.loads(row[0])
                if (other.get("device_id", "") == task.get("device_id", "")
                        and other["endpoint"] == task["endpoint"]):
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
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            task = self._read(db, task_id)
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
                receipt.update(
                    offset=arguments.get("offset", 0),
                    next_offset=value.get("next_offset", 0),
                    omitted=value.get("omitted_rules", 1),
                    detail_keys=[
                        compact([r.get("scope", "global"), r["rule_key"], r.get("revision", 0)])
                        for r in value.get("user_rules", [])
                        if r.get("detail_required")
                    ],
                    rules_hash=self._rules_hash(task["project_id"]),
                )
            if tool == "fx_get_user_rule":
                receipt["rule_key"] = compact(
                    [value.get("scope", "global"), value.get("rule_key"), value.get("revision", 0)]
                )
            if tool == "fx_get_module_knowledge":
                receipt["module"] = arguments.get("module")
                receipt["truncated"] = bool(value.get("truncated"))
            if tool == "get_workflow_guide":
                receipt["topic"] = arguments.get("topic")
            if tool.startswith("fx_search_"):
                receipt["query"] = arguments.get("query", "")
                receipt["module"] = arguments.get("module", "")
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
            questions = list(value.get("card", {}).get("questions", []))
            current_heads = set(value.get("current_card_ids", []))
            equivalent = set(value.get("equivalent_card_ids", []))
            if (
                value.get("has_conflict")
                or value.get("conflict_memory_ids")
                or value.get("status") in {"disputed", "needs_review"}
                or len(current_heads) > 1
                and not current_heads <= equivalent
            ):
                questions.append(
                    "知识来源存在未解决争议，请核对来源并确认适用方案："
                    + ",".join(value.get("conflict_memory_ids", value.get("current_card_ids", [])))
                )
            for question in questions:
                if not any(q["question"] == question for q in task["questions"]):
                    task["questions"].append(
                        {
                            "question_id": uuid.uuid4().hex,
                            "question": question,
                            "answer": "",
                            "source_ref": "effect_card:" + arguments.get("card_id", ""),
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

    def finish(self, task_id, expected_revision, case_id, verification_note):
        old = self.get(task_id, True)
        with endpoint_lock(self.root, old["endpoint"]):
            task = self.get(task_id, True)
            self._revision(task, expected_revision)
            if any(not q.get("answer") for q in task["questions"]):
                raise ValueError("Unresolved question; ask the user and save the answer")
            if not verification_note.strip():
                raise ValueError("Actual verification observation is required")
            if not task["plan"] or self._missing(task):
                raise ValueError("Task needs a plan backed by current knowledge before completion")
            if task["kind"] == "production":
                self.authorize(task_id, task["software"], task["endpoint"], task["scene_path"])
                if task["saved_seq"] != task["edit_seq"]:
                    raise ValueError("Current scene must be saved after the last edit")
                if task["verified_seq"] != task["edit_seq"]:
                    raise ValueError("Current scene requires actual structural verification")
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
                    if f["role"] == "project" and same_path(f["source_path"], task["scene_path"])
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


def install_task_gates(*args, **kwargs):
    from fx_agent.task_tools import install_task_gates as install

    return install(*args, **kwargs)
