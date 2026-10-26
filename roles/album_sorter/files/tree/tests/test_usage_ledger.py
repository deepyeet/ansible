"""B33 usage ledger: dated pricing, the recording client,
manifest + central stores, and the cli usage report."""
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from album_sorter.config import Config
from album_sorter.gemini_client import LLMResult, StubClient
from album_sorter.manifest import Manifest
from album_sorter.usage import (
    UsageRecordingClient, append_central, attach_manifest, central_rows,
    est_cost, summarize, usage_context)


def test_est_cost_dated_prices():
    # Flash promo rate through 2026, doubled from 2027-01-01
    assert est_cost("gemini-3.8-flash", 1_000_000, 1_000_000,
                    "2026-10-22T00:00:00+00:00") == 0.75 + 3.75
    assert est_cost("gemini-3.8-flash", 1_000_000, 1_000_000,
                    "2027-02-01T00:00:00+00:00") == 1.50 + 7.50
    # Flash-Lite: no announced change
    assert est_cost("gemini-3.1-flash-lite", 2_000_000, 0,
                    "2027-02-01T00:00:00+00:00") == 0.50
    # unknown model: tokens only, never a guessed price
    assert est_cost("gemini-9.9-ultra", 1_000_000, 1_000_000,
                    "2026-10-22T00:00:00+00:00") is None


def _stub_with_tokens():
    def handler(model, system, parts):
        return {"ok": True}
    stub = StubClient(handler)
    real = stub.generate_json

    def generate(model, system, parts, schema_hint="", max_output_tokens=8192, **_kw):
        r = real(model, system, parts, schema_hint=schema_hint,
                 max_output_tokens=max_output_tokens)
        return LLMResult(data=r.data, tokens_in=100, tokens_out=7,
                         model=model)
    stub.generate_json = generate
    return stub


def test_recording_client_captures_context():
    rows = []
    client = UsageRecordingClient(_stub_with_tokens())
    client.sinks.append(rows.append)
    with usage_context(stage="linkage", subject="item x -> episode 3"):
        client.generate_json("gemini-3.8-flash", "sys", [])
    assert len(rows) == 1
    row = rows[0]
    assert row["stage"] == "linkage"
    assert row["subject"] == "item x -> episode 3"
    assert row["manifest"] == "unattributed"
    assert (row["tokens_in"], row["tokens_out"]) == (100, 7)
    # outside the context the attribution is gone again
    client.generate_json("gemini-3.8-flash", "sys", [])
    assert rows[1]["stage"] is None
    # nested contexts merge and restore
    with usage_context(stage="grouping"):
        with usage_context(subject="window 2"):
            client.generate_json("gemini-3.8-flash", "sys", [])
        client.generate_json("gemini-3.8-flash", "sys", [])
    assert rows[2]["stage"] == "grouping" and rows[2]["subject"] == "window 2"
    assert rows[3]["stage"] == "grouping" and rows[3]["subject"] is None


def test_recording_client_sink_failure_never_breaks_call():
    def bad_sink(row):
        raise RuntimeError("ledger on fire")
    client = UsageRecordingClient(_stub_with_tokens())
    client.sinks.append(bad_sink)
    res = client.generate_json("gemini-3.8-flash", "sys", [])
    assert res.data == {"ok": True}
    assert client.sink_failures == 1


def test_manifest_store_and_attach():
    with tempfile.TemporaryDirectory() as td:
        man = Manifest(Path(td) / "m.sqlite3")
        client = UsageRecordingClient(_stub_with_tokens())
        assert attach_manifest(client, man) is client
        assert client.manifest_label == str(man.db_path)
        with usage_context(stage="phase1", subject="a" * 32):
            client.generate_json("gemini-3.1-flash-lite", "sys", [])
        rows = man.api_call_rows()
        assert len(rows) == 1 and rows[0]["stage"] == "phase1"
        s = summarize(rows)
        assert s["total"]["calls"] == 1
        assert s["by_stage"]["phase1"]["tokens_in"] == 100
        assert s["unpriced_calls"] == 0
        # a bare stub is NOT wrapped or recorded: free calls
        # stay out of the ledger
        stub = StubClient(lambda m, s2, p: {})
        assert attach_manifest(stub, man) is stub
        assert len(man.api_call_rows()) == 1


def test_central_ledger_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "central.sqlite3"
        old = os.environ.get("ALBUM_USAGE_LEDGER")
        os.environ["ALBUM_USAGE_LEDGER"] = str(path)
        try:
            append_central({"ts": "2026-10-22T00:00:00+00:00",
                            "manifest": "/x/m.sqlite3", "stage": "grouping",
                            "subject": "window 0", "model": "gemini-3.8-flash",
                            "tokens_in": 1000, "tokens_out": 50})
            rows = central_rows()
        finally:
            if old is None:
                del os.environ["ALBUM_USAGE_LEDGER"]
            else:
                os.environ["ALBUM_USAGE_LEDGER"] = old
        assert len(rows) == 1 and rows[0]["manifest"] == "/x/m.sqlite3"
        assert summarize(rows)["by_stage"]["grouping"]["calls"] == 1


def test_cli_usage_dispatch():
    from album_sorter import cli
    with tempfile.TemporaryDirectory() as td:
        cfg = Config(work_dir=Path(td))
        man = Manifest(cfg.manifest_path)
        man.record_api_call("2026-10-22T00:00:00+00:00", "linkage",
                            "item x -> episode 1", "gemini-3.8-flash",
                            5000, 30)
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli._dispatch(SimpleNamespace(command="usage", central=False),
                          cfg, man)
        out = json.loads(buf.getvalue())
        assert out["total"]["calls"] == 1
        assert out["by_stage"]["linkage"]["est_usd"] > 0


def test_est_cost_tier_factors():
    # Flex and Batch are a documented flat 50% off standard,
    # thoughts included in tokens_out here as billed output
    std = est_cost("gemini-3.8-flash", 1_000_000, 1_000_000,
                   "2026-10-22T00:00:00+00:00")
    assert est_cost("gemini-3.8-flash", 1_000_000, 1_000_000,
                    "2026-10-22T00:00:00+00:00",
                    service_tier="flex") == std / 2
    assert est_cost("gemini-3.8-flash", 1_000_000, 1_000_000,
                    "2026-10-22T00:00:00+00:00",
                    service_tier="batch") == std / 2
    # unset or unrecognized tiers price at standard, never
    # at a guessed discount
    assert est_cost("gemini-3.8-flash", 1_000_000, 1_000_000,
                    "2026-10-22T00:00:00+00:00",
                    service_tier=None) == std


def test_flex_call_recorded_and_priced_at_half():
    # the pricing sweep's finding: before the tier was
    # stored, a Flex run reported (and cap-accounted) at
    # standard rates - 2x its real cost
    rows: list = []
    client = UsageRecordingClient(_stub_with_tokens())
    client.sinks.append(rows.append)
    with usage_context(stage="linkage", subject="s"):
        client.generate_json("gemini-3.8-flash", "sys", [],
                             service_tier="flex")
    assert rows[0]["service_tier"] == "flex"
    std = est_cost("gemini-3.8-flash", 100, 7, rows[0]["ts"])
    assert client.spent_usd == std / 2
    # summarize rounds bucket dollars to 4 places
    assert summarize(rows)["total"]["est_usd"] == round(std / 2, 4)
