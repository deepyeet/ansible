"""Per-call API usage ledger (B33).

Every completed generate_json call is recorded at call time,
so a run that dies mid-stage still leaves a complete account
of what it spent - the failure mode this replaces is the
client's in-memory counters printing only on success, which
left failed runs (B30 run 2) with no attributable spend at
all, and grouping unmeasured even in success.

Two stores, one row per call:
- the manifest's own api_calls table (via attach_manifest),
- a central ledger file shared by every client this code
  creates, so dev and calibration calls land in the same
  store as production ones. Path: env ALBUM_USAGE_LEDGER,
  'off' disables, otherwise a usage-ledger.sqlite3 beside
  the code tree. Rows carry the manifest label set by
  attach_manifest ('unattributed' until then).

Attribution: stages wrap their calls in usage_context(),
which sets stage/subject for whatever the wrapped client
records. The context is a contextvar, so it must be set in
the thread that makes the call (phase 1 sets it inside its
worker).

Costs are computed at report time from the dated PRICES
table, never stored: a price change must not rewrite the
cost history of calls already made. Google's billing page
remains the account-level authority; this ledger is the
attribution layer.

B35: rows carry the FULL usage picture - thoughts and
cached tokens and the provider's total, plus an outcome
(ok / unparseable / empty) - because output is billed as
candidates + thoughts and the B33 fields priced only
candidates, understating every call. Calls that fail
after generation (unparseable, empty) are recorded from
the usage their exception carries. Transport attempts
that never produce a response carry no usage in the SDK;
they are counted in api_attempts (notify_attempt) so the
blind spot is at least measured.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

from .gemini_client import GeminiClient, LLMResult

# model -> [(effective_from ISO date, $/M input, $/M output)]
# in chronological order; the last entry in effect at the
# call's date applies. Flash's promotional rate doubles on
# 2027-01-01 (announced on Google's pricing page, checked
# 2026-10-21). Flash-Lite had no announced change.
# Provider candidates (B37, priced 2026-10-23; figures as
# recorded in BACKLOG B37 and IMPROVEMENT-PLAN S7): GPT-5
# mini $0.25/$2.00. Claude Haiku 5.5 is NOT here - its rate
# depends on prompt size (the <=100K cliff), so it lives in
# TOKEN_TIERED_PRICES below.
PRICES: dict[str, list[tuple[str, float, float]]] = {
    "gemini-3.1-flash-lite": [("2026-01-01", 0.25, 1.50)],
    "gemini-3.8-flash": [("2026-01-01", 0.75, 3.75),
                         ("2027-01-01", 1.50, 7.50)],
    "gemini-3.1-pro": [("2026-01-01", 2.00, 12.00)],
    "gemini-3.8-pro": [("2026-01-01", 2.00, 12.00)],
    "gpt-5-mini": [("2026-01-01", 0.25, 2.00)],
}


# Models whose rate depends on the call's input size:
# model -> [(effective_from ISO date, input-token ceiling
# (None = no ceiling), $/M input, $/M output)] in
# chronological then ascending-ceiling order; the first
# entry in effect whose ceiling covers tokens_in applies.
# Claude Haiku 5.5 (B37, priced 2026-10-23): $0.10/$0.50
# for prompts <= 100K tokens, $0.50/$2.50 above - the
# cliff is why Haiku is barred from grouping-size calls
# (see providers.HAIKU_INPUT_CLIFF_TOKENS).
TOKEN_TIERED_PRICES: dict[str, list[tuple[str, int | None, float, float]]] = {
    "claude-haiku-5-5": [("2026-01-01", 100_000, 0.10, 0.50),
                         ("2026-01-01", None, 0.50, 2.50)],
}


# Service tiers price as a factor of the model's standard
# rates: Flex and Batch are a documented flat 50% off -
# input, output AND thinking tokens alike (Google pricing
# docs; cost-overhaul research.md). A tier not in this
# table prices at standard (factor 1.0) rather than
# guessing; no priority-tier factor is recorded because
# its markup was never verified against a primary source.
TIER_FACTORS = {"flex": 0.5, "batch": 0.5}


def est_cost(model: str, tokens_in: int, tokens_out: int,
             ts: str, service_tier: str | None = None) -> float | None:
    """Estimated $ for one call, or None when the model has
    no price entry (reported as tokens-only, never guessed).
    tokens_out is BILLED output - candidates + thoughts
    (B35): Google's output rate applies to both, and pricing
    candidates alone was the B30 understatement. The tier
    factor applies on top: before it existed, a Flex call
    was priced (and cap-accounted) at standard rates - a
    2x overstatement of every discounted call. Token-tiered
    models (TOKEN_TIERED_PRICES) resolve their rate from
    tokens_in first - the Haiku <=100K cliff."""
    day = (ts or "")[:10]
    tiered = TOKEN_TIERED_PRICES.get(model)
    if tiered:
        in_effect = [e for e in tiered if e[0] <= day] or [tiered[0]]
        rate_in, rate_out = in_effect[-1][2], in_effect[-1][3]
        for entry in in_effect:
            if entry[1] is None or tokens_in <= entry[1]:
                rate_in, rate_out = entry[2], entry[3]
                break
        factor = TIER_FACTORS.get((service_tier or "").lower(), 1.0)
        return factor * (tokens_in / 1e6 * rate_in
                         + tokens_out / 1e6 * rate_out)
    entries = PRICES.get(model)
    if not entries:
        return None
    rate = entries[0]
    for entry in entries:
        if entry[0] <= day:
            rate = entry
    factor = TIER_FACTORS.get((service_tier or "").lower(), 1.0)
    return factor * (tokens_in / 1e6 * rate[1]
                     + tokens_out / 1e6 * rate[2])


def price_known(model: str, tokens_in: int = 0,
                 ts: str = "2026-10-23") -> bool:
    """True when est_cost can price this model. The Phase F
    guard (IMPROVEMENT-PLAN): a registered provider model
    with no price makes est_cost return None, and the
    spend cap silently under-enforces - tests pin every
    registered model as priced."""
    return est_cost(model, tokens_in, 0, ts) is not None


def _row_get(row, key, default=0):
    """Ledger rows arrive as dicts (central) or sqlite Rows
    (manifest); legacy rows predate the B35 columns."""
    try:
        v = row[key]
    except (KeyError, IndexError):
        return default
    return default if v is None else v


def billed_out(row) -> int:
    """A ledger row's billed output: candidates + thoughts."""
    return int(_row_get(row, "tokens_out")) + int(_row_get(row, "thoughts"))


_CTX: ContextVar[dict] = ContextVar("usage_ctx", default={})


@contextmanager
def usage_context(stage: str | None = None, subject: str | None = None,
                  decision_key: str | None = None):
    """Attribute calls made inside the block. Merges over any
    enclosing context; restores on exit, including on error.
    decision_key stamps the call's raw-response spill with
    the journal key of the question being asked (B35 sweep):
    subjects are not injective for several stages, and a
    spill without its key cannot be matched back to the
    question it answers - the blocker that made the spill
    write-only."""
    merged = dict(_CTX.get())
    if stage is not None:
        merged["stage"] = stage
    if subject is not None:
        merged["subject"] = subject
    if decision_key is not None:
        merged["decision_key"] = decision_key
    token = _CTX.set(merged)
    try:
        yield
    finally:
        _CTX.reset(token)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def central_ledger_path() -> Path | None:
    import os
    raw = os.environ.get("ALBUM_USAGE_LEDGER")
    if raw is not None:
        return None if raw.strip().lower() == "off" else Path(raw)
    return Path(__file__).resolve().parent.parent / "usage-ledger.sqlite3"


_CENTRAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS api_calls (
    id INTEGER PRIMARY KEY,
    ts TEXT,
    manifest TEXT,
    stage TEXT,
    subject TEXT,
    model TEXT,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    thoughts INTEGER NOT NULL DEFAULT 0,
    cached INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    outcome TEXT NOT NULL DEFAULT 'ok',
    service_tier TEXT
)
"""

_ATTEMPTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS api_attempts (
    id INTEGER PRIMARY KEY,
    ts TEXT,
    stage TEXT,
    subject TEXT,
    attempt INTEGER,
    error_class TEXT,
    error_code INTEGER
)
"""

# B35 additive columns for ledgers created before they existed
_API_CALLS_ADDED = (("thoughts", "INTEGER NOT NULL DEFAULT 0"),
                    ("cached", "INTEGER NOT NULL DEFAULT 0"),
                    ("total_tokens", "INTEGER NOT NULL DEFAULT 0"),
                    ("outcome", "TEXT NOT NULL DEFAULT 'ok'"),
                    ("service_tier", "TEXT"))


def _ensure_api_calls_columns(db) -> None:
    cols = {r[1] for r in db.execute("PRAGMA table_info(api_calls)")}
    for name, decl in _API_CALLS_ADDED:
        if name not in cols:
            db.execute(f"ALTER TABLE api_calls ADD COLUMN {name} {decl}")


def append_central(row: dict) -> None:
    """Best-effort mirror into the central ledger. Any
    failure is swallowed by the caller-side wrapper: the
    ledger must never break a paid run."""
    path = central_ledger_path()
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=5)
    try:
        db.execute(_CENTRAL_SCHEMA)
        _ensure_api_calls_columns(db)
        db.execute(
            "INSERT INTO api_calls (ts, manifest, stage, subject, model,"
            " tokens_in, tokens_out, thoughts, cached, total_tokens,"
            " outcome, service_tier) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (row["ts"], row.get("manifest"), row.get("stage"),
             row.get("subject"), row["model"], row["tokens_in"],
             row["tokens_out"], row.get("thoughts", 0),
             row.get("cached", 0), row.get("total_tokens", 0),
             row.get("outcome", "ok"), row.get("service_tier")))
        db.commit()
    finally:
        db.close()


_RAW_SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_responses (
    id INTEGER PRIMARY KEY,
    ts TEXT,
    manifest TEXT,
    stage TEXT,
    subject TEXT,
    model TEXT,
    raw_text TEXT,
    decision_key TEXT
)
"""


def append_central_raw(row: dict) -> None:
    """Central mirror of the raw-response spill (B35).
    Best-effort like append_central."""
    path = central_ledger_path()
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=5)
    try:
        db.execute(_RAW_SCHEMA)
        cols = {r[1] for r in db.execute("PRAGMA table_info(raw_responses)")}
        if "decision_key" not in cols:
            db.execute("ALTER TABLE raw_responses ADD COLUMN decision_key TEXT")
        db.execute(
            "INSERT INTO raw_responses (ts, manifest, stage, subject,"
            " model, raw_text, decision_key) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (row["ts"], row.get("manifest"), row.get("stage"),
             row.get("subject"), row["model"], row.get("raw_text", ""),
             row.get("decision_key")))
        db.commit()
    finally:
        db.close()


def notify_attempt(attempt: int, error: Exception) -> None:
    """Record one failed transport attempt (B35). Called by
    with_retries. Transport errors carry no usage in the
    SDK, so the row holds what exists: when, which stage/
    subject (from the usage context), which attempt, and
    the error class/code. Best-effort: any failure here is
    swallowed - accounting never breaks a call."""
    path = central_ledger_path()
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(path), timeout=5)
        try:
            db.execute(_ATTEMPTS_SCHEMA)
            ctx = _CTX.get()
            code = getattr(error, "code", None)
            db.execute(
                "INSERT INTO api_attempts (ts, stage, subject, attempt,"
                " error_class, error_code) VALUES (?, ?, ?, ?, ?, ?)",
                (_now(), ctx.get("stage"), ctx.get("subject"), attempt,
                 type(error).__name__,
                 code if isinstance(code, int) else None))
            db.commit()
        finally:
            db.close()
    except Exception:
        pass


def attempt_rows() -> list[dict]:
    """All recorded failed attempts from the central ledger."""
    path = central_ledger_path()
    if path is None or not path.exists():
        return []
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table'"
                          " AND name='api_attempts'").fetchone():
            return []
        return [dict(r) for r in db.execute(
            "SELECT * FROM api_attempts ORDER BY id")]
    finally:
        db.close()


class UsageRecordingClient(GeminiClient):
    """Wraps a real client and records every call to each
    attached sink - completed AND billed-but-failed (B35):
    when the inner call raises an exception carrying .usage
    (unparseable output, empty response), the row is still
    written with its outcome before the exception
    propagates. Sinks receive a row dict; a sink that
    raises is counted, never propagated.

    Also the spend-cap enforcer: when budget_usd is set,
    generate_json raises BudgetExceeded BEFORE making a
    call once recorded spend reaches the cap, so a capped
    run stops cleanly (and resumably) instead of dying on
    the provider's 402 mid-stage."""

    def __init__(self, inner: GeminiClient):
        self._inner = inner
        self.sinks: list = []
        self.spill_sinks: list = []
        self.sink_failures = 0
        self.manifest_label = "unattributed"
        self.budget_usd: float | None = None
        self.spent_usd = 0.0

    def __getattr__(self, name):
        # usage_summary() and anything else the runner asks
        # the client for belong to the wrapped client
        return getattr(self._inner, name)

    def _spill(self, model: str, raw_text: str) -> None:
        """Write the raw response to every spill sink BEFORE
        the caller sees the result (B35, no-re-spend audit):
        obtained output becomes durable at receipt, so a
        crash in parsing, validation, or application can
        never turn it into a re-buy."""
        if not raw_text:
            return
        ctx = _CTX.get()
        row = {"ts": _now(), "manifest": self.manifest_label,
               "stage": ctx.get("stage"), "subject": ctx.get("subject"),
               "model": model, "raw_text": raw_text,
               "decision_key": ctx.get("decision_key")}
        for sink in self.spill_sinks:
            try:
                sink(row)
            except Exception:
                self.sink_failures += 1

    def _record(self, row: dict) -> None:
        cost = est_cost(row["model"], row["tokens_in"],
                        row["tokens_out"] + row.get("thoughts", 0),
                        row["ts"],
                        service_tier=row.get("service_tier"))
        if cost is not None:
            self.spent_usd += cost
        for sink in self.sinks:
            try:
                sink(row)
            except Exception:
                self.sink_failures += 1

    def _row(self, model: str, fields: dict, outcome: str,
             service_tier: str | None = None) -> dict:
        ctx = _CTX.get()
        return {"ts": _now(), "manifest": self.manifest_label,
                "stage": ctx.get("stage"), "subject": ctx.get("subject"),
                "model": model,
                "tokens_in": fields.get("prompt", 0),
                "tokens_out": fields.get("candidates", 0),
                "thoughts": fields.get("thoughts", 0),
                "cached": fields.get("cached", 0),
                "total_tokens": fields.get("total", 0),
                "outcome": outcome,
                "service_tier": service_tier}

    def generate_json(self, model, system, parts, schema_hint="",
                      max_output_tokens=8192, thinking_level=None,
                      service_tier=None) -> LLMResult:
        from .gemini_client import BudgetExceeded
        if self.budget_usd is not None and self.spent_usd >= self.budget_usd:
            raise BudgetExceeded(
                f"spend cap reached: ${self.spent_usd:.4f} recorded of "
                f"${self.budget_usd:.2f} cap")
        try:
            result = self._inner.generate_json(
                model, system, parts, schema_hint=schema_hint,
                max_output_tokens=max_output_tokens,
                thinking_level=thinking_level, service_tier=service_tier)
        except Exception as e:
            raw = getattr(e, "raw_text", "")
            if raw:
                self._spill(model, raw)
            u = getattr(e, "usage", None)
            if u:
                self._record(self._row(model, u,
                                       u.get("outcome", "error"),
                                       service_tier=service_tier))
            raise
        self._spill(result.model or model, result.raw_text)
        self._record(self._row(
            result.model or model,
            {"prompt": result.tokens_in, "candidates": result.tokens_out,
             "thoughts": result.thoughts, "cached": result.cached,
             "total": result.total_tokens}, "ok",
            service_tier=service_tier))
        return result


def ledger_total_usd(man) -> float:
    """What this manifest's ledger says the batch has
    already cost, priced with each row's own service tier.
    Unpriced rows (unknown model) contribute 0 - the same
    convention summarize() reports."""
    total = 0.0
    for r in man.api_call_rows():
        cost = est_cost(r["model"], r["tokens_in"], billed_out(r),
                        r["ts"],
                        service_tier=_row_get(r, "service_tier", None))
        if cost is not None:
            total += cost
    return total


def attach_manifest(client: GeminiClient, man) -> GeminiClient:
    """Route a wrapped client's rows into a manifest's
    api_calls table and label its central rows with that
    manifest. No-op for unwrapped clients (keyless stubs):
    stub calls are free and stay unrecorded."""
    if isinstance(client, UsageRecordingClient):
        client.manifest_label = str(man.db_path)
        # The spend cap is a BATCH budget, not a per-process
        # one (B35 sweep): seed the counter from the
        # manifest's own ledger, so a death + relaunch -
        # the designed resume path - cannot re-authorize
        # the whole cap a second time.
        client.spent_usd = ledger_total_usd(man)
        client.sinks.append(
            lambda row: man.record_api_call(
                row["ts"], row["stage"], row["subject"], row["model"],
                row["tokens_in"], row["tokens_out"],
                thoughts=row.get("thoughts", 0),
                cached=row.get("cached", 0),
                total_tokens=row.get("total_tokens", 0),
                outcome=row.get("outcome", "ok"),
                service_tier=row.get("service_tier")))
        client.spill_sinks.append(
            lambda row: man.record_raw_response(
                row["ts"], row["stage"], row["subject"], row["model"],
                row["raw_text"],
                decision_key=row.get("decision_key")))
    return client


def summarize(rows) -> dict:
    """Aggregate ledger rows (dicts or sqlite Rows with ts,
    stage, model, tokens_in, tokens_out) into per-stage and
    per-model totals with estimated dollars. Calls whose
    model has no price entry count toward tokens and are
    flagged via unpriced_calls."""
    by_stage: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    total = {"calls": 0, "tokens_in": 0, "tokens_out": 0,
             "thoughts": 0, "est_usd": 0.0}
    unpriced = 0
    for r in rows:
        billed = billed_out(r)
        cost = est_cost(r["model"], r["tokens_in"], billed, r["ts"],
                        service_tier=_row_get(r, "service_tier", None))
        if cost is None:
            unpriced += 1
            cost = 0.0
        for bucket, key in ((by_stage, r["stage"] or "unattributed"),
                            (by_model, r["model"])):
            b = bucket.setdefault(key, {"calls": 0, "tokens_in": 0,
                                        "tokens_out": 0, "thoughts": 0,
                                        "est_usd": 0.0})
            b["calls"] += 1
            b["tokens_in"] += r["tokens_in"]
            b["tokens_out"] += r["tokens_out"]
            b["thoughts"] += int(_row_get(r, "thoughts"))
            b["est_usd"] += cost
        total["calls"] += 1
        total["tokens_in"] += r["tokens_in"]
        total["tokens_out"] += r["tokens_out"]
        total["thoughts"] += int(_row_get(r, "thoughts"))
        total["est_usd"] += cost
    for bucket in (by_stage, by_model):
        for b in bucket.values():
            b["est_usd"] = round(b["est_usd"], 4)
    total["est_usd"] = round(total["est_usd"], 4)
    return {"by_stage": by_stage, "by_model": by_model, "total": total,
            "unpriced_calls": unpriced}


def central_rows() -> list[dict]:
    """All central-ledger rows as dicts (empty when the
    ledger does not exist or is disabled)."""
    path = central_ledger_path()
    if path is None or not path.exists():
        return []
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in db.execute("SELECT * FROM api_calls")]
    finally:
        db.close()
