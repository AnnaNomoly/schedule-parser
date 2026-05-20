"""
Strip down the game's full t2_output.json to just the `schedules` subtree.

The full file is ~180 MB (over GitHub's 100 MB hard limit). The schedules
subtree alone is ~24 MB, which fits comfortably in a regular git repo.

Run this locally each time a new game patch drops a new t2_output.json,
then commit the resulting data/schedules.json file. The GitHub Actions
workflow picks it up and rebuilds the static site.

USAGE:
    python extract_schedules.py path/to/t2_output.json
    python extract_schedules.py path/to/t2_output.json --out data/schedules.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys

DEFAULT_OUT = os.path.join(os.path.dirname(__file__), "data", "schedules.json")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("t2", help="Path to the full t2_output.json from the game")
    ap.add_argument("--out", default=DEFAULT_OUT, help="Where to write the stripped JSON")
    args = ap.parse_args()

    if not os.path.exists(args.t2):
        print(f"ERROR: {args.t2} does not exist", file=sys.stderr)
        return 1

    full_size_mb = os.path.getsize(args.t2) / (1024 * 1024)
    print(f"Reading {args.t2} ({full_size_mb:.1f} MB)...")
    with open(args.t2, "r", encoding="utf-8") as f:
        data = json.load(f)

    if "schedules" not in data:
        print("ERROR: input file has no 'schedules' key. Are you sure this is t2_output.json?", file=sys.stderr)
        return 1

    out_dir = os.path.dirname(args.out)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    # Preserve the wrapping `{"schedules": ...}` so parse_schedules.py reads
    # it the same way as the full file -- zero parser changes required.
    payload = {"schedules": data["schedules"]}

    # Compact JSON (no indentation) for the smallest possible file. Add
    # `indent=2` here if you ever want a human-readable diff in git.
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))

    stripped_mb = os.path.getsize(args.out) / (1024 * 1024)
    npc_count = len(data["schedules"])
    print(f"Wrote {args.out} ({stripped_mb:.1f} MB, {npc_count} NPCs)")
    print(f"Size reduction: {full_size_mb:.1f} MB -> {stripped_mb:.1f} MB ({stripped_mb/full_size_mb*100:.0f}% of original)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
