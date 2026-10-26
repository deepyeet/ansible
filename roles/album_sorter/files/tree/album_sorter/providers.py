"""Non-Gemini provider clients (B37 / Phase F scaffolding).

Built 2026-10-23 so the provider-validation arms can start
the day API keys arrive. NOTHING in production uses these
clients yet: no stage selects them, gemini_client.make_client
is unchanged, and the only entry point is client_for_model()
below, called by harness code of the future. No provider
calls were made while building this - every test runs
against injected fake transports.

Both clients implement the exact GeminiClient contract
(generate_json -> LLMResult) and subclass GeminiClient, so
usage.UsageRecordingClient wraps them unchanged: same ledger
rows, same stage/subject attribution via usage_context(),
same spend-cap enforcement, same raw-response spill.

API choice - OpenAI: the Responses API, not Chat
Completions. It is OpenAI's primary surface for the GPT-5
family; its usage object separates reasoning cleanly
(usage.output_tokens_details.reasoning_tokens), and it
takes reasoning.effort and service_tier as first-class
parameters. Chat Completions exposes the same split, so the
deciding factors were the native reasoning controls and
that new GPT-5 features land on Responses first.

CRITICAL accounting rule (IMPROVEMENT-PLAN Phase F):
OpenAI's completion/output token count INCLUDES reasoning
tokens. LLMResult keeps the Gemini split - tokens_out is
candidates ONLY and thoughts carries the reasoning count -
so billed_out = tokens_out + thoughts reconstructs the
provider's output total exactly once. Adding reasoning on
top of the raw completion count would double-count it in
every ledger row and cap check.

Anthropic usage maps differently: input_tokens EXCLUDES
cache traffic, so the prompt total is input + cache
creation + cache read; the cache-read share lands in
LLMResult.cached (the same "subset of prompt" meaning as
Gemini's cached_content_token_count). Cache reads are NOT
free input - they are inside tokens_in and priced by
est_cost at the model's input rate, a conservative
overstatement (Anthropic bills reads at 0.1x) that is
recorded here rather than hidden. Anthropic does not split
thinking out of output_tokens, so thoughts stays 0 and
billed_out is exact without it.

Knob mappings - explicit, never silent:
  thinking_level   OpenAI: reasoning.effort (same names).
                   Anthropic: thinking budget_tokens
                   (low 1024 / medium 4096 / high 16384);
                   a budget >= max_output_tokens raises
                   ValueError instead of silently shrinking.
  service_tier     OpenAI: passed through ("flex" is a
                   real tier, half price, matches
                   usage.TIER_FACTORS); "batch" raises
                   NotImplementedError - the Batch API is
                   asynchronous and this contract is sync.
                   Anthropic: any value raises
                   NotImplementedError (no sync flex tier;
                   its 50% Batch API is async).
  media_resolution OpenAI image detail: low->"low",
                   medium->"auto", high->"high" (the
                   provider's own repricing knob; "auto" is
                   a documented degradation - the provider
                   chooses). Unknown values raise.
                   Anthropic: no per-image resolution
                   control exists (the API scales images
                   server-side); any value raises
                   NotImplementedError.
  schema_hint      appended to the system text on both
                   providers (Gemini's client ignores it;
                   here it also guarantees the word "json"
                   appears for OpenAI's json_object mode).

Keys come from the environment ONLY (OPENAI_API_KEY /
ANTHROPIC_API_KEY) - never code, never files in the repo.

Haiku prompt-size guard: HAIKU_INPUT_CLIFF_TOKENS and
estimate_prompt_tokens()/haiku_over_cliff() let harness
code keep Haiku out of >100K-token stages (grouping) per
plan S7. It is a measurement helper, deliberately NOT a
runtime block inside the client: the pricing cliff is a
cost fact, not a correctness one, and the harness owns
stage assignment.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Optional

from .gemini_client import (EmptyResponse, GeminiClient, LLMResult,
                            UnparseableOutput)


class ProviderError(RuntimeError):
    """Transport-level provider failure, mapped onto the
    taxonomy gemini_client.with_retries already understands:
    .code is the HTTP status (429 is retried, other 4xx
    fail fast via _fail_fast, 5xx is retried); .usage
    carries whatever the provider reported before failing
    (normally empty - transport failures have no usage)."""

    def __init__(self, message, code: int | None = None,
                 usage: dict | None = None):
        super().__init__(message)
        self.code = code
        self.usage = usage or {}


def _get(obj, name, default=None):
    """Attribute-or-key read: real SDK responses are objects,
    fake transports in tests may be either."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _status_of(error: Exception) -> int | None:
    """Best-effort HTTP status from an SDK-shaped exception:
    explicit status attributes first, then the class names
    both SDKs use (RateLimitError, AuthenticationError...)."""
    for attr in ("status_code", "status", "code"):
        v = getattr(error, attr, None)
        if isinstance(v, int) and 100 <= v < 600:
            return v
    name = type(error).__name__.lower()
    if "ratelimit" in name or "rate_limit" in name:
        return 429
    if "authentication" in name or "permission" in name:
        return 401
    if "timeout" in name or "timedout" in name:
        return 503    # retryable shape; never fail-fast
    return None


def _map_error(provider: str, error: Exception) -> ProviderError:
    return ProviderError(
        f"{provider} transport error: {error}",
        code=_status_of(error))


def _image_bytes(part: dict, assets_dir: Path) -> bytes:
    """Resolve an image part exactly as GenAIClient does:
    image_md5 -> assets_dir/<md5[:2]>/<md5>_a.jpg (the
    analysis copy); image_path -> the file itself."""
    if "image_md5" in part:
        md5 = part["image_md5"]
        path = assets_dir / md5[:2] / f"{md5}_a.jpg"
    else:
        path = Path(part["image_path"])
    return path.read_bytes()


def _parse_json_text(provider: str, text: str, usage: dict) -> dict:
    """Shared response-text handling: malformed JSON and a
    non-object root are the same semantic failure Gemini
    raises (UnparseableOutput, usage attached - the call
    was billed even though no result survived)."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise UnparseableOutput(
            f"{provider} output is not valid JSON: {e}",
            usage={**usage, "outcome": "unparseable"},
            raw_text=text) from e
    if not isinstance(data, dict):
        raise UnparseableOutput(
            f"{provider} output root is {type(data).__name__}, not an object",
            usage={**usage, "outcome": "unparseable"},
            raw_text=text)
    return data


# ----------------------------------------------------------------
# Haiku prompt-size guard (plan S7: the <=100K pricing cliff)

HAIKU_INPUT_CLIFF_TOKENS = 100_000

# Conservative flat allowance per image part in the
# estimate: Anthropic prices an image at (w*h)/750 tokens
# after server-side scaling to at most ~1568px on the long
# edge, so a large photo lands near ~1.5K tokens. The guard
# exists to catch grouping-size questions (hundreds of
# records + sheets), not to price a call - est_cost does
# pricing from the provider's reported usage.
IMAGE_TOKEN_ALLOWANCE = 1_600


def estimate_prompt_tokens(system: str, parts: list[dict]) -> int:
    """Rough prompt size for a generate_json question:
    text at ~4 chars/token plus a flat per-image allowance.
    Deliberately conservative (overestimates text slightly)
    so the cliff guard errs toward the cheap side."""
    chars = len(system or "")
    images = 0
    for part in parts:
        if "text" in part:
            chars += len(part["text"])
        elif "image_md5" in part or "image_path" in part:
            images += 1
    return chars // 4 + images * IMAGE_TOKEN_ALLOWANCE


def haiku_over_cliff(system: str, parts: list[dict]) -> bool:
    """True when the question would price Haiku 5.5 at the
    >100K rate. Harness code consults this before assigning
    a stage to Haiku; grouping is always over."""
    return estimate_prompt_tokens(system, parts) > HAIKU_INPUT_CLIFF_TOKENS


# ----------------------------------------------------------------
# Registry: model string -> provider wiring. Nothing in
# production consults this yet.

PROVIDER_MODELS: dict[str, dict] = {
    "gpt-5-mini": {"provider": "openai", "env": "OPENAI_API_KEY"},
    "claude-haiku-5-5": {"provider": "anthropic",
                         "env": "ANTHROPIC_API_KEY"},
}


def provider_for_model(model: str) -> str | None:
    info = PROVIDER_MODELS.get(model)
    return info["provider"] if info else None


# ----------------------------------------------------------------
# OpenAI (Responses API)

_OPENAI_DETAIL = {"low": "low", "medium": "auto", "high": "high"}


def openai_usage_fields(usage) -> dict:
    """usage object -> the ledger's field shape. Reasoning
    is INSIDE output_tokens: candidates = output - reasoning
    so billed output (candidates + thoughts) equals the
    provider's output total exactly once."""
    if usage is None:
        return {"prompt": 0, "candidates": 0, "thoughts": 0,
                "cached": 0, "total": 0}
    prompt = int(_get(usage, "input_tokens", 0) or 0)
    output = int(_get(usage, "output_tokens", 0) or 0)
    details = _get(usage, "output_tokens_details")
    reasoning = int(_get(details, "reasoning_tokens", 0) or 0)
    in_details = _get(usage, "input_tokens_details")
    cached = int(_get(in_details, "cached_tokens", 0) or 0)
    total = int(_get(usage, "total_tokens", 0) or 0) or prompt + output
    return {"prompt": prompt, "candidates": output - reasoning,
            "thoughts": reasoning, "cached": cached, "total": total}


class OpenAIClient(GeminiClient):
    """GPT-5-family client over the Responses API. transport
    is injectable for tests: an object with
    responses_create(**kwargs) -> response. The real
    transport (openai SDK) is built by client_for_model."""

    def __init__(self, assets_dir: Optional[Path] = None,
                 transport=None):
        self.assets_dir = Path(assets_dir) if assets_dir else Path("work/assets")
        self.transport = transport
        self.calls = 0
        self.total_in = 0
        self.total_out = 0
        self.total_thoughts = 0
        self.by_model: dict[str, list[int]] = {}

    def _content(self, parts: list[dict]) -> list[dict]:
        content: list[dict] = []
        for part in parts:
            if "text" in part:
                content.append({"type": "input_text",
                                "text": part["text"]})
            elif "image_md5" in part or "image_path" in part:
                block: dict = {"type": "input_image",
                               "image_url": "data:image/jpeg;base64,"
                               + base64.b64encode(
                                   _image_bytes(part, self.assets_dir)
                               ).decode("ascii")}
                res = part.get("media_resolution")
                if res is not None:
                    if res not in _OPENAI_DETAIL:
                        raise ValueError(
                            f"unknown media_resolution {res!r}; "
                            f"expected one of {sorted(_OPENAI_DETAIL)}")
                    block["detail"] = _OPENAI_DETAIL[res]
                content.append(block)
        if not content:
            content.append({"type": "input_text", "text": ""})
        return content

    def generate_json(self, model, system, parts, schema_hint="",
                      max_output_tokens=8192, thinking_level=None,
                      service_tier=None):
        if self.transport is None:
            raise RuntimeError(
                "OpenAIClient has no transport - build it via "
                "client_for_model() (tests inject a fake)")
        if service_tier == "batch":
            raise NotImplementedError(
                "OpenAI Batch API is asynchronous; the generate_json "
                "contract is synchronous. Use service_tier='flex' "
                "for the same 50% rate class.")
        instructions = system
        if schema_hint:
            instructions = f"{system}\n{schema_hint}"
        if "json" not in instructions.lower():
            # json_object mode requires the word in the input
            instructions += "\nRespond with a single JSON object."
        kwargs: dict = {
            "model": model,
            "instructions": instructions,
            "input": [{"role": "user",
                       "content": self._content(parts)}],
            "text": {"format": {"type": "json_object"}},
            "temperature": 0.2,
            "max_output_tokens": max_output_tokens,
        }
        if thinking_level:
            kwargs["reasoning"] = {"effort": thinking_level}
        if service_tier:
            kwargs["service_tier"] = service_tier
        try:
            resp = self.transport.responses_create(**kwargs)
        except (UnparseableOutput, EmptyResponse, ProviderError):
            raise
        except Exception as e:
            raise _map_error("openai", e) from e
        u = openai_usage_fields(_get(resp, "usage"))
        self.calls += 1
        self.total_in += u["prompt"]
        self.total_out += u["candidates"]
        self.total_thoughts += u["thoughts"]
        agg = self.by_model.setdefault(model, [0, 0, 0, 0])
        agg[0] += 1
        agg[1] += u["prompt"]
        agg[2] += u["candidates"]
        agg[3] += u["thoughts"]
        text = ""
        refused = False
        for item in _get(resp, "output", None) or []:
            if _get(item, "type") != "message":
                continue
            for block in _get(item, "content", None) or []:
                btype = _get(block, "type")
                if btype == "output_text":
                    text += _get(block, "text", "") or ""
                elif btype == "refusal":
                    refused = True
        if not text:
            fallback = _get(resp, "output_text")
            if isinstance(fallback, str):
                text = fallback
        if refused or not text:
            raise EmptyResponse(
                "openai response carried no output text "
                "(refusal or empty completion)",
                usage={**u, "outcome": "empty"})
        data = _parse_json_text("openai", text, u)
        return LLMResult(data=data, tokens_in=u["prompt"],
                         tokens_out=u["candidates"], model=model,
                         thoughts=u["thoughts"], cached=u["cached"],
                         total_tokens=u["total"], raw_text=text)

    def usage_summary(self) -> dict:
        return {"calls": self.calls, "tokens_in": self.total_in,
                "tokens_out": self.total_out,
                "thoughts": self.total_thoughts,
                "billed_out": self.total_out + self.total_thoughts,
                "by_model": {m: {"calls": v[0], "in": v[1], "out": v[2],
                                 "thoughts": v[3]}
                             for m, v in self.by_model.items()}}


# ----------------------------------------------------------------
# Anthropic (Messages API)

_ANTHROPIC_THINKING_BUDGETS = {"low": 1024, "medium": 4096,
                               "high": 16384}


def anthropic_usage_fields(usage) -> dict:
    """usage object -> the ledger's field shape. Prompt
    total = input + cache creation + cache read (the API
    reports input_tokens EXCLUDING cache traffic); cached
    carries the cache-read share. Output has no reasoning
    split - thinking is billed inside output_tokens - so
    thoughts is 0 and billed output is exact as-is."""
    if usage is None:
        return {"prompt": 0, "candidates": 0, "thoughts": 0,
                "cached": 0, "total": 0}
    plain = int(_get(usage, "input_tokens", 0) or 0)
    created = int(_get(usage, "cache_creation_input_tokens", 0) or 0)
    read = int(_get(usage, "cache_read_input_tokens", 0) or 0)
    output = int(_get(usage, "output_tokens", 0) or 0)
    prompt = plain + created + read
    return {"prompt": prompt, "candidates": output, "thoughts": 0,
            "cached": read, "total": prompt + output}


class AnthropicClient(GeminiClient):
    """Claude client over the Messages API. There is no
    JSON mode: JSON comes from the prompt, and the
    parse-failure rate is a first-class arm metric (plan
    Phase F). transport is injectable: an object with
    messages_create(**kwargs) -> response."""

    def __init__(self, assets_dir: Optional[Path] = None,
                 transport=None):
        self.assets_dir = Path(assets_dir) if assets_dir else Path("work/assets")
        self.transport = transport
        self.calls = 0
        self.total_in = 0
        self.total_out = 0
        self.total_thoughts = 0
        self.by_model: dict[str, list[int]] = {}

    def _content(self, parts: list[dict]) -> list[dict]:
        content: list[dict] = []
        for part in parts:
            if "text" in part:
                content.append({"type": "text", "text": part["text"]})
            elif "image_md5" in part or "image_path" in part:
                if part.get("media_resolution") is not None:
                    raise NotImplementedError(
                        "Anthropic exposes no per-image resolution "
                        "control (images are scaled server-side); "
                        "media_resolution cannot be honored, and "
                        "dropping it silently would reprice the "
                        "question without saying so")
                content.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": base64.b64encode(
                            _image_bytes(part, self.assets_dir)
                        ).decode("ascii"),
                    },
                })
        if not content:
            content.append({"type": "text", "text": ""})
        return content

    def generate_json(self, model, system, parts, schema_hint="",
                      max_output_tokens=8192, thinking_level=None,
                      service_tier=None):
        if self.transport is None:
            raise RuntimeError(
                "AnthropicClient has no transport - build it via "
                "client_for_model() (tests inject a fake)")
        if service_tier:
            raise NotImplementedError(
                f"Anthropic has no synchronous {service_tier!r} tier "
                "(its 50% Batch API is asynchronous); run standard "
                "or leave service_tier unset")
        system_text = system
        if schema_hint:
            system_text = f"{system}\n{schema_hint}"
        kwargs: dict = {
            "model": model,
            "system": system_text,
            "messages": [{"role": "user",
                          "content": self._content(parts)}],
            "max_tokens": max_output_tokens,
            "temperature": 0.2,
        }
        if thinking_level:
            if thinking_level not in _ANTHROPIC_THINKING_BUDGETS:
                raise ValueError(
                    f"unknown thinking_level {thinking_level!r}; "
                    f"expected one of {sorted(_ANTHROPIC_THINKING_BUDGETS)}")
            budget = _ANTHROPIC_THINKING_BUDGETS[thinking_level]
            if budget >= max_output_tokens:
                raise ValueError(
                    f"thinking budget {budget} must be smaller than "
                    f"max_output_tokens {max_output_tokens}; raise the "
                    "output cap rather than silently shrinking the "
                    "budget")
            kwargs["thinking"] = {"type": "enabled",
                                  "budget_tokens": budget}
        try:
            resp = self.transport.messages_create(**kwargs)
        except (UnparseableOutput, EmptyResponse, ProviderError):
            raise
        except Exception as e:
            raise _map_error("anthropic", e) from e
        u = anthropic_usage_fields(_get(resp, "usage"))
        self.calls += 1
        self.total_in += u["prompt"]
        self.total_out += u["candidates"]
        agg = self.by_model.setdefault(model, [0, 0, 0, 0])
        agg[0] += 1
        agg[1] += u["prompt"]
        agg[2] += u["candidates"]
        text = ""
        for block in _get(resp, "content", None) or []:
            if _get(block, "type") == "text":
                text += _get(block, "text", "") or ""
        if _get(resp, "stop_reason") == "refusal" or not text:
            raise EmptyResponse(
                "anthropic response carried no output text "
                "(refusal or empty completion)",
                usage={**u, "outcome": "empty"})
        data = _parse_json_text("anthropic", text, u)
        return LLMResult(data=data, tokens_in=u["prompt"],
                         tokens_out=u["candidates"], model=model,
                         thoughts=u["thoughts"], cached=u["cached"],
                         total_tokens=u["total"], raw_text=text)

    def usage_summary(self) -> dict:
        return {"calls": self.calls, "tokens_in": self.total_in,
                "tokens_out": self.total_out,
                "thoughts": self.total_thoughts,
                "billed_out": self.total_out + self.total_thoughts,
                "by_model": {m: {"calls": v[0], "in": v[1], "out": v[2],
                                 "thoughts": v[3]}
                             for m, v in self.by_model.items()}}


# ----------------------------------------------------------------
# Real transports (SDK imports are lazy - neither SDK is a
# dependency of the dev environment until keys day)

class _OpenAIResponsesTransport:
    def __init__(self, api_key: str):
        from openai import OpenAI
        self._client = OpenAI(api_key=api_key)

    def responses_create(self, **kwargs):
        return self._client.responses.create(**kwargs)


class _AnthropicMessagesTransport:
    def __init__(self, api_key: str):
        import anthropic
        self._client = anthropic.Anthropic(api_key=api_key)

    def messages_create(self, **kwargs):
        return self._client.messages.create(**kwargs)


def client_for_model(model: str,
                     assets_dir: Optional[Path] = None,
                     wrap: bool = True) -> GeminiClient:
    """Build the registered provider client for a model
    string, wrapped for the central usage ledger exactly as
    gemini_client.make_client wraps the Gemini live client
    (recording wrapper + central sinks), so caps and reports
    work identically across providers. Keys come from the
    environment only; a missing key is a loud error, never
    a silent stub (the F17 lesson). Nothing in production
    calls this yet."""
    info = PROVIDER_MODELS.get(model)
    if info is None:
        raise ValueError(
            f"no provider registered for model {model!r}; "
            f"registered: {sorted(PROVIDER_MODELS)}")
    key = os.environ.get(info["env"])
    if not key:
        raise RuntimeError(
            f"{info['env']} is not set - the {model} client is "
            "unavailable. Keys are read from the environment "
            "only, never from files in the repo.")
    if info["provider"] == "openai":
        client: GeminiClient = OpenAIClient(
            assets_dir=assets_dir,
            transport=_OpenAIResponsesTransport(key))
    else:
        client = AnthropicClient(
            assets_dir=assets_dir,
            transport=_AnthropicMessagesTransport(key))
    if wrap:
        from . import usage
        wrapped = usage.UsageRecordingClient(client)
        wrapped.sinks.append(usage.append_central)
        wrapped.spill_sinks.append(usage.append_central_raw)
        return wrapped
    return client
