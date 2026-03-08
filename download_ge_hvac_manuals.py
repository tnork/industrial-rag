#!/usr/bin/env python3
"""
GE Appliances HVAC — Product Manuals & Instructions Downloader
==============================================================
Downloads product manuals, installation guides, service manuals, owner's
manuals, and spec sheets for GE Appliances HVAC / Ductless product lines,
with primary focus on current (2023-2026) product series.

Product lines covered:
  Connect Series    │ Variable-speed split heat pumps (premium)     │ connect_series/
  Endure Series     │ Single-zone ductless mini-splits              │ endure_series/
  Endure Multi      │ Multi-zone ductless systems                   │ endure_multi/
  Altitude Series   │ High-elevation ductless heat pumps            │ altitude_series/
  Window / Room AC  │ Room air conditioners                         │ room_ac/
  Model-Specific    │ Per-model support docs fetched from GEA API   │ model_specific/

Sources:
  1. GE Appliances ductless downloads CDN   (geappliances.com/content/ductless/downloads/)
  2. GE Appliances Haier CDN                (haierappliances.com/content/downloads/ductless/)
  3. GE Appliances HVAC brochure CDN        (geappliances.com/content/hvac/downloads/)
  4. GEA products support API               (products.geappliances.com)

Requirements:
  pip install requests beautifulsoup4

Usage:
  python download_ge_hvac_manuals.py

Output:
  ge_hvac_manuals/          ← organised folder tree
  ge_hvac_manuals.zip
"""

import csv
import json
import os
import re
import time
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote

import requests
from bs4 import BeautifulSoup

# ── Config ──────────────────────────────────────────────────────────────────
OUTPUT_DIR  = Path("ge_hvac_manuals")
ZIP_NAME    = "ge_hvac_manuals.zip"
SLEEP       = 0.3               # seconds between requests — be polite
TIMEOUT     = 30

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/pdf,*/*",
}

# ── Known Direct PDF URLs ────────────────────────────────────────────────────
# All confirmed-accessible PDFs discovered from GE Appliances CDN and Haier CDN.
# Organised by product series. "date" = approximate document date (YYYY-MM).

GEA_BASE    = "https://www.geappliances.com/content/ductless/downloads/"
HAIER_BASE  = "https://www.haierappliances.com/content/downloads/ductless/"
HVAC_BASE   = "https://www.geappliances.com/content/hvac/downloads/"

KNOWN_DOCS = [

    # ── Connect Series (premium variable-speed) ──────────────────────────────
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

    # ── Endure Series Multi-Zone ────────────────────────────────────────────
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

    # ── Altitude Series ─────────────────────────────────────────────────────
    {
        "series":   "Altitude Series",
        "folder":   "altitude_series",
        "doc_type": "Service Manual",
        "date":     "2019-06",
        "url":      GEA_BASE + "Altitude%20Series/GE%20Altitute%20Series%20-%20Service%20Manual%2006-27-19.pdf",
    },

    # ── Haier / GE Ductless CDN ─────────────────────────────────────────────
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

    # ── Residential HVAC Brochures / Homeowner Guides ───────────────────────
    {
        "series":   "Residential HVAC",
        "folder":   "brochures",
        "doc_type": "Homeowner Brochure",
        "date":     "2022-08",
        "url":      HVAC_BASE + "GE-Residential-HVAC-Homeowner%20Brochure-83122.pdf",
    },
]

# ── Current GEA Product Model Numbers (2023–2026) ───────────────────────────
# Model numbers for current Connect and Endure series products.
# The script fetches their support pages and scrapes linked PDF manuals.
# Source: GE Appliances website product catalog.

CURRENT_HVAC_MODELS = [
    # Connect Series — split type variable-speed heat pumps
    "AUH2436ZGDA",   # 24k–36k BTU outdoor unit
    "AUH4860ZGDA",   # 48k–60k BTU outdoor unit
    "UUY09ZGDAA",    # 9k BTU indoor
    "UUY12ZGDAA",    # 12k BTU indoor
    "UUY18ZGDAA",    # 18k BTU indoor
    "UUY24ZGDAA",    # 24k BTU indoor
    "UUY36ZGDAA",    # 36k BTU indoor
    "UUY48ZGDAA",    # 48k BTU indoor
    "UUY60ZGDAA",    # 60k BTU indoor

    # Endure Series — single-zone ductless mini-splits
    "ASH109URDEB",   # 9k BTU outdoor
    "ASH112URDSE",   # 12k BTU outdoor
    "ASH118CRDWA",   # 18k BTU outdoor
    "ASH124CRDWA",   # 24k BTU outdoor
    "AHH09LXW1",     # 9k BTU indoor (highwall)
    "AHH12LXW1",     # 12k BTU indoor
    "AHH18LXW1",     # 18k BTU indoor
    "AHH24LXW1",     # 24k BTU indoor

    # Endure Multi-Zone outdoor units
    "ASH424NCDWA",   # 4-zone 24k BTU
    "ASH436NCDWA",   # 4-zone 36k BTU
    "ASH448NCDWA",   # 4-zone 48k BTU
]

GEA_PRODUCT_SUPPORT_URL = "https://products.geappliances.com/appliance/gea-specs/{model}/support"
GEA_PRODUCT_DOCS_API    = "https://products.geappliances.com/api/products/{model}/documents"


# ── Helpers ─────────────────────────────────────────────────────────────────

def get(url: str, retries: int = 3, stream: bool = False, **kwargs):
    """GET with retry + polite sleep."""
    for attempt in range(retries):
        try:
            r = requests.get(
                url, headers=HEADERS, timeout=TIMEOUT,
                stream=stream, **kwargs
            )
            time.sleep(SLEEP)
            if r.status_code == 200:
                return r
            if r.status_code in (403, 404):
                print(f"    ✗  HTTP {r.status_code}: {url}")
                return None
            print(f"    ⚠  HTTP {r.status_code} (attempt {attempt+1}): {url}")
        except requests.exceptions.ConnectionError:
            print(f"    ✗  Connection error: {url}")
            return None
        except Exception as exc:
            print(f"    ⚠  Error (attempt {attempt+1}): {exc}")
            time.sleep(2 ** attempt)
    return None


def slugify(text: str) -> str:
    """Clean filename from URL path or description."""
    text = unquote(text)
    text = re.sub(r"[^\w\-. ]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text[:120]


def download_pdf(url: str, dest_path: Path) -> bool:
    """Download a single file. Returns True on success."""
    if dest_path.exists() and dest_path.stat().st_size > 500:
        print(f"    ↩  Cached: {dest_path.name}")
        return True
    r = get(url, stream=True)
    if not r:
        return False
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_bytes(r.content)
    size_kb = dest_path.stat().st_size / 1024
    print(f"    ✓  {dest_path.name}  ({size_kb:.0f} KB)")
    return True


def filename_from_url(url: str) -> str:
    """Derive a clean filename from a URL."""
    path = urlparse(url).path
    name = Path(path).name
    return slugify(name) if name else "document.pdf"


# ── Section 1: Download all known static PDFs ───────────────────────────────

def download_known_docs() -> list[dict]:
    """Download every entry in KNOWN_DOCS. Returns manifest rows."""
    print("\n" + "═" * 65)
    print("  SECTION 1 — Known Static PDFs from GEA/Haier CDNs")
    print("═" * 65)
    manifest = []
    for doc in KNOWN_DOCS:
        folder  = OUTPUT_DIR / doc["folder"]
        folder.mkdir(parents=True, exist_ok=True)
        fname   = filename_from_url(doc["url"])
        dest    = folder / fname
        print(f"\n  [{doc['series']}]  {doc['doc_type']}  ({doc['date']})")
        print(f"    URL: {doc['url']}")
        ok = download_pdf(doc["url"], dest)
        manifest.append({
            "series":   doc["series"],
            "doc_type": doc["doc_type"],
            "date":     doc["date"],
            "url":      doc["url"],
            "local_file": str(dest) if ok else "",
            "status":   "ok" if ok else "failed",
        })
    return manifest


# ── Section 2: Model-specific manuals via GEA products API ──────────────────

def fetch_model_docs(model: str) -> list[dict]:
    """
    Try multiple approaches to discover document URLs for a given model number:
    1. JSON documents API
    2. HTML support page scraping

    Returns list of {url, doc_type, title}.
    """
    docs = []

    # Approach A: JSON documents API
    api_url = GEA_PRODUCT_DOCS_API.format(model=model)
    r = get(api_url)
    if r:
        try:
            data = r.json()
            # Handle array or dict response shapes
            items = data if isinstance(data, list) else data.get("documents", data.get("docs", []))
            for item in items:
                url = item.get("url") or item.get("pdfUrl") or item.get("path")
                if url and url.lower().endswith(".pdf"):
                    if not url.startswith("http"):
                        url = "https://products.geappliances.com" + url
                    docs.append({
                        "url":      url,
                        "doc_type": item.get("type", item.get("docType", "Manual")),
                        "title":    item.get("title", item.get("name", "")),
                    })
        except Exception:
            pass

    # Approach B: Scrape HTML support page
    if not docs:
        support_url = GEA_PRODUCT_SUPPORT_URL.format(model=model)
        r = get(support_url)
        if r and r.text:
            soup = BeautifulSoup(r.text, "html.parser")
            # Look for PDF links
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if ".pdf" in href.lower():
                    full_url = urljoin(support_url, href)
                    label = a.get_text(strip=True) or "Manual"
                    docs.append({
                        "url":      full_url,
                        "doc_type": label,
                        "title":    label,
                    })
            # Also check for JSON embedded in page source
            m = re.search(r'"documents"\s*:\s*(\[.*?\])', r.text, re.DOTALL)
            if m:
                try:
                    items = json.loads(m.group(1))
                    for item in items:
                        url = item.get("url") or item.get("pdfUrl")
                        if url and ".pdf" in url.lower():
                            if not url.startswith("http"):
                                url = "https://products.geappliances.com" + url
                            docs.append({
                                "url":      url,
                                "doc_type": item.get("type", "Manual"),
                                "title":    item.get("title", ""),
                            })
                except Exception:
                    pass

    # Deduplicate by URL
    seen = set()
    unique = []
    for d in docs:
        if d["url"] not in seen:
            seen.add(d["url"])
            unique.append(d)
    return unique


def download_model_docs(models: list[str]) -> list[dict]:
    """Download model-specific manuals via GEA API. Returns manifest rows."""
    print("\n" + "═" * 65)
    print("  SECTION 2 — Model-Specific Manuals via GEA Products API")
    print("═" * 65)
    manifest = []
    model_dir = OUTPUT_DIR / "model_specific"
    model_dir.mkdir(parents=True, exist_ok=True)

    for model in models:
        print(f"\n  Model: {model}")
        docs = fetch_model_docs(model)

        if not docs:
            print(f"    ℹ  No PDF documents found via API for {model}")
            manifest.append({
                "series":    "Model-Specific",
                "doc_type":  "N/A",
                "date":      "",
                "url":       "",
                "local_file": "",
                "status":    "no_docs",
                "model":     model,
            })
            continue

        for doc in docs:
            fname = f"{model}_{filename_from_url(doc['url'])}"
            dest  = model_dir / fname
            print(f"    [{doc['doc_type']}]  {doc['title']}")
            print(f"    URL: {doc['url']}")
            ok = download_pdf(doc["url"], dest)
            manifest.append({
                "series":    "Model-Specific",
                "doc_type":  doc["doc_type"],
                "date":      "",
                "url":       doc["url"],
                "local_file": str(dest) if ok else "",
                "status":   "ok" if ok else "failed",
                "model":     model,
            })
    return manifest


# ── Section 3: Crawl GEA ductless downloads directory ───────────────────────

CRAWL_DIRS = [
    # (label, base_url, series_folder)
    ("Connect Series",    GEA_BASE + "GE%20Connect%20Series/",    "connect_series"),
    ("Endure Multi",      GEA_BASE + "Endure%20Series%20Multi/",   "endure_multi"),
    ("Altitude Series",   GEA_BASE + "Altitude%20Series/",         "altitude_series"),
    ("Haier GE Ductless", GEA_BASE,                                "root_downloads"),
    ("Haier CDN Ductless",HAIER_BASE,                              "haier_ductless"),
    ("Haier Tempo",       HAIER_BASE + "Tempo-Series/",            "tempo_series"),
]


def crawl_directory(label: str, dir_url: str, local_folder: str) -> list[dict]:
    """
    Attempt to list a GEA CDN directory (HTML index) and download all PDFs.
    GE CDNs may not expose directory listings; this tries anyway.
    Returns manifest rows.
    """
    manifest = []
    print(f"\n  Crawling: {label}  →  {dir_url}")
    r = get(dir_url)
    if not r:
        print(f"    ✗  Directory not accessible (expected — no open listing)")
        return manifest

    soup = BeautifulSoup(r.text, "html.parser")
    pdf_links = [
        a["href"] for a in soup.find_all("a", href=True)
        if ".pdf" in a["href"].lower()
    ]

    if not pdf_links:
        print(f"    ℹ  No PDF links found (directory listing likely disabled)")
        return manifest

    print(f"    Found {len(pdf_links)} PDF link(s)")
    dest_dir = OUTPUT_DIR / local_folder
    dest_dir.mkdir(parents=True, exist_ok=True)

    for href in pdf_links:
        full_url = urljoin(dir_url, href)
        fname = filename_from_url(full_url)
        dest  = dest_dir / fname
        print(f"    ↓  {fname}")
        ok = download_pdf(full_url, dest)
        manifest.append({
            "series":    label,
            "doc_type":  "PDF (crawled)",
            "date":      "",
            "url":       full_url,
            "local_file": str(dest) if ok else "",
            "status":   "ok" if ok else "failed",
        })
    return manifest


def crawl_all_directories() -> list[dict]:
    """Crawl all known GEA directory URLs. Returns manifest rows."""
    print("\n" + "═" * 65)
    print("  SECTION 3 — Directory Crawl (discovers additional PDFs)")
    print("═" * 65)
    manifest = []
    seen_urls = set()
    for label, dir_url, folder in CRAWL_DIRS:
        rows = crawl_directory(label, dir_url, folder)
        for row in rows:
            if row["url"] not in seen_urls:
                seen_urls.add(row["url"])
                manifest.append(row)
    return manifest


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("═" * 65)
    print("  GE Appliances HVAC — Product Manuals Downloader")
    print(f"  Run date : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("═" * 65)

    OUTPUT_DIR.mkdir(exist_ok=True)

    all_manifest = []

    # 1. Download known static PDFs
    all_manifest += download_known_docs()

    # 2. Model-specific API lookup
    all_manifest += download_model_docs(CURRENT_HVAC_MODELS)

    # 3. Directory crawl (discovers any additional public PDFs)
    all_manifest += crawl_all_directories()

    # ── Write manifest CSV ───────────────────────────────────────────────────
    manifest_path = OUTPUT_DIR / "manifest.csv"
    fields = ["series", "doc_type", "date", "url", "local_file", "status"]
    with open(manifest_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(all_manifest)
    print(f"\n  Manifest saved → {manifest_path}")

    # ── Write README ─────────────────────────────────────────────────────────
    readme_path = OUTPUT_DIR / "README.txt"
    ok_count   = sum(1 for r in all_manifest if r["status"] == "ok")
    fail_count = sum(1 for r in all_manifest if r["status"] == "failed")
    with open(readme_path, "w") as f:
        f.write("GE Appliances HVAC — Product Manuals & Instructions\n")
        f.write("=" * 60 + "\n")
        f.write(f"Downloaded : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Source     : GE Appliances (geappliances.com) — public documentation\n\n")
        f.write("Folder structure:\n")
        f.write("  connect_series/   — GE Connect Series variable-speed heat pumps\n")
        f.write("  endure_multi/     — GE Endure Series multi-zone systems\n")
        f.write("  altitude_series/  — GE Altitude Series (high-elevation)\n")
        f.write("  tempo_series/     — Haier/GE Tempo Series\n")
        f.write("  brochures/        — Residential HVAC homeowner guides\n")
        f.write("  model_specific/   — Per-model manuals from GEA support API\n")
        f.write("  manifest.csv      — Full index of every file attempted\n\n")
        f.write(f"Summary:\n")
        f.write(f"  Attempted  : {len(all_manifest)}\n")
        f.write(f"  Downloaded : {ok_count}\n")
        f.write(f"  Failed     : {fail_count}\n\n")
        f.write("Notes:\n")
        f.write("  GE Appliances HVAC documentation is produced by Haier (which\n")
        f.write("  acquired GE Appliances in 2016). Many manuals span multiple model\n")
        f.write("  years. The most current 2025 Connect Series installation manual\n")
        f.write("  (Rev. 07-23) applies to all current Connect Series products.\n")

    # ── Create ZIP ───────────────────────────────────────────────────────────
    print(f"\nCreating {ZIP_NAME} …")
    with zipfile.ZipFile(ZIP_NAME, "w", zipfile.ZIP_DEFLATED) as zf:
        for fp in sorted(OUTPUT_DIR.rglob("*")):
            if fp.is_file():
                zf.write(fp, fp.relative_to(OUTPUT_DIR.parent))

    zip_mb = Path(ZIP_NAME).stat().st_size / (1024 * 1024)

    # ── Summary ──────────────────────────────────────────────────────────────
    print("\n" + "═" * 65)
    print("  ✅  COMPLETE")
    print(f"  Zip  : {ZIP_NAME}  ({zip_mb:.1f} MB)")
    print(f"  Files attempted : {len(all_manifest)}")
    print(f"  Downloaded      : {ok_count}")
    print(f"  Failed/No docs  : {fail_count}")
    print("═" * 65)
    print("\nTip: For model-specific manuals not found automatically,")
    print("visit products.geappliances.com, search your model number,")
    print("and click the 'Owner Support' tab → 'Manuals & Downloads'.")


if __name__ == "__main__":
    main()
