#!/usr/bin/env python3
"""Central usage-ledger spend check (experiment tooling).

Usage (container):
  ~/album-venv/bin/python exp_ledger_check.py [baseline_id]

With no argument: prints max api_calls id and the all-time
estimated total. With a baseline id: prints the estimated
NEW spend for rows with id > baseline, broken down by stage.
Costs are usage.est_cost over billed output (candidates +
thoughts), tier factors applied - the project's standard
accounting.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from album_sorter.usage import billed_out, est_cost

LEDGER = Path(__file__).resolve().parent / "usage-ledger.sqlite3"


def main() -> int:
    base = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    db = sqlite3.connect(f"file:{LEDGER}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT * FROM api_calls WHERE id > ? ORDER BY id",
        (base,)).fetchall()
    total = 0.0
    by_stage: dict[str, float] = {}
    unpriced = 0
    for r in rows:
        c = est_cost(r["model"], r["tokens_in"], billed_out(r),
                     r["ts"], r["service_tier"])
        if c is None:
            unpriced += 1
            c = 0.0
        total += c
        by_stage[r["stage"]] = by_stage.get(r["stage"], 0.0) + c
    mx = db.execute("SELECT MAX(id) FROM api_calls").fetchone()[0]
    print(f"rows since id {base}: {len(rows)}; est ${total:.4f}"
          f" (unpriced rows: {unpriced})")
    for s, c in sorted(by_stage.items(), key=lambda kv: -kv[1]):
        print(f"  {s}: ${c:.4f}")
    print(f"max_id: {mx}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
