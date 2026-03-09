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

A multimodal RAG assistant for GE Connect Series product manuals. Ask a question in plain English — get a precise, sourced answer with the exact page region it came from.

- **The problem** — Technical docs (installation manuals, service guides, wiring schematics, spec sheets) are dense, visually complex, and spread across multiple revision versions. Finding a spec or error code means page-flipping through hundreds of pages, being limited to keyword search, or LLM knowledge without grounding for accuracy.
- **The solution** — LandingAI ADE API ouput for markdown, JSON bounding box locations, and confidence scores + dual-encoder retrieval for text and image + Claude vision. Surfaces the right information, reads diagrams the way a human would, finds the right information, and cites the exact source. No hallucinations, no missing context.
- **The broader pattern** — the same architecture applies anywhere high-stakes decisions depend on visually complex documents: financial services, healthcare, manufacturing, logistics. In those domains, text-only RAG isn't just incomplete — stripping the visual layer from a wiring diagram or dosage table can produce confidently wrong answers.

## What it does

- Answers technical questions about GE Connect Series heat pumps and related equipment
- Retrieves relevant context from 10 GE Connect Series PDFs (service manuals, installation manuals, spec sheets, specification guide, submittal docs) spanning Aug 2020 – Nov 2022
- **Parsed with LandingAI Agentic Document Extraction (ADE)** — extracts figures, tables, and text blocks accurately with precise bounding boxes and confidence scores, enabling the RAG system to retrieve the exact page region that answers a question
- **Dual-encoder retrieval** — image chunks embedded with `clip-ViT-B-32` (visual content) and text-only chunks embedded with `all-MiniLM-L6-v2` (semantic search); both ranked lists merged per query using Reciprocal Rank Fusion (RRF) so neither encoder dominates
- **Adaptive vision** — sends 0–2 images to Claude per request: images are included only when top-ranked results are image chunks that meet a minimum similarity threshold; purely textual queries incur no vision token cost
- Streams answers via Claude Opus 4.6 with source citations; each source card shows the chunk image and a text snippet
- Includes an Original Doc Viewer for browsing source PDFs (opens in new tab)

## Corpus

| Document | Type |
|---|---|
| GE Connect Series Service Manual (Nov 2022) | Service manual (latest) |
| GE Connect Series Service Manual (Jun 2021) | Service manual |
| GE Connect Series Service Manual (Mar 2021) | Service manual |
| GE Connect Series Service Manual (Sep 2020) | Service manual |
| Connect Series Installation Manual (Outdoor) | Installation manual |
| Connect Series Installation Manual (Indoor AHU — High Static, Apr 2021) | Installation manual |
| Connect Series Installation Manual (Indoor AHU — Standard, 2021) | Installation manual |
| GE Connect Specification Guide (Mar 2022) | Specification guide |
| GE Connect Series Submittal (May 2021) | Submittal / specs |
| GE Connect Spec Sheet (Aug 2020) | Spec sheet |

**~2,131 unique chunks** after deduplication across overlapping manual versions (2,112 image chunks + 19 text-only chunks).

## Stack

| Layer | Technology |
|---|---|
| LLM | Anthropic `claude-opus-4-6` (streaming + adaptive vision: 0–2 images per request) |
| Text embedding | `sentence-transformers/all-MiniLM-L6-v2` (384-dim, dense text retrieval, local) |
| Image embedding | `sentence-transformers/clip-ViT-B-32` image encoder (512-dim, visual content, local) |
| Retrieval fusion | Reciprocal Rank Fusion (RRF, k=60) — merges CLIP and MiniLM ranked lists by rank position, not raw cosine score (scores are incomparable across encoders) |
| Vector store | NumPy flat files (`embeddings.npy` + `text_embeddings.npy`) + cosine similarity |
| Document parsing | LandingAI ADE `dpt-2-latest` (build-time only) |
| Chunk images | PyMuPDF page render → Pillow bbox crop → PNG for CLIP + Claude vision |
| Web framework | Flask + SSE streaming |

## Environment Variables

| Variable | Required For | Where to Get |
|---|---|---|
| `ANTHROPIC_API_KEY` | RAG pipeline + web UI (always required) | [console.anthropic.com](https://console.anthropic.com) |
| `VISION_AGENT_API_KEY` | ADE parsing only (`--parse` mode, build-time) | [va.landing.ai/settings/api-key](https://va.landing.ai/settings/api-key) |

Set these as Space secrets in HuggingFace Space settings (or in a local `.env` file for development). `VISION_AGENT_API_KEY` is only needed when re-parsing PDFs; it is not required at runtime.

## Local Development

```bash
# Install dependencies
pip install anthropic sentence-transformers numpy flask gunicorn python-dotenv pymupdf Pillow requests beautifulsoup4

# Download GE Connect Series PDFs
python web_app.py --download

# Parse PDFs with LandingAI ADE (requires VISION_AGENT_API_KEY)
python web_app.py --parse --all

# Re-parse all PDFs from scratch (e.g. to capture new ADE features like confidence scores)
python web_app.py --parse --all --force

# Build dual-encoder vector store
python web_app.py --rebuild

# Run locally (port 8080)
python web_app.py

# Or use the HF Spaces entrypoint (port 7860, matches Dockerfile CMD)
gunicorn --worker-class gthread --threads 4 --timeout 300 --bind 0.0.0.0:7860 app:application
```

## Production Notes

This is a demo scoped to GE Connect Series. The same architecture generalizes to any document-heavy domain — equipment manufacturers, financial services, healthcare, logistics — wherever dense, visually complex documentation drives high-stakes decisions. A real deployment would change most of the infrastructure:

- **Vector store** — replace NumPy flat-file scan with a proper vector database (pgvector, Pinecone, Weaviate, Qdrant). Full cosine scan over 2,131 chunks is fine locally; it won't scale.
- **Embedding** — consider a domain-adapted or higher-capacity model. MiniLM-L6 is fast and surprisingly capable, but larger models (e.g. `bge-large`, `text-embedding-3-large`) close the gap on technical/domain-specific retrieval.
- **Reranking** — add a cross-encoder reranker (e.g. `ms-marco-MiniLM-L6-reranking`) on top of the dual-encoder retrieval for a significant precision boost.
- **Auth & rate limiting** — the web UI has no authentication or per-user quota. Add both before exposing publicly.
- **Observability** — no tracing, no query logging, no retrieval quality metrics. Add LangSmith, Langfuse, or similar.
- **Multi-tenancy / access control** — a single shared index is fine for a demo; a real product would scope retrieval per user or per organization.
- **Pinned dependencies** — `requirements.txt` is now pinned to versions confirmed working as of Mar 2026; re-evaluate pins when upgrading.
