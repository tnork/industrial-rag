# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## LandingAI ADE Documentation Note

This repo uses the `landingai-ade` skill for LandingAI API knowledge. The skill was last synced on **2026-03-07**. When working in this repo, check whether the LandingAI ADE documentation has been updated since that date. If new docs are available, update the skill file at `~/.claude/skills/landingai-ade/` before proceeding with any ADE-related tasks.

---

## What This App Does

GE Connect Technical Assistant — a retrieval-augmented generation system for querying GE Connect Series HVAC product manuals. Parses Connect Series PDFs with LandingAI ADE into grounded image chunks, deduplicates chunks by text content, builds a local NumPy vector store, and serves a Flask web UI where Claude Opus 4.6 answers technical questions with source citations traced back to the original document page.

**Current corpus stats:** 7 documents, 1,478 unique chunks, 1,451 chunk images (deduplicated)

---

## Current Stack

| Layer | Technology | Notes |
|---|---|---|
| **Web framework** | Flask (unversioned) | Port 8080; SSE streaming; vanilla JS/CSS frontend |
| **Vector store** | NumPy + JSON (local files) | `embeddings.npy` + `documents.json` |
| **Embedding model** | `sentence-transformers/all-MiniLM-L6-v2` | 384-dim, local, free, no API key |
| **LLM** | Anthropic `claude-opus-4-6` | Streaming via `anthropic` SDK directly |
| **Document parsing** | LandingAI ADE (`dpt-2-latest`) | Requires `VISION_AGENT_API_KEY` |
| **PDF rendering** | PyMuPDF (`fitz`) | Renders pages for chunk image cropping |
| **Image cropping** | Pillow (`PIL`) | Saves PNG crops of each chunk bbox |
| **PDF downloader** | Playwright + requests | Headless Chromium for GE HVAC site scraping |
| **Python version** | 3.14 | No ChromaDB/LangChain (pydantic v1 incompatible) |

---

## Environment Variables Required

| Variable | Used By | How to Get |
|---|---|---|
| `ANTHROPIC_API_KEY` | `parse_and_extract.py` | console.anthropic.com |
| `VISION_AGENT_API_KEY` | `parse_and_extract.py` (ADE parsing only) | va.landing.ai/settings/api-key |

Store in a `.env` file in the project root (excluded from git via `.gitignore`).

---

## Scripts

| Script | Purpose |
|---|---|
| `download_ge_hvac_manuals.py` | Download GE Connect Series PDFs into `ge_hvac_manuals/connect_series/` |
| `parse_and_extract.py` | Batch-parse PDFs with ADE, crop chunk images, deduplicate, build/update the vector store, and serve the Flask web UI |

---

## Dependencies

```bash
# ADE pipeline
pip install landingai-ade pymupdf pillow

# RAG system + web UI
pip install anthropic sentence-transformers numpy python-dotenv flask

# PDF downloader only
pip install playwright requests && playwright install chromium
```

> **No requirements.txt exists** — all dependencies installed manually. Versions are unpinned.

> **Python 3.14 note:** ChromaDB and LangChain fail on Python 3.14 due to pydantic v1 incompatibility. The RAG system uses `numpy` + `anthropic` SDK directly instead.

---

## How to Run Locally

```bash
# Step 1: Download GE Connect Series PDFs
python3 download_ge_hvac_manuals.py

# Step 2: Parse all unprocessed PDFs with ADE and update the vector store
python3 parse_and_extract.py --parse --all

# Step 3: Rebuild vector store with deduplication (run after parsing new docs)
python3 parse_and_extract.py --rebuild

# Step 4: Launch the RAG web UI (default — opens http://localhost:8080)
python3 parse_and_extract.py

# Alternative parse modes:
python3 parse_and_extract.py --parse          # parse next 2 files (validation)
python3 parse_and_extract.py --parse --limit 10  # parse next N files

# Alternative run modes:
python3 parse_and_extract.py --web            # explicit web UI on http://localhost:8080
python3 parse_and_extract.py --terminal       # interactive terminal Q&A (iTerm2 inline images)
python3 parse_and_extract.py "your question"  # one-shot CLI answer and exit
```

> **macOS note:** Port 5000 is used by AirPlay Receiver. The web server runs on port **8080**.

---

## Architecture

### download_ge_hvac_manuals.py — PDF downloader

- **Web scraping** — Playwright headless browser scrapes GE Appliances HVAC product pages and collects PDF links
- **Scope** — Connect Series only: installation manuals, service manuals, spec sheets, submittal docs
- **Output** — `ge_hvac_manuals/connect_series/`

### parse_and_extract.py — ADE batch parser + RAG pipeline + web server

**Parsing (`--parse`):**
1. `_collect_pdfs()` walks `ge_hvac_manuals/connect_series/` and skips already-parsed files
2. PDFs >90 pages are automatically split into 90-page chunks and parsed separately
3. `client.parse(document=Path(...), model="dpt-2-latest")` → saves to `parse_results/connect_series/{stem}.txt`
4. `_crop_chunks()` renders each PDF page with PyMuPDF and crops chunk bounding boxes → `chunk_images/connect_series/{stem}/NNN_type_uuid.png`

**Vector store build (`--rebuild`):**
1. `_load_all_chunks()` reads all `parse_results/connect_series/*.txt` and extracts CHUNKS JSON
2. `SentenceTransformer("all-MiniLM-L6-v2")` encodes each chunk's markdown text (384-dim)
3. `_deduplicate_chunks()` removes chunks with identical text, keeping first occurrence; logs count
4. `_delete_orphaned_images()` deletes PNG files for removed duplicate chunk IDs
5. Saves deduplicated `embeddings.npy` + `documents.json` + `indexed_files.json`

**RAG pipeline:**
1. **Search** — cosine similarity in numpy over 384-dim embeddings
2. **Generate** — `anthropic.Anthropic().messages.stream()` with `claude-opus-4-6`; system prompt instructs the model to answer only from retrieved GE Connect manual context
3. **Web UI** — Flask on port 8080; `/ask?q=...` streams SSE events (`sources` → `token`... → `done`)

### Web UI — templates/index.html

Single-page chat interface with LandingAI + Claude branding:
- **Left panel** — streaming chat; responses appear token-by-token via Server-Sent Events
- **Right panel** — source chunks sidebar; chunk preview image, confidence bar, page number
- Clicking a chunk opens a full-screen lightbox with prev/next navigation
- Pre-built suggestion chips on the welcome screen (5 GE Connect questions in a 2-column grid)
- **Original Doc Viewer** — browse Connect Series PDFs; opens PDFs in-browser (Connect Series folder only)
- **Claude Explorer** button returns to welcome/home screen
- No JS framework — vanilla JS + CSS only

---

## Claude API Usage

- **Model:** `claude-opus-4-6`
- **Pattern:** streaming via `client.messages.stream()`
- **Max tokens:** 1024 per response
- **System prompt:** instructs Claude to answer only from retrieved GE Connect manual context
- **No LangChain** — uses `anthropic` SDK directly

---

## Key Paths

- Input PDFs: `ge_hvac_manuals/connect_series/`
- Parse results: `parse_results/connect_series/{stem}.txt`
- Chunk images: `chunk_images/connect_series/{stem}/NNN_type_uuid.png` *(excluded from git)*
- Vector store: `vector_store/` (`embeddings.npy`, `documents.json`, `indexed_files.json`)
- Web UI template: `templates/index.html`

**Connect Series documents (7 files):**
| File | Type |
|---|---|
| `62 - GE Connect Series Submittal 05-13-21.pdf` | Submittal / specs |
| `65 - GE_Connect Series Service Manual_Rev. 06-09-2021.pdf` | Service manual |
| `Connect-Installation-Manual-Outdoor.pdf` | Installation manual |
| `GE Connect Series Service Manual 2020902.pdf` | Service manual |
| `GE Connect Service Manual 03-12-2021.pdf` | Service manual |
| `GE Connect Spec Sheet 08-29-20.pdf` | Spec sheet |
| `GE_Connect_Series_Service_Manual_110422.pdf` | Service manual (latest, Nov 2022) |

---

## Deduplication

Multiple service manual versions share many identical pages. The pipeline deduplicates at two levels:

1. **Chunk-level (text)** — `_deduplicate_chunks()` removes chunks with identical text before saving the vector store. Runs automatically on every `--rebuild`.
2. **Image-level (content hash)** — pixel-identical PNG files across document subdirs can be removed by running the hash-dedup script manually (see previous session notes).

Result: 3,692 raw chunks → 1,478 unique chunks after dedup.

---

## Known Limitations & Tech Debt

- **No `requirements.txt`** — dependency versions are unpinned; breakage risk on fresh installs
- **chunk_images** — excluded from git; must be regenerated after fresh clone (`--parse --all` then `--rebuild`)
- **Vector store is flat files** — no indexing; full cosine scan per query (fast enough locally, won't scale)
- **Port hardcoded** — Flask runs on 8080 (not configurable without code change)
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
