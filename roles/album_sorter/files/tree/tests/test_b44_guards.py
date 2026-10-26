"""B44: two payload/selection guards.

Item A - grouping visible_text dedupe: a >= 40-char
visible_text repeated within one window (v2 labels quote
the same event banner verbatim on every photo) renders
verbatim on the first record only; later repeats become
"[same text as <first file>]". Targets the RECITATION
empty-response failure observed live 2026-10-23.

Item B - S5a: name_verify exemplar selection prefers
members whose phase-1 label carries visible_text (the
only ones that can hold signage evidence), time order
preserved within tiers, cap 4.
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from album_sorter.config import Config
# B46: the verify-selection policy moved to exemplars.py
# (the one home for exemplar selection); these tests now
# pin the policy at its new home, behavior unchanged.
from album_sorter.exemplars import verify_exemplars as _verify_exemplars
from album_sorter.gates import verify_specific_name
from album_sorter.gemini_client import StubClient
from album_sorter.manifest import Manifest
from album_sorter.windows import dedupe_window_visible_text, item_record

BANNER = ("SPARTAN, 1ST, ESSENTIA, ATHLETIC BREWING CO, GOVX, "
          "GIVESTAR, PROTEIN PUCK")  # 68 chars normalized


def _row(name, text, i=0):
    return {"done_name": name,
            "capture_time": f"2026-07-15T12:{i:02d}:00",
            "device": "Pixel", "gps_lat": None, "gps_lon": None,
            "analysis_json": json.dumps(
                {"description": "race photo", "visible_text": text})}


def _records(*specs):
    return [item_record(_row(n, t, i), i)
            for i, (n, t) in enumerate(specs)]


def _texts(records):
    return [r["analysis"]["visible_text"] for r in records]


# ---- Item A: window visible_text dedupe -------------------

def test_repeated_long_text_verbatim_once_then_markers():
    recs = _records(("a.jpg", BANNER), ("b.jpg", BANNER),
                    ("c.jpg", BANNER))
    out = dedupe_window_visible_text(recs)
    assert _texts(out) == [BANNER, "[same text as a.jpg]",
                           "[same text as a.jpg]"]


def test_whitespace_variants_count_as_the_same_text():
    messy = "  SPARTAN,  1ST,\nESSENTIA, ATHLETIC BREWING CO, GOVX, " \
            "GIVESTAR, PROTEIN PUCK "
    recs = _records(("a.jpg", BANNER), ("b.jpg", messy))
    out = dedupe_window_visible_text(recs)
    # first record keeps its own verbatim string untouched
    assert _texts(out) == [BANNER, "[same text as a.jpg]"]


def test_short_repeats_untouched():
    recs = _records(("a.jpg", "EXIT"), ("b.jpg", "EXIT"),
                    ("c.jpg", "EXIT"))
    assert _texts(dedupe_window_visible_text(recs)) == ["EXIT"] * 3


def test_nonrepeated_long_text_untouched():
    other = "HAPPY BIRTHDAY HENRY - A COMPLETELY DIFFERENT LONG SIGN"
    recs = _records(("a.jpg", BANNER), ("b.jpg", other))
    assert _texts(dedupe_window_visible_text(recs)) == [BANNER, other]


def test_repeat_threshold_is_40_normalized_chars():
    t40 = "x" * 40
    recs = _records(("a.jpg", t40), ("b.jpg", t40))
    assert _texts(dedupe_window_visible_text(recs))[1] == \
        "[same text as a.jpg]"
    t39 = "x" * 39
    recs = _records(("a.jpg", t39), ("b.jpg", t39))
    assert _texts(dedupe_window_visible_text(recs)) == [t39, t39]


def test_grouping_fingerprint_reacts_to_payload_version():
    import album_sorter.grouping as grouping
    with tempfile.TemporaryDirectory() as td:
        man = Manifest(Path(td) / "m.sqlite3")
        man.upsert_item(md5="x1", done_name="f.jpg",
                        capture_time="2026-07-15T12:00:00",
                        capture_time_raw="2026-07-15T12:00:00",
                        status="analyzed", category="PHOTO")
        assert grouping.PAYLOAD_VERSION == 5
        f5 = grouping.stream_fingerprint(Config(), man)
        try:
            grouping.PAYLOAD_VERSION = 4
            f4 = grouping.stream_fingerprint(Config(), man)
        finally:
            grouping.PAYLOAD_VERSION = 5
        assert f5 != f4


# ---- Item B: verify exemplar selection --------------------

def _item(md5, text, minute):
    a = {"description": "photo"}
    if text is not None:
        a["visible_text"] = text
    return {"md5": md5, "capture_time": f"2026-07-15T12:{minute:02d}:00",
            "analysis_json": json.dumps(a)}


def test_verify_exemplars_prefers_text_bearing_time_order_in_tiers():
    items = [_item("m1", None, 1), _item("m2", "", 2),
             _item("m3", "SIGN A", 3), _item("m4", None, 4),
             _item("m5", "SIGN B", 5), _item("m6", None, 6)]
    chosen = _verify_exemplars(items)
    # text tier by time (m3, m5), then the rest by time, cap 4
    assert [i["md5"] for i in chosen] == ["m3", "m5", "m1", "m2"]


def test_verify_exemplars_all_empty_text_plain_time_order():
    items = [_item(f"m{i}", None, i) for i in range(1, 7)]
    chosen = _verify_exemplars(items)
    assert [i["md5"] for i in chosen] == ["m1", "m2", "m3", "m4"]


def test_verify_exemplars_whitespace_only_counts_as_no_text():
    items = [_item("m1", "   \n ", 1), _item("m2", "REAL SIGN", 2)]
    chosen = _verify_exemplars(items)
    assert [i["md5"] for i in chosen] == ["m2", "m1"]


def test_verify_exemplars_cap_respected_with_many_text_bearing():
    items = [_item(f"m{i}", f"SIGN {i}", i) for i in range(1, 7)]
    chosen = _verify_exemplars(items)
    assert [i["md5"] for i in chosen] == ["m1", "m2", "m3", "m4"]


def test_verify_call_sends_text_bearing_members_first():
    with tempfile.TemporaryDirectory() as td:
        man = Manifest(Path(td) / "m.sqlite3")
        eid = man.create_episode("race day", [])
        specs = [("m1", None, 1), ("m2", None, 2),
                 ("m3", "SPARTAN SIGN", 3), ("m4", None, 4),
                 ("m5", "FINISH BANNER", 5), ("m6", None, 6)]
        for md5, text, minute in specs:
            a = {"description": "photo"}
            if text is not None:
                a["visible_text"] = text
            man.upsert_item(
                md5=md5, done_name=f"{md5}.jpg",
                capture_time=f"2026-07-15T12:{minute:02d}:00",
                capture_time_raw=f"2026-07-15T12:{minute:02d}:00",
                episode_id=eid, status="grouped", category="PHOTO",
                analysis_json=json.dumps(a))
        items = man.query(
            "SELECT * FROM items WHERE episode_id = ? "
            "ORDER BY capture_time, md5", (eid,))
        captured = {}

        def handler(model, system, parts):
            captured["parts"] = parts
            return {"evidenced": True, "confidence": 0.95,
                    "evidence": "SPARTAN SIGN"}

        ok, _ev, _inc = verify_specific_name(
            Config(), man, StubClient(handler), man.get_episode(eid),
            items, "2026 Spartan Race")
        assert ok
        sent = [p["image_md5"] for p in captured["parts"][1:]]
        assert sent == ["m3", "m5", "m1", "m2"]
