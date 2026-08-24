"""Generate deterministic Minesweeper Canonical V3 data with provenance."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from minesweeper.collector import collect_dataset
from minesweeper.engine import BoardConfig


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output_root", type=Path)
    parser.add_argument("--records", type=int, default=1000)
    parser.add_argument("--seed-start", type=int, default=0)
    parser.add_argument("--max-attempts", type=int, default=None)
    parser.add_argument("--width", type=int, default=9)
    parser.add_argument("--height", type=int, default=9)
    parser.add_argument("--mines", type=int, default=10)
    parser.add_argument("--max-steps", type=int, default=100)
    args = parser.parse_args()
    config = BoardConfig(width=args.width, height=args.height, mines=args.mines)
    stats = collect_dataset(
        args.output_root,
        requested_records=args.records,
        seed_start=args.seed_start,
        max_attempts=args.max_attempts,
        max_steps=args.max_steps,
        config=config,
    )
    print(json.dumps(stats.__dict__, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
