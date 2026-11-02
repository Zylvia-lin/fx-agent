"""Cross-DCC feedback is a versioned issue, never an automatic render trigger."""

import pytest

from fx_agent.handoff import RenderIssues
from fx_agent.store import compact


def report(store):
    return store.report(
        project_id="p",
        shot_id="s",
        render_path="$HIP/render/smoke.$F4.exr",
        first_frame=10,
        last_frame=12,
        expected="连续的烟雾与alpha",
        observed="第11帧alpha缺失",
        evidence=["Nuke Read节点通道检查：11帧缺alpha"],
        source_ref="Nuke检查记录",
    )


def test_issue_requires_houdini_investigation_and_nuke_final_review(tmp_path):
    store = RenderIssues(tmp_path)
    issue = report(store)
    assert issue["status"] == "open"
    assert len(store.list("p", "s")["items"]) == 1
    with pytest.raises(ValueError):
        store.get(issue["issue_id"], "other")
    with pytest.raises(ValueError):
        store.update(issue["issue_id"], "p", "houdini", "closed", "已修复", ["log"], 1)
    investigating = store.update(
        issue["issue_id"], "p", "houdini", "investigating", "核查ROP", ["ROP参数"], 1
    )
    with pytest.raises(ValueError, match="revision"):
        store.update(
            issue["issue_id"],
            "p",
            "houdini",
            "awaiting_nuke_review",
            "输出新版本",
            ["render v2"],
            1,
        )
    ready = store.update(
        issue["issue_id"],
        "p",
        "houdini",
        "awaiting_nuke_review",
        "输出新版本",
        ["render v2"],
        investigating["revision"],
    )
    closed = store.update(
        issue["issue_id"],
        "p",
        "nuke",
        "closed",
        "重新读取检查通过",
        ["帧10-12通道检查"],
        ready["revision"],
    )
    assert closed["status"] == "closed"
    assert len(store.get(issue["issue_id"], "p")["history"]) == 4
    assert len(compact(store.list("p", "s", max_chars=1024))) <= 1024


def test_nuke_can_reopen_failed_fix_with_evidence(tmp_path):
    store = RenderIssues(tmp_path)
    issue = report(store)
    ready = store.update(
        issue["issue_id"],
        "p",
        "houdini",
        "awaiting_nuke_review",
        "确认渲染无缺帧",
        ["render log"],
        1,
    )
    reopened = store.update(
        issue["issue_id"], "p", "nuke", "open", "仍缺alpha", ["新版exr通道"], ready["revision"]
    )
    assert reopened["revision"] == 3


def test_issue_paging_keeps_older_open_issues_discoverable(tmp_path):
    store = RenderIssues(tmp_path)
    ids = {report(store)["issue_id"] for _ in range(55)}
    discovered, cursor = set(), 0
    while True:
        page = store.list("p", "s", max_chars=1024, before_cursor=cursor, status="open")
        assert len(compact(page)) <= 1024
        discovered.update(item["issue_id"] for item in page["items"])
        if not page["truncated"]:
            break
        assert page["next_cursor"] != cursor
        cursor = page["next_cursor"]
    assert discovered == ids
    issue = next(iter(ids))
    store.update(issue, "p", "houdini", "investigating", "检查", ["log"], 1)
    assert store.get(issue, "p", revision=1)["status"] == "open"
    assert "evidence" not in store.get(issue, "p")["history"][0]
