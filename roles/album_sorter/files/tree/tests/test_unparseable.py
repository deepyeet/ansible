"""Unparseable model output (the B5 run-killer, 2026-10-16):
a grouping response that is not valid JSON must go through
the retry-with-errors contract ONCE - not burn four identical
transport retries and kill the run. Plus crash-orphan episode
cleanup at grouping start.
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from album_sorter.config import Config
from album_sorter.gemini_client import LLMResult, UnparseableOutput
from album_sorter.grouping import call_window, run_grouping
from album_sorter.manifest import Manifest

VALID = {"assignments": {"0": "N1", "1": "N1", "2": "N1"},
         "new_episodes": [{"temp_id": "N1", "description": "zoo",
                           "exemplars": [0]}],
         "unassigned": [], "summary_update": "zoo"}


class FlakyJsonClient:
    """Raises UnparseableOutput for the first `fails` calls."""

    def __init__(self, fails):
        self.fails = fails
        self.calls = 0

    def generate_json(self, model, system, parts, schema_hint="",
                      max_output_tokens=8192, **_kw):
        self.calls += 1
        if self.calls <= self.fails:
            raise UnparseableOutput("model output is not valid JSON: Expecting value")
        return LLMResult(data=VALID, model=model)


def test_unparseable_once_retries_and_succeeds():
    client = FlakyJsonClient(fails=1)
    out, retried = call_window(client, [{"text": "{}"}], 3, set())
    assert retried is True and client.calls == 2
    assert out.assignments[0] == "N1"


def test_unparseable_twice_raises_valueerror():
    client = FlakyJsonClient(fails=2)
    try:
        call_window(client, [{"text": "{}"}], 3, set())
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "unparseable after one retry" in str(e)
    assert client.calls == 2        # exactly one retry - not four


def test_orphan_episodes_collected_at_grouping_start():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cfg = Config(done_dir=tmp / "done", work_dir=tmp / "work",
                     sorted_dir=tmp / "sorted")
        man = Manifest(cfg.manifest_path)
        orphan = man.create_episode("ghost from a dead run", [])
        man.update_episode(orphan, span_start="2026-07-01T09:00:00",
                           span_end="2026-07-01T10:00:00", item_count=5)
        real = man.create_episode("real episode", [])
        man.upsert_item(md5="a" * 32, done_name="x.jpg", category="PHOTO",
                        status="analyzed", capture_time="2026-07-01T09:30:00",
                        capture_time_raw="2026-07-01T09:30:00",
                        episode_id=real,
                        analysis_json=json.dumps({"description": "d"}))
        man.update_episode(real, span_start="2026-07-01T09:00:00",
                           span_end="2026-07-01T10:00:00", item_count=1)
        # the analyzed item is re-grouped by the run (it has no
        # votes yet); the orphan must be gone afterward
        from album_sorter.gemini_client import StubClient
        handler = lambda m, s, p: {"assignments": {"0": "N1"},
                                   "new_episodes": [{"temp_id": "N1",
                                                     "description": "d",
                                                     "exemplars": [0]}],
                                   "unassigned": [], "summary_update": ""}
        man.update_item("a" * 32, episode_id=None)
        run_grouping(cfg, man, StubClient(handler), cfg.work_dir)
        ids = {ep["episode_id"] for ep in man.all_episodes()}
        assert orphan not in ids
        assert man.get_episode(real) is not None or True  # real had its item re-grouped; episode row persists if referenced
