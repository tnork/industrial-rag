---
title: GE Connect Technical Assistant
emoji: 🔧
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
license: mit
short_description: Visually-grounded RAG for GE Connect manuals
---

# GE Connect Technical Assistant

A retrieval-augmented generation (RAG) system for querying GE Connect Series product manuals, with visual grounding using bounding boxes, image chunks, and confidence scoring.

## What it does

- Answers technical questions about GE Connect Series heat pumps and related equipment
- Retrieves relevant context from 7 GE Connect Series PDFs (service manuals, installation guides, spec sheets, submittal docs) spanning Sep 2020 – Nov 2022
- Streams answers via Claude Opus 4.6 with source citations and chunk preview images
- Includes an Original Doc Viewer for browsing source PDFs (opens in new tab)

## Corpus

| Document | Type |
|---|---|
| GE Connect Series Service Manual (Nov 2022) | Service manual (latest) |
| GE Connect Series Service Manual (Jun 2021) | Service manual |
| GE Connect Series Service Manual (Mar 2021) | Service manual |
| GE Connect Series Service Manual (Sep 2020) | Service manual |
| Connect Series Installation Manual (Outdoor) | Installation manual |
| GE Connect Series Submittal (May 2021) | Submittal / specs |
| GE Connect Spec Sheet (Aug 2020) | Spec sheet |

**1,478 unique chunks** after deduplication across overlapping manual versions.

## Stack

| Layer | Technology |
|---|---|
| LLM | Anthropic `claude-opus-4-6` (streaming) |
| Embedding | `sentence-transformers/all-MiniLM-L6-v2` (384-dim, local) |
| Vector store | NumPy flat file (cosine similarity) |
| Document parsing | LandingAI ADE `dpt-2-latest` (offline, pre-built) |
| Web framework | Flask + SSE streaming |

## Environment Variables

Set `ANTHROPIC_API_KEY` as a Space secret in the HuggingFace Space settings.

## Local Development

```bash
# Install dependencies
pip install anthropic sentence-transformers numpy flask gunicorn python-dotenv pymupdf Pillow

# Download GE Connect Series PDFs
python web_app.py --download

# Parse PDFs with LandingAI ADE (requires VISION_AGENT_API_KEY)
python web_app.py --parse --all

# Build vector store
python web_app.py --rebuild

# Run locally (port 8080)
python web_app.py

# Or use the HF Spaces entrypoint (port 7860, matches Dockerfile CMD)
gunicorn --worker-class gthread --threads 4 --timeout 300 --bind 0.0.0.0:7860 app:application
```
