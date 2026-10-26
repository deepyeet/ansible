#!/usr/bin/env python3
"""Stage 2 Step 2 output validator (spec v0.9).

The group-by model returns episodes as contiguous index ranges over a
window's ordered items. Deterministic code validates — never silently
repairs — before anything downstream consumes it:

  - every episode range is contiguous and within [min_index, max_index]
  - ranges do not overlap
  - every item in the window is accounted for exactly once: inside one
    episode, or listed in that window's `unassigned` list
  - no item appears in both an episode and `unassigned`
  - each episode carries a non-empty description and >=1 exemplar that
    lies inside its own range

Input JSON (one window):
{
  "episodes": [
    {"start": 0, "end": 41, "description": "blue-walled hotel in Sydney",
     "exemplars": [3, 17]}, ...
  ],
  "unassigned": [55, 56]
}
`end` is inclusive. Exit 0 = valid; exit 1 prints every violation.
"""

import json
import sys
from pathlib import Path


def validate(data, n_items=None, min_index=0):
    errors = []
    episodes = data.get("episodes")
    unassigned = data.get("unassigned")
    if not isinstance(episodes, list):
        return ["top-level 'episodes' must be a list"]
    if not isinstance(unassigned, list):
        return ["top-level 'unassigned' must be a list"]

    covered = {}
    for i, ep in enumerate(episodes):
        s, e = ep.get("start"), ep.get("end")
        if not isinstance(s, int) or not isinstance(e, int) or e < s:
            errors.append(f"episode {i}: bad range start={s} end={e}")
            continue
        if s < min_index or (n_items is not None and e >= min_index + n_items):
            errors.append(f"episode {i}: range {s}-{e} out of bounds")
        if not str(ep.get("description", "")).strip():
            errors.append(f"episode {i} ({s}-{e}): empty description")
        ex = ep.get("exemplars")
        if not isinstance(ex, list) or not ex:
            errors.append(f"episode {i} ({s}-{e}): no exemplars")
        else:
            for x in ex:
                if not (s <= x <= e):
                    errors.append(f"episode {i} ({s}-{e}): exemplar {x} outside range")
        for idx in range(s, e + 1):
            if idx in covered:
                errors.append(f"index {idx}: in both episode {covered[idx]} and episode {i}")
            covered.setdefault(idx, i)

    seen_un = set()
    for idx in unassigned:
        if idx in seen_un:
            errors.append(f"index {idx}: listed twice in unassigned")
        seen_un.add(idx)
        if idx in covered:
            errors.append(f"index {idx}: in episode {covered[idx]} and unassigned")

    if n_items is not None:
        expected = set(range(min_index, min_index + n_items))
        accounted = set(covered) | seen_un
        missing = sorted(expected - accounted)
        extra = sorted(accounted - expected)
        if missing:
            errors.append(f"unaccounted indexes: {missing[:20]}{'...' if len(missing) > 20 else ''}")
        if extra:
            errors.append(f"out-of-window indexes: {extra[:20]}")
    return errors


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    data = json.loads(Path(sys.argv[1]).read_text())
    n_items = int(sys.argv[2]) if len(sys.argv) > 2 else None
    errors = validate(data, n_items=n_items)
    if errors:
        print(f"INVALID ({len(errors)} problems):")
        for e in errors:
            print(f"  - {e}")
        return 1
    n_ep = len(data["episodes"])
    n_cov = sum(ep["end"] - ep["start"] + 1 for ep in data["episodes"])
    print(f"VALID: {n_ep} episodes covering {n_cov} items, "
          f"{len(data['unassigned'])} unassigned")
    return 0


if __name__ == "__main__":
    sys.exit(main())
