#!/usr/bin/env python3
"""Diagnostic probe (experiment tooling): WHY does
grouping window 0 come back empty?

Runs validate_grouping.run_arm with GenAIClient.
generate_json monkeypatched (this process only) so the
FIRST grouping call is issued through the raw SDK with
the client's own payload conversion, printing the
response's prompt_feedback, per-candidate finish_reason
and safety ratings, and usage - the metadata the
production client discards when it raises EmptyResponse.
Then aborts before any further call.

The probe call bypasses the recording client, so it
does NOT land in the usage ledger; its cost must be
accounted from the printed usage_metadata by hand
(36.7K input tokens at Flash standard ~ $0.028, plus
any output actually generated).

Usage (container):
  ~/album-venv/bin/python exp_groupby_probe.py \
      <backup> <exp-dir>
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import validate_grouping as G
from album_sorter import gemini_client as gc


def probe(self, model, system, parts, schema_hint="",
          max_output_tokens=8192, thinking_level=None,
          service_tier=None):
    from google.genai import types
    res_values = {p["media_resolution"] for p in parts
                  if ("image_md5" in p or "image_path" in p)
                  and p.get("media_resolution")}
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
            thinking_level=types.ThinkingLevel[
                thinking_level.upper()])
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
    print("PROMPT_FEEDBACK:", resp.prompt_feedback)
    for i, cand in enumerate(resp.candidates or []):
        print(f"CANDIDATE {i}: finish_reason="
              f"{cand.finish_reason} finish_message="
              f"{getattr(cand, 'finish_message', None)}")
        print("  safety_ratings:",
              [(str(r.category), str(r.probability))
               for r in (cand.safety_ratings or [])])
    print("USAGE:", resp.usage_metadata)
    print("TEXT_PRESENT:", bool(resp.text),
          (resp.text or "")[:200])
    raise SystemExit(0)


def main() -> int:
    backup = Path(sys.argv[1])
    exp_dir = Path(sys.argv[2])
    exp_dir.mkdir(parents=True, exist_ok=True)
    ref_src = Path("exp/groupby-reference.json")
    ref_dst = exp_dir / "groupby-reference.json"
    if not ref_dst.exists():
        shutil.copy(ref_src, ref_dst)
    ref = json.loads(ref_dst.read_text())
    gc.GenAIClient.generate_json = probe
    G.run_arm("control", backup, exp_dir, ref, "standard",
              False)
    print("probe: run_arm returned without a grouping "
          "call - unexpected")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
