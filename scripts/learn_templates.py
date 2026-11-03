"""Preview or import saved Houdini/Nuke templates into the portable FX data root."""

import argparse
import json
import sys
from pathlib import Path

from fx_agent.config import data_root
from fx_agent.templates import TemplateLibrary


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    parser.add_argument(
        "--apply", action="store_true", help="Archive and extract; otherwise preview only"
    )
    parser.add_argument("--data-root", type=Path, default=data_root())
    parser.add_argument("--project-id", default="")
    parser.add_argument("--shot-id", default="")
    parser.add_argument("--template-key", default="")
    parser.add_argument("--module", action="append", default=[])
    parser.add_argument("--preview", action="append", default=[])
    parser.add_argument("--dependency", action="append", default=[])
    parser.add_argument("--max-total-mib", type=int, default=256)
    parser.add_argument("--max-batch-mib", type=int, default=512)
    parser.add_argument("--max-asset-mib", type=int, default=32)
    args = parser.parse_args()
    if not args.data_root.is_absolute():
        parser.error("--data-root must be absolute")
    if args.apply and (not args.project_id or not args.shot_id):
        parser.error("--apply requires --project-id and --shot-id")
    library = TemplateLibrary(args.data_root)
    policy = {
        "max_total_bytes": args.max_total_mib * 1024**2,
        "max_batch_bytes": args.max_batch_mib * 1024**2,
        "max_asset_bytes": args.max_asset_mib * 1024**2,
    }
    try:
        if args.apply:
            result = library.ingest(
                args.source,
                project_id=args.project_id,
                shot_id=args.shot_id,
                template_key=args.template_key,
                modules=args.module or None,
                files=[{"path": p, "role": "preview"} for p in args.preview]
                + [{"path": p, "role": "dependency"} for p in args.dependency],
                policy=policy,
            )
        else:
            result = library.plan(args.source, policy)
    except (ValueError, OSError) as exc:
        parser.exit(1, str(exc) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result.get("counts", {}).get("failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
