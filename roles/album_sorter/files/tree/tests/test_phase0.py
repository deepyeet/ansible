"""B35 Phase 0: true accounting (thoughts billed, exit-path
recording, attempt log), fail-fast retries, the hard spend
cap, the gates journal replay, and the nominate() pending
filter. The principle under test: every token spent is
accounted for, and output already obtained is never
re-bought."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from album_sorter import usage
from album_sorter.config import Config
from album_sorter.gates import apply_gate
from album_sorter.gemini_client import (BudgetExceeded, GeminiClient,
                                        LLMResult, StubClient,
                                        UnparseableOutput, usage_fields,
                                        with_retries)
from album_sorter.linkage import nominate
from album_sorter.manifest import Manifest


class _U:
    prompt_token_count = 100
    candidates_token_count = 5
    thoughts_token_count = 42
    cached_content_token_count = 7
    total_token_count = 147


def test_usage_fields_extracts_everything():
    u = usage_fields(_U())
    assert u == {"prompt": 100, "candidates": 5, "thoughts": 42,
                 "cached": 7, "total": 147}
    assert usage_fields(None)["thoughts"] == 0


def test_est_cost_bills_thoughts_as_output():
    # tokens_out passed to est_cost is BILLED output
    # (candidates + thoughts): 1M in + 1M billed out at
    # Flash's 2026 rates = 0.75 + 3.75
    assert usage.est_cost("gemini-3.8-flash", 1_000_000, 1_000_000,
                          "2026-10-22") == 4.50


def test_summarize_counts_thoughts():
    rows = [{"ts": "2026-10-22T00:00:00+00:00", "stage": "linkage",
             "model": "gemini-3.8-flash", "tokens_in": 1_000_000,
             "tokens_out": 0, "thoughts": 1_000_000},
            {"ts": "2026-10-22T00:00:00+00:00", "stage": "linkage",
             "model": "gemini-3.8-flash", "tokens_in": 500,
             "tokens_out": 10}]     # legacy row: no thoughts key
    s = usage.summarize(rows)
    assert s["total"]["thoughts"] == 1_000_000
    assert s["total"]["est_usd"] > 4.50   # first row alone is 4.50


class _Raising(GeminiClient):
    def __init__(self, exc):
        self.exc = exc
        self.calls = 0

    def generate_json(self, model, system, parts, schema_hint="",
                      max_output_tokens=8192, **_kw):
        self.calls += 1
        raise self.exc


def test_wrapper_records_billed_failure():
    exc = UnparseableOutput("bad json", usage={
        "prompt": 100, "candidates": 5, "thoughts": 50, "cached": 0,
        "total": 155, "outcome": "unparseable"})
    client = usage.UsageRecordingClient(_Raising(exc))
    rows = []
    client.sinks.append(rows.append)
    try:
        client.generate_json("gemini-3.8-flash", "s", [])
        raise AssertionError("should have raised")
    except UnparseableOutput:
        pass
    assert len(rows) == 1
    assert rows[0]["outcome"] == "unparseable"
    assert rows[0]["thoughts"] == 50 and rows[0]["tokens_in"] == 100
    # the failed call still spent: cap accounting saw it
    assert client.spent_usd > 0


class _Fixed(GeminiClient):
    def __init__(self):
        self.calls = 0

    def generate_json(self, model, system, parts, schema_hint="",
                      max_output_tokens=8192, **_kw):
        self.calls += 1
        return LLMResult(data={}, tokens_in=1_000_000, tokens_out=0,
                         thoughts=1_000_000, model=model)


def test_spend_cap_stops_before_the_call():
    inner = _Fixed()
    client = usage.UsageRecordingClient(inner)
    client.budget_usd = 1.00
    client.generate_json("gemini-3.8-flash", "s", [])   # costs 4.50
    assert inner.calls == 1 and client.spent_usd == 4.50
    try:
        client.generate_json("gemini-3.8-flash", "s", [])
        raise AssertionError("cap should have stopped the call")
    except BudgetExceeded:
        pass
    assert inner.calls == 1     # the capped call was never made


class _APIErr(Exception):
    def __init__(self, code):
        super().__init__(f"api {code}")
        self.code = code


def test_with_retries_fail_fast_on_402():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise _APIErr(402)
    try:
        with_retries(fn, attempts=4, base_delay=0)
        raise AssertionError("should have raised")
    except _APIErr:
        pass
    assert calls["n"] == 1      # no retry storm on a dead account


def test_with_retries_still_retries_429_and_5xx():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _APIErr(429)
        return LLMResult(data={"ok": True})
    assert with_retries(fn, attempts=4, base_delay=0).data == {"ok": True}
    assert calls["n"] == 3


def test_with_retries_never_retries_budget():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise BudgetExceeded("cap")
    try:
        with_retries(fn, attempts=4, base_delay=0)
        raise AssertionError("should have raised")
    except BudgetExceeded:
        pass
    assert calls["n"] == 1


def test_attempts_are_logged():
    import os
    with tempfile.TemporaryDirectory() as td:
        ledger = Path(td) / "ledger.sqlite3"
        old = os.environ.get("ALBUM_USAGE_LEDGER")
        os.environ["ALBUM_USAGE_LEDGER"] = str(ledger)
        try:
            calls = {"n": 0}

            def fn():
                calls["n"] += 1
                if calls["n"] < 3:
                    raise RuntimeError("boom")
                return LLMResult(data={})
            with usage.usage_context(stage="linkage", subject="item x"):
                with_retries(fn, attempts=4, base_delay=0)
            rows = usage.attempt_rows()
            assert len(rows) == 2
            assert rows[0]["stage"] == "linkage"
            assert rows[0]["error_class"] == "RuntimeError"
        finally:
            if old is None:
                os.environ.pop("ALBUM_USAGE_LEDGER", None)
            else:
                os.environ["ALBUM_USAGE_LEDGER"] = old


_GATE_JSON = {"registry_match": None, "match_confidence": 0.0,
              "notability": "notable", "notability_confidence": 0.9,
              "notability_reason": "a trip abroad",
              "proposed_name": "Japan Trip", "name_basis": "generic",
              "generic_name": "Japan Trip"}


def test_gate_verdict_replays_without_rebuying():
    with tempfile.TemporaryDirectory() as td:
        man = Manifest(Path(td) / "m.sqlite3")
        eid = man.create_episode("an episode", [])
        for i in range(5):
            man.upsert_item(md5=f"{eid}-{i}", done_name=f"f{i}.jpg",
                            capture_time="2026-07-15T12:00:00",
                            capture_time_raw="2026-07-15T12:00:00",
                            episode_id=eid, status="grouped",
                            category="PHOTO")
        man.recompute_episode_span(eid)
        client = StubClient(lambda model, system, parts: _GATE_JSON)
        out1 = apply_gate(man, Config(), client, [], eid)
        assert out1 == "album_new" and len(client.calls) == 1
        # the crash scenario B35 closes: verdict bought, then
        # the outcome application was lost - the episode is
        # pending again, but the verdict is in the journal
        man.update_episode(eid, outcome="pending", album_name=None,
                           gate_character=None)
        out2 = apply_gate(man, Config(), client, [], eid)
        assert out2 == "album_new"
        assert len(client.calls) == 1, "gate verdict was re-bought"
        assert man.get_episode(eid)["album_name"] == "2026 Japan Trip"


def test_nominate_skips_non_pending_episodes():
    with tempfile.TemporaryDirectory() as td:
        man = Manifest(Path(td) / "m.sqlite3")
        ids = {}
        for label, outcome in (("open", "pending"), ("done", "album_new")):
            eid = man.create_episode(f"ep {label}", [])
            man.upsert_item(md5=f"m-{label}", done_name=f"{label}.jpg",
                            capture_time="2026-07-15T12:00:00",
                            capture_time_raw="2026-07-15T12:00:00",
                            episode_id=eid, status="grouped",
                            category="PHOTO")
            man.recompute_episode_span(eid)
            if outcome != "pending":
                man.update_episode(eid, outcome=outcome)
            ids[label] = eid
        man.upsert_item(md5="loose-1", done_name="loose.jpg",
                        capture_time="2026-07-15T13:00:00",
                        capture_time_raw="2026-07-15T13:00:00",
                        status="analyzed", category="PHOTO")
        item = man.query_one("SELECT * FROM items WHERE md5 = 'loose-1'")
        got = nominate(Config(), man, item)
        assert got == [ids["open"]]


def test_wrapper_spills_raw_response():
    class _Raw(GeminiClient):
        def generate_json(self, model, system, parts, schema_hint="",
                          max_output_tokens=8192, **_kw):
            return LLMResult(data={"a": 1}, tokens_in=10, tokens_out=2,
                             model=model, raw_text='{"a": 1}')
    client = usage.UsageRecordingClient(_Raw())
    spills = []
    client.spill_sinks.append(spills.append)
    with usage.usage_context(stage="merge", subject="episodes 1+2"):
        client.generate_json("gemini-3.8-flash", "s", [])
    assert len(spills) == 1
    assert spills[0]["raw_text"] == '{"a": 1}'
    assert spills[0]["stage"] == "merge"


def test_wrapper_spills_unparseable_raw_text():
    exc = UnparseableOutput("bad", usage={
        "prompt": 10, "candidates": 1, "thoughts": 0, "cached": 0,
        "total": 11, "outcome": "unparseable"}, raw_text='{"a": ')
    client = usage.UsageRecordingClient(_Raising(exc))
    spills = []
    client.spill_sinks.append(spills.append)
    try:
        client.generate_json("gemini-3.8-flash", "s", [])
        raise AssertionError("should have raised")
    except UnparseableOutput as e:
        assert e.raw_text == '{"a": '
    assert len(spills) == 1 and spills[0]["raw_text"] == '{"a": '


def test_attach_manifest_spills_into_manifest():
    with tempfile.TemporaryDirectory() as td:
        man = Manifest(Path(td) / "m.sqlite3")
        inner = StubClient(lambda m, s, p: {"ok": True})
        client = usage.UsageRecordingClient(inner)
        usage.attach_manifest(client, man)
        client.generate_json("gemini-3.8-flash", "s", [])
        rows = man.raw_response_rows()
        assert len(rows) == 1 and '"ok": true' in rows[0]["raw_text"]


def test_non_dict_root_is_unparseable_not_a_crash():
    # the guard lives in GenAIClient (SDK path); run it
    # against a fake transport when the SDK is importable
    # (locally it is not - the container run covers it)
    try:
        import google.genai  # noqa: F401
        from album_sorter.gemini_client import GenAIClient
        client = GenAIClient.__new__(GenAIClient)
    except Exception:
        return
    import threading
    client.calls = 0
    client.total_in = 0
    client.total_out = 0
    client.total_thoughts = 0
    client.by_model = {}
    client._lock = threading.Lock()
    client.assets_dir = Path(".")

    class _Resp:
        text = '["not", "an", "object"]'
        usage_metadata = None

    class _Models:
        def generate_content(self, **kw):
            return _Resp()

    class _Fake:
        models = _Models()
    client.client = _Fake()
    try:
        client.generate_json("gemini-3.8-flash", "s", [{"text": "x"}])
        raise AssertionError("non-dict root should raise UnparseableOutput")
    except UnparseableOutput as e:
        assert e.raw_text == '["not", "an", "object"]'
        assert e.usage.get("outcome") == "unparseable"


def test_call_extra_empty_at_defaults_and_key_stable():
    from album_sorter.journal import call_extra, decision_key
    cfg = Config()
    assert call_extra(cfg, "judgment") == {}
    # S6a adopted 2026-10-23: media_res_sheet defaults to "low",
    # so grouping's extra at defaults is exactly that one entry —
    # a deliberate, gated re-key of future grouping questions.
    # Judgment (linkage) defaults stay empty, so banked linkage
    # verdicts survive untouched.
    assert call_extra(cfg, "grouping") == {"media_res_sheet": "low"}
    k_default = decision_key("linkage", "m", "sys", {"a": 1},
                             extra=call_extra(cfg, "judgment"))
    k_legacy = decision_key("linkage", "m", "sys", {"a": 1})
    assert k_default == k_legacy      # banked verdicts survive the plumbing
    cfg2 = Config(thinking_judgment="low", media_res_exemplar="low")
    extra2 = call_extra(cfg2, "judgment")
    assert extra2 == {"thinking": "low", "media_res_exemplar": "low"}
    k_new = decision_key("linkage", "m", "sys", {"a": 1}, extra=extra2)
    assert k_new != k_legacy          # a flipped knob is a new question


def test_windows_projection_slims_people_prose():
    from album_sorter.windows import item_record
    row = {"done_name": "f.jpg", "capture_time": "2026-07-15T12:00:00",
           "device": "Pixel", "gps_lat": None, "gps_lon": None,
           "analysis_json": '{"description": "kids at a lake", '
                            '"people": {"count": 3, "description": "two adults and a child smiling"}, '
                            '"place_description": "lakeshore", '
                            '"place_name_guess": null, '
                            '"objects": ["boat"], "visible_text": ""}'}
    rec = item_record(row, 0)
    a = rec["analysis"]
    assert a["description"] == "kids at a lake"
    assert a["people_count"] == 3
    assert "people" not in a                  # the prose is gone
    assert "place_name_guess" not in a        # nulls dropped
    assert "visible_text" not in a            # empties dropped
    assert a["objects"] == ["boat"]


def test_grouping_fingerprint_reacts_to_call_knobs():
    from album_sorter.grouping import stream_fingerprint
    with tempfile.TemporaryDirectory() as td:
        man = Manifest(Path(td) / "m.sqlite3")
        man.upsert_item(md5="x1", done_name="f.jpg",
                        capture_time="2026-07-15T12:00:00",
                        capture_time_raw="2026-07-15T12:00:00",
                        status="analyzed", category="PHOTO")
        f_default = stream_fingerprint(Config(), man)
        f_again = stream_fingerprint(Config(), man)
        assert f_default == f_again
        f_low = stream_fingerprint(Config(thinking_grouping="low"), man)
        assert f_low != f_default
