"""Nuke learns independently while sharing the same portable data root."""

import pytest

from fx_agent.learning import LearningStore


def experience(software):
    return dict(
        project_id="p",
        software=software,
        kind="workflow",
        title="分层调整",
        method="保留边缘层次",
        reason="改善层次",
        applies_when="逆光",
        source_type="user",
        source_ref="user",
        project_dependent=False,
    )


def test_memory_recall_and_dream_do_not_mix_software(tmp_path):
    store = LearningStore(tmp_path)
    a = store.submit(**experience("houdini"))
    b = store.submit(**experience("nuke"))
    assert a["memory_id"] != b["memory_id"]
    assert store.search("p", "层次", software="nuke")["items"][0]["memory_id"] == b["memory_id"]
    assert len(store.search("p", "层次")["items"]) == 1
    assert store.dream("p")["actions"] == [], "Do not merge across DCCs"
    with pytest.raises(ValueError):
        store.submit(**(experience("nuke") | {"method": "改进层次", "supersedes": a["memory_id"]}))


def test_nuke_feedback_and_final_method_are_nuke_scoped(tmp_path):
    store = LearningStore(tmp_path / "data")
    values = dict(
        project_id="p",
        shot_id="s",
        effect_type="边缘合成",
        attempt_id="a",
        problem="边缘发黑",
        change="核对预乘",
        reason="避免双重预乘",
        result="达到参考",
        outcome="accepted",
        user_quote="这个结果可以",
        source_ref="user",
        expected_revision=0,
        software="nuke",
    )
    feedback = store.record_feedback(**values)
    assert store.history("p", "s")["items"] == []
    assert (
        store.history("p", "s", software="nuke")["items"][0]["feedback_id"]
        == feedback["feedback_id"]
    )
    learned = store.finalize(feedback["feedback_id"], "p", "检查预乘再调色", "避免黑边", "预乘素材")
    assert store.get(learned["memory_id"], "p")["software"] == "nuke"
    assert store.search("p", "预乘")["items"] == []
    assert len(store.search("p", "预乘", software="nuke")["items"]) == 1


def test_checkpoints_and_feedback_attempts_are_independent_between_software(tmp_path):
    store = LearningStore(tmp_path)
    for software in ("houdini", "nuke"):
        store.save_checkpoint(
            project_id="p",
            shot_id="s",
            software=software,
            summary=software,
            scene_path=f"scene.{software}",
            next_steps=["继续"],
            expected_revision=0,
        )
        store.record_feedback(
            project_id="p",
            shot_id="s",
            software=software,
            attempt_id="same",
            effect_type="边缘",
            problem="层次不足",
            change=software,
            reason="test",
            result="pending",
            outcome="pending",
            user_quote="调整",
            source_ref="test",
            expected_revision=0,
        )
    assert store.get_checkpoint("p", "s")["summary"] == "houdini"
    assert store.get_checkpoint("p", "s", "nuke")["summary"] == "nuke"
    assert store.history("p", "s")["items"][0]["change"] == "houdini"
    assert store.history("p", "s", software="nuke")["items"][0]["change"] == "nuke"


def test_intermediate_nuke_feedback_works_without_case_archive(tmp_path):
    store = LearningStore(tmp_path / "data")
    result = store.record_feedback(
        project_id="p",
        shot_id="s",
        software="nuke",
        attempt_id="a",
        effect_type="合成",
        problem="黑边",
        change="检查预乘",
        reason="定位原因",
        result="待复查",
        outcome="pending",
        user_quote="这里有黑边",
        source_ref="user",
        expected_revision=0,
    )
    assert result["revision"] == 1
