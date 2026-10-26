"""Gemini transport.

The pipeline codes against the GeminiClient interface only.
Implementations: StubClient (deterministic fixtures; the whole
pipeline runs without a key) and GenAIClient (google-genai SDK).
Prompts and schemas live with their phases; this module owns
transport, retries, and usage accounting only.

Part protocol (dicts in the parts list):
  {"text": "..."}         -> a text part
  {"image_md5": md5}      -> the item's analysis copy (assets dir)
  {"image_path": path}    -> an image file (contact sheets)
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional


@dataclass
class LLMResult:
    data: dict                      # parsed JSON output
    tokens_in: int = 0              # prompt tokens
    tokens_out: int = 0             # candidate tokens ONLY
    model: str = ""
    # B35: the full usage picture. Google bills output as
    # candidates + thoughts; tokens_out alone understates
    # every call (the B30 cost crisis: thoughts were never
    # read at all). thoughts/cached/total come straight
    # from usage_metadata; total is the cross-check
    # (prompt + candidates + thoughts should equal it).
    thoughts: int = 0
    cached: int = 0
    total_tokens: int = 0
    # B35 (no-re-spend audit): the raw response text. The
    # recording client spills it to durable storage before
    # the caller sees the result, so obtained output is a
    # re-parseable asset, never vapor.
    raw_text: str = ""

    @property
    def billed_out(self) -> int:
        """Output tokens as billed: candidates + thoughts."""
        return self.tokens_out + self.thoughts


class BudgetExceeded(RuntimeError):
    """The run's spend cap (usage.UsageRecordingClient) is
    exhausted. Raised BEFORE a call is made, so stopping is
    free; never retried by with_retries."""


def usage_fields(usage) -> dict:
    """Extract every billed dimension from a response's
    usage_metadata (None-safe; missing fields read as 0)."""
    if usage is None:
        return {"prompt": 0, "candidates": 0, "thoughts": 0,
                "cached": 0, "total": 0}
    def _n(name):
        return int(getattr(usage, name, None) or 0)
    return {"prompt": _n("prompt_token_count"),
            "candidates": _n("candidates_token_count"),
            "thoughts": _n("thoughts_token_count"),
            "cached": _n("cached_content_token_count"),
            "total": _n("total_token_count")}


class UnparseableOutput(ValueError):
    """The model returned text that is not valid JSON (truncated
    or malformed). A SEMANTIC failure, not transport: retrying
    the identical call at the transport layer just burns the
    same failure - callers handle it with their retry-with-
    errors contract. Carries .usage (B35): the call was still
    billed, and its tokens must reach the ledger even though
    no result did."""

    def __init__(self, message, usage: dict | None = None,
                 raw_text: str = ""):
        super().__init__(message)
        self.usage = usage or {}
        self.raw_text = raw_text


class EmptyResponse(RuntimeError):
    """Safety-blocked or empty completion (text=None). The
    generation still happened and may be billed: carries
    .usage like UnparseableOutput (B35)."""

    def __init__(self, message, usage: dict | None = None):
        super().__init__(message)
        self.usage = usage or {}


class GeminiClient:
    """Interface: one structured call = prompt parts -> JSON dict.

    B35 call options (all default to the pre-B35 behavior):
    thinking_level ("low"|"medium"|"high") sets the model's
    thinking budget class; service_tier ("flex") selects the
    50%-price best-effort tier; a part dict may carry
    "media_resolution" ("low"|"medium"|"high") to reprice
    that image (Gemini 3 prices images by this setting, not
    by pixel size). LIVE-API FINDING (2026-10-23, Phase B):
    the gemini-3 (v1main) surface REJECTS media_resolution
    on individual parts (400 INVALID_ARGUMENT for low,
    medium, and high alike) but accepts it on the request
    config, where it reprices every image in the call.
    GenAIClient therefore hoists a UNIFORM per-part value
    to GenerateContentConfig.media_resolution; a call whose
    parts carry MIXED values keeps the per-part form (and
    the API's rejection) - no production call mixes values
    today. With no knob set, requests are byte-identical
    to the pre-B35 form."""

    def generate_json(self, model: str, system: str,
                      parts: list[dict], schema_hint: str = "",
                      max_output_tokens: int = 8192,
                      thinking_level: str | None = None,
                      service_tier: str | None = None) -> LLMResult:
        raise NotImplementedError


class StubClient(GeminiClient):
    """Fixture-driven client for tests and keyless smoke runs.
    handler: callable(model, system, parts) -> dict."""

    def __init__(self, handler: Optional[Callable[..., dict]] = None):
        self.calls: list[dict] = []
        if handler is None:
            # Refuse to fabricate: a bare stub returning {} for
            # every call once silently marked whole batches ERROR
            # when an environment was missing its API key (F17).
            def handler(model, system, parts):
                raise RuntimeError(
                    "StubClient with no handler and no API key - "
                    "refusing to fabricate model results")
        self.handler = handler

    def generate_json(self, model, system, parts, schema_hint="",
                      max_output_tokens=8192, thinking_level=None,
                      service_tier=None):
        self.calls.append({"model": model, "system": system, "parts": parts,
                           "thinking_level": thinking_level,
                           "service_tier": service_tier})
        data = self.handler(model, system, parts)
        return LLMResult(data=data, model=model,
                         raw_text=json.dumps(data))


class GenAIClient(GeminiClient):
    """Live client backed by google-genai. Resolves image parts
    from the assets dir; records cumulative usage for spend
    reporting (testing credit is finite - account for every call)."""

    def __init__(self, api_key: str, assets_dir: Path):
        import threading
        from google import genai
        self._genai = genai
        self.client = genai.Client(api_key=api_key)
        self.assets_dir = Path(assets_dir)
        self.calls = 0
        self.total_in = 0
        self.total_out = 0
        self.total_thoughts = 0
        self.by_model: dict[str, list[int]] = {}
        self._lock = threading.Lock()   # counters mutate from Phase 1's worker threads (F21)

    def _image_part(self, part: dict, hoisted: bool = False):
        from google.genai import types
        if "image_md5" in part:
            md5 = part["image_md5"]
            path = self.assets_dir / md5[:2] / f"{md5}_a.jpg"
        else:
            path = Path(part["image_path"])
        p = types.Part.from_bytes(data=path.read_bytes(),
                                  mime_type="image/jpeg")
        res = part.get("media_resolution")
        if res and not hoisted:
            p.media_resolution = types.MediaResolution[
                "MEDIA_RESOLUTION_" + res.upper()]
        return p

    def generate_json(self, model, system, parts, schema_hint="",
                      max_output_tokens=8192, thinking_level=None,
                      service_tier=None):
        from google.genai import types
        # media_resolution hoist (see the interface
        # docstring): a uniform value across the call's
        # image parts moves to the request config, the only
        # surface gemini-3 accepts it on.
        res_values = {part["media_resolution"] for part in parts
                      if ("image_md5" in part or "image_path" in part)
                      and part.get("media_resolution")}
        hoisted = len(res_values) == 1
        contents = []
        for part in parts:
            if "text" in part:
                contents.append(part["text"])
            elif "image_md5" in part or "image_path" in part:
                contents.append(self._image_part(part, hoisted))
        cfg_kwargs: dict = {}
        if hoisted:
            cfg_kwargs["media_resolution"] = types.MediaResolution[
                "MEDIA_RESOLUTION_" + res_values.pop().upper()]
        if thinking_level:
            cfg_kwargs["thinking_config"] = types.ThinkingConfig(
                thinking_level=types.ThinkingLevel[thinking_level.upper()])
        if service_tier == "flex":
            cfg_kwargs["service_tier"] = types.ServiceTier.FLEX
        resp = self.client.models.generate_content(
            model=model, contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                temperature=0.2,
                max_output_tokens=max_output_tokens,
                **cfg_kwargs))
        # B35: usage is read FIRST, before any raise path, and
        # travels with the exception on failure - a billed
        # call's tokens are never discarded with its result.
        u = usage_fields(getattr(resp, "usage_metadata", None))
        with self._lock:
            self.calls += 1
            self.total_in += u["prompt"]
            self.total_out += u["candidates"]
            self.total_thoughts += u["thoughts"]
            agg = self.by_model.setdefault(model, [0, 0, 0, 0])
            agg[0] += 1
            agg[1] += u["prompt"]
            agg[2] += u["candidates"]
            agg[3] += u["thoughts"]
        if not resp.text:
            # safety-blocked or empty completions arrive as
            # text=None; name it instead of dying in json.loads
            raise EmptyResponse(
                "empty model response (possibly safety-blocked)",
                usage={**u, "outcome": "empty"})
        try:
            data = json.loads(resp.text)
        except json.JSONDecodeError as e:
            raise UnparseableOutput(
                f"model output is not valid JSON: {e}",
                usage={**u, "outcome": "unparseable"},
                raw_text=resp.text) from e
        if not isinstance(data, dict):
            # B35 (no-re-spend audit F4): a valid-JSON but
            # non-object root used to crash four stages on
            # .get(), discarding a billed response. It is
            # the same semantic failure as malformed JSON -
            # route it down the same path, raw text attached.
            raise UnparseableOutput(
                f"model output root is {type(data).__name__}, not an object",
                usage={**u, "outcome": "unparseable"},
                raw_text=resp.text)
        return LLMResult(data=data, tokens_in=u["prompt"],
                         tokens_out=u["candidates"], model=model,
                         thoughts=u["thoughts"], cached=u["cached"],
                         total_tokens=u["total"], raw_text=resp.text)

    def usage_summary(self) -> dict:
        return {"calls": self.calls, "tokens_in": self.total_in,
                "tokens_out": self.total_out,
                "thoughts": self.total_thoughts,
                "billed_out": self.total_out + self.total_thoughts,
                "by_model": {m: {"calls": v[0], "in": v[1], "out": v[2],
                                 "thoughts": v[3]}
                             for m, v in self.by_model.items()}}


def coerce_float(v, default: float = 0.0) -> float:
    """Model JSON is untrusted typing: confidences arrive as
    strings ('high'), nulls, or worse. Every confidence read in
    the package goes through here so a mistyped value degrades
    to the default instead of crashing the pass mid-run (F9)."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _fail_fast(e: Exception) -> bool:
    """Client errors that retrying cannot fix (B35): any 4xx
    except 429 (rate limit) - a 402 (quota exhausted) or 400
    (bad request) fails identically on every retry, and each
    retry is another admission attempt against a dead or
    invalid request. SDK errors carry .code; anything else
    retries as before."""
    code = getattr(e, "code", None)
    return isinstance(code, int) and 400 <= code < 500 and code != 429


def with_retries(fn: Callable[[], LLMResult], attempts: int = 4,
                 base_delay: float = 2.0) -> LLMResult:
    """Exponential backoff, mirroring the WSL pipeline's pattern.
    Every failed attempt is reported to the usage layer
    (B35 attempt accounting): transport failures carry no
    token data - provably, in the installed SDK - but their
    count and error class must be visible, not silent."""
    last = None
    for i in range(attempts):
        try:
            return fn()
        except (UnparseableOutput, EmptyResponse, BudgetExceeded):
            raise   # semantic failure / budget stop - never retried
                    # (F-2, B35 review: an empty/blocked response
                    # is a completed, billed generation - retrying
                    # it re-buys the same dead question)
        except Exception as e:  # transport-level; callers handle semantics
            last = e
            try:
                from . import usage
                usage.notify_attempt(i + 1, e)
            except Exception:
                pass    # accounting must never break a call
            if _fail_fast(e):
                raise
            if i < attempts - 1:
                time.sleep(base_delay * (2 ** i))
    raise RuntimeError(f"LLM call failed after {attempts} attempts: {last}")


def _load_key() -> Optional[str]:
    if os.environ.get("GEMINI_API_KEY"):
        return os.environ["GEMINI_API_KEY"]
    for env_path in (Path(".env"), Path(__file__).resolve().parent.parent / ".env"):
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("GEMINI_API_KEY="):
                    return line.split("=", 1)[1].strip()
    return None


def make_client(assets_dir: Optional[Path] = None) -> GeminiClient:
    """Live client when a key is configured; stub otherwise.
    Live clients are wrapped for the usage ledger (B33):
    every completed call lands in the central ledger, and
    attach_manifest() adds the run's own manifest as a
    second store. Stub calls are free and stay unwrapped."""
    key = _load_key()
    if key:
        from . import usage
        client = usage.UsageRecordingClient(
            GenAIClient(key, assets_dir or Path("work/assets")))
        client.sinks.append(usage.append_central)
        client.spill_sinks.append(usage.append_central_raw)
        return client
    return StubClient()
