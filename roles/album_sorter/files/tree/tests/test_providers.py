"""Phase F scaffolding tests (B37/B45): provider clients
against injected fake transports - no network, no keys.
Pins the shared generate_json contract, the OpenAI
reasoning-counted-once rule, Anthropic cache-field mapping,
error taxonomy mapping, pricing resolution (including the
Haiku <=100K cliff) and the central-ledger row shape."""
import base64
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

from album_sorter import providers, usage
from album_sorter.gemini_client import (EmptyResponse, UnparseableOutput,
                                        _fail_fast)
from album_sorter.providers import (AnthropicClient, OpenAIClient,
                                    ProviderError, client_for_model,
                                    estimate_prompt_tokens, haiku_over_cliff,
                                    provider_for_model)

MD5 = "ab" + "c" * 30
JPEG = b"\xff\xd8\xff\xe0fake-jpeg-bytes"


def _assets(tmp: str) -> Path:
    d = Path(tmp) / "assets" / MD5[:2]
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{MD5}_a.jpg").write_bytes(JPEG)
    return Path(tmp) / "assets"


def _b64() -> str:
    return base64.b64encode(JPEG).decode("ascii")


# ---------------- fake transports & responses ----------------

class FakeOpenAITransport:
    def __init__(self, resp=None, error=None):
        self.resp, self.error, self.requests = resp, error, []

    def responses_create(self, **kwargs):
        self.requests.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.resp


class FakeAnthropicTransport:
    def __init__(self, resp=None, error=None):
        self.resp, self.error, self.requests = resp, error, []

    def messages_create(self, **kwargs):
        self.requests.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.resp


def oai_resp(text, prompt=1000, output=300, reasoning=120, cached=0,
             content_type="output_text"):
    u = SimpleNamespace(
        input_tokens=prompt, output_tokens=output,
        total_tokens=prompt + output,
        output_tokens_details=SimpleNamespace(reasoning_tokens=reasoning),
        input_tokens_details=SimpleNamespace(cached_tokens=cached))
    block = SimpleNamespace(type=content_type, text=text)
    item = SimpleNamespace(type="message", content=[block])
    return SimpleNamespace(status="completed", output=[item], usage=u)


def ant_resp(text, plain=500, created=100, read=400, output=200,
             stop_reason="end_turn", blocks=None):
    u = SimpleNamespace(input_tokens=plain,
                        cache_creation_input_tokens=created,
                        cache_read_input_tokens=read,
                        output_tokens=output)
    if blocks is None:
        blocks = [SimpleNamespace(type="text", text=text)]
    return SimpleNamespace(content=blocks, stop_reason=stop_reason,
                           usage=u)


class RateLimitError(Exception):
    pass


class AuthenticationError(Exception):
    pass


class StatusError(Exception):
    def __init__(self, msg, status_code=None, status=None):
        super().__init__(msg)
        self.status_code = status_code
        self.status = status


# ---------------- OpenAI ----------------

def test_openai_request_construction():
    with tempfile.TemporaryDirectory() as tmp:
        t = FakeOpenAITransport(resp=oai_resp('{"ok": true}'))
        c = OpenAIClient(assets_dir=_assets(tmp), transport=t)
        img = Path(tmp) / "loose.jpg"
        img.write_bytes(JPEG)
        r = c.generate_json(
            "gpt-5-mini", "You are a matcher. Answer in JSON.",
            [{"text": "question body"},
             {"image_md5": MD5},
             {"image_path": str(img), "media_resolution": "low"}],
            schema_hint='{"same": bool}')
        assert r.data == {"ok": True}
        (req,) = t.requests
        assert req["model"] == "gpt-5-mini"
        assert req["instructions"].startswith("You are a matcher.")
        assert '{"same": bool}' in req["instructions"]
        assert req["text"] == {"format": {"type": "json_object"}}
        assert req["temperature"] == 0.2
        (msg,) = req["input"]
        assert msg["role"] == "user"
        kinds = [b["type"] for b in msg["content"]]
        assert kinds == ["input_text", "input_image", "input_image"]
        assert msg["content"][0]["text"] == "question body"
        assert msg["content"][1]["image_url"] == \
            "data:image/jpeg;base64," + _b64()
        assert "detail" not in msg["content"][1]   # unset knob stays unset
        assert msg["content"][2]["detail"] == "low"


def test_openai_media_resolution_mapping_and_bad_value():
    with tempfile.TemporaryDirectory() as tmp:
        for res, detail in (("low", "low"), ("medium", "auto"),
                            ("high", "high")):
            t = FakeOpenAITransport(resp=oai_resp('{"ok": true}'))
            c = OpenAIClient(assets_dir=_assets(tmp), transport=t)
            c.generate_json("gpt-5-mini", "JSON please",
                            [{"image_md5": MD5, "media_resolution": res}])
            block = t.requests[0]["input"][0]["content"][0]
            assert block["detail"] == detail, (res, block)
        t = FakeOpenAITransport(resp=oai_resp('{"ok": true}'))
        c = OpenAIClient(assets_dir=_assets(tmp), transport=t)
        try:
            c.generate_json("gpt-5-mini", "JSON please",
                            [{"image_md5": MD5,
                              "media_resolution": "ultra"}])
            raise AssertionError("expected ValueError")
        except ValueError:
            pass


def test_openai_reasoning_counted_once():
    # completion 300 INCLUDES 120 reasoning: candidates must
    # be 180, thoughts 120, billed output exactly 300 - never 420
    t = FakeOpenAITransport(resp=oai_resp('{"ok": true}', prompt=1000,
                                          output=300, reasoning=120,
                                          cached=250))
    c = OpenAIClient(transport=t)
    r = c.generate_json("gpt-5-mini", "JSON please", [{"text": "q"}])
    assert r.tokens_in == 1000
    assert r.tokens_out == 180
    assert r.thoughts == 120
    assert r.billed_out == 300
    assert r.cached == 250
    assert r.total_tokens == 1300
    cost = usage.est_cost("gpt-5-mini", r.tokens_in, r.billed_out,
                          "2026-10-23")
    assert abs(cost - (1000 * 0.25 + 300 * 2.00) / 1e6) < 1e-12


def test_openai_flex_tier_and_batch_rejected():
    t = FakeOpenAITransport(resp=oai_resp('{"ok": true}'))
    c = OpenAIClient(transport=t)
    c.generate_json("gpt-5-mini", "JSON please", [{"text": "q"}],
                    service_tier="flex")
    assert t.requests[0]["service_tier"] == "flex"
    std = usage.est_cost("gpt-5-mini", 1000, 300, "2026-10-23")
    flex = usage.est_cost("gpt-5-mini", 1000, 300, "2026-10-23",
                          service_tier="flex")
    assert flex == std / 2
    try:
        c.generate_json("gpt-5-mini", "JSON please", [{"text": "q"}],
                        service_tier="batch")
        raise AssertionError("expected NotImplementedError")
    except NotImplementedError:
        pass


def test_openai_thinking_level_maps_to_reasoning_effort():
    t = FakeOpenAITransport(resp=oai_resp('{"ok": true}'))
    c = OpenAIClient(transport=t)
    c.generate_json("gpt-5-mini", "JSON please", [{"text": "q"}],
                    thinking_level="low")
    assert t.requests[0]["reasoning"] == {"effort": "low"}


def test_openai_unparseable_and_nondict_carry_usage():
    for bad in ("not json at all", '["a", "list"]'):
        t = FakeOpenAITransport(resp=oai_resp(bad, prompt=700,
                                              output=90, reasoning=30))
        c = OpenAIClient(transport=t)
        try:
            c.generate_json("gpt-5-mini", "JSON please", [{"text": "q"}])
            raise AssertionError("expected UnparseableOutput")
        except UnparseableOutput as e:
            assert e.usage["prompt"] == 700
            assert e.usage["candidates"] == 60
            assert e.usage["thoughts"] == 30
            assert e.usage["outcome"] == "unparseable"
            assert e.raw_text == bad


def test_openai_refusal_is_empty_response_with_usage():
    t = FakeOpenAITransport(resp=oai_resp("", prompt=450, output=10,
                                          reasoning=0,
                                          content_type="refusal"))
    c = OpenAIClient(transport=t)
    try:
        c.generate_json("gpt-5-mini", "JSON please", [{"text": "q"}])
        raise AssertionError("expected EmptyResponse")
    except EmptyResponse as e:
        assert e.usage["prompt"] == 450
        assert e.usage["outcome"] == "empty"


def test_openai_error_mapping_onto_retry_taxonomy():
    cases = [(RateLimitError("slow down"), 429, False),
             (AuthenticationError("bad key"), 401, True),
             (StatusError("boom", status_code=500), 500, False),
             (StatusError("bad request", status_code=400), 400, True)]
    for err, code, fail_fast in cases:
        t = FakeOpenAITransport(error=err)
        c = OpenAIClient(transport=t)
        try:
            c.generate_json("gpt-5-mini", "JSON please", [{"text": "q"}])
            raise AssertionError("expected ProviderError")
        except ProviderError as e:
            assert e.code == code, (err, e.code)
            assert _fail_fast(e) is fail_fast, (err, code)


# ---------------- Anthropic ----------------

def test_anthropic_request_construction():
    with tempfile.TemporaryDirectory() as tmp:
        t = FakeAnthropicTransport(resp=ant_resp('{"ok": true}'))
        c = AnthropicClient(assets_dir=_assets(tmp), transport=t)
        r = c.generate_json(
            "claude-haiku-5-5", "You are a matcher.",
            [{"text": "question body"}, {"image_md5": MD5}],
            schema_hint='{"same": bool}', max_output_tokens=2048)
        assert r.data == {"ok": True}
        (req,) = t.requests
        assert req["model"] == "claude-haiku-5-5"
        assert req["system"].startswith("You are a matcher.")
        assert '{"same": bool}' in req["system"]
        assert req["max_tokens"] == 2048
        assert req["temperature"] == 0.2
        (msg,) = req["messages"]
        assert msg["content"][0] == {"type": "text",
                                     "text": "question body"}
        img = msg["content"][1]
        assert img["type"] == "image"
        assert img["source"]["type"] == "base64"
        assert img["source"]["media_type"] == "image/jpeg"
        assert img["source"]["data"] == _b64()


def test_anthropic_media_resolution_raises_not_drops():
    with tempfile.TemporaryDirectory() as tmp:
        t = FakeAnthropicTransport(resp=ant_resp('{"ok": true}'))
        c = AnthropicClient(assets_dir=_assets(tmp), transport=t)
        try:
            c.generate_json("claude-haiku-5-5", "sys",
                            [{"image_md5": MD5,
                              "media_resolution": "low"}])
            raise AssertionError("expected NotImplementedError")
        except NotImplementedError:
            pass
        assert t.requests == []   # refused before any call


def test_anthropic_usage_cache_fields_mapped_honestly():
    # input_tokens EXCLUDES cache: prompt = 500 + 100 + 400;
    # cached carries the read share; output has no split
    t = FakeAnthropicTransport(resp=ant_resp('{"ok": true}'))
    c = AnthropicClient(transport=t)
    r = c.generate_json("claude-haiku-5-5", "sys", [{"text": "q"}])
    assert r.tokens_in == 1000
    assert r.cached == 400
    assert r.tokens_out == 200
    assert r.thoughts == 0
    assert r.billed_out == 200
    assert r.total_tokens == 1200


def test_anthropic_refusal_is_empty_response_with_usage():
    t = FakeAnthropicTransport(resp=ant_resp("", stop_reason="refusal",
                                             blocks=[]))
    c = AnthropicClient(transport=t)
    try:
        c.generate_json("claude-haiku-5-5", "sys", [{"text": "q"}])
        raise AssertionError("expected EmptyResponse")
    except EmptyResponse as e:
        assert e.usage["prompt"] == 1000
        assert e.usage["outcome"] == "empty"


def test_anthropic_unparseable_carries_usage():
    t = FakeAnthropicTransport(resp=ant_resp("definitely not json"))
    c = AnthropicClient(transport=t)
    try:
        c.generate_json("claude-haiku-5-5", "sys", [{"text": "q"}])
        raise AssertionError("expected UnparseableOutput")
    except UnparseableOutput as e:
        assert e.usage["candidates"] == 200
        assert e.usage["outcome"] == "unparseable"
        assert e.raw_text == "definitely not json"


def test_anthropic_service_tier_raises():
    t = FakeAnthropicTransport(resp=ant_resp('{"ok": true}'))
    c = AnthropicClient(transport=t)
    for tier in ("flex", "batch"):
        try:
            c.generate_json("claude-haiku-5-5", "sys", [{"text": "q"}],
                            service_tier=tier)
            raise AssertionError("expected NotImplementedError")
        except NotImplementedError:
            pass
    assert t.requests == []


def test_anthropic_thinking_budget_mapping_and_guard():
    t = FakeAnthropicTransport(resp=ant_resp('{"ok": true}'))
    c = AnthropicClient(transport=t)
    c.generate_json("claude-haiku-5-5", "sys", [{"text": "q"}],
                    thinking_level="low")
    assert t.requests[0]["thinking"] == {"type": "enabled",
                                         "budget_tokens": 1024}
    try:
        c.generate_json("claude-haiku-5-5", "sys", [{"text": "q"}],
                        thinking_level="high", max_output_tokens=8192)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_anthropic_error_mapping_onto_retry_taxonomy():
    cases = [(StatusError("overloaded", status=529), 529, False),
             (StatusError("bad request", status_code=400), 400, True),
             (RateLimitError("slow down"), 429, False)]
    for err, code, fail_fast in cases:
        t = FakeAnthropicTransport(error=err)
        c = AnthropicClient(transport=t)
        try:
            c.generate_json("claude-haiku-5-5", "sys", [{"text": "q"}])
            raise AssertionError("expected ProviderError")
        except ProviderError as e:
            assert e.code == code, (err, e.code)
            assert _fail_fast(e) is fail_fast, (err, code)


# ---------------- pricing, guard, ledger, factory ----------------

def test_every_registered_provider_model_is_priced():
    # The Phase F guard: an unpriced registered model makes
    # est_cost return None and the spend cap under-enforces
    # silently. Pin every registry entry as priced, on both
    # sides of the Haiku cliff.
    assert providers.PROVIDER_MODELS, "registry must not be empty"
    for model in providers.PROVIDER_MODELS:
        assert usage.price_known(model, tokens_in=1_000), model
        assert usage.price_known(model, tokens_in=500_000), model


def test_haiku_cliff_pricing_boundaries():
    below = usage.est_cost("claude-haiku-5-5", 100_000, 1_000,
                           "2026-10-23")
    assert abs(below - (100_000 * 0.10 + 1_000 * 0.50) / 1e6) < 1e-12
    above = usage.est_cost("claude-haiku-5-5", 100_001, 1_000,
                           "2026-10-23")
    assert abs(above - (100_001 * 0.50 + 1_000 * 2.50) / 1e6) < 1e-9
    small = usage.est_cost("claude-haiku-5-5", 5_000, 500, "2026-10-23")
    assert abs(small - (5_000 * 0.10 + 500 * 0.50) / 1e6) < 1e-12
    half = usage.est_cost("claude-haiku-5-5", 5_000, 500, "2026-10-23",
                          service_tier="batch")
    assert half == small / 2


def test_haiku_cliff_guard():
    assert providers.HAIKU_INPUT_CLIFF_TOKENS == 100_000
    small = [{"text": "short question"}]
    assert estimate_prompt_tokens("sys", small) < 100_000
    assert not haiku_over_cliff("sys", small)
    big = [{"text": "x" * 500_000}]
    assert haiku_over_cliff("sys", big)
    many_images = [{"image_md5": MD5}] * 100
    assert estimate_prompt_tokens("", many_images) == 100 * \
        providers.IMAGE_TOKEN_ALLOWANCE


def test_recording_wrapper_row_shape_and_spend():
    # Same ledger row the Gemini path writes: stage/subject
    # from usage_context, candidates-only tokens_out,
    # thoughts separate, spend accounted at the mini rate
    t = FakeOpenAITransport(resp=oai_resp('{"ok": true}', prompt=1000,
                                          output=300, reasoning=120,
                                          cached=40))
    wrapped = usage.UsageRecordingClient(OpenAIClient(transport=t))
    rows, spills = [], []
    wrapped.sinks.append(rows.append)
    wrapped.spill_sinks.append(spills.append)
    with usage.usage_context(stage="exp-b37", subject="pair-1"):
        r = wrapped.generate_json("gpt-5-mini", "JSON please",
                                  [{"text": "q"}])
    assert r.data == {"ok": True}
    (row,) = rows
    assert row["stage"] == "exp-b37" and row["subject"] == "pair-1"
    assert row["model"] == "gpt-5-mini"
    assert row["tokens_in"] == 1000
    assert row["tokens_out"] == 180      # candidates only
    assert row["thoughts"] == 120
    assert row["cached"] == 40
    assert row["total_tokens"] == 1300
    assert row["outcome"] == "ok"
    assert abs(wrapped.spent_usd
               - (1000 * 0.25 + 300 * 2.00) / 1e6) < 1e-12
    (spill,) = spills
    assert spill["raw_text"] == '{"ok": true}'


def test_factory_registry_and_key_errors():
    assert provider_for_model("gpt-5-mini") == "openai"
    assert provider_for_model("claude-haiku-5-5") == "anthropic"
    assert provider_for_model("gemini-3.8-flash") is None
    try:
        client_for_model("no-such-model")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    for model, env in (("gpt-5-mini", "OPENAI_API_KEY"),
                       ("claude-haiku-5-5", "ANTHROPIC_API_KEY")):
        saved = os.environ.pop(env, None)
        try:
            try:
                client_for_model(model)
                raise AssertionError("expected RuntimeError")
            except RuntimeError as e:
                assert env in str(e)
        finally:
            if saved is not None:
                os.environ[env] = saved
