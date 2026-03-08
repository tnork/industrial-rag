"""
HuggingFace Spaces entrypoint for GE Connect Technical Assistant.

On cold start:
  1. Extract chunk_images.zip → chunk_images/ (if zip present and dir missing)
  2. Load vector store from vector_store/ (embeddings.npy + documents.json)
  3. Index chunk image map
  4. Expose Flask app as `application` for gunicorn

Run with:
  gunicorn --worker-class gthread --threads 4 --timeout 300 --bind 0.0.0.0:7860 app:application
"""

import zipfile
from pathlib import Path

# ── Auto-extract chunk images if bundled as zip ────────────────────────────
_zip = Path("chunk_images.zip")
_images_dir = Path("chunk_images")

if _zip.exists() and not _images_dir.exists():
    print(f"Extracting {_zip} ...", flush=True)
    with zipfile.ZipFile(_zip) as zf:
        zf.extractall(".")
    n = len(list(_images_dir.rglob("*.png")))
    print(f"✓ Extracted {n} chunk images", flush=True)

# ── Load RAG resources ─────────────────────────────────────────────────────
from parse_and_extract import build_vector_store, _build_chunk_image_map, make_flask_app

embedder, vectors, store = build_vector_store(force=False)
image_map = _build_chunk_image_map()
print(f"✓ {len(image_map)} chunk images indexed", flush=True)

# ── Expose Flask app for gunicorn ──────────────────────────────────────────
application = make_flask_app(embedder, vectors, store, image_map)
