"""Generate certainty-first Canonical V4 Minesweeper records and PNGs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from minesweeper.collector_v4 import collect_dataset_v4
from minesweeper.engine import BoardConfig


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_root", type=Path)
    parser.add_argument("--records", type=int, default=6500)
    parser.add_argument("--seed-start", type=int, default=100_000)
    parser.add_argument("--max-attempts", type=int, default=None)
    parser.add_argument("--width", type=int, default=9)
    parser.add_argument("--height", type=int, default=9)
    parser.add_argument("--mines", type=int, default=10)
    parser.add_argument("--max-steps", type=int, default=100)
    parser.add_argument("--max-component-solutions", type=int, default=200_000)
    parser.add_argument("--solver", choices=("constraint",), default="constraint")
    parser.add_argument("--include-ambiguous", action="store_true")
    parser.add_argument("--randomize-layout", action="store_true")
    args = parser.parse_args()
    stats = collect_dataset_v4(
        args.output_root,
        requested_records=args.records,
        seed_start=args.seed_start,
        max_attempts=args.max_attempts,
        max_steps=args.max_steps,
        config=BoardConfig(width=args.width, height=args.height, mines=args.mines),
        include_ambiguous=args.include_ambiguous,
        randomize_layout=args.randomize_layout,
        max_component_solutions=args.max_component_solutions,
    )
    print(json.dumps(stats.__dict__, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
