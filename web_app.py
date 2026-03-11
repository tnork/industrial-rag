"""
GE Connect Technical Assistant — Unified Application
=====================================================
Combines PDF downloading, ADE parsing, dual-encoder vector store, and Flask web UI.

Modes:
    python web_app.py                      # launch web UI (default)
    python web_app.py --web                # launch web UI explicitly
    python web_app.py --terminal           # interactive terminal Q&A
    python web_app.py "question"           # one-shot CLI answer

    python web_app.py --download           # download all GE Connect Series PDFs
    python web_app.py --parse              # parse 2 new files (validation)
    python web_app.py --parse --all        # parse all unprocessed files
    python web_app.py --parse --limit 10   # parse next N files
    python web_app.py --rebuild            # rebuild vector store from parse results

Parse output:
    parse_results/{category}/{stem}.txt
    chunk_images/{category}/{stem}/NNN_type_uuid.png

RAG vector store (NumPy flat files, dual-encoder):
    vector_store/embeddings.npy        ← CLIP 512-dim vectors for image chunks
    vector_store/text_embeddings.npy   ← MiniLM 384-dim vectors for text-only chunks
    vector_store/img_indices.json      ← maps CLIP matrix rows → store indices
    vector_store/txt_indices.json      ← maps MiniLM matrix rows → store indices
    vector_store/documents.json        ← chunk metadata (text, bbox, page, category, etc.)
    vector_store/indexed_files.json    ← tracks which parse results are indexed

Embedding strategy (dual-encoder):
    - Image chunks (have a matching PNG crop):
        → embedded with CLIP image encoder (clip-ViT-B-32, 512-dim)
        → captures visual content of wiring diagrams, figures, spec tables
    - Text-only chunks (no image):
        → embedded with MiniLM text encoder (all-MiniLM-L6-v2, 384-dim)
        → optimized for dense semantic passage retrieval

Retrieval — two-stage pipeline:
  Stage 1 — Reciprocal Rank Fusion (RRF, k=60):
    At query time the question is encoded by BOTH encoders. Each returns a ranked
    list; RRF merges them by rank position (1/(k+rank+1)) rather than raw cosine
    score. This is necessary because MiniLM text-to-text scores (0.4–0.8) are
    systematically higher than CLIP text-to-image scores (0.1–0.35) — raw-score
    merging would cause text-only chunks to dominate every result set. RRF gives
    each encoder equal weight. Top RERANK_CANDIDATES (40) forwarded to Stage 2.
    The original cosine score is preserved for UI display.
  Stage 2 — Cross-encoder reranking (cross-encoder/ms-marco-MiniLM-L6-v2):
    Scores (query, chunk_text) pairs jointly in a single forward pass — far more
    accurate than bi-encoder cosine proximity for final ranking. ADE confidence
    is applied as a 15% soft multiplicative boost (CONF_WEIGHT=0.15). Final top 5
    returned to Claude and the web UI.

Claude vision (adaptive):
    - 0–2 images sent per request (MAX_CLAUDE_IMGS = 2)
    - An image is included for each hit whose cosine similarity meets
      CLAUDE_IMG_MIN_SIM (0.20) and has a matching chunk image, up to the cap
    - Visual questions (diagram ranked #1) get images; text/lookup questions
      (text passages ranked top) get 0 images, eliminating vision token cost
    - claude-opus-4-6 reads diagrams visually; answers streamed via SSE

Environment:
    VISION_AGENT_API_KEY  — for ADE parsing only (.env)
    ANTHROPIC_API_KEY     — for Claude RAG + web UI (.env)
"""

import base64
import csv
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote

from dotenv import load_dotenv

load_dotenv(override=True)

# ─── Paths ────────────────────────────────────────────────────────────────────

BASE_DIR      = Path(__file__).parent
INPUT_DIR     = BASE_DIR / "ge_hvac_manuals"
PARSE_RESULTS = BASE_DIR / "parse_results"
CHUNK_IMAGES  = BASE_DIR / "chunk_images"
VECTOR_DIR    = BASE_DIR / "vector_store"
VECTORS_FILE      = VECTOR_DIR / "embeddings.npy"       # CLIP image embeddings (M × 512)
TEXT_VECTORS_FILE = VECTOR_DIR / "text_embeddings.npy"  # MiniLM text embeddings (N × 384)
IMG_INDICES_FILE  = VECTOR_DIR / "img_indices.json"     # store index for each row in VECTORS_FILE
TXT_INDICES_FILE  = VECTOR_DIR / "txt_indices.json"     # store index for each row in TEXT_VECTORS_FILE
DOCS_FILE         = VECTOR_DIR / "documents.json"
INDEXED_FILE      = VECTOR_DIR / "indexed_files.json"

EMBEDDING_MODEL      = "clip-ViT-B-32"        # CLIP image encoder — embeds image chunks (512-dim)
TEXT_EMBEDDING_MODEL = "all-MiniLM-L6-v2"    # MiniLM — dense text retrieval for text chunks + queries (384-dim)
RERANK_MODEL         = "cross-encoder/ms-marco-MiniLM-L6-v2"  # cross-encoder reranker (~40MB)
RERANK_CANDIDATES    = 40                     # candidates fetched from RRF before cross-encoder reranking; wider net means the right chunk is more likely to reach Stage 2
CONF_WEIGHT          = 0.15                   # ADE confidence soft boost inside reranking (0 = off)
CLAUDE_MODEL         = "claude-opus-4-6"
IMAGE_THRESHOLD      = 0.30
MAX_CLAUDE_IMGS      = 2                      # max chunk images per Claude request (top 2 most relevant)
CLAUDE_IMG_MIN_SIM   = 0.20                   # minimum cosine similarity to send an image to Claude (skip low-relevance figures)
CLAUDE_IMG_SIDE      = 768                    # resize images to this max dimension before sending to Claude
SEP             = "─" * 80


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — PDF DOWNLOADER
# ══════════════════════════════════════════════════════════════════════════════

ZIP_NAME    = "ge_hvac_manuals.zip"
SLEEP       = 0.3
TIMEOUT     = 30

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/pdf,*/*",
}

GEA_BASE   = "https://www.geappliances.com/content/ductless/downloads/"
HAIER_BASE = "https://www.haierappliances.com/content/downloads/ductless/"
HVAC_BASE  = "https://www.geappliances.com/content/hvac/downloads/"

KNOWN_DOCS = [
    # ── Connect Series ────────────────────────────────────────────────────────
    {
        "series":   "Connect Series",
        "folder":   "connect_series",
        "doc_type": "Service Manual",
        "date":     "2020-09",
        "url":      GEA_BASE + "GE%20Connect%20Series/GE%20Connect%20Series%20Service%20Manual%202020902.pdf",
    },
    {
        "series":   "Connect Series",
        "folder":   "connect_series",
        "doc_type": "Service Manual",
        "date":     "2021-03",
        "url":      GEA_BASE + "GE%20Connect%20Series/GE%20Connect%20Service%20Manual%2003-12-2021.pdf",
    },
    {
        "series":   "Connect Series",
        "folder":   "connect_series",
        "doc_type": "Service Manual",
        "date":     "2021-06",
        "url":      GEA_BASE + "GE%20Connect%20Series/65%20-%20GE_Connect%20Series%20Service%20Manual_Rev.%2006-09-2021.pdf",
    },
    {
        "series":   "Connect Series",
        "folder":   "connect_series",
        "doc_type": "Service Manual (Full — latest revision)",
        "date":     "2022-11",
        "url":      GEA_BASE + "GE%20Connect%20Series/GE_Connect_Series_Service_Manual_110422.pdf",
    },
    {
        "series":   "Connect Series",
        "folder":   "connect_series",
        "doc_type": "Installation Manual (Outdoor)",
        "date":     "2023-07",
        "url":      GEA_BASE + "GE%20Connect%20Series/Connect-Installation-Manual-Outdoor.pdf",
    },
    {
        "series":   "Connect Series",
        "folder":   "connect_series",
        "doc_type": "Submittal Sheet",
        "date":     "2021-05",
        "url":      GEA_BASE + "GE%20Connect%20Series/62%20-%20GE%20Connect%20Series%20Submittal%2005-13-21.pdf",
    },
    {
        "series":   "Connect Series",
        "folder":   "connect_series",
        "doc_type": "Spec Sheet",
        "date":     "2020-08",
        "url":      GEA_BASE + "GE%20Connect%20Series/GE%20Connect%20Spec%20Sheet%2008-29-20.pdf",
    },
    # ── Connect Series (additional authoritative documents) ───────────────────
    # Indoor air handler manuals — the existing corpus only covers the outdoor
    # unit. These two cover the ducted AHU (high-static and standard variants).
    {
        "series":   "Connect Series",
        "folder":   "connect_series",
        "doc_type": "Installation Manual (Indoor AHU — High Static)",
        "date":     "2021-04",
        "url":      GEA_BASE + "GE%20Connect%20Series/61%20-%20GE_Connect-High%20static%20AHU%20Installation%20Manual_04302021.pdf",
    },
    {
        "series":   "Connect Series",
        "folder":   "connect_series",
        "doc_type": "Installation Manual (Indoor AHU — Standard)",
        "date":     "2021",
        "url":      GEA_BASE + "GE%20Connect%20Series/GE%20Connect%20Installation%20Manual%20Indoor%20AHU%202021.pdf",
    },
    # Specification Guide (Mar 2022) — more detailed than the basic spec sheet;
    # includes engineering selection data, capacity tables, and electrical specs.
    # Newer than any spec document currently in the corpus.
    {
        "series":   "Connect Series",
        "folder":   "connect_series",
        "doc_type": "Specification Guide",
        "date":     "2022-03",
        "url":      GEA_BASE + "GE%20Connect%20Series/GE_Connect_Specification_Guide_032822.pdf",
    },
    # ── Endure Series Multi-Zone ──────────────────────────────────────────────
    {
        "series":   "Endure Series Multi",
        "folder":   "endure_multi",
        "doc_type": "Service Manual",
        "date":     "2022-04",
        "url":      GEA_BASE + "Endure%20Series%20Multi/GE_Endure_Multi_Service_Manual.pdf",
    },
    {
        "series":   "Endure Series Multi",
        "folder":   "endure_multi",
        "doc_type": "Installation Manual (Compact Cassette)",
        "date":     "2019-11",
        "url":      GEA_BASE + "Endure%20Series%20Multi/Compact%20Cassette%20Install%20Manual.pdf",
    },
    {
        "series":   "Endure Series Multi",
        "folder":   "endure_multi",
        "doc_type": "Owner's Manual (Highwall/Cassette/Console)",
        "date":     "2019-08",
        "url":      GEA_BASE + "Endure%20Series%20Multi/GEA%20Owner%20Manual%20YR-HG%20for%20highwall%20cassette%20and%20console.pdf",
    },
    # ── Altitude Series ───────────────────────────────────────────────────────
    {
        "series":   "Altitude Series",
        "folder":   "altitude_series",
        "doc_type": "Service Manual",
        "date":     "2019-06",
        "url":      GEA_BASE + "Altitude%20Series/GE%20Altitute%20Series%20-%20Service%20Manual%2006-27-19.pdf",
    },
    # ── Haier / GE Ductless CDN ───────────────────────────────────────────────
    {
        "series":   "Endure Series Multi",
        "folder":   "endure_multi",
        "doc_type": "Service Manual (Haier/GE Multi)",
        "date":     "2022-04",
        "url":      GEA_BASE + "Haier_GE_Multi_Service_Manual_4.11.22.pdf",
    },
    {
        "series":   "Tempo Series",
        "folder":   "tempo_series",
        "doc_type": "Service Manual (Low Ambient Cooling)",
        "date":     "2022-04",
        "url":      HAIER_BASE + "Tempo-Series/Haier_GE_Low_Ambient_Cooling_Service_Manual_4.11.22.pdf",
    },
    # ── Residential HVAC Brochures ────────────────────────────────────────────
    {
        "series":   "Residential HVAC",
        "folder":   "brochures",
        "doc_type": "Homeowner Brochure",
        "date":     "2022-08",
        "url":      HVAC_BASE + "GE-Residential-HVAC-Homeowner%20Brochure-83122.pdf",
    },
]

CURRENT_HVAC_MODELS = [
    "AUH2436ZGDA", "AUH4860ZGDA",
    "UUY09ZGDAA", "UUY12ZGDAA", "UUY18ZGDAA", "UUY24ZGDAA",
    "UUY36ZGDAA", "UUY48ZGDAA", "UUY60ZGDAA",
    "ASH109URDEB", "ASH112URDSE", "ASH118CRDWA", "ASH124CRDWA",
    "AHH09LXW1", "AHH12LXW1", "AHH18LXW1", "AHH24LXW1",
    "ASH424NCDWA", "ASH436NCDWA", "ASH448NCDWA",
]

GEA_PRODUCT_SUPPORT_URL = "https://products.geappliances.com/appliance/gea-specs/{model}/support"
GEA_PRODUCT_DOCS_API    = "https://products.geappliances.com/api/products/{model}/documents"

CRAWL_DIRS = [
    ("Connect Series",    GEA_BASE + "GE%20Connect%20Series/",  "connect_series"),
    ("Endure Multi",      GEA_BASE + "Endure%20Series%20Multi/", "endure_multi"),
    ("Altitude Series",   GEA_BASE + "Altitude%20Series/",       "altitude_series"),
    ("Haier GE Ductless", GEA_BASE,                              "root_downloads"),
    ("Haier CDN Ductless",HAIER_BASE,                            "haier_ductless"),
    ("Haier Tempo",       HAIER_BASE + "Tempo-Series/",          "tempo_series"),
]


def _http_get(url: str, retries: int = 3, stream: bool = False, **kwargs):
    import requests
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, stream=stream, **kwargs)
            time.sleep(SLEEP)
            if r.status_code == 200:
                return r
            if r.status_code in (403, 404):
                print(f"    ✗  HTTP {r.status_code}: {url}")
                return None
            print(f"    ⚠  HTTP {r.status_code} (attempt {attempt+1}): {url}")
        except Exception as exc:
            print(f"    ⚠  Error (attempt {attempt+1}): {exc}")
            time.sleep(2 ** attempt)
    return None


def _slugify(text: str) -> str:
    text = unquote(text)
    text = re.sub(r"[^\w\-. ]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text[:120]


def _download_pdf(url: str, dest_path: Path) -> bool:
    if dest_path.exists() and dest_path.stat().st_size > 500:
        print(f"    ↩  Cached: {dest_path.name}")
        return True
    r = _http_get(url, stream=True)
    if not r:
        return False
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_bytes(r.content)
    print(f"    ✓  {dest_path.name}  ({dest_path.stat().st_size / 1024:.0f} KB)")
    return True


def _filename_from_url(url: str) -> str:
    path = urlparse(url).path
    name = Path(path).name
    return _slugify(name) if name else "document.pdf"


def _download_known_docs() -> list[dict]:
    print("\n" + "═" * 65)
    print("  SECTION 1 — Known Static PDFs from GEA/Haier CDNs")
    print("═" * 65)
    manifest = []
    for doc in KNOWN_DOCS:
        folder = INPUT_DIR / doc["folder"]
        folder.mkdir(parents=True, exist_ok=True)
        fname = _filename_from_url(doc["url"])
        dest  = folder / fname
        print(f"\n  [{doc['series']}]  {doc['doc_type']}  ({doc['date']})")
        ok = _download_pdf(doc["url"], dest)
        manifest.append({
            "series": doc["series"], "doc_type": doc["doc_type"], "date": doc["date"],
            "url": doc["url"], "local_file": str(dest) if ok else "", "status": "ok" if ok else "failed",
        })
    return manifest


def _fetch_model_docs(model: str) -> list[dict]:
    from bs4 import BeautifulSoup
    docs = []
    r = _http_get(GEA_PRODUCT_DOCS_API.format(model=model))
    if r:
        try:
            data  = r.json()
            items = data if isinstance(data, list) else data.get("documents", data.get("docs", []))
            for item in items:
                url = item.get("url") or item.get("pdfUrl") or item.get("path")
                if url and url.lower().endswith(".pdf"):
                    if not url.startswith("http"):
                        url = "https://products.geappliances.com" + url
                    docs.append({"url": url, "doc_type": item.get("type", "Manual"), "title": item.get("title", "")})
        except Exception:
            pass
    if not docs:
        support_url = GEA_PRODUCT_SUPPORT_URL.format(model=model)
        r = _http_get(support_url)
        if r and r.text:
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if ".pdf" in href.lower():
                    docs.append({"url": urljoin(support_url, href), "doc_type": a.get_text(strip=True) or "Manual", "title": ""})
            m = re.search(r'"documents"\s*:\s*(\[.*?\])', r.text, re.DOTALL)
            if m:
                try:
                    for item in json.loads(m.group(1)):
                        url = item.get("url") or item.get("pdfUrl")
                        if url and ".pdf" in url.lower():
                            if not url.startswith("http"):
                                url = "https://products.geappliances.com" + url
                            docs.append({"url": url, "doc_type": item.get("type", "Manual"), "title": item.get("title", "")})
                except Exception:
                    pass
    seen = set()
    return [d for d in docs if not (d["url"] in seen or seen.add(d["url"]))]


def _download_model_docs(models: list[str]) -> list[dict]:
    print("\n" + "═" * 65)
    print("  SECTION 2 — Model-Specific Manuals via GEA Products API")
    print("═" * 65)
    manifest = []
    model_dir = INPUT_DIR / "model_specific"
    model_dir.mkdir(parents=True, exist_ok=True)
    for model in models:
        print(f"\n  Model: {model}")
        docs = _fetch_model_docs(model)
        if not docs:
            print(f"    ℹ  No PDF documents found for {model}")
            manifest.append({"series": "Model-Specific", "doc_type": "N/A", "date": "", "url": "", "local_file": "", "status": "no_docs", "model": model})
            continue
        for doc in docs:
            dest = model_dir / f"{model}_{_filename_from_url(doc['url'])}"
            ok   = _download_pdf(doc["url"], dest)
            manifest.append({"series": "Model-Specific", "doc_type": doc["doc_type"], "date": "", "url": doc["url"], "local_file": str(dest) if ok else "", "status": "ok" if ok else "failed", "model": model})
    return manifest


def _crawl_directory(label: str, dir_url: str, local_folder: str) -> list[dict]:
    from bs4 import BeautifulSoup
    manifest = []
    print(f"\n  Crawling: {label}  →  {dir_url}")
    r = _http_get(dir_url)
    if not r:
        print(f"    ✗  Directory not accessible")
        return manifest
    soup = BeautifulSoup(r.text, "html.parser")
    pdf_links = [a["href"] for a in soup.find_all("a", href=True) if ".pdf" in a["href"].lower()]
    if not pdf_links:
        print(f"    ℹ  No PDF links found")
        return manifest
    dest_dir = INPUT_DIR / local_folder
    dest_dir.mkdir(parents=True, exist_ok=True)
    for href in pdf_links:
        full_url = urljoin(dir_url, href)
        fname    = _filename_from_url(full_url)
        ok       = _download_pdf(full_url, dest_dir / fname)
        manifest.append({"series": label, "doc_type": "PDF (crawled)", "date": "", "url": full_url, "local_file": str(dest_dir / fname) if ok else "", "status": "ok" if ok else "failed"})
    return manifest


def run_download():
    print("═" * 65)
    print("  GE Appliances HVAC — Product Manuals Downloader")
    print(f"  Run date : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("═" * 65)

    INPUT_DIR.mkdir(exist_ok=True)
    all_manifest  = []
    all_manifest += _download_known_docs()
    all_manifest += _download_model_docs(CURRENT_HVAC_MODELS)

    # Directory crawl
    print("\n" + "═" * 65)
    print("  SECTION 3 — Directory Crawl")
    print("═" * 65)
    seen_urls = set()
    for label, dir_url, folder in CRAWL_DIRS:
        for row in _crawl_directory(label, dir_url, folder):
            if row["url"] not in seen_urls:
                seen_urls.add(row["url"])
                all_manifest.append(row)

    # Manifest CSV
    manifest_path = INPUT_DIR / "manifest.csv"
    with open(manifest_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["series", "doc_type", "date", "url", "local_file", "status"], extrasaction="ignore")
        w.writeheader()
        w.writerows(all_manifest)

    ok_count   = sum(1 for r in all_manifest if r["status"] == "ok")
    fail_count = sum(1 for r in all_manifest if r["status"] in ("failed",))
    print("\n" + "═" * 65)
    print("  ✅  DOWNLOAD COMPLETE")
    print(f"  Files attempted : {len(all_manifest)}  |  Downloaded : {ok_count}  |  Failed : {fail_count}")
    print(f"  Manifest: {manifest_path}")
    print("═" * 65)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — ADE BATCH PARSING
# ══════════════════════════════════════════════════════════════════════════════

def _build_parse_text(parse_response, pdf_path: Path) -> str:
    full = parse_response.model_dump()
    return "\n".join([
        "SOURCE",   SEP, str(pdf_path), SEP, "",
        "METADATA", SEP, json.dumps(full["metadata"], indent=2, default=str), SEP, "",
        "CHUNKS",   SEP, json.dumps(full.get("chunks", []), indent=2, default=str), SEP, "",
        "GROUNDING", SEP, json.dumps(full.get("grounding", {}), indent=2, default=str), SEP, "",
        "SPLITS",   SEP, json.dumps(full.get("splits", []), indent=2, default=str), SEP, "",
        "MARKDOWN", SEP, parse_response.markdown or "", SEP,
    ])


def _crop_chunks(pdf_path: Path, chunks: list, output_dir: Path, dpi: int = 200) -> int:
    import fitz
    from PIL import Image

    output_dir.mkdir(parents=True, exist_ok=True)
    doc   = fitz.open(pdf_path)
    scale = dpi / 72

    page_images: dict[int, Image.Image] = {}
    for page_num in range(len(doc)):
        mat = fitz.Matrix(scale, scale)
        pix = doc[page_num].get_pixmap(matrix=mat)
        page_images[page_num] = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

    saved = 0
    for i, chunk in enumerate(chunks):
        grounding = chunk.get("grounding")
        if not grounding:
            continue
        box      = grounding.get("box", {})
        page_num = grounding.get("page", 0)
        img      = page_images.get(page_num)
        if img is None:
            continue
        w, h   = img.size
        left   = int(box.get("left",   0) * w)
        top    = int(box.get("top",    0) * h)
        right  = int(box.get("right",  1) * w)
        bottom = int(box.get("bottom", 1) * h)
        out_path = output_dir / f"{i:03d}_{chunk.get('type','unknown')}_{chunk.get('id', f'chunk_{i}')}.png"
        img.crop((left, top, right, bottom)).save(out_path)
        saved += 1

    doc.close()
    return saved


def _crop_single_chunk(pdf_path: Path, chunk: dict, global_idx: int,
                       local_page: int, output_dir: Path, dpi: int = 200):
    import fitz
    from PIL import Image

    grounding = chunk.get("grounding")
    if not grounding:
        return
    box = grounding.get("box", {})
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        doc   = fitz.open(pdf_path)
        scale = dpi / 72
        pix   = doc[local_page].get_pixmap(matrix=fitz.Matrix(scale, scale))
        img   = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        doc.close()
    except Exception:
        return
    w, h = img.size
    out_path = output_dir / f"{global_idx:03d}_{chunk.get('type','unknown')}_{chunk.get('id', f'chunk_{global_idx}')}.png"
    img.crop((int(box.get("left", 0) * w), int(box.get("top", 0) * h),
              int(box.get("right", 1) * w), int(box.get("bottom", 1) * h))).save(out_path)


def _collect_pdfs() -> list[tuple[str, Path]]:
    pdfs = []
    category_dir = INPUT_DIR / "connect_series"
    if category_dir.is_dir():
        seen = set()
        for pdf in sorted(category_dir.iterdir()):
            if pdf.suffix.lower() == ".pdf" and pdf not in seen:
                pdfs.append(("connect_series", pdf))
                seen.add(pdf)
    return pdfs


MAX_PAGES = 90


def _pdf_page_count(pdf_path: Path) -> int:
    import fitz
    doc = fitz.open(pdf_path)
    n   = len(doc)
    doc.close()
    return n


def _split_pdf(pdf_path: Path, chunk_size: int = MAX_PAGES) -> list:
    import fitz, tempfile
    doc   = fitz.open(pdf_path)
    parts = []
    for start in range(0, len(doc), chunk_size):
        end    = min(start + chunk_size, len(doc))
        tmp    = Path(tempfile.mktemp(suffix=f"_p{start}-{end-1}.pdf"))
        subdoc = fitz.open()
        subdoc.insert_pdf(doc, from_page=start, to_page=end - 1)
        subdoc.save(tmp)
        subdoc.close()
        parts.append((start, tmp))
    doc.close()
    return parts


def _parse_one(client, category: str, pdf_path: Path) -> bool:
    stem        = pdf_path.stem
    result_dir  = PARSE_RESULTS / category
    result_file = result_dir / f"{stem}.txt"
    images_dir  = CHUNK_IMAGES / category / stem

    result_dir.mkdir(parents=True, exist_ok=True)

    if result_file.exists():
        print(f"  [skip] {category}/{stem}")
        return True

    page_count = _pdf_page_count(pdf_path)

    if page_count > MAX_PAGES:
        print(f"  Parsing (split): {category}/{pdf_path.name} ({page_count} pages) ...", flush=True)
        return _parse_split(client, category, pdf_path, page_count, images_dir, result_file)

    print(f"  Parsing: {category}/{pdf_path.name} ...", flush=True)
    try:
        parse_response = client.parse(document=pdf_path, model="dpt-2-latest")
    except Exception as e:
        print(f"  [error] {pdf_path.name}: {e}")
        return False

    pages  = getattr(getattr(parse_response, "metadata", None), "page_count", "?")
    result_file.write_text(_build_parse_text(parse_response, pdf_path), encoding="utf-8")
    chunks   = parse_response.model_dump().get("chunks", [])
    n_images = _crop_chunks(pdf_path, chunks, images_dir)
    print(f"  ✓ {pages} pages | {len(chunks)} chunks | {n_images} images")
    return True


def _parse_split(client, category: str, pdf_path: Path, total_pages: int,
                 images_dir: Path, result_file: Path) -> bool:
    parts           = _split_pdf(pdf_path)
    all_chunks      = []
    all_grounding   = {}
    all_markdown    = []
    total_chunks    = 0

    try:
        for part_idx, (page_offset, tmp_path) in enumerate(parts):
            part_pages = _pdf_page_count(tmp_path)
            print(f"    Part {part_idx+1}/{len(parts)}: pages {page_offset}–{page_offset+part_pages-1}", flush=True)
            try:
                resp = client.parse(document=tmp_path, model="dpt-2-latest")
            except Exception as e:
                print(f"    [error] part {part_idx+1}: {e}")
                tmp_path.unlink(missing_ok=True)
                continue

            part_data = resp.model_dump()
            for chunk in part_data.get("chunks", []):
                grounding = chunk.get("grounding")
                if grounding and "page" in grounding:
                    grounding["page"] += page_offset
                all_chunks.append(chunk)

            # Merge top-level grounding (confidence scores), adjusting page numbers
            for uid, gdata in (part_data.get("grounding") or {}).items():
                entry = dict(gdata) if gdata else {}
                if "page" in entry and entry["page"] is not None:
                    entry["page"] = entry["page"] + page_offset
                all_grounding[uid] = entry

            for i, chunk in enumerate(part_data.get("chunks", [])):
                _crop_single_chunk(tmp_path, chunk, i + total_chunks,
                                   (chunk.get("grounding") or {}).get("page", page_offset) - page_offset,
                                   images_dir)

            total_chunks += len(part_data.get("chunks", []))
            if resp.markdown:
                all_markdown.append(resp.markdown)
            tmp_path.unlink(missing_ok=True)
            time.sleep(1)

    finally:
        for _, tmp_path in parts:
            tmp_path.unlink(missing_ok=True)

    merged = {
        "metadata":  {"page_count": total_pages, "source": str(pdf_path), "split": True},
        "chunks":    all_chunks,
        "grounding": all_grounding,
        "splits":    [],
    }

    class _FakeResp:
        def model_dump(self): return merged
        markdown = "\n\n---\n\n".join(all_markdown)

    result_file.write_text(_build_parse_text(_FakeResp(), pdf_path), encoding="utf-8")
    print(f"  ✓ {total_pages} pages (split) | {len(all_chunks)} chunks total")
    return True


def run_parse(parse_all: bool = False, limit: int = 2, force: bool = False):
    from landingai_ade import LandingAIADE

    pdfs = _collect_pdfs()
    if not pdfs:
        print(f"No PDFs found under {INPUT_DIR}. Run with --download first.")
        sys.exit(1)

    if force:
        # Delete existing parse results so they are re-parsed from scratch
        import shutil
        deleted = 0
        for cat, p in pdfs:
            result_file = PARSE_RESULTS / cat / f"{p.stem}.txt"
            if result_file.exists():
                result_file.unlink()
                deleted += 1
        if deleted:
            print(f"  [force] Deleted {deleted} existing parse result(s) — will re-parse")

    pending = [(cat, p) for cat, p in pdfs if not (PARSE_RESULTS / cat / f"{p.stem}.txt").exists()]
    batch   = pdfs if parse_all else pending[:limit]
    label   = f"all {len(batch)} files" if parse_all else f"{len(batch)} of {len(pending)} pending"

    print("=" * 60)
    print(f"GE HVAC Batch Parser — {label}")
    print("=" * 60)

    PARSE_RESULTS.mkdir(exist_ok=True)
    CHUNK_IMAGES.mkdir(exist_ok=True)
    client = LandingAIADE()

    success = fail = skipped = 0
    for i, (category, pdf_path) in enumerate(batch, 1):
        print(f"\n[{i}/{len(batch)}]")
        stem = pdf_path.stem
        if (PARSE_RESULTS / category / f"{stem}.txt").exists():
            skipped += 1
            print(f"  [skip] already parsed: {category}/{stem}")
            continue
        ok = _parse_one(client, category, pdf_path)
        if ok:
            success += 1
        else:
            fail += 1
        if i < len(batch):
            time.sleep(1)

    print("\n" + "=" * 60)
    print(f"DONE  parsed={success}  skipped={skipped}  failed={fail}")
    print(f"Results: {PARSE_RESULTS}/")
    print(f"Images:  {CHUNK_IMAGES}/")
    print("=" * 60)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — RAG VECTOR STORE
# ══════════════════════════════════════════════════════════════════════════════

_UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)


def _load_friendly_names() -> dict:
    p = BASE_DIR / "friendly_names.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {}


def _load_all_chunks() -> list[dict]:
    friendly   = _load_friendly_names()
    all_chunks = []
    for txt_file in sorted(PARSE_RESULTS.rglob("*.txt")):
        category = txt_file.parent.name
        stem     = txt_file.stem
        if _UUID_RE.match(stem) and stem in friendly:
            stem = friendly[stem]
        raw = txt_file.read_text(encoding="utf-8", errors="ignore")

        chunks_match = re.search(r"CHUNKS\n─+\n(.*?)\n─+\n", raw, re.DOTALL)
        if not chunks_match:
            continue
        try:
            chunks = json.loads(chunks_match.group(1))
        except json.JSONDecodeError:
            continue

        # Top-level grounding dict (keyed by chunk UUID) — contains confidence scores.
        # Present only in files parsed on or after 2026-02-12 (ADE confidence launch).
        top_grounding: dict = {}
        grounding_match = re.search(r"GROUNDING\n─+\n(.*?)\n─+\n", raw, re.DOTALL)
        if grounding_match:
            try:
                top_grounding = json.loads(grounding_match.group(1))
            except json.JSONDecodeError:
                pass

        for chunk in chunks:
            grounding  = chunk.get("grounding") or {}
            box        = grounding.get("box", {})
            chunk_id   = chunk.get("id", "")
            top_gdata  = top_grounding.get(chunk_id) or {}
            confidence = top_gdata.get("confidence")  # float 0–1 or None
            all_chunks.append({
                "_source_file": str(txt_file.relative_to(BASE_DIR)),
                "_category":    category,
                "_doc_stem":    stem,
                "id":           chunk_id,
                "chunk_type":   chunk.get("type", "unknown"),
                "page":         grounding.get("page", 0),
                "text":         re.sub(r"<a[^>]*>.*?</a>", "", chunk.get("markdown", ""), flags=re.DOTALL).strip(),
                "confidence":   confidence,
                "bbox": {
                    "left":   box.get("left",   0),
                    "top":    box.get("top",    0),
                    "right":  box.get("right",  1),
                    "bottom": box.get("bottom", 1),
                } if box else {},
            })
    return [c for c in all_chunks if c["text"]]


def _indexed_files() -> list[str]:
    if INDEXED_FILE.exists():
        return json.loads(INDEXED_FILE.read_text())
    return []


def _current_parse_files() -> list[str]:
    return sorted(str(p.relative_to(BASE_DIR)) for p in PARSE_RESULTS.rglob("*.txt"))


def _deduplicate_chunks(chunks: list, vectors) -> tuple:
    import numpy as np
    seen_text   = {}
    keep_idx    = []
    removed_ids = []
    for i, chunk in enumerate(chunks):
        # Normalize: strip markdown formatting, collapse whitespace, and lowercase
        # so near-identical chunks (different revisions, heading styles, minor
        # wording differences) are treated as duplicates.
        # Original text is preserved in the stored chunk — only the key is stripped.
        raw = chunk.get("text", "")
        raw = re.sub(r'\*{1,3}', '', raw)          # strip bold/italic markers
        raw = re.sub(r'^#{1,6}\s*', '', raw, flags=re.MULTILINE)  # strip heading #
        text_key = re.sub(r'\s+', ' ', raw).strip().lower()
        if text_key not in seen_text:
            seen_text[text_key] = i
            keep_idx.append(i)
        else:
            removed_ids.append(chunk.get("id", ""))
    if removed_ids:
        print(f"  Deduplication: {len(chunks)} → {len(keep_idx)} chunks ({len(removed_ids)} duplicates removed)")
    return [chunks[i] for i in keep_idx], vectors[keep_idx], removed_ids


def _delete_orphaned_images(chunk_ids: list[str]) -> None:
    deleted = 0
    for chunk_id in chunk_ids:
        if not chunk_id:
            continue
        for img in CHUNK_IMAGES.rglob(f"*_{chunk_id}.png"):
            img.unlink(missing_ok=True)
            deleted += 1
    if deleted:
        print(f"  Deleted {deleted} duplicate chunk images")


def _build_chunk_image_map() -> dict[str, Path]:
    image_map: dict[str, Path] = {}
    if not CHUNK_IMAGES.exists():
        return image_map
    for img in CHUNK_IMAGES.rglob("*.png"):
        parts = img.stem.split("_", 2)
        if len(parts) == 3:
            image_map[parts[2]] = img
    return image_map


def _strip_html(text: str) -> str:
    """Remove HTML tags and collapse whitespace.

    Used before MiniLM embedding for table chunks: ADE stores table content as
    HTML (<table><tr><td>…</td></tr></table>). The tag noise dilutes the semantic
    signal — embedding the raw HTML causes MiniLM to encode <td id="..."> tokens
    instead of the actual cell values. Stripping tags before encoding improves
    cosine similarity scores for table queries (e.g. 'refrigerant charge for 36K').
    The original HTML is preserved in documents.json for UI display.
    """
    cleaned = re.sub(r"<[^>]+>", " ", text)          # remove all tags
    return re.sub(r"\s+", " ", cleaned).strip()       # collapse whitespace


def _display_text(text: str, max_len: int = 200) -> str:
    """Clean chunk text for UI display: strip ADE markers, HTML, and markdown formatting."""
    cleaned = re.sub(r"<::.*?::>", "", text, flags=re.DOTALL)   # ADE image markers
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)                  # HTML tags (table chunks)
    cleaned = re.sub(r'\*{1,3}(.*?)\*{1,3}', r'\1', cleaned)    # bold/italic **x**
    cleaned = re.sub(r'^#{1,6}\s*', '', cleaned, flags=re.MULTILINE)  # headings
    cleaned = re.sub(r"\s+", " ", cleaned)                       # collapse whitespace
    return cleaned.strip()[:max_len]


def _encode_image_for_claude(img_path: Path, max_side: int = CLAUDE_IMG_SIDE) -> str:
    """Resize a chunk PNG and return a base64-encoded JPEG string for Claude's vision API."""
    import io
    from PIL import Image
    img = Image.open(img_path).convert("RGB")
    w, h = img.size
    if max(w, h) > max_side:
        scale = max_side / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


def build_vector_store(force: bool = False):
    """Build or load the dual-encoder vector store.

    Dual-encoder strategy:
      - Image chunks  → CLIP image encoder (512-dim) → embeddings.npy
      - Text chunks   → MiniLM text encoder (384-dim) → text_embeddings.npy

    At query time, the question is encoded with:
      - CLIP text encoder → searched against image embeddings (finds relevant figures)
      - MiniLM            → searched against text embeddings (finds relevant passages)

    Results from both searches are merged by cosine score, giving accurate
    text retrieval (MiniLM) alongside visual diagram retrieval (CLIP).

    Returns a vs dict with keys:
        clip_embedder, text_embedder, img_vecs, txt_vecs,
        img_indices, txt_indices, store
    """
    import numpy as np
    from sentence_transformers import SentenceTransformer, CrossEncoder
    from PIL import Image

    VECTOR_DIR.mkdir(exist_ok=True)

    clip_embedder = SentenceTransformer(EMBEDDING_MODEL)
    text_embedder = SentenceTransformer(TEXT_EMBEDDING_MODEL)
    reranker      = CrossEncoder(RERANK_MODEL)

    store_exists = (
        VECTORS_FILE.exists() and TEXT_VECTORS_FILE.exists()
        and IMG_INDICES_FILE.exists() and TXT_INDICES_FILE.exists()
        and DOCS_FILE.exists()
    )

    if store_exists and not force:
        print("Loading existing dual-encoder vector store...")
        img_vecs    = np.load(VECTORS_FILE)
        txt_vecs    = np.load(TEXT_VECTORS_FILE)
        img_indices = json.loads(IMG_INDICES_FILE.read_text())
        txt_indices = json.loads(TXT_INDICES_FILE.read_text())
        store       = json.loads(DOCS_FILE.read_text(encoding="utf-8"))
        current     = _current_parse_files()
        indexed     = _indexed_files()
        new_count   = len(set(current) - set(indexed))
        if new_count:
            print(f"  ⚠  {new_count} new parse file(s) not yet indexed — run with --rebuild to update")
        print(f"✓ {len(store)} chunks loaded "
              f"({img_vecs.shape[0]} image/CLIP + {txt_vecs.shape[0]} text/MiniLM)")
        return {"clip_embedder": clip_embedder, "text_embedder": text_embedder,
                "reranker": reranker,
                "img_vecs": img_vecs, "txt_vecs": txt_vecs,
                "img_indices": img_indices, "txt_indices": txt_indices, "store": store}

    current   = _current_parse_files()
    image_map = _build_chunk_image_map()
    print("Building dual-encoder vector store from parse results...")
    chunks = _load_all_chunks()
    print(f"  Found {len(chunks)} chunks across {len(current)} parse files")

    # Deduplicate on text before encoding to avoid embedding duplicates.
    # Pass a dummy index array — we only need the deduped chunks and removed_ids.
    chunks, _, removed_ids = _deduplicate_chunks(chunks, np.arange(len(chunks)))
    _delete_orphaned_images(removed_ids)

    # Separate deduplicated chunks into image and text groups.
    # - Image chunks (figure, logo, scan_code, etc.): CLIP image encoder (512-dim).
    # - Table chunks: MiniLM text encoder using ADE HTML text, even if a PNG exists.
    #   Tables are text-dense; CLIP visual similarity is poor for tabular content.
    #   Their PNG is still indexed in image_map for UI preview and lightbox.
    # - Text chunks (no PNG): MiniLM text encoder (384-dim).
    img_store_indices, txt_store_indices = [], []
    img_pil_inputs,    txt_inputs        = [], []
    for i, chunk in enumerate(chunks):
        img_path = image_map.get(chunk["id"])
        if img_path and img_path.exists() and chunk["chunk_type"] != "table":
            img_store_indices.append(i)
            img_pil_inputs.append(Image.open(img_path).convert("RGB"))
        else:
            txt_store_indices.append(i)
            # Strip HTML tags from table chunks before embedding so MiniLM encodes
            # cell values rather than <table><tr><td id="..."> noise. Text-only
            # chunks contain plain markdown and need no stripping.
            raw = chunk["text"]
            txt_inputs.append(_strip_html(raw) if chunk["chunk_type"] == "table" else raw)

    # Encode image chunks with CLIP image encoder (512-dim).
    if img_pil_inputs:
        print(f"  Encoding {len(img_pil_inputs)} image chunks (CLIP image encoder, 512-dim)...")
        img_vecs = clip_embedder.encode(img_pil_inputs, show_progress_bar=True,
                                        convert_to_numpy=True, batch_size=32)
    else:
        img_vecs = np.zeros((0, 512), dtype=np.float32)

    # Encode text chunks with MiniLM (384-dim — optimised for dense text retrieval).
    if txt_inputs:
        txt_dim = text_embedder.get_sentence_embedding_dimension() or 384
        print(f"  Encoding {len(txt_inputs)} text chunks (MiniLM, {txt_dim}-dim)...")
        txt_vecs = text_embedder.encode(txt_inputs, show_progress_bar=True,
                                        convert_to_numpy=True, batch_size=128)
    else:
        txt_vecs = np.zeros((0, 384), dtype=np.float32)

    np.save(VECTORS_FILE, img_vecs)
    np.save(TEXT_VECTORS_FILE, txt_vecs)
    IMG_INDICES_FILE.write_text(json.dumps(img_store_indices))
    TXT_INDICES_FILE.write_text(json.dumps(txt_store_indices))
    DOCS_FILE.write_text(json.dumps(chunks, indent=2), encoding="utf-8")
    INDEXED_FILE.write_text(json.dumps(current, indent=2), encoding="utf-8")
    print(f"✓ Dual-encoder vector store saved: {len(chunks)} unique chunks "
          f"({len(img_pil_inputs)} image/CLIP-512 + {len(txt_inputs)} text/MiniLM-384)")
    return {"clip_embedder": clip_embedder, "text_embedder": text_embedder,
            "reranker": reranker,
            "img_vecs": img_vecs, "txt_vecs": txt_vecs,
            "img_indices": img_store_indices, "txt_indices": txt_store_indices,
            "store": chunks}


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — SEARCH + CLAUDE GENERATION
# ══════════════════════════════════════════════════════════════════════════════

def similarity_search(vs: dict, question: str, top_k: int = 5) -> list[dict]:
    """Dual-encoder retrieval with cross-encoder reranking.

    Stage 1 — Reciprocal Rank Fusion (RRF):
      CLIP text encoder searches image chunks; MiniLM searches text chunks.
      RRF merges by rank position (not raw cosine score, which is incomparable
      across encoders). Fetches RERANK_CANDIDATES (40) candidates — a wider net
      for the reranker to work from.

    Stage 2 — Cross-encoder reranking:
      ms-marco-MiniLM-L6-v2 scores each (query, chunk_text) pair together in a
      single forward pass, producing a true relevance score rather than a vector
      proximity approximation. ADE confidence is blended as a small soft boost
      (CONF_WEIGHT=0.15) to prefer better-parsed chunks when scores are close.
      Figures (confidence=None) receive a neutral factor of 1.0.

    Both scores are returned per hit:
      - `rerank_score`: the Stage 2 cross-encoder score (raw logit). Used for final
        ranking. The UI converts it to a % badge via sigmoid (1/(1+e^-score)), which
        maps logits to 0–100% without breaking when scores are negative. This is the
        honest relevance signal; cosine scores are incomparable across encoders.
      - `similarity`: the raw Stage 1 cosine score, preserved for the Claude image
        selection threshold (CLAUDE_IMG_MIN_SIM) which needs a stable 0–1 value.
    """
    import numpy as np

    clip_embedder = vs["clip_embedder"]
    text_embedder = vs["text_embedder"]
    reranker      = vs["reranker"]
    img_vecs      = vs["img_vecs"]
    txt_vecs      = vs["txt_vecs"]
    img_indices   = vs["img_indices"]
    txt_indices   = vs["txt_indices"]
    store         = vs["store"]

    RRF_K = 60          # standard constant; higher = flatter rank-weight curve
    rrf:       dict[int, float] = {}
    raw_score: dict[int, float] = {}  # cosine scores kept for UI display

    # Stage 1a — Search image chunks with CLIP text encoder.
    if img_vecs.shape[0] > 0:
        q_clip = clip_embedder.encode([question], convert_to_numpy=True)[0]
        q_norm = q_clip / (np.linalg.norm(q_clip) + 1e-10)
        m_norm = img_vecs / (np.linalg.norm(img_vecs, axis=1, keepdims=True) + 1e-10)
        scores = m_norm @ q_norm
        for rank, (row_i, score) in enumerate(sorted(enumerate(scores), key=lambda x: x[1], reverse=True)):
            store_i = img_indices[row_i]
            rrf[store_i]       = rrf.get(store_i, 0.0) + 1.0 / (RRF_K + rank + 1)
            raw_score[store_i] = float(score)

    # Stage 1b — Search text chunks with MiniLM.
    if txt_vecs.shape[0] > 0:
        q_mini = text_embedder.encode([question], convert_to_numpy=True)[0]
        q_norm = q_mini / (np.linalg.norm(q_mini) + 1e-10)
        m_norm = txt_vecs / (np.linalg.norm(txt_vecs, axis=1, keepdims=True) + 1e-10)
        scores = m_norm @ q_norm
        for rank, (row_i, score) in enumerate(sorted(enumerate(scores), key=lambda x: x[1], reverse=True)):
            store_i = txt_indices[row_i]
            rrf[store_i]       = rrf.get(store_i, 0.0) + 1.0 / (RRF_K + rank + 1)
            raw_score[store_i] = float(score)

    ranked     = sorted(rrf.items(), key=lambda x: x[1], reverse=True)
    candidates = [{**store[i], "similarity": raw_score[i]} for i, _ in ranked[:RERANK_CANDIDATES]]

    # Stage 2 — Cross-encoder reranking over the top RERANK_CANDIDATES.
    # Pairs (question, chunk_text) are scored jointly — far more accurate than
    # bi-encoder cosine proximity for ranking the final top-k.
    pairs     = [(question, c["text"]) for c in candidates]
    ce_scores = reranker.predict(pairs)
    for c, ce_score in zip(candidates, ce_scores):
        conf        = c.get("confidence")
        conf_factor = 1.0 + CONF_WEIGHT * conf if conf is not None else 1.0
        c["rerank_score"] = float(ce_score) * conf_factor

    candidates.sort(key=lambda c: c["rerank_score"], reverse=True)
    return candidates[:top_k]


def _context_from_hits(hits: list[dict]) -> str:
    return "\n\n---\n\n".join(
        f"[{h['_doc_stem']} | Page {h['page'] + 1} | {h['chunk_type']}]\n{h['text']}"
        for h in hits
    )


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — TERMINAL MODE
# ══════════════════════════════════════════════════════════════════════════════

def print_image_iterm2(image_path: Path, width: str = "40%") -> None:
    data    = base64.b64encode(image_path.read_bytes()).decode("ascii")
    payload = f"\033]1337;File=inline=1;width={width};preserveAspectRatio=1:{data}\a"
    sys.stdout.write(payload)
    sys.stdout.flush()


def ask_terminal(vs, image_map, question: str) -> None:
    import anthropic
    hits    = similarity_search(vs, question)
    context = _context_from_hits(hits)

    print(f"\nYou: {question}")
    print("\nClaude: ", end="", flush=True)

    client = anthropic.Anthropic()
    with client.messages.stream(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        system=(
            "You are a technical support assistant for GE Appliances HVAC products. "
            "Answer questions using only the retrieved context from GE HVAC product manuals, "
            "installation guides, service manuals, and spec sheets below. "
            "If the answer is not in the context, say so.\n\n"
            f"{context}"
        ),
        messages=[{"role": "user", "content": question}],
    ) as stream:
        for text in stream.text_stream:
            print(text, end="", flush=True)
    print()

    sources = [h for h in hits if h["similarity"] >= IMAGE_THRESHOLD]
    if sources:
        print(f"\n{'─'*60}\nSOURCES ({len(sources)} chunks)\n{'─'*60}")
        for i, h in enumerate(sources):
            print(f"\n  [{i+1}] {h['_doc_stem']} | p{h['page']+1} | {h['chunk_type']} | sim={h['similarity']:.3f}")
            print(f"       {h['text'][:120]}{'...' if len(h['text']) > 120 else ''}")
            img = image_map.get(h["id"])
            if img:
                print_image_iterm2(img)


def run_terminal(vs, image_map):
    print("\n" + "─" * 60)
    print("GE HVAC Manuals RAG  (Claude opus-4-6 + LandingAI ADE)")
    print("Type your question. 'quit' to exit.")
    print("─" * 60)
    while True:
        try:
            question = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break
        if not question:
            continue
        if question.lower() in ("quit", "exit", "q"):
            print("Goodbye!")
            break
        ask_terminal(vs, image_map, question)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 6 — FLASK WEB APP
# ══════════════════════════════════════════════════════════════════════════════

def make_flask_app(vs, image_map):
    import anthropic
    from flask import Flask, Response, render_template, send_file, stream_with_context, request, make_response

    flask_app  = Flask(__name__, template_folder=str(BASE_DIR / "templates"))
    flask_app.config["TEMPLATES_AUTO_RELOAD"] = True
    ant_client = anthropic.Anthropic()

    @flask_app.route("/")
    def index():
        r = make_response(render_template("index.html"))
        r.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return r

    @flask_app.route("/claude-logo-cropped.png")
    def claude_logo():
        return send_file(BASE_DIR / "claude-logo-cropped.png", mimetype="image/png")

    @flask_app.route("/api/docs/<folder>")
    def api_docs_folder(folder):
        allowed = {"connect_series"}
        if folder not in allowed:
            return ("Not found", 404)
        folder_path = BASE_DIR / "ge_hvac_manuals" / folder
        if not folder_path.exists():
            return json.dumps([]), 200, {"Content-Type": "application/json"}
        files = sorted([
            f.name for f in folder_path.iterdir()
            if f.suffix.lower() in (".pdf", ".ppt", ".pptx")
        ])
        return json.dumps(files), 200, {"Content-Type": "application/json"}

    @flask_app.route("/pdf/<folder>/<path:filename>")
    def serve_pdf(folder, filename):
        allowed = {"connect_series"}
        if folder not in allowed:
            return ("Not found", 404)
        pdf_path = BASE_DIR / "ge_hvac_manuals" / folder / filename
        if not pdf_path.exists():
            return ("Not found", 404)
        return send_file(pdf_path, mimetype="application/pdf")

    @flask_app.route("/chunk_images/<path:filename>")
    def chunk_image(filename):
        full_path = CHUNK_IMAGES / filename
        if full_path.exists():
            return send_file(full_path)
        return ("Not found", 404)

    @flask_app.route("/page_render")
    def page_render():
        """Render a PDF page at 150 DPI with the chunk bbox highlighted.

        Query params: doc (stem), category, page (0-indexed), left/top/right/bottom (normalized 0-1).
        Returns JSON: {image: <base64 JPEG>}.
        """
        import fitz as _fitz, io as _io, base64 as _b64
        from PIL import Image as _Image, ImageDraw as _Draw

        doc_stem = request.args.get("doc", "")
        category = request.args.get("category", "connect_series")
        page     = int(request.args.get("page", 0))
        left     = float(request.args.get("left",   0))
        top      = float(request.args.get("top",    0))
        right    = float(request.args.get("right",  1))
        bottom   = float(request.args.get("bottom", 1))

        pdf_dir  = INPUT_DIR / category
        pdf_path = pdf_dir / f"{doc_stem}.pdf"
        if not pdf_path.exists():
            for f in pdf_dir.iterdir():
                if f.suffix.lower() == ".pdf" and f.stem == doc_stem:
                    pdf_path = f
                    break
            else:
                return ({"error": "PDF not found"}, 404)

        try:
            doc_obj = _fitz.open(pdf_path)
            if page >= len(doc_obj):
                return ({"error": "Page out of range"}, 400)
            scale = 150 / 72  # 150 DPI
            pix   = doc_obj[page].get_pixmap(matrix=_fitz.Matrix(scale, scale))
            img   = _Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            doc_obj.close()

            w, h = img.size
            x1, y1 = int(left * w), int(top * h)
            x2, y2 = int(right * w), int(bottom * h)

            overlay = _Image.new("RGBA", img.size, (0, 0, 0, 0))
            draw    = _Draw.Draw(overlay)
            draw.rectangle([x1, y1, x2, y2], fill=(255, 200, 0, 55))
            draw.rectangle([x1, y1, x2, y2], outline=(255, 130, 0, 230), width=3)
            img = _Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")

            buf = _io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            return {"image": _b64.b64encode(buf.getvalue()).decode()}
        except Exception as e:
            return ({"error": str(e)}, 500)

    @flask_app.route("/readme")
    def readme():
        content = (BASE_DIR / "README.md").read_text(encoding="utf-8")
        # Strip HuggingFace frontmatter block (--- ... ---)
        if content.startswith("---"):
            end = content.find("---", 3)
            if end != -1:
                content = content[end + 3:].lstrip("\n")
        return content, 200, {"Content-Type": "text/plain; charset=utf-8"}

    @flask_app.route("/health")
    def health():
        return {"status": "ok"}, 200

    @flask_app.route("/ask")
    def ask_web():
        question = request.args.get("q", "").strip()
        if not question:
            return Response('data: {"error": "No question provided"}\n\n', mimetype="text/event-stream")

        def generate():
            try:
                hits    = similarity_search(vs, question)
                context = _context_from_hits(hits)

                # Send raw cross-encoder logit as rerank_score; sigmoid is applied
                # in the UI (1/(1+e^-score)) to convert to 0–100%. Division-based
                # normalization breaks when all logits are negative (dividing by a
                # negative max inverts the ordering and produces values >100%).
                # cosine similarity is kept separately for CLAUDE_IMG_MIN_SIM.
                sources = []
                for h in hits:
                    img     = image_map.get(h["id"])
                    img_url = f"/chunk_images/{img.relative_to(CHUNK_IMAGES)}" if img else None
                    sources.append({
                        "id":         h.get("id", ""),
                        "similarity":    round(h["similarity"], 3),
                        "rerank_score":  round(h.get("rerank_score", 0), 4),
                        "chunk_type": h["chunk_type"],
                        "page":       h["page"] + 1,
                        "text":       _display_text(h["text"]),
                        "raw_text":   re.sub(r"<::(.*?)::>", r"\1", h["text"], flags=re.DOTALL).strip(),
                        "image_url":  img_url,
                        "doc":        h["_doc_stem"],
                        "category":   h["_category"],
                        "bbox":       h.get("bbox", {}),
                        "confidence": h.get("confidence"),
                    })

                yield f"data: {json.dumps({'type': 'sources', 'sources': sources})}\n\n"

                # Build multimodal user message: send up to MAX_CLAUDE_IMGS images —
                # one per image chunk that meets the minimum similarity threshold,
                # in ranked order. Text-only chunks and low-similarity hits are
                # excluded. If no hits qualify, only the text question is sent.
                user_content = []
                for h in hits:
                    if (h.get("similarity", 0) >= CLAUDE_IMG_MIN_SIM
                            and image_map.get(h["id"])
                            and len(user_content) < MAX_CLAUDE_IMGS):
                        try:
                            img_b64 = _encode_image_for_claude(image_map[h["id"]])
                            user_content.append({
                                "type": "image",
                                "source": {
                                    "type":       "base64",
                                    "media_type": "image/jpeg",
                                    "data":       img_b64,
                                },
                            })
                        except Exception:
                            pass
                user_content.append({"type": "text", "text": question})

                with ant_client.messages.stream(
                    model=CLAUDE_MODEL,
                    max_tokens=1024,
                    system=(
                        "You are a technical support assistant for GE Connect Series HVAC products. "
                        "Answer questions using the retrieved context and the chunk images provided. "
                        "The images are visual excerpts (page crops) from GE Connect Series service "
                        "manuals and installation guides — refer to them when answering. "
                        "If the answer is not in the context or images, say so.\n\n"
                        f"{context}"
                    ),
                    messages=[{"role": "user", "content": user_content}],
                ) as stream:
                    for text in stream.text_stream:
                        yield f"data: {json.dumps({'type': 'token', 'text': text})}\n\n"

                yield f"data: {json.dumps({'type': 'done'})}\n\n"

            except Exception as e:
                import traceback
                print(traceback.format_exc(), flush=True)
                yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"

        return Response(
            stream_with_context(generate()),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return flask_app


def run_web(vs, image_map):
    import os
    flask_app = make_flask_app(vs, image_map)
    port = int(os.environ.get("PORT", 8080))
    print(f"\n" + "─" * 60)
    print(f"GE HVAC Manuals RAG  →  http://localhost:{port}")
    print("─" * 60 + "\n")
    flask_app.run(debug=False, host="0.0.0.0", port=port, threaded=True)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 7 — ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def main():
    args       = sys.argv[1:]
    flags      = {a for a in args if a.startswith("--")}
    positional = [a for a in args if not a.startswith("--")]

    # ── Download mode ─────────────────────────────────────────────────────────
    if "--download" in flags:
        run_download()
        return

    # ── Parse mode ────────────────────────────────────────────────────────────
    if "--parse" in flags:
        parse_all = "--all" in flags
        force     = "--force" in flags
        limit = 2
        for arg in args:
            if arg.startswith("--limit="):
                limit = int(arg.split("=")[1])
            elif arg == "--limit":
                idx = args.index(arg)
                if idx + 1 < len(args):
                    limit = int(args[idx + 1])
        run_parse(parse_all=parse_all, limit=limit, force=force)
        return

    # ── RAG modes — build shared resources ───────────────────────────────────
    force_rebuild = "--rebuild" in flags
    if force_rebuild and (not PARSE_RESULTS.exists() or not any(PARSE_RESULTS.rglob("*.txt"))):
        print("No parse results found. Run 'python web_app.py --parse' first.")
        sys.exit(1)
    vs        = build_vector_store(force=force_rebuild)
    image_map = _build_chunk_image_map()
    print(f"✓ {len(image_map)} chunk images indexed")

    # ── One-shot CLI ──────────────────────────────────────────────────────────
    if positional:
        for q in positional:
            ask_terminal(vs, image_map, q)
        return

    # ── Terminal mode ─────────────────────────────────────────────────────────
    if "--terminal" in flags:
        run_terminal(vs, image_map)
        return

    # ── Web mode (default) ────────────────────────────────────────────────────
    run_web(vs, image_map)


if __name__ == "__main__":
    main()
