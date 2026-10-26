#!/usr/bin/env python3
"""
Scrape Aura Canna Co (Fall River, MA) menu via PLAYWRIGHT.

Backend (mapped live 2026-06-30):
================================
auracannaco.com runs the Leafbridge WordPress plugin. The first-paint HTML
ships only SKELETON cards; the real product cards are injected after page
load by 5 POSTs to /wp-admin/admin-ajax.php (one per widget). Each POST
returns rendered HTML fragments that the page splices in.

Because admin-ajax.php is gated by a per-session WordPress nonce and the
response is HTML (not JSON), the cleanest scraper is to load the page in
Playwright, wait for cards to render, then read the live DOM.

URL pattern:
    https://auracannaco.com/categories/<slug>/?pagination=<N>&sort=NAME_ASC
        &retailer_id=<UUID>&menu_type=RECREATIONAL
        &collection_type=PICKUP&categories=<UPPER_SLUG>

Retailer IDs (lb_retailers_details on the homepage):
    0cbf4cf0-99fb-4258-b301-63920e39a8ff  Central Falls, RI
    363f9a71-2b1d-4c20-b6c8-8423bc1e382b  Fall River, MA   <-- this scraper

This scraper covers MA ONLY (Fall River).

Per-card extraction (live DOM, document.querySelectorAll inside page.evaluate):
    productId  -> card[data-productid]
    name       -> .leafbridge_product_name
    brand      -> .leafbridge_brand_name
    sticker $  -> .leafbridge_product_price
    strainType -> .leafbridge_strain_type_label
    THC/CBD/TAC + size+price variants -> regex on the hidden modal's
                                         innerText ("THC : 18.3% - 23.1%",
                                         "1/8oz - $22", "100mg - $25")

Install once:
    pip install playwright pandas beautifulsoup4 pytz
    playwright install chromium
"""
from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
import re
import sys
from datetime import datetime

import pandas as pd
import pytz
from playwright.async_api import async_playwright


# === LOGGER =================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("aura")


# === CONFIG =================================================================

company = "AuraCannaCo"
state = "Massachusetts"

us_tz = pytz.timezone("America/New_York")
today = datetime.now(us_tz).strftime("%Y-%m-%d")

output_folder = "app/output/pending"
csv_folder = os.path.join(output_folder, "csv")
json_folder = os.path.join(output_folder, "json")
os.makedirs(csv_folder, exist_ok=True)
os.makedirs(json_folder, exist_ok=True)
output_file = os.path.join(csv_folder, f"{today}_{company}.csv")
json_file_path = os.path.join(json_folder, f"{today}_{company}.json")

BASE = "https://auracannaco.com/categories/{slug}/"

PER_PAGE = 20
MAX_PAGES = 100
NAV_TIMEOUT_MS = 30000
PAGE_RENDER_TIMEOUT_S = 18      # max seconds to wait for cards to appear

CATEGORIES = [
    ("flower",       "FLOWER"),
    ("pre_rolls",    "PRE_ROLLS"),
    ("vaporizers",   "VAPORIZERS"),
    ("edibles",      "EDIBLES"),
    ("concentrates", "CONCENTRATES"),
    ("topicals",     "TOPICALS"),
    ("tinctures",    "TINCTURES"),
    ("accessories",  "ACCESSORIES"),
]

stores = [
    {
        "retailer_id": "363f9a71-2b1d-4c20-b6c8-8423bc1e382b",
        "dispensary_location": "Fall River",
    },
]


# === GLOBAL STORAGE =========================================================

products: list[dict] = []
seen_keys: set[tuple] = set()
all_raw_cards: list[dict] = []


# === HELPERS ================================================================

_STRAIN_MAP = {
    "hybrid": "Hybrid",
    "sativa": "Sativa",
    "indica": "Indica",
    "indica hybrid": "Indica Hybrid",
    "sativa hybrid": "Sativa Hybrid",
    "cbd": "High CBD",
    "high cbd": "High CBD",
    "1:1": "1:1",
    "1 to 1": "1:1",
}

_RE_THC = re.compile(r"THC\s*:\s*([0-9.]+(?:\s*-\s*[0-9.]+)?)\s*%", re.I)
_RE_CBD = re.compile(r"CBD\s*:\s*([0-9.]+(?:\s*-\s*[0-9.]+)?)\s*%", re.I)
_RE_TAC = re.compile(r"TAC\s*[:\s]*([0-9.]+(?:\s*-\s*[0-9.]+)?)\s*%", re.I)
_RE_THC_MG = re.compile(r"THC\s*:\s*([0-9.]+)\s*mg", re.I)
_RE_CBD_MG = re.compile(r"CBD\s*:\s*([0-9.]+)\s*mg", re.I)
_RE_VARIANT = re.compile(
    r"([0-9]+(?:\.[0-9]+)?\s*(?:g|mg|oz)|1/[248]\s*oz|each)\s*-\s*\$\s*([0-9]+(?:\.[0-9]+)?)",
    re.I,
)


def resolve_strain_type(label: str) -> str:
    if not isinstance(label, str):
        return ""
    key = label.strip().lower()
    return _STRAIN_MAP.get(key, label.strip()) if key else ""


def resolve_category(slug_upper: str) -> str:
    return slug_upper.replace("_", " ").title()


def resolve_subcategory(slug_upper: str) -> str:
    return slug_upper.upper().replace("_", "-")


def _normalize_size(raw: str) -> str:
    return re.sub(r"\s+", "", raw or "").lower()


def _extract_potency(modal_text: str, kind: str) -> str:
    if not modal_text:
        return ""
    if kind == "thc":
        m = _RE_THC.search(modal_text)
        if m: return f"{m.group(1).replace(' ', '')}%"
        m = _RE_THC_MG.search(modal_text)
        if m: return f"{m.group(1)}mg"
    elif kind == "cbd":
        m = _RE_CBD.search(modal_text)
        if m: return f"{m.group(1).replace(' ', '')}%"
        m = _RE_CBD_MG.search(modal_text)
        if m: return f"{m.group(1)}mg"
    elif kind == "tac":
        m = _RE_TAC.search(modal_text)
        if m: return f"{m.group(1).replace(' ', '')}%"
    return ""


def _extract_variants(modal_text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for m in _RE_VARIANT.finditer(modal_text):
        size = _normalize_size(m.group(1))
        price = m.group(2)
        key = (size, price)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


# === PLAYWRIGHT FETCHER =====================================================

# JS we run inside the page once it's settled. Returns a list of raw card
# dicts; the Python layer turns them into rows.
CARD_DUMP_JS = """
() => {
  const cards = Array.from(document.querySelectorAll('.leafbridge_product_card'));
  // Skip skeleton placeholders -- they have .skeleton-card-container inside.
  const real = cards.filter(c => !c.querySelector('.skeleton-card-container'));
  const sel = (root, q) => { const e = root.querySelector(q); return e ? e.innerText.trim() : ''; };
  return real.map(card => ({
    productId: card.getAttribute('data-productid') || (card.querySelector('[data-productid]') ? card.querySelector('[data-productid]').getAttribute('data-productid') : ''),
    href: (card.querySelector('a') && card.querySelector('a').href) || '',
    name: sel(card, '.leafbridge_product_name'),
    brand: sel(card, '.leafbridge_brand_name'),
    stickerPrice: sel(card, '.leafbridge_product_price'),
    strainLabel: sel(card, '.leafbridge_strain_type_label'),
    modalText: (card.querySelector('.leafbridge_product_modal') ? card.querySelector('.leafbridge_product_modal').innerText : ''),
  }));
}
"""


async def _dismiss_age_gate(page) -> None:
    """Aura sometimes shows an age confirm modal; click through if present."""
    try:
        btn = page.get_by_role("button", name=re.compile(r"21|enter|continue|yes", re.I))
        await btn.first.click(timeout=2500)
    except Exception:
        pass


async def fetch_category_page(page, slug: str, slug_upper: str, retailer_id: str,
                              pagination: int) -> list[dict]:
    url = (
        f"{BASE.format(slug=slug)}"
        f"?pagination={pagination}&sort=NAME_ASC"
        f"&retailer_id={retailer_id}&menu_type=RECREATIONAL"
        f"&collection_type=PICKUP&categories={slug_upper}"
    )
    await page.goto(url, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
    await _dismiss_age_gate(page)
    # Wait until at least one real (non-skeleton) card exists OR the
    # render timeout expires. Returning 0 cards is the legitimate "end of
    # pagination" signal.
    deadline = asyncio.get_event_loop().time() + PAGE_RENDER_TIMEOUT_S
    while True:
        cards = await page.evaluate(
            "() => Array.from(document.querySelectorAll('.leafbridge_product_card')).filter(c => !c.querySelector('.skeleton-card-container')).length"
        )
        if cards > 0:
            break
        if asyncio.get_event_loop().time() > deadline:
            break
        await asyncio.sleep(0.5)
    # tiny buffer so modal HTML fully populates after card paint
    await asyncio.sleep(1.0)
    return await page.evaluate(CARD_DUMP_JS)


def card_to_rows(raw: dict, store: dict, slug_upper: str) -> int:
    category_label = resolve_category(slug_upper)
    subcategory_label = resolve_subcategory(slug_upper)
    strain_type = resolve_strain_type(raw.get("strainLabel"))
    modal_text = raw.get("modalText") or ""
    thc = _extract_potency(modal_text, "thc")
    cbd = _extract_potency(modal_text, "cbd")
    tac = _extract_potency(modal_text, "tac")
    variants = _extract_variants(modal_text)

    if not variants:
        sticker = (raw.get("stickerPrice") or "").replace("$", "").replace(",", "").strip()
        variants = [("", sticker)]

    rows_added = 0
    for size, price in variants:
        dedupe_key = (
            store["retailer_id"],
            raw.get("productId") or raw.get("name"),
            size,
        )
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)
        row = {
            "date": today,
            "location": state,
            "company": company,
            "dispensary_location": store["dispensary_location"],
            "name": raw.get("name") or "",
            "category": category_label,
            "subcategory": subcategory_label,
            "brand": raw.get("brand") or "",
            "strain_type": strain_type,
            "THC": thc,
            "CBD": cbd,
            "TAC": tac,
            "weight": size,
            "original_price": price,
        }
        products.append(row)
        rows_added += 1
    return rows_added


# === MAIN ===================================================================

async def main_async():
    logger.info(
        f"Starting {company} scrape | backend=Leafbridge (Playwright) | stores={len(stores)}"
    )
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        
        # 🔥 FIX: Pass a real-world User-Agent
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            viewport={'width': 1920, 'height': 1080}
        )
        
        page = await context.new_page()

        for store in stores:
            logger.info(
                f"--- {store['dispensary_location']} "
                f"(retailer_id={store['retailer_id'][:8]}...) ---"
            )
            for slug, slug_upper in CATEGORIES:
                cat_before = len(products)
                for pagination in range(1, MAX_PAGES + 1):
                    try:
                        raw_cards = await fetch_category_page(
                            page, slug, slug_upper, store["retailer_id"], pagination
                        )
                    except Exception as e:  # noqa: BLE001
                        logger.warning(f"  [{slug_upper}] p={pagination} failed: {e}")
                        break
                    all_raw_cards.append({
                        "store": store["dispensary_location"],
                        "category": slug_upper,
                        "pagination": pagination,
                        "card_count": len(raw_cards),
                    })
                    if not raw_cards:
                        logger.info(
                            f"  [{slug_upper}] p={pagination} +0 -- end of category"
                        )
                        break
                    added = 0
                    for raw in raw_cards:
                        added += card_to_rows(raw, store, slug_upper)
                    logger.info(
                        f"  [{slug_upper}] p={pagination} +{added} cards "
                        f"(running {len(products) - cat_before})"
                    )
                logger.info(
                    f"  {slug_upper}: {len(products) - cat_before} rows from "
                    f"{store['dispensary_location']}"
                )
        await context.close()
        await browser.close()


def main():
    try:
        asyncio.run(main_async())
    except Exception as e:  # noqa: BLE001
        logger.critical(f"❌ Fetch failed: {e}", exc_info=True)
        sys.exit(1)

    # === SAVE JSON ===
    try:
        if not products:
            logger.critical("❌ No products scraped")
            sys.exit(1)
        with open(json_file_path, "w", encoding="utf-8") as f:
            json.dump({"pages": all_raw_cards, "rows": products}, f, indent=2)
        logger.info(f"JSON saved: {json_file_path}")
    except Exception as e:  # noqa: BLE001
        logger.critical(f"❌ JSON save failed: {e}", exc_info=True)
        sys.exit(1)

    # === SAVE CSV ===
    try:
        df = pd.DataFrame(products).drop_duplicates(
            subset=["dispensary_location", "name", "weight"]
        )
        if df.empty or len(df) < 5:
            logger.critical(f"❌ Invalid CSV output. Rows: {len(df)}")
            sys.exit(1)

        fixed_cols = [
            "date", "location", "company", "dispensary_location", "name",
            "category", "subcategory", "brand", "strain_type",
            "THC", "CBD", "TAC",
            "weight", "original_price",
        ]
        deal_cols = sorted(
            [c for c in df.columns if c.startswith("deal_")],
            key=lambda x: int(x.split("_")[1]),
        )
        df = df[fixed_cols + deal_cols]
        df.to_csv(output_file, index=False, na_rep="")
        per_store = df.groupby("dispensary_location").size().to_dict()
        cat_counts = df.groupby("category").size().to_dict()
        thc_filled = int((df["THC"].astype(str).str.len() > 0).sum())
        logger.info(
            f"CSV saved: {output_file} | Rows: {len(df)} | Deal columns: {len(deal_cols)}"
        )
        logger.info(f"Rows per dispensary: {per_store}")
        logger.info(f"Rows per category: {cat_counts}")
        logger.info(f"THC filled: {thc_filled}/{len(df)}")
    except Exception as e:  # noqa: BLE001
        logger.critical(f"❌ CSV save failed: {e}", exc_info=True)
        sys.exit(1)

    logger.info(f"✅ {company} scraper completed successfully")
    sys.exit(0)


if __name__ == "__main__":
    main()