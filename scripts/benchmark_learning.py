"""Reproducible synthetic checks; does not simulate or grade real FX images."""

import argparse
import json
import tempfile
import time
from pathlib import Path

from fx_agent.archive import CaseArchive
from fx_agent.learning import LearningStore
from fx_agent.production import check_path
from fx_agent.store import compact


def benchmark(root: Path) -> dict:
    store = LearningStore(root)
    for number in range(8):
        store.record_feedback(
            project_id="benchmark",
            shot_id="sh010",
            effect_type="逆光烟尘",
            attempt_id=f"try-{number}",
            problem="轮廓不清晰",
            change="测试中的不同调整记录" * 25,
            reason="模拟完整纠错轨迹的上下文成本" * 10,
            result="尚未达到最终目标",
            outcome="improved",
            user_quote="合成测试对话，不代表真实用户评价",
            source_ref="synthetic benchmark",
            expected_revision=0,
        )
    baseline_hit = bool(store.search("benchmark", "逆光烟尘")["items"])
    history = store.history("benchmark", "sh010", max_chars=12000)
    for number in range(2):
        hip, image = root / f"fixture-{number}.hip", root / f"fixture-{number}.png"
        hip.write_bytes(f"synthetic HIP bytes {number}; not a real scene".encode())
        image.write_bytes(b"synthetic preview bytes; not an image")
        case = CaseArchive(root).create(
            project_id="benchmark",
            shot_id="sh010",
            title="synthetic accepted case",
            files=[{"path": str(hip), "role": "project"}, {"path": str(image), "role": "preview"}],
        )
        feedback = store.record_feedback(
            project_id="benchmark",
            shot_id="sh010",
            effect_type="逆光烟尘",
            attempt_id=f"accepted-{number}",
            problem="轮廓不清晰",
            change="调整轮廓光后单独调局部对比",
            reason="保留背景曝光",
            result="合成测试中的接受状态",
            outcome="accepted",
            user_quote="合成测试：这个版本可以",
            source_ref="synthetic benchmark",
            case_id=case["case_id"],
            expected_revision=0,
        )
        store.finalize(
            feedback["feedback_id"],
            "benchmark",
            "先检查轮廓光，再单独调整局部对比；不要整体抬亮。",
            "避免背景与噪点同时变亮",
            "逆光烟尘且背景曝光已确定",
        )
    before = store.search("benchmark", "逆光烟尘")
    plan = store.dream("benchmark")
    store.dream("benchmark", mode="apply", expected_snapshot=plan["snapshot"])
    after = store.search("benchmark", "逆光烟尘")
    elapsed = []
    for _ in range(10):
        start = time.perf_counter_ns()
        store.search("benchmark", "逆光烟尘")
        elapsed.append((time.perf_counter_ns() - start) / 1_000_000)
    invalid_paths = ["D:/outside/a.bgeo", "$JOB/a.bgeo", "$HIP/../a.bgeo", "$HIPNAME/a.bgeo"]
    return {
        "dataset": "synthetic_dialogue_and_file_fixtures",
        "real_houdini_simulation": False,
        "visual_quality_evaluated": False,
        "accepted_workflow_recall_before_learning": baseline_hit,
        "accepted_workflow_recall_after_learning": bool(after["items"]),
        "cross_shot_recall_after_learning": bool(
            store.search("benchmark", "逆光烟尘", shot_id="sh020")["items"]
        ),
        "duplicate_cards_before_dream": len(before["items"]),
        "duplicate_cards_after_dream": len(after["items"]),
        "retained_active_source_memories": store.dream("benchmark")["active_memories"],
        "recall_json_chars_before_dream": len(compact(before)),
        "recall_json_chars_after_dream": len(compact(after)),
        "correction_history_json_chars": len(compact(history)),
        "history_truncated": history["truncated"],
        "lookup_median_ms": round(sorted(elapsed)[len(elapsed) // 2], 3),
        "invalid_path_proposals": len(invalid_paths),
        "invalid_path_proposals_rejected": sum(
            not check_path(path)["valid"] for path in invalid_paths
        ),
        "limitations": [
            "Literal Chinese lookup only; no semantic retrieval benchmark.",
            "No wall-clock FX production speedup or rendered quality claim.",
            "Full corrective text and source memories are retained.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".tmp/benchmarks/learning.json"))
    args = parser.parse_args()
    temp_root = Path(__file__).resolve().parents[1] / ".tmp"
    temp_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="learning-benchmark-", dir=temp_root) as folder:
        report = benchmark(Path(folder))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
