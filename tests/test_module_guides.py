"""Accepted lessons update module workflow MDs without overwriting user notes."""

from pathlib import Path

import pytest

from fx_agent.archive import CaseArchive
from fx_agent.learning import LearningStore
from fx_agent.module_guides import ModuleGuides
from fx_agent.store import compact


@pytest.mark.parametrize("software", ["houdini", "maya", "nuke"])
def test_omitted_module_reads_general_knowledge(tmp_path, software):
    result = ModuleGuides(tmp_path).get(software=software)
    assert result["module"] == "general"
    assert result["software"] == software


def accepted(root, **changes):
    hip, image = root / "scene.hip", root / "preview.png"
    hip.write_bytes(b"synthetic HIP")
    image.write_bytes(b"synthetic preview")
    store = LearningStore(root / "data")
    case = CaseArchive(store.root).create(
        project_id="p",
        shot_id="s",
        title="fixture",
        files=[{"path": str(hip), "role": "project"}, {"path": str(image), "role": "preview"}],
    )
    values = (
        dict(
            project_id="p",
            shot_id="s",
            effect_type="卷浪",
            attempt_id="a",
            modules=["sop", "ocean"],
            problem="形态不正确",
            change="改为控制曲线",
            reason="可编辑",
            result="用户确认",
            outcome="accepted",
            user_quote="这个版本可以",
            source_ref="user",
            case_id=case["case_id"],
            expected_revision=0,
        )
        | changes
    )
    return store, store.record_feedback(**values)


def test_finalization_updates_corresponding_module_mds_and_bounded_retrieval(tmp_path):
    store, feedback = accepted(tmp_path)
    guides = ModuleGuides(store.root)
    assert not guides.get("p", "sop")["available"], "Acceptance alone is not a distilled method"
    result = store.finalize(
        feedback["feedback_id"], "p", "沿curveu使用可编辑Ramp", "便于造型调整", "局部卷浪"
    )
    assert result["module_guides"]["state"] in ("updated", "current")
    for module in ("sop", "ocean"):
        doc = guides.get("p", module)
        assert doc["available"]
        assert "沿curveu使用可编辑Ramp" in doc["content"]
        assert "用户确认" not in doc["content"], (
            "Do not copy full dialogue/feedback into workflow MD"
        )
        assert Path(doc["path"]).is_relative_to(store.root / "modules")
        assert Path(doc["path"]).name == module + ".md"
        assert len(compact(guides.get("p", module, max_chars=1024))) <= 1024
    assert guides.get("other-project", "sop")["available"]


def test_feedback_revision_removes_stale_lesson_but_preserves_manual_notes(tmp_path):
    store, feedback = accepted(tmp_path)
    store.finalize(feedback["feedback_id"], "p", "旧方法", "原因", "条件")
    guides = ModuleGuides(store.root)
    path = Path(guides.get("p", "sop")["path"])
    with path.open("a", encoding="utf-8") as stream:
        stream.write("\n## 手工备注\n保留用户自己的内容\n")
    store.record_feedback(
        project_id="p",
        shot_id="s",
        effect_type="卷浪",
        attempt_id="a",
        modules=["sop", "ocean"],
        problem="仍有问题",
        change="待重新制作",
        reason="旧方法不适用",
        result="待调整",
        outcome="failed",
        user_quote="这个版本仍不对",
        source_ref="user",
        expected_revision=1,
    )
    content = path.read_text(encoding="utf-8")
    assert "旧方法" not in content
    assert "保留用户自己的内容" in content


def test_document_write_failure_preserves_learned_record_and_is_repairable(tmp_path, monkeypatch):
    store, feedback = accepted(tmp_path)
    original = ModuleGuides._write

    def fail(*args, **kwargs):
        raise OSError("simulated locked markdown")

    monkeypatch.setattr(ModuleGuides, "_write", fail)
    result = store.finalize(feedback["feedback_id"], "p", "有效方法", "原因", "条件")
    assert result["module_guides"]["state"] == "pending_rebuild"
    assert store.get(result["memory_id"], "p")["status"] == "active"
    monkeypatch.setattr(ModuleGuides, "_write", original)
    assert ModuleGuides(store.root).refresh("p")["state"] == "updated"
    assert "有效方法" in ModuleGuides(store.root).get("p", "sop")["content"]


def test_unclassified_and_shot_only_lessons_do_not_become_project_guidelines(tmp_path):
    store, feedback = accepted(tmp_path, modules=["unclassified"])
    store.finalize(feedback["feedback_id"], "p", "未分类方法", "原因", "条件")
    assert not list((store.root / "modules").glob("**/*.md"))
    scoped = store.record_feedback(
        **{
            **{
                key: value
                for key, value in feedback.items()
                if key not in ("feedback_id", "revision", "created_at")
            },
            "modules": ["sop"],
            "attempt_id": "shot-only",
            "expected_revision": 0,
        }
    )
    store.finalize(scoped["feedback_id"], "p", "仅限此镜头", "原因", "条件", reuse_scope="shot")
    assert not ModuleGuides(store.root).get("p", "sop")["available"]


def test_malformed_managed_markers_are_preserved_and_reported(tmp_path):
    from fx_agent.module_guides import START, refresh_guides

    store, feedback = accepted(tmp_path)
    store.finalize(feedback["feedback_id"], "p", "有效方法", "原因", "条件")
    path = Path(ModuleGuides(store.root).get("p", "sop")["path"])
    malformed = path.read_text(encoding="utf-8") + "\n" + START
    path.write_text(malformed, encoding="utf-8")
    assert refresh_guides(store.root, "p")["state"] == "pending_rebuild"
    assert path.read_text(encoding="utf-8") == malformed


def test_concurrent_finalization_keeps_both_distilled_methods(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    store, first = accepted(tmp_path)
    _, second = accepted(tmp_path, attempt_id="b")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda item: store.finalize(
                    item["feedback_id"], "p", "方法" + item["attempt_id"], "原因", "条件"
                ),
                [first, second],
            )
        )
    path = ModuleGuides(store.root)._path("p", "houdini", "sop")
    content = path.read_text(encoding="utf-8")
    assert "方法a" in content and "方法b" in content
    assert all(item["module_guides"]["state"] != "pending_rebuild" for item in results)
