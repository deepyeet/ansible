"""B27 naming evidence policy: a specific album name must
verify against the episode's exemplar images, or the
episode takes its generic fallback name. (The denylist
design was rejected by the user - places are not wrong in
themselves; unverified claims are the failure.)"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from album_sorter.config import Config
from album_sorter.gates import GATE_SYSTEM, VERIFY_SYSTEM, apply_gate
from album_sorter.gemini_client import StubClient
from album_sorter.manifest import Manifest
from album_sorter.naming import coerce_basis

GATE_OUT = {"registry_match": None, "match_confidence": 0.0,
            "notability": "notable", "notability_confidence": 0.9,
            "notability_reason": "a destination outing",
            "proposed_name": "Rattlesnake Lake",
            "name_basis": "famous_landmark",
            "generic_name": "Lake Outing"}


def _fixture(tmp: Path):
    cfg = Config(done_dir=tmp / "done", work_dir=tmp / "work",
                 sorted_dir=tmp / "sorted")
    man = Manifest(cfg.manifest_path)
    ep = man.create_episode("family outing to a lake with stumps", ["0" * 32])
    for i in range(6):
        man.upsert_item(md5=f"{i:032x}", done_name=f"p{i}.jpg", category="PHOTO",
                        status="grouped", episode_id=ep,
                        capture_time=f"2026-08-12T1{i}:00:00",
                        capture_time_raw=f"2026-08-12T1{i}:00:00",
                        analysis_json='{"description": "lake"}')
    man.update_episode(ep, span_start="2026-08-12T10:00:00",
                       span_end="2026-08-12T15:00:00", item_count=6)
    return cfg, man, ep


def _client(verify_out):
    def handler(model, system, parts):
        if system == VERIFY_SYSTEM:
            if isinstance(verify_out, Exception):
                raise verify_out
            return verify_out
        assert system == GATE_SYSTEM
        return dict(GATE_OUT)
    return StubClient(handler)


def test_unverified_specific_falls_back_to_generic():
    with tempfile.TemporaryDirectory() as td:
        cfg, man, ep = _fixture(Path(td))
        out = apply_gate(man, cfg, _client({"evidenced": False,
                                            "confidence": 0.9,
                                            "evidence": "generic shoreline"}),
                         [], ep)
        row = man.get_episode(ep)
        assert out == "album_new" and row["album_name"] == "2026 Lake Outing"
        assert row["name_basis"] == "generic"
        assert "not evidenced in photos" in row["notability_reason"]


def test_verified_specific_is_kept_with_basis():
    with tempfile.TemporaryDirectory() as td:
        cfg, man, ep = _fixture(Path(td))
        out = apply_gate(man, cfg, _client({"evidenced": True,
                                            "confidence": 0.95,
                                            "evidence": "sign reads Rattlesnake Lake"}),
                         [], ep)
        row = man.get_episode(ep)
        assert out == "album_new" and row["album_name"] == "2026 Rattlesnake Lake"
        assert row["name_basis"] == "famous_landmark"


def test_verifier_failure_is_safe_not_fatal():
    with tempfile.TemporaryDirectory() as td:
        cfg, man, ep = _fixture(Path(td))
        out = apply_gate(man, cfg, _client(RuntimeError("api down")), [], ep)
        row = man.get_episode(ep)
        assert out == "album_new" and row["album_name"] == "2026 Lake Outing"


def test_basis_coercion():
    assert coerce_basis("signage") == "signage"
    assert coerce_basis("FAMOUS_LANDMARK") == "famous_landmark"
    assert coerce_basis("vibes") == "generic"
    assert coerce_basis(None) == "generic"
