"""Configuration and calibration knobs for the album sorter.

Defaults come from ALBUM-SPEC-DRAFT.md v1.2, Section 16 (parameter
table). Anything marked KNOB there is a sample-run calibration
parameter and lives here, not scattered through the code.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    # Roots. Dev defaults point at the local workspace; production
    # (ds223j-muse container) overrides via environment variables.
    done_dir: Path = Path(os.environ.get(
        "ALBUM_DONE_DIR", Path(__file__).resolve().parent.parent / "sample_corpus"))
    sorted_dir: Path = Path(os.environ.get(
        "ALBUM_SORTED_DIR", Path(__file__).resolve().parent.parent / "sample_sorted"))
    work_dir: Path = Path(os.environ.get(
        "ALBUM_WORK_DIR", Path(__file__).resolve().parent.parent / "work"))

    # Off-volume mirror for manifest backups. The local backup sits
    # next to the manifest (work_dir), so it cannot survive loss of
    # the work volume / container. place_all also mirrors its
    # pre-place snapshot here. Set ALBUM_BACKUP_DIR explicitly
    # (production: a directory on the pixel share, e.g.
    # /mnt/pixel/manifest-backups). Default when only
    # ALBUM_SORTED_DIR is set (a production-style run): a
    # manifest-backups folder next to the sorted root, which lives
    # on the pixel share. Dev/test runs leave this unset.
    backup_dir: Path | None = None

    def __post_init__(self):
        if self.backup_dir is None:
            if os.environ.get("ALBUM_BACKUP_DIR"):
                self.backup_dir = Path(os.environ["ALBUM_BACKUP_DIR"])
            elif os.environ.get("ALBUM_SORTED_DIR"):
                self.backup_dir = (Path(os.environ["ALBUM_SORTED_DIR"]).parent
                                    / "manifest-backups")

    # Derived assets
    analysis_px: int = 1024          # long edge, analysis copy (fixed v1)
    thumb_px: int = 512              # long edge, contact-sheet thumbnail
    poster_fraction: float = 0.25    # video poster frame position
    # B35 cost knobs. Every default reproduces the pre-B35
    # behavior exactly (standard tier, provider-default
    # thinking, provider-default image resolution), and a
    # knob joins the decision-key material the moment it is
    # set (journal.call_extra), so flipping one can never
    # silently replay verdicts bought under other settings.
    # Adoption of any non-default value is gated on the
    # validation suite (cost-overhaul/validation-plan.md).
    service_tier: str = "standard"   # KNOB: "standard" | "flex"
                                     # (flex = 50% price, 1-15 min
                                     # latency target, sheddable).
    thinking_judgment: str | None = None  # KNOB: linkage/merge/
                                     # rollup/gates/name_verify
                                     # thinking level ("low" |
                                     # "medium" | "high"); None =
                                     # provider default (medium).
    thinking_grouping: str | None = None  # KNOB: grouping's
                                     # thinking level; None =
                                     # provider default.
    media_res_item: str | None = None     # KNOB: media_resolution
                                     # for the subject image of a
                                     # judgment call ("low" |
                                     # "medium" | "high"); None =
                                     # provider default (1,120 tok).
    media_res_exemplar: str | None = None # KNOB: media_resolution
                                     # for exemplar images; None =
                                     # provider default.
    media_res_sheet: str | None = "low"   # KNOB: media_resolution
                                     # for contact/exemplar sheets;
                                     # None = provider default.
                                     # ADOPTED "low" 2026-10-23
                                     # (S6a arm passed all gates:
                                     # Jaccard 1.0000 vs reference,
                                     # 0 fusion episodes; single
                                     # draw — see IMPROVEMENT-PLAN
                                     # Phase B addendum).
    linkage_span_drift_days: float = 14.0  # KNOB (B35): a stored
                                     # linkage verdict replays while
                                     # the episode's span geometry
                                     # stays within this drift of the
                                     # judged geometry (and its
                                     # containment is unchanged);
                                     # beyond it the pair is re-asked
                                     # once and the row self-heals.
    spend_cap_usd: float = 0.0       # KNOB (B35): hard API spend cap
                                     # in USD for the BATCH (per
                                     # manifest), 0 = no cap. The
                                     # recording client's counter is
                                     # seeded from the manifest's
                                     # ledger at attach time, so a
                                     # relaunch cannot re-authorize
                                     # spend the batch already made.
                                     # Enforced BEFORE a call is made
                                     # once recorded spend reaches it, so a
                                     # capped run stops cleanly and
                                     # resumably instead of dying on the
                                     # provider's 402 mid-stage.
    ingest_workers: int = 1          # KNOB: ingest process-pool width.
                                     # Each worker holds a full-res decode
                                     # (~113 MB RSS measured); on the 1 GB
                                     # DS223j, DSM's ~560 MB baseline + 3
                                     # workers hit the RAM ceiling and
                                     # swap-thrashed the 2026-10 B30 ingest.
                                     # 1 is the safe default; raise it on
                                     # hosts with RAM to spare.

    # Phase 1 (merged classify + analyze)
    phase1_model_lite: str = "gemini-3.1-flash-lite"
    phase1_model_flash: str = "gemini-3.8-flash"
    phase1_model: str = "gemini-3.1-flash-lite"   # KNOB: sample-run A/B
    triage_confidence_floor: float = 0.6          # KNOB: below -> REVIEW
    phase1_max_output_tokens: int = 1200   # B41: raised from 700 for
                                     # the enriched v2 label - output
                                     # truncating at the old cap would
                                     # surface as label-less items and
                                     # masquerade as systemic phase-1
                                     # failure (IMPROVEMENT-PLAN S1).

    # Phase 2 windowing
    grouping_model: str = "gemini-3.8-flash"   # DECIDED 2026-10-15 (B11): Flash - the calibration A/B showed 0.91 agreement with Pro at ~1/3 cost and ~1/9 latency, and with same-run ledger visibility (B20) Flash produced the cleanest structure directly
    window_size: int = 150           # KNOB
    window_stride: int = 75          # KNOB (50% overlap)
    sheet_grid: int = 5              # KNOB: 5x5 cells per sheet
    ledger_time_pad_days: int = 14   # KNOB

    # Phase 2 linkage
    linkage_adjacency_days: int = 3
    # B34: the GPS strong-include used to nominate at ANY
    # time distance, which nominated home photos against
    # every home episode ever recorded (84.5% of attempt
    # 2's 696 verdicts were >30 days out, all "no"). GPS
    # may extend nomination only within this wider window.
    # B35 experiment (e) tightened 14 -> 7 on a 64/64
    # counterfactual. The filters sweep then measured the
    # (3, 7] slice at 133 asks and 0 winners (~$0.80/batch)
    # and the user retired the extension entirely
    # (2026-10-23), accepting the loss of the blue-wall
    # design case (a photo exactly 7 days past a same-
    # place episode no longer nominates on GPS evidence).
    # At 3 the window equals the plain adjacency window,
    # so the strong-include extends nothing; GPS keeps
    # its veto. The knob remains for future tuning.
    linkage_gps_adjacency_days: int = 3
    # B34: cap on verifier calls per loose item, applied
    # after the existing distance ranking. B35 experiment
    # (e) tightened 5 -> 3 on the same evidence (64/64
    # winners survive; combined with the 7-day window the
    # nominated surface drops 1,027 -> 646 pairs, and 341
    # of those eliminated are banked "no" verdicts -
    # questions that never need asking again).
    linkage_max_candidates: int = 3             # KNOB
    verifier_attach_threshold: float = 0.7        # KNOB
    gps_veto_km: float = 50.0
    gps_strong_km: float = 1.0

    # Trip rollup (step 2c)
    rollup_gap_hours: float = 36.0                # KNOB: chain link gap
    rollup_max_span_days: float = 30.0            # KNOB: chain span cap
    rollup_confidence: float = 0.7                # KNOB

    # Phase 2 gates (notability judgment since 2026-10-15;
    # the 100-item count rule was superseded - see spec v1.4)
    sweep_min_age_days: int = 60                  # KNOB (B29, user decision 2026-10-17): a loose photo is
    # swept into its month bucket only once its capture time is
    # at least this old; younger loose photos stay held in
    # 3_Done so late-forming episodes can still claim them
    # before placement locks them into a bucket.
    gate_registry_floor: float = 0.7              # KNOB
    gate_notability_floor: float = 0.6            # KNOB: >= -> new album
    gate_borderline_floor: float = 0.4            # KNOB: >= -> month bucket + report flag
    gate_fragment_max_items: int = 5              # KNOB: below this an episode is a
                                                  # fragment, not an occasion - a notable
                                                  # verdict flags it borderline instead
                                                  # of creating a 2-photo album

    # Clock correction
    clock_min_samples: int = 3
    clock_max_mad_minutes: float = 10.0
    clock_cluster_gap_minutes: float = 30.0       # two-cluster split threshold

    # Registry
    registry_csv: Path = Path(__file__).resolve().parent.parent / "reference" / "album-registry-photos.csv"

    @property
    def manifest_path(self) -> Path:
        return self.work_dir / "manifest.sqlite3"

    @property
    def assets_dir(self) -> Path:
        return self.work_dir / "assets"


DEFAULT = Config()
