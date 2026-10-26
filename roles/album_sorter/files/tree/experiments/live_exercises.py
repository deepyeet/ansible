"""Live exercises B6 + B7 (container only, real API, ~$0.02).

B6: registry-match path - reset the fused Vegas episode to
pending in a COPY of the rollup manifest, hand the gate the
real harvested registry plus one fixture entry (as if the
family already had a "2026 Las Vegas" album), and see whether
the live model matches it, and whether placement would target
that folder.

B7: linkage backstop - detach 3 items from the Arte episode
in a COPY of the flash manifest and run linkage.try_link
with the real client; count correct reattachments. (Current
code: the verifier sees the item image + exemplar MD5
strings as text - backlog B22 tracks the missing images.)
"""
from __future__ import annotations

import csv
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from album_sorter import gates, linkage
from album_sorter.config import Config
from album_sorter.gemini_client import make_client
from album_sorter.manifest import Manifest

BASE = Path(__file__).resolve().parent


def b6() -> None:
    src = BASE / "cal_work_flash_r"
    work = BASE / "gate_work_b6"
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(src, work)
    cfg = Config(done_dir=BASE / "cal_corpus", work_dir=work,
                 sorted_dir=BASE / "gate_sorted_b6")
    man = Manifest(cfg.manifest_path)
    target = None
    for ep in man.all_episodes():
        if ep["album_name"] == "2026 Las Vegas" and ep["item_count"] >= 100:
            target = ep
            break
    assert target, "fused Vegas episode not found"
    man.update_episode(target["episode_id"], outcome="pending", album_name=None)
    registry = []
    with open(BASE / "reference" / "album-registry-photos.csv", encoding="utf-8") as f:
        for row in csv.reader(f, delimiter="|"):
            if row:
                registry.append({"name": row[0]})
    registry.append({"name": "2026 Las Vegas"})   # the fixture: album already exists
    client = make_client(cfg.assets_dir)
    out = gates.apply_gate(man, cfg, client, registry, target["episode_id"])
    ep = man.get_episode(target["episode_id"])
    print(f"B6 outcome: {out} album_name={ep['album_name']!r} items={ep['item_count']}")
    print(f"B6 usage: {client.usage_summary()}")


def b7() -> None:
    src = BASE / "cal_work_flash"
    work = BASE / "link_work_b7"
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(src, work)
    cfg = Config(done_dir=BASE / "cal_corpus", work_dir=work,
                 sorted_dir=BASE / "link_sorted_b7")
    man = Manifest(cfg.manifest_path)
    big = max((ep for ep in man.all_episodes() if ep["outcome"] != "merged"),
              key=lambda e: e["item_count"])
    rows = man.query("SELECT * FROM items WHERE episode_id = ? LIMIT 3",
                     (big["episode_id"],))
    print(f"B7 detaching {len(rows)} items from episode {big['episode_id']} "
          f"({big['item_count']} items, {big['description'][:60]})")
    client = make_client(cfg.assets_dir)
    hits = 0
    for r in rows:
        man.update_item(r["md5"], episode_id=None, status="analyzed")
    man.recompute_episode_span(big["episode_id"])
    for r in rows:
        item = man.get_by_md5(r["md5"])
        ok = linkage.try_link(cfg, man, client, item)
        now = man.get_by_md5(r["md5"])
        correct = ok and now["episode_id"] == big["episode_id"]
        hits += int(correct)
        print(f"  {r['done_name']}: linked={ok} back_to_original={correct} "
              f"episode_now={now['episode_id']}")
    print(f"B7 result: {hits}/{len(rows)} reattached to the correct episode")
    print(f"B7 usage: {client.usage_summary()}")


if __name__ == "__main__":
    b6()
    b7()
