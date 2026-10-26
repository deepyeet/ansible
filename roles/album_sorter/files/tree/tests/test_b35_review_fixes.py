"""B35 independent-review fix list, pinned.

F-1: manifest usage/spill sinks must land from worker
threads (Phase 1's pool). F-2: EmptyResponse is a billed
completed generation - never retried as transport. F-3:
one question's semantic failure must not abort its whole
stage pass. F-5 (audit): Phase 1's missing-label retry
salvages call #1's classification instead of discarding
it."""
import json
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from album_sorter.config import Config
from album_sorter.gemini_client import (
    EmptyResponse, LLMResult, StubClient, UnparseableOutput, with_retries)
from album_sorter.linkage import try_link
from album_sorter.manifest import Manifest
from album_sorter.merge import run_merge
from album_sorter.phase1 import _classify_one
from album_sorter.rollup import run_rollup
from album_sorter.usage import UsageRecordingClient, attach_manifest, usage_context


class _Raw:
    """Inner client returning raw text (so spills engage)."""

    def generate_json(self, model, system, parts, **_kw):
        return LLMResult(data={"ok": True}, model=model, tokens_in=10,
                         tokens_out=3, raw_text='{"ok": true}')


def test_f1_threaded_manifest_sinks_land():
    with tempfile.TemporaryDirectory() as td:
        man = Manifest(Path(td) / "m.sqlite3")
        client = attach_manifest(UsageRecordingClient(_Raw()), man)

        def work(i):
            with usage_context(stage="phase1", subject=f"item-{i}"):
                client.generate_json("gemini-3.1-flash-lite", "sys", [])

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(work, range(8)))
        assert client.sink_failures == 0
        assert len(man.api_call_rows()) == 8
        assert len(man.raw_response_rows()) == 8


def test_f2_empty_response_never_retried():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise EmptyResponse("blocked", usage={"prompt": 5, "candidates": 0,
                                              "thoughts": 3, "total": 8,
                                              "outcome": "empty"})

    try:
        with_retries(fn, attempts=4, base_delay=0.01)
        raise AssertionError("EmptyResponse should propagate")
    except EmptyResponse:
        pass
    assert calls["n"] == 1


def _item(man, md5, when, eid=None, status="grouped"):
    man.upsert_item(md5=md5, done_name=f"{md5}.jpg", capture_time=when,
                    capture_time_raw=when, episode_id=eid,
                    status=status, category="PHOTO",
                    analysis_json=json.dumps({"description": f"photo {md5}"}))


def _episode(man, desc, times):
    eid = man.create_episode(desc, ["ex0"])
    for i, t in enumerate(times):
        _item(man, f"{desc[:2]}-{eid}-{i}", t, eid=eid)
    man.recompute_episode_span(eid)
    return eid


def test_f3_linkage_isolates_unparseable_question():
    with tempfile.TemporaryDirectory() as td:
        man = Manifest(Path(td) / "m.sqlite3")
        eid = _episode(man, "lake day", ["2026-05-01T10:00:00",
                                         "2026-05-02T10:00:00"])
        _item(man, "loose-1", "2026-05-02T12:00:00", status="analyzed")
        item = man.query_one("SELECT * FROM items WHERE md5 = 'loose-1'")

        def boom(model, system, parts):
            raise UnparseableOutput("not json")

        stats = {}
        assert try_link(Config(), man, StubClient(boom), item,
                        stats) is False
        assert stats.get("failed_questions") == 1
        assert man.decision_counts() == {}


def test_f3_merge_and_rollup_isolate_unparseable():
    with tempfile.TemporaryDirectory() as td:
        man = Manifest(Path(td) / "m.sqlite3")
        _episode(man, "museum hall", ["2026-05-01T10:00:00"])
        _episode(man, "museum annex", ["2026-05-01T11:00:00"])

        def boom(model, system, parts):
            raise UnparseableOutput("not json")

        stats = run_merge(Config(), man, StubClient(boom))
        assert stats.get("failed_pairs") == 1
    with tempfile.TemporaryDirectory() as td:
        man = Manifest(Path(td) / "m.sqlite3")
        _episode(man, "home morning", ["2026-05-01T09:00:00"])
        _episode(man, "home evening", ["2026-05-02T09:00:00"])
        stats = run_rollup(Config(), man, StubClient(boom))
        assert stats.get("failed_chains") == 1


def _row():
    return {"done_name": "x.jpg", "capture_time_raw": "2026-05-01T10:00:00",
            "device": "Pixel", "time_confidence": "high", "md5": "abc"}


def test_f5_label_retry_failure_salvages_first_classification():
    seen = {"n": 0}

    def handler(model, system, parts):
        seen["n"] += 1
        if seen["n"] == 1:
            return {"category": "PHOTO", "confidence": 0.95,
                    "label": None, "description": "a lake"}
        raise UnparseableOutput("retry died")

    _md5, data, _tin, _tout = _classify_one(
        Config(), StubClient(handler), _row())
    assert data["category"] == "PHOTO"          # call #1 stood
    assert data["description"] == "a lake"
    assert seen["n"] == 2                       # the retry did happen


def test_f5_label_retry_contributes_only_its_label():
    seen = {"n": 0}

    def handler(model, system, parts):
        seen["n"] += 1
        if seen["n"] == 1:
            return {"category": "PHOTO", "confidence": 0.95,
                    "label": None, "description": "a lake"}
        return {"category": "MISC", "confidence": 0.99,
                "label": "receipt", "description": "overwritten"}

    _md5, data, _tin, _tout = _classify_one(
        Config(), StubClient(handler), _row())
    assert data["category"] == "PHOTO"          # NOT replaced wholesale
    assert data["description"] == "a lake"
    assert data["label"] == "receipt"           # only the label crossed


def test_f_flex_payload_parity():
    """Experiment (f), Flex arm: the tier changes scheduling
    priority only - the request that reaches the model must
    be identical apart from the service_tier field itself.
    Runs against the REAL client code with a capturing fake
    transport; skips where the SDK is not installed."""
    try:
        from google.genai import types  # noqa: F401
    except Exception:
        return
    import threading
    from album_sorter.gemini_client import GenAIClient

    with tempfile.TemporaryDirectory() as td:
        assets = Path(td)
        (assets / "ab").mkdir()
        (assets / "ab" / "ab12_a.jpg").write_bytes(b"\xff\xd8fake-jpeg")
        captured = {}

        class _Resp:
            text = '{"ok": true}'
            usage_metadata = None

        class _Models:
            def generate_content(self, model, contents, config):
                captured["model"] = model
                captured["contents"] = contents
                captured["config"] = config
                return _Resp()

        class _Client:
            models = _Models()

        client = object.__new__(GenAIClient)
        client.client = _Client()
        client.assets_dir = assets
        client.calls = 0
        client.total_in = 0
        client.total_out = 0
        client.total_thoughts = 0
        client.by_model = {}
        client._lock = threading.Lock()

        parts = [{"text": "judge this"},
                 {"image_md5": "ab12"},
                 {"image_md5": "ab12", "media_resolution": "low"}]
        client.generate_json("gemini-3.8-flash", "SYS", parts)
        std = (captured["model"],
               [c if isinstance(c, str) else c.inline_data.data
                for c in captured["contents"]],
               captured["config"].model_dump(exclude={"service_tier"}))
        assert captured["config"].service_tier is None
        client.generate_json("gemini-3.8-flash", "SYS", parts,
                             service_tier="flex")
        assert str(captured["config"].service_tier).endswith("FLEX")
        flx = (captured["model"],
               [c if isinstance(c, str) else c.inline_data.data
                for c in captured["contents"]],
               captured["config"].model_dump(exclude={"service_tier"}))
        assert std == flx     # byte-identical apart from the tier
