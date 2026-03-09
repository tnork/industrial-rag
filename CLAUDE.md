# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## LandingAI ADE Documentation Note

The `landingai-ade` skill is installed globally at `~/.claude/skills/landingai-ade/` (last synced **2026-03-07**). When working on ADE-related tasks in this repo, check whether the LandingAI ADE documentation has been updated since that date and update the global skill if needed.

---

## What This App Does

GE Connect Technical Assistant — a retrieval-augmented generation system for querying GE Connect Series HVAC product manuals. Parses Connect Series PDFs with LandingAI ADE into grounded image chunks, deduplicates chunks by text content, builds a dual-encoder NumPy vector store, and serves a Flask web UI where Claude Opus 4.6 answers technical questions with source citations and visual chunk previews.

**Current corpus stats:** 10 documents, 2,029 unique chunks (1,532 figure/logo CLIP + 497 table/text MiniLM after dedup), 4,096 chunk images

---

## Current Stack

| Layer | Technology | Notes |
|---|---|---|
| **Web framework** | Flask (unversioned) | Port 8080 local / 7860 HF; SSE streaming; vanilla JS/CSS frontend |
| **Vector store** | NumPy + JSON (local files) | `embeddings.npy` (512-dim CLIP) + `text_embeddings.npy` (384-dim MiniLM) + `documents.json` |
| **Image embedding** | `sentence-transformers/clip-ViT-B-32` | 512-dim; figure/logo chunks encoded by visual content via CLIP image encoder |
| **Text embedding** | `sentence-transformers/all-MiniLM-L6-v2` | 384-dim; table chunks + text-only chunks encoded by MiniLM using ADE-extracted text |
| **Retrieval Stage 1** | Reciprocal Rank Fusion (RRF, k=60) | Merges CLIP + MiniLM ranked lists by rank position; top 20 candidates forwarded to Stage 2 |
| **Retrieval Stage 2** | `cross-encoder/ms-marco-MiniLM-L6-v2` | Reranks top 20 by scoring (query, chunk_text) jointly; ADE confidence = 15% soft boost; returns top 5 |
| **LLM** | Anthropic `claude-opus-4-6` | Streaming + adaptive vision (0–2 images per request) via `anthropic` SDK directly |
| **Document parsing** | LandingAI ADE (`dpt-2-latest`) | Requires `VISION_AGENT_API_KEY` |
| **PDF rendering** | PyMuPDF (`fitz`) | Renders pages for chunk image cropping |
| **Image cropping** | Pillow (`PIL`) | Saves PNG crops of each chunk bbox |
| **PDF downloader** | `requests` + BeautifulSoup | HTTP download of known GE CDN URLs; directory crawl |
| **Python version** | 3.14 | No ChromaDB/LangChain (pydantic v1 incompatible) |

---

## Environment Variables Required

| Variable | Used By | How to Get |
|---|---|---|
| `ANTHROPIC_API_KEY` | `web_app.py` (RAG + web UI) | console.anthropic.com |
| `VISION_AGENT_API_KEY` | `web_app.py` (ADE parsing only) | va.landing.ai/settings/api-key |

Store in a `.env` file in the project root (excluded from git via `.gitignore`).

---

## Scripts

| Script | Purpose |
|---|---|
| `web_app.py` | Unified script: download PDFs, parse with ADE, build vector store, serve Flask web UI |
| `app.py` | HuggingFace Spaces gunicorn entrypoint — imports from `web_app.py`, not run directly |

---

## Dependencies

```bash
# RAG system + web UI (required for all modes)
# sentence-transformers[clip] adds torch + open_clip_torch for clip-ViT-B-32;
# all-MiniLM-L6-v2 is included in the base sentence-transformers package
pip install anthropic "sentence-transformers[clip]" numpy python-dotenv flask gunicorn pymupdf Pillow

# PDF downloader only (--download mode)
pip install requests beautifulsoup4

# ADE parsing only (--parse mode)
pip install landingai-ade
```

> **Pinned versions** — `requirements.txt` is pinned to versions confirmed working Mar 2026. Use `pip install -r requirements.txt` for a reproducible install.

> **Python 3.14 note:** ChromaDB and LangChain fail on Python 3.14 due to pydantic v1 incompatibility. The RAG system uses `numpy` + `anthropic` SDK directly instead.

---

## How to Run Locally

```bash
# Step 1: Download GE Connect Series PDFs
python3 web_app.py --download

# Step 2: Parse all unprocessed PDFs with ADE and update the vector store
python3 web_app.py --parse --all

# Step 3: Rebuild vector store with deduplication (run after parsing new docs)
python3 web_app.py --rebuild

# Step 4: Launch the RAG web UI (default — opens http://localhost:8080)
python3 web_app.py

# Alternative parse modes:
python3 web_app.py --parse           # parse next 2 files (validation)
python3 web_app.py --parse --limit 10  # parse next N files

# Alternative run modes:
python3 web_app.py --web             # explicit web UI on http://localhost:8080
python3 web_app.py --terminal        # interactive terminal Q&A (iTerm2 inline images)
python3 web_app.py "your question"   # one-shot CLI answer and exit
```

> **macOS note:** Port 5000 is used by AirPlay Receiver. The web server runs on port **8080** locally (or `PORT` env var).

---

## Architecture

### web_app.py — unified application

All functionality lives in one file with 7 sections:

**Section 1 — PDF Downloader (`--download`):**
- Downloads known GE Connect Series PDFs from GEA/Haier CDN URLs via `requests`
- Crawls GEA directory listings with BeautifulSoup to find additional PDFs
- Output: `ge_hvac_manuals/connect_series/` and sibling folders

**Section 2 — ADE Batch Parsing (`--parse`):**
1. `_collect_pdfs()` walks `ge_hvac_manuals/connect_series/` and skips already-parsed files
2. PDFs >90 pages are automatically split into 90-page chunks and parsed separately
3. `client.parse(document=Path(...), model="dpt-2-latest")` → saves to `parse_results/connect_series/{stem}.txt`
4. `_crop_chunks()` renders each PDF page with PyMuPDF and crops chunk bounding boxes → `chunk_images/connect_series/{stem}/NNN_type_uuid.png`

**Section 3 — Vector store build (`--rebuild`) — dual-encoder:**
1. `_load_all_chunks()` reads all `parse_results/connect_series/*.txt` and extracts CHUNKS JSON
2. `_deduplicate_chunks()` removes chunks with identical text *before* encoding; logs count
3. `_delete_orphaned_images()` deletes PNG files for removed duplicate chunk IDs
4. Chunks are separated into two groups based on whether a matching chunk image exists:
   - **Image chunks** → encoded with CLIP image encoder (512-dim) via `SentenceTransformer("clip-ViT-B-32")`; captures visual content of figures, wiring diagrams, tables
   - **Text-only chunks** → encoded with MiniLM (384-dim) via `SentenceTransformer("all-MiniLM-L6-v2")`; optimized for dense semantic passage retrieval
5. Saves `embeddings.npy` (CLIP, M×512) + `text_embeddings.npy` (MiniLM, N×384) + `img_indices.json` + `txt_indices.json` + `documents.json` + `indexed_files.json`

**Sections 4–6 — RAG pipeline + web server:**
1. **Search** — `similarity_search(vs, question)` uses Reciprocal Rank Fusion (RRF, k=60) to merge results from both encoders:
   - CLIP text encoder → ranked image results
   - MiniLM → ranked text results
   - RRF merges by rank rather than raw score, giving each encoder equal weight (raw cosine scores are incomparable: MiniLM text-to-text is typically 0.4–0.8 while CLIP text-to-image is 0.1–0.35). Top 20 forwarded to Stage 2.
   - Cross-encoder reranks top 20; ADE confidence = 15% soft boost. Returns top 5.
   - **UI relevance % badge** = `rerank_score / max(rerank_scores)` — normalized to batch max so top chunk = 100%. This is the honest relevance signal. Raw cosine score is NOT shown (it's incomparable across encoders and would be misleading). Cosine score is kept only for `CLAUDE_IMG_MIN_SIM` image-selection threshold.
2. **Generate** — `anthropic.Anthropic().messages.stream()` with `claude-opus-4-6`; **0–2** images per request: images included for hits with cosine similarity ≥ `CLAUDE_IMG_MIN_SIM` (0.20) and a matching PNG, up to `MAX_CLAUDE_IMGS=2`.
3. **Web UI** — Flask on port 8080; `/ask?q=...` streams SSE events (`sources` → `token`... → `done`); image chunk cards show preview + snippet and are clickable (lightbox); text-only cards are non-clickable (`cursor: default`, no hover lift)

### app.py — HuggingFace Spaces entrypoint

Thin boot script for gunicorn:
1. Extracts `chunk_images.zip` on cold start (if dir missing)
2. Imports `build_vector_store`, `_build_chunk_image_map`, `_build_page_index`, `make_flask_app` from `web_app`
3. Exposes `application` for gunicorn

### Web UI — templates/index.html

Single-page chat interface with LandingAI + Claude branding:
- **Left panel** — streaming chat; responses appear token-by-token via Server-Sent Events
- **Right panel** — source chunks sidebar; each card shows: chunk image (if available) + text snippet + doc name + page number + % match confidence bar
- Clicking a chunk opens a full-screen lightbox with prev/next navigation and frosted doc name pill
- Pre-built suggestion chips on the welcome screen (4 GE Connect questions in a 2-column grid)
- **Original Doc Viewer** — browse Connect Series PDFs; opens PDFs in a new tab
- **Claude Explorer** button returns to welcome/home screen
- No JS framework — vanilla JS + CSS only

---

## Claude API Usage

- **Model:** `claude-opus-4-6`
- **Pattern:** streaming via `client.messages.stream()`
- **Max tokens:** 1024 per response
- **Vision:** 0 or 1 image per request (adaptive). Sent only when `hits[0]` is an image chunk with similarity ≥ `CLAUDE_IMG_MIN_SIM` (0.20). Base64 JPEG, max 768px. Visual queries get 1 image; text/lookup queries get 0.
- **System prompt:** instructs Claude to answer only from retrieved GE Connect manual context and images
- **No LangChain** — uses `anthropic` SDK directly

---

## Key Paths

- Input PDFs: `ge_hvac_manuals/connect_series/`
- Parse results: `parse_results/connect_series/{stem}.txt`
- Chunk images: `chunk_images/connect_series/{stem}/NNN_type_uuid.png` *(excluded from git)*
- Vector store: `vector_store/` (`embeddings.npy`, `text_embeddings.npy`, `img_indices.json`, `txt_indices.json`, `documents.json`, `indexed_files.json`)
- Web UI template: `templates/index.html`

**Connect Series documents (7 files):**
| File | Type |
|---|---|
| `62 - GE Connect Series Submittal 05-13-21.pdf` | Submittal / specs |
| `65 - GE_Connect Series Service Manual_Rev. 06-09-2021.pdf` | Service manual |
| `61 - GE_Connect-High static AHU Installation Manual_04302021.pdf` | Installation manual (indoor, high-static AHU) |
| `GE Connect Installation Manual Indoor AHU 2021.pdf` | Installation manual (indoor, standard AHU) |
| `GE_Connect_Specification_Guide_032822.pdf` | Specification guide (Mar 2022) |
| `Connect-Installation-Manual-Outdoor.pdf` | Installation manual (outdoor) |
| `GE Connect Series Service Manual 2020902.pdf` | Service manual |
| `GE Connect Service Manual 03-12-2021.pdf` | Service manual |
| `GE Connect Spec Sheet 08-29-20.pdf` | Spec sheet |
| `GE_Connect_Series_Service_Manual_110422.pdf` | Service manual (latest, Nov 2022) |

---

## Deduplication

Multiple service manual versions share many identical pages. The pipeline deduplicates at two levels:

1. **Chunk-level (text)** — `_deduplicate_chunks()` removes chunks with identical text before saving the vector store. Runs automatically on every `--rebuild`.
2. **Image-level (content hash)** — pixel-identical PNG files across document subdirs can be removed by running the hash-dedup script manually (see previous session notes).

Result: 5,181 raw chunks → 2,241 unique chunks after dedup (2,048 image-embedded + 193 text-only). Deduplication now runs before encoding in `build_vector_store` to avoid wasting time embedding duplicates.

---

## Known Limitations & Tech Debt

- **Pinned requirements** — all dependencies pinned to versions confirmed working Mar 2026; re-evaluate on upgrades
- **chunk_images** — excluded from git; must be regenerated after fresh clone (`--parse --all` then `--rebuild`)
- **Vector store is flat files** — no indexing; full cosine scan per query (fast enough locally, won't scale)
- **Max tokens capped at 1024** — long answers may be truncated
- **ADE parsing is slow** — ~2–5 min per large PDF; large manuals are auto-split at 90 pages
- **No authentication** — web UI is open with no login/API key protection

---

## ADE Models

| Model | Use |
|---|---|
| `dpt-2-latest` | Default — complex layouts, scanned docs, forms, handwriting |
| `dpt-2-mini` | Simple digital-native PDFs only, no visual elements |
| `extract-latest` | Always use for extraction |

For PDFs >100 pages, switch `client.parse()` to `client.parse_jobs.create()` (async).
