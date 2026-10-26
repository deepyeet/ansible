#!/usr/bin/env python3
"""Paired scoring for the B38 s2-lowres arm vs the banked
control arm (IMPROVEMENT-PLAN Phase B / S2 gates).

Reads two validate_linkage results JSONLs (treatment +
control) and reports, per arm and paired on the questions
both arms answered:
- agreement with the banked (ground-truth) applied verdict
- yes-recall overall and confident-only (banked conf >= 0.9)
- false-attach flips (banked no -> applied yes) by band:
  borderline (conf < 0.9), 0.9 band, conf-1.0 band
- paired discordance: questions where exactly one arm's
  applied verdict disagrees with the banked verdict
  (treatment-only vs control-only), overall and restricted
  to false-attach flips

Hard gates (plan): confident yes-recall intact; ZERO
conf-1.0 false-attach flips in the treatment; treatment
false-attach flips not exceeding control on the paired
comparison.

Usage (container):
  ~/album-venv/bin/python score_s2.py \
      --treatment exp/linkage-s2-lowres.jsonl \
      --control exp/linkage-control.jsonl
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def load(path: Path) -> dict[tuple[str, int], dict]:
    rows: dict[tuple[str, int], dict] = {}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("error"):
            continue
        rows[(r["md5"], r["episode_id"])] = r   # last wins
    return rows


def applied_yes(r: dict) -> bool:
    return r["verdict"] == "yes" and float(r["confidence"]) >= 0.7


def band(r: dict) -> str:
    c = float(r["banked_conf"])
    if r["banked_verdict"] == "yes":
        return "yes-conf" if c >= 0.9 else "yes-borderline"
    if c < 0.9:
        return "no-borderline"
    if c < 1.0:
        return "no-0.9"
    return "no-1.0"


def arm_stats(rows: dict, label: str) -> dict:
    yes = [r for r in rows.values() if r["banked_verdict"] == "yes"]
    no = [r for r in rows.values() if r["banked_verdict"] == "no"]
    conf_yes = [r for r in yes if float(r["banked_conf"]) >= 0.9]
    fa = [r for r in no if applied_yes(r)]
    fa_band = {}
    for r in fa:
        fa_band[band(r)] = fa_band.get(band(r), 0) + 1
    agree = sum(1 for r in rows.values()
                if applied_yes(r) == (r["banked_verdict"] == "yes"
                                       and float(r["banked_conf"]) >= 0.7))
    s = {
        "n": len(rows),
        "yes_recall_all": (sum(1 for r in yes if applied_yes(r)), len(yes)),
        "yes_recall_conf": (sum(1 for r in conf_yes if applied_yes(r)),
                            len(conf_yes)),
        "fa_total": (len(fa), len(no)),
        "fa_by_band": fa_band,
        "agreement": (agree, len(rows)),
    }
    print(f"{label}: n={s['n']} | yes-recall "
          f"{s['yes_recall_all'][0]}/{s['yes_recall_all'][1]} "
          f"(conf {s['yes_recall_conf'][0]}/{s['yes_recall_conf'][1]}) | "
          f"false-attach {s['fa_total'][0]}/{s['fa_total'][1]} "
          f"by band {fa_band} | agreement {agree}/{len(rows)} "
          f"= {agree / len(rows):.1%}")
    return s


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--treatment", required=True)
    ap.add_argument("--control", required=True)
    args = ap.parse_args()
    treat = load(Path(args.treatment))
    ctrl = load(Path(args.control))
    print("=== per-arm (each on its own answered set) ===")
    ts = arm_stats(treat, "treatment")
    cs = arm_stats(ctrl, "control ")

    shared = sorted(set(treat) & set(ctrl))
    print(f"\n=== paired on {len(shared)} shared questions ===")
    t_sub = {k: treat[k] for k in shared}
    c_sub = {k: ctrl[k] for k in shared}
    t_stats = arm_stats(t_sub, "treatment")
    c_stats = arm_stats(c_sub, "control ")

    def wrong(r: dict) -> bool:
        return applied_yes(r) != (r["banked_verdict"] == "yes"
                                  and float(r["banked_conf"]) >= 0.7)

    def fa_flip(r: dict) -> bool:
        return r["banked_verdict"] == "no" and applied_yes(r)

    t_only = [k for k in shared if wrong(treat[k]) and not wrong(ctrl[k])]
    c_only = [k for k in shared if wrong(ctrl[k]) and not wrong(treat[k])]
    both = [k for k in shared if wrong(treat[k]) and wrong(ctrl[k])]
    t_fa_only = [k for k in t_only if fa_flip(treat[k])]
    c_fa_only = [k for k in c_only if fa_flip(ctrl[k])]
    print(f"discordant: treatment-only wrong {len(t_only)} "
          f"(of which false-attach {len(t_fa_only)}), "
          f"control-only wrong {len(c_only)} "
          f"(of which false-attach {len(c_fa_only)}), "
          f"both wrong {len(both)}")
    for k in t_only:
        r = treat[k]
        print(f"  T-ONLY {r['md5'][:8]} ep{r['episode_id']} "
              f"{r['stratum']} banked {r['banked_verdict']}@"
              f"{r['banked_conf']} -> treat {r['verdict']}@"
              f"{r['confidence']} vs control "
              f"{ctrl[k]['verdict']}@{ctrl[k]['confidence']}")
    for k in c_only:
        r = ctrl[k]
        print(f"  C-ONLY {r['md5'][:8]} ep{r['episode_id']} "
              f"{r['stratum']} banked {r['banked_verdict']}@"
              f"{r['banked_conf']} -> control {r['verdict']}@"
              f"{r['confidence']} vs treat "
              f"{treat[k]['verdict']}@{treat[k]['confidence']}")

    print("\n=== gates (plan) ===")
    g1 = ts["yes_recall_conf"][0] == ts["yes_recall_conf"][1]
    print(f"confident yes-recall intact (treatment, full set): "
          f"{ts['yes_recall_conf'][0]}/{ts['yes_recall_conf'][1]} "
          f"-> {'PASS' if g1 else 'FAIL'}")
    fa10 = ts["fa_by_band"].get("no-1.0", 0)
    print(f"conf-1.0 false-attach flips (treatment): {fa10} "
          f"-> {'PASS' if fa10 == 0 else 'FAIL'}")
    g3 = t_stats["fa_total"][0] <= c_stats["fa_total"][0]
    print(f"paired false-attach flips: treatment "
          f"{t_stats['fa_total'][0]} vs control {c_stats['fa_total'][0]} "
          f"-> {'PASS' if g3 else 'FAIL'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
