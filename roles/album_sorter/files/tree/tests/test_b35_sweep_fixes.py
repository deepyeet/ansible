"""B35 sweep fixes: (1) the gate decision key must not
cover registry-file bytes the model never sees (the
payload already carries the exact filtered name list);
(2) phase 1 parks a deterministically failing item after
MAX_PHASE1_ATTEMPTS failed runs instead of re-buying one
call per run forever."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from album_sorter.config import Config
from album_sorter.gates import apply_gate, load_registry
from album_sorter.gemini_client import StubClient
from album_sorter.manifest import Manifest
from album_sorter import phase1

GATE_JSON = {"registry_match": None, "match_confidence": 0.0,
             "character": "family_event", "is_notable": True,
             "notability_reason": "a gathering",
             "proposed_name": "2026 July Gathering",
             "name_basis": "generic"}


def _gated_key(tmp: Path, csv_text: str) -> str:
    csv = tmp / "registry.csv"
    csv.write_text(csv_text)
    cfg = Config(registry_csv=csv)
    man = Manifest(tmp / "m.sqlite3")
    eid = man.create_episode("an episode", [])
    man.upsert_item(md5="k1", done_name="f.jpg",
                    capture_time="2026-07-15T12:00:00",
                    capture_time_raw="2026-07-15T12:00:00",
                    episode_id=eid, status="grouped",
                    category="PHOTO",
                    analysis_json='{"description": "x"}')
    man.recompute_episode_span(eid)
    client = StubClient(lambda model, system, parts: GATE_JSON)
    apply_gate(man, cfg, client, load_registry(cfg), eid)
    rows = man.query("SELECT decision_key FROM decisions"
                     " WHERE stage = 'gate'")
    assert len(rows) == 1
    return rows[0]["decision_key"]


def test_gate_key_ignores_registry_bytes_the_model_never_sees():
    # two registries: same 2026 names, different counts and
    # an extra other-year album -> identical question, so
    # the key must be identical (pre-fix it differed, and
    # every pending gate re-bought on any re-harvest)
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (tmp / "a").mkdir(); (tmp / "b").mkdir()
        ka = _gated_key(tmp / "a", "2026 Some Album|10\n")
        kb = _gated_key(tmp / "b",
                        "2026 Some Album|999\n2019 Other Album|5\n")
        assert ka == kb


def test_phase1_parks_a_deterministically_failing_item():
    import json as _json
    from album_sorter.gemini_client import UnparseableOutput
    calls = {"n": 0}

    def handler(model, system, parts):
        meta = _json.loads(parts[0]["text"])
        if meta["filename"].startswith("bad"):
            calls["n"] += 1
            raise UnparseableOutput("persistently malformed")
        return {"category": "MISC", "confidence": 0.95, "label": None}

    with tempfile.TemporaryDirectory() as td:
        man = Manifest(Path(td) / "m.sqlite3")
        man.upsert_item(md5="bad1", done_name="bad.jpg",
                        capture_time="2026-07-15T12:00:00",
                        capture_time_raw="2026-07-15T12:00:00",
                        status="ingested")
        # a healthy companion item keeps the all-fail
        # systemic guard from (correctly) aborting the run
        man.upsert_item(md5="ok1", done_name="ok.jpg",
                        capture_time="2026-07-15T12:00:00",
                        capture_time_raw="2026-07-15T12:00:00",
                        status="ingested")
        cfg = Config()
        client = StubClient(handler)
        for _ in range(phase1.MAX_PHASE1_ATTEMPTS):
            stats = phase1.run_phase1(cfg, man, client)
            assert stats["deferred"] == 1
        row = man.query("SELECT * FROM items WHERE md5 = 'bad1'")[0]
        assert row["phase1_attempts"] == phase1.MAX_PHASE1_ATTEMPTS
        assert row["status"] == "ingested"   # parked, not judged
        before = calls["n"]
        stats = phase1.run_phase1(cfg, man, client)
        assert stats["parked"] == 1 and stats["deferred"] == 0
        assert calls["n"] == before          # never re-bought


def test_spend_cap_is_seeded_from_the_manifest_ledger():
    # the crash audit's finding: spent_usd used to start at
    # 0 in every process, so N relaunches of a capped run
    # were authorized N x the cap. The counter now seeds
    # from the manifest's own api_calls (tier-aware).
    from album_sorter.gemini_client import BudgetExceeded
    from album_sorter.usage import (
        UsageRecordingClient, attach_manifest, ledger_total_usd)
    with tempfile.TemporaryDirectory() as td:
        man = Manifest(Path(td) / "m.sqlite3")
        # batch history: one standard call + one flex call
        man.record_api_call("2026-10-22T00:00:00+00:00", "linkage",
                            "s1", "gemini-3.8-flash", 1_000_000, 0)
        man.record_api_call("2026-10-22T00:00:00+00:00", "linkage",
                            "s2", "gemini-3.8-flash", 1_000_000, 0,
                            service_tier="flex")
        assert ledger_total_usd(man) == 0.75 + 0.375
        stub = StubClient(lambda model, system, parts: {"ok": True})
        client = attach_manifest(UsageRecordingClient(stub), man)
        assert client.spent_usd == 1.125
        client.budget_usd = 1.00      # already over the cap
        try:
            client.generate_json("gemini-3.8-flash", "sys", [])
            raise AssertionError("cap must trip before any call")
        except BudgetExceeded:
            pass


def _gate_with_verify(verify_error):
    """One episode whose gate proposes a signage-based
    specific name; the verify call then raises verify_error
    (or returns a judged negative when None)."""
    import json as _json
    from album_sorter.gates import GATE_SYSTEM, VERIFY_SYSTEM, apply_gate
    gate_json = {"registry_match": None, "match_confidence": 0.0,
                 "notability": "notable", "notability_confidence": 0.9,
                 "notability_reason": "a gathering",
                 "proposed_name": "Rattlesnake Lake Hike",
                 "generic_name": "July Outing",
                 "name_basis": "famous_landmark"}

    def handler(model, system, parts):
        if system == VERIFY_SYSTEM:
            if verify_error is not None:
                raise verify_error
            return {"evidenced": False, "confidence": 0.9,
                    "evidence": "no such landmark visible"}
        return gate_json

    with tempfile.TemporaryDirectory() as td:
        man = Manifest(Path(td) / "m.sqlite3")
        eid = man.create_episode("a hike", [])
        for i in range(6):   # >= gate_fragment_max_items
            man.upsert_item(md5=f"v{i}", done_name=f"f{i}.jpg",
                            capture_time="2026-07-15T12:00:00",
                            capture_time_raw="2026-07-15T12:00:00",
                            episode_id=eid, status="grouped",
                            category="PHOTO",
                            analysis_json='{"description": "trail"}')
        man.recompute_episode_span(eid)
        client = StubClient(handler)
        outcome = apply_gate(man, Config(), client, [], eid)
        rows = man.query("SELECT result_json FROM decisions"
                         " WHERE stage = 'gate'")
        rec = _json.loads(rows[0]["result_json"]) if rows else {}
        return outcome, man.get_episode(eid), rec


def test_verify_budget_trip_aborts_the_gate_not_the_name():
    from album_sorter.gemini_client import BudgetExceeded
    try:
        _gate_with_verify(BudgetExceeded("cap reached"))
        raise AssertionError("BudgetExceeded must propagate")
    except BudgetExceeded:
        pass


def test_verify_semantic_failure_is_inconclusive_not_negative():
    from album_sorter.gemini_client import UnparseableOutput
    outcome, ep, rec = _gate_with_verify(UnparseableOutput("garbled"))
    # the gate still resolves safely to the generic fallback
    assert ep["album_name"] == "2026 July Outing"
    # ...but the record says the verify never judged
    assert rec["name_verification"]["inconclusive"] is True
    assert rec["name_verification"]["evidenced"] is False


def test_spills_carry_the_pending_decision_key():
    # the crash audit's F-6 blocker: spills were matchable
    # to their question only by subject string, which is
    # not injective for gate/merge/rollup/grouping. The
    # pending key now rides the usage context onto the row.
    import json as _json
    from album_sorter.usage import UsageRecordingClient, attach_manifest, usage_context
    with tempfile.TemporaryDirectory() as td:
        man = Manifest(Path(td) / "m.sqlite3")
        stub = StubClient(lambda model, system, parts: {"ok": True})
        client = attach_manifest(UsageRecordingClient(stub), man)
        with usage_context(stage="linkage", subject="item x -> episode 3",
                           decision_key="KEY-123"):
            client.generate_json("gemini-3.8-flash", "sys", [])
        rows = man.raw_response_rows()
        assert len(rows) == 1
        assert rows[0]["decision_key"] == "KEY-123"
        assert _json.loads(rows[0]["raw_text"]) == {"ok": True}


def test_backup_is_verified_and_sidecarred():
    import json as _json
    with tempfile.TemporaryDirectory() as td:
        man = Manifest(Path(td) / "m.sqlite3")
        man.record_decision("k" * 64, "linkage", "s", {"attach": False})
        p1 = man.backup("snap")
        side = _json.loads(Path(str(p1) + ".json").read_text())
        assert side["decisions"] == 1 and side["tag"] == "snap"
        # a second backup under the same tag never silently
        # overwrites the first
        p2 = man.backup("snap")
        assert p2 != p1 and p1.exists() and p2.exists()
