"""Phase 2 deterministic logic: windowing, grouping-output
validation, cross-window reconciliation, episode merge detection.
Pure functions - the model call lives in grouping.py; everything
testable lives here (spec Sections 9-10)."""
from __future__ import annotations

from dataclasses import dataclass, field


def windows(n_items: int, size: int, stride: int) -> list[tuple[int, int]]:
    """[start, end) index ranges over the time-ordered item list.
    Every item appears in >= 2 windows except near the ends."""
    if n_items <= size:
        return [(0, n_items)] if n_items else []
    if stride < 1 or stride >= size:
        # a stride >= size leaves items between windows that no
        # window ever sees - refuse the configuration loudly (Q2)
        raise ValueError(f"window stride {stride} must be in [1, {size})")
    out = []
    start = 0
    while start < n_items:
        end = min(start + size, n_items)
        out.append((start, end))
        if end == n_items:
            break
        start += stride
    return out


def centrality(pos_in_window: int, window_len: int) -> float:
    """0 at the edges, 1 at the centre. The window where an item
    is most central owns its assignment (spec Section 10)."""
    if window_len <= 1:
        return 1.0
    return 1.0 - abs(pos_in_window - (window_len - 1) / 2) / ((window_len - 1) / 2)


@dataclass
class GroupingOutput:
    assignments: dict[int, str]          # window index -> episode ref (ledger id or temp id)
    new_episodes: list[dict] = field(default_factory=list)
    unassigned: list[int] = field(default_factory=list)
    summary_update: str = ""


def validate_grouping(out: GroupingOutput, window_len: int,
                        known_episode_ids: set[str]) -> list[str]:
    """Strict validation (spec Section 10): every window item
    accounted for exactly once; references must exist; new
    episodes need exemplars in-window. Returns error list;
    empty = valid. Callers retry once with errors, never patch."""
    errors = []
    seen = set()
    for idx in list(out.assignments) + list(out.unassigned):
        if not isinstance(idx, int) or isinstance(idx, bool):
            errors.append(f"index {idx!r} is not an integer")
            continue
        if idx < 0 or idx >= window_len:
            errors.append(f"index {idx} out of range")
        if idx in seen:
            errors.append(f"index {idx} accounted for twice")
        seen.add(idx)
    missing = set(range(window_len)) - seen
    if missing:
        errors.append(f"indices missing: {sorted(missing)[:10]}")
    temp_ids = set()
    for ep in out.new_episodes:
        tid = ep.get("temp_id")
        if not tid:
            errors.append("new episode without temp_id")
            continue
        temp_ids.add(str(tid))
        ex = ep.get("exemplars") or []
        if not ex or any((not isinstance(i, int) or isinstance(i, bool)
                          or i < 0 or i >= window_len) for i in ex):
            errors.append(f"new episode {tid}: exemplars missing or out of window")
        if not (ep.get("description") or "").strip():
            errors.append(f"new episode {tid}: empty description")
    for idx, ref in out.assignments.items():
        if not isinstance(idx, int) or isinstance(idx, bool):
            errors.append(f"assignment key {idx!r} is not an integer index")
            continue
        ref = str(ref)
        if ref not in known_episode_ids and ref not in temp_ids:
            errors.append(f"assignment for {idx} references unknown episode {ref}")
    return errors


@dataclass
class Vote:
    episode_ref: str
    centrality: float
    window_no: int


def reconcile(votes_by_item: dict[int, list[Vote]]) -> dict[int, str]:
    """Pick each item's episode: highest centrality wins; ties go
    to the later window (more ledger context)."""
    result = {}
    for item_idx, votes in votes_by_item.items():
        best = max(votes, key=lambda v: (v.centrality, v.window_no))
        result[item_idx] = best.episode_ref
    return result

