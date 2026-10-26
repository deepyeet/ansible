#!/usr/bin/env python3
"""
Sample: Gemini Embedding 2 — text, image, and interleaved embeddings.

What this demonstrates (the pieces the photo pipeline actually needs):
  1. One model, one vector space: a text query is directly comparable
     (cosine similarity) to a photo. No separate image model.
  2. Interleaved input: [text, image] in ONE embed call returns ONE
     aggregated embedding, not two.
  3. Matryoshka truncation: the full vector is 3072-dim. Asking the API
     for 768 dims (or truncating yourself) costs almost nothing in
     similarity quality and 4x less storage. Truncated vectors are NOT
     pre-normalized by the API -- you must re-normalize them yourself.
  4. EXIF is NOT an input. If you want capture time / GPS in the
     algorithm, extract it locally and use it as structured data
     (time segmentation, haversine) -- do not embed it as text.
     This script prints EXIF-ish info via Pillow if present, purely
     as a demonstration of that local, separate channel.

Model: defaults to the GA model `gemini-embedding-2`.
       The preview (`gemini-embedding-2-preview`, 2026-03-10) is
       superseded; override with --model if you want to compare.
       Vectors are locked to their model's space: never mix vectors
       from different model IDs in one index.

Key: read from the environment (GEMINI_API_KEY or GOOGLE_API_KEY),
     or from a .env file next to this script / in the cwd.
     The key is never printed.

Usage:
  python3 embedding_sample.py                      # built-in demo
  python3 embedding_sample.py --image photo.jpg    # + your own photo
  python3 embedding_sample.py --dims 3072          # full-size vectors
  python3 embedding_sample.py --model gemini-embedding-2-preview
"""

import argparse
import math
import os
import sys
from pathlib import Path

DEFAULT_MODEL = "gemini-embedding-2"
DEFAULT_DIMS = 768


# ---------------------------------------------------------------- key / .env
def load_dotenv() -> None:
    """Minimal .env loader (no python-dotenv dependency).

    Looks next to this script, then in the cwd. Existing env vars win.
    """
    for candidate in (Path(__file__).with_name(".env"), Path.cwd() / ".env"):
        if not candidate.is_file():
            continue
        for line in candidate.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            name, value = name.strip(), value.strip().strip().strip("'\"")
            os.environ.setdefault(name.strip(), value)
        break


def get_api_key() -> str:
    load_dotenv()
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        sys.exit("No GEMINI_API_KEY / GOOGLE_API_KEY in env or .env — aborting.")
    return key


# ---------------------------------------------------------------- vector math
def normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec] if norm else vec


def cosine(a: list[float], b: list[float]) -> float:
    # Both vectors are unit-normalized by embed(), so this is a dot product.
    return sum(x * y for x, y in zip(a, b))


# ---------------------------------------------------------------- embedding
def embed(client, model: str, contents, dims: int) -> list[float]:
    """Embed ONE logical item and return a unit-normalized vector.

    `contents` may be:
      - a str                       -> one text embedding
      - a list of parts             -> ONE aggregated embedding for the
                                       whole list (e.g. [text, image])
    To get separate embeddings for separate items, call this once per
    item (or wrap each item in its own types.Content).
    """
    from google.genai import types

    result = client.models.embed_content(
        model=model,
        contents=contents,
        config=types.EmbedContentConfig(output_dimensionality=dims),
    )
    # F7 (no-re-spend audit): these calls are billed but
    # used to leave no trace in any ledger. Record the call
    # in the central usage ledger - the embed response
    # carries no usage block in this SDK, so tokens are
    # recorded as 0 and the model prices as unpriced: the
    # call is VISIBLE in `cli usage --central`, which is
    # the point; inventing token counts would be worse.
    try:
        from datetime import datetime, timezone
        from album_sorter.usage import append_central
        append_central({
            "ts": datetime.now(timezone.utc).isoformat(),
            "manifest": "embedding_sample", "stage": "embedding_sample",
            "subject": f"dims={dims}", "model": model,
            "tokens_in": 0, "tokens_out": 0, "thoughts": 0,
            "cached": 0, "total_tokens": 0, "outcome": "ok"})
    except Exception:
        pass    # a demo must not die on its own bookkeeping
    if len(result.embeddings) != 1:
        raise RuntimeError(f"expected 1 embedding, got {len(result.embeddings)}")
    # The 3072-dim output arrives pre-normalized; truncated sizes do not.
    # Normalizing unconditionally is harmless and keeps cosine == dot.
    return normalize(result.embeddings[0].values)


def image_part(path: Path):
    from google.genai import types

    mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png", ".webp": "image/webp"}.get(path.suffix.lower())
    if not mime:
        raise ValueError(f"unsupported image type: {path.suffix}")
    return types.Part.from_bytes(data=path.read_bytes(), mime_type=mime)


def print_exif_locally(path: Path) -> None:
    """EXIF lives in a separate, local, structured channel — demo only."""
    try:
        from PIL import Image
    except ImportError:
        print(f"  (Pillow not installed — skipping local EXIF read of {path.name})")
        return
    try:
        with Image.open(path) as im:
            exif = im.getexif()
        if not exif:
            print(f"  EXIF {path.name}: none present")
            return
        interesting = {k: exif.get(k) for k in (271, 272, 306, 36867) if exif.get(k)}
        print(f"  EXIF {path.name} (local only, NOT sent as an embedding input): "
              f"Make/Model/DateTime/DateTimeOriginal = {interesting or 'n/a'}")
    except Exception as exc:  # demo helper; never fail the embedding run on EXIF
        print(f"  EXIF {path.name}: unreadable ({exc})")


# ---------------------------------------------------------------- demo
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--model", default=os.environ.get("EMBED_MODEL", DEFAULT_MODEL))
    ap.add_argument("--dims", type=int, default=DEFAULT_DIMS,
                    help="output dimensionality (128..3072, default 768)")
    ap.add_argument("--image", type=Path, action="append", default=[],
                    help="extra image file to embed (repeatable)")
    args = ap.parse_args()

    from google import genai

    client = genai.Client(api_key=get_api_key())
    print(f"model={args.model}  dims={args.dims}  (key loaded, not shown)")

    here = Path(__file__).parent
    picnic = here / "picnic.jpg"
    engine = here / "engine.jpg"

    items: list[tuple[str, object]] = [
        ("text: picnic (query)", "a family picnic in a park, kids eating ice cream"),
        ("text: picnic (paraphrase)", "children eating ice cream at an outdoor family gathering"),
        ("text: engine bay", "a car engine bay being repaired with a wrench"),
    ]
    if picnic.is_file():
        items.append((f"image: {picnic.name}", [image_part(picnic)]))
    if engine.is_file():
        items.append((f"image: {engine.name}", [image_part(engine)]))
    for extra in args.image:
        items.append((f"image: {extra.name}", [image_part(extra)]))
        print_exif_locally(extra)
    if picnic.is_file():
        print_exif_locally(picnic)
    if engine.is_file():
        print_exif_locally(engine)

    # Interleaved demo: caption + photo in one call = one fused vector.
    if picnic.is_file():
        items.append(("interleaved: caption + picnic.jpg",
                      ["kids eating ice cream outdoors", image_part(picnic)]))

    vectors = {}
    for label, contents in items:
        vec = embed(client, args.model, contents, args.dims)
        vectors[label] = vec
        print(f"embedded  {label:42s} len={len(vec)}  |v|={math.sqrt(sum(x*x for x in vec)):.4f}  "
              f"first3={[round(x, 4) for x in vec[:3]]}")

    labels = list(vectors)
    print("\ncosine similarity (rows vs cols):")
    print(" " * 44 + "".join(f"{i:>7d}" for i in range(len(labels))))
    for i, a in enumerate(labels):
        row = "".join(f"{cosine(vectors[a], vectors[b]):7.3f}" for b in labels)
        print(f"[{i}] {a:40s} {row}")

    print("\nRead it as: paraphrase ~ query should be highest text pair;")
    print("the picnic *photo* should be closer to the picnic texts than to")
    print("the engine text, and vice versa — same space, no image model glue.")


if __name__ == "__main__":
    main()
