"""Relocate FX state after stopping all clients that may write archive files."""

import argparse
import json
from pathlib import Path

from fx_agent.migration import migrate_data

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    args = parser.parse_args()
    print(json.dumps(migrate_data(args.source, args.target), ensure_ascii=False, indent=2))
