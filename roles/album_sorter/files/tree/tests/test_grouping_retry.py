"""Tests for the Phase 2 grouping-call retry (spec Sec. 10):
invalid output is retried exactly once with the validation
errors appended; a second invalid output raises, never repaired."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from album_sorter.gemini_client import StubClient
from album_sorter.grouping import call_window

VALID = {"assignments": {"0": "N1", "1": "N1", "2": "1"},
         "new_episodes": [{"temp_id": "N1", "description": "zoo",
                           "exemplars": [0]}],
         "unassigned": [], "summary_update": "at the zoo"}
INVALID = {"assignments": {"0": "1"}, "new_episodes": [],
           "unassigned": [], "summary_update": ""}


def test_invalid_then_valid_retries_once_with_errors():
    seen = []

    def handler(model, system, parts):
        seen.append(parts)
        return INVALID if len(seen) == 1 else VALID

    client = StubClient(handler)
    out, retried = call_window(client, [{"text": "{}"}], 3, {"1"})
    assert retried is True
    assert len(client.calls) == 2
    retry_text = client.calls[1]["parts"][-1]["text"]
    payload = json.loads(retry_text)
    assert any("missing" in e for e in payload["validation_errors"])
    assert out.assignments[2] == "1"


def test_valid_first_try_no_retry():
    client = StubClient(lambda model, system, parts: VALID)
    out, retried = call_window(client, [{"text": "{}"}], 3, {"1"})
    assert retried is False
    assert len(client.calls) == 1


def test_invalid_twice_raises_after_exactly_one_retry():
    client = StubClient(lambda model, system, parts: INVALID)
    try:
        call_window(client, [{"text": "{}"}], 3, {"1"})
    except ValueError as e:
        assert "after one retry" in str(e)
    else:
        raise AssertionError("expected ValueError")
    assert len(client.calls) == 2
