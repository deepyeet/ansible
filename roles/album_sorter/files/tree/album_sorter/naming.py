"""Album-name evidence policy (B27, redesigned 2026-10-16).

The failure: model narration asserted specific names -
'Rattlesnake Lake' (never been there), 'Metaversary' (an
office visit, not an anniversary) - and they became folder
names unchecked.

A denylist of disputed names was built first and REJECTED
by the user the same day, correctly: a place name is not
wrong in itself (he may visit Rattlesnake Lake for real
next year), a blacklist does not scale, and it confuses
'wrong for that episode' with 'forbidden forever'.

The scalable principle: a specific name is a factual
claim about THIS episode, and claims need evidence from
the photos themselves. So the gate proposes two names -
a specific one (only with a declared basis: signage read
in the photos, or an unmistakable famous landmark) and a
generic fallback - and code VERIFIES the specific one
against the episode's exemplar images with a dedicated
call before it may be used. Unverified specifics fall
back to the generic name; the episode still gets its
album, just under a name that makes no false claim. The
basis is stored on the episode and specific names are
listed in the report for one-glance human review.
"""
from __future__ import annotations

BASES = ("signage", "famous_landmark", "generic")


def coerce_basis(value) -> str:
    b = str(value or "generic").strip().lower()
    return b if b in BASES else "generic"
