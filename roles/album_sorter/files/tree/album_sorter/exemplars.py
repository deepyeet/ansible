"""M1 (B46): the ONE home for episode exemplar selection.

Before B46 every stage sampled episode evidence its own way,
and several read the grouping-time snapshot stored on the
episode row (episodes.exemplars) long after membership had
moved on. The rule now: membership-derived consumers select
from the episode's CURRENT member items in the manifest, in
the canonical capture_time/md5 order every stage already
used for journal-key stability (B32), each keeping its
historical cap and rule shape:

- gates' exemplar analyses: first 5 members carrying a
  non-empty analysis_json;
- merge's brief analyses (rollup inherits them via
  merge._episode_brief): first 3 members carrying an
  analysis_json;
- name_verify's images: B44's tier policy - members whose
  label carries visible_text first, chronological within
  tiers - cap 4.

ONE deliberate exception, documented rather than hidden:
linkage's verifier identity list still comes from the
STORED episodes.exemplars. Grouping's stored exemplars are
model-chosen during grouping (window indices the model
picked, with a first-3-members fallback in grouping's apply
path) - that choice is not reproducible from membership,
so per the B46 scope rule its source is unchanged; only
the cap slice lives here now. Selection feeds decision
keys at every site (linkage keys carry the exemplar md5s;
gate/merge payloads embed the analyses), so membership
freshness re-keys those questions prospectively by
construction: a grown episode asks a new question instead
of replaying a verdict judged from a stale sample."""
from __future__ import annotations

import json

LINKAGE_IMAGE_CAP = 4
GATE_ANALYSIS_CAP = 5
MERGE_ANALYSIS_CAP = 3
VERIFY_IMAGE_CAP = 4


def member_items(man, episode_id: int) -> list:
    """The episode's current members in canonical order -
    the single membership query every selection below (and
    the freshness tests) build on."""
    return man.query(
        "SELECT * FROM items WHERE episode_id = ? "
        "ORDER BY capture_time, md5", (episode_id,))


def exemplar_analyses(man, episode_id: int, cap: int, *,
                      nonempty: bool = False) -> list:
    """The first `cap` member analysis_json strings in
    canonical order. nonempty=False reproduces merge's
    historical SQL filter (analysis_json IS NOT NULL);
    nonempty=True reproduces gates' historical truthiness
    filter (None and "" both excluded)."""
    sql = ("SELECT analysis_json FROM items WHERE episode_id = ? "
           "AND analysis_json IS NOT NULL")
    if nonempty:
        sql += " AND analysis_json != ''"
    sql += " ORDER BY capture_time, md5 LIMIT ?"
    return [r["analysis_json"]
            for r in man.query(sql, (episode_id, cap))]


def stored_exemplars(ep, cap: int | None = LINKAGE_IMAGE_CAP) -> list:
    """Linkage's identity list: the grouping-time stored
    snapshot, order preserved, capped (cap=None returns the
    full stored list - the validation harness's image slice
    is taken from it). See the module docstring for why this
    source is NOT membership-derived."""
    stored = json.loads(ep["exemplars"] or "[]")
    return stored if cap is None else stored[:cap]


def label_has_visible_text(analysis_json) -> bool:
    """True when a phase-1 label carries non-whitespace
    visible_text. Malformed or missing labels count as no
    text (B44 semantics, moved verbatim)."""
    if not analysis_json:
        return False
    try:
        a = json.loads(analysis_json)
    except ValueError:
        return False
    return isinstance(a, dict) and \
        bool((a.get("visible_text") or "").strip())


def verify_exemplars(items: list, cap: int = VERIFY_IMAGE_CAP) -> list:
    """name_verify's selection policy (B44 S5a, folded in
    verbatim): the images name_verify judges are evidence
    carriers - a specific name is evidenced by readable
    signage, and only a member whose phase-1 label carries
    visible_text can hold it. Prefer text-bearing members,
    chronological order preserved within each tier (items
    arrive in the canonical capture_time/md5 order);
    text-less members follow in time order. Whitespace-only
    text counts as no text. Selection is payload material
    (it feeds the decision key), so this re-keys verify
    questions prospectively."""
    return ([i for i in items if label_has_visible_text(i["analysis_json"])]
            + [i for i in items
               if not label_has_visible_text(i["analysis_json"])])[:cap]
