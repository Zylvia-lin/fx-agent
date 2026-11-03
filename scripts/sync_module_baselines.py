"""Copy the installed module rules into the portable data directory."""

import argparse
import json
import sys
from pathlib import Path

from fx_agent.baselines import sync_baselines
from fx_agent.config import data_root

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=data_root())
    args = parser.parse_args()
    if not args.data_root.is_absolute():
        parser.error("--data-root must be absolute")
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(sync_baselines(args.data_root), ensure_ascii=False, indent=2))
