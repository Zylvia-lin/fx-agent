"""Preview or append another device's FX data; stop archive writers first."""

import argparse
import json
from pathlib import Path

from fx_agent.data_import import import_data

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            import_data(args.source, args.target, apply=args.apply), ensure_ascii=False, indent=2
        )
    )
