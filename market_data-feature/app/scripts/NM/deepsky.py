import pandas as pd
import json
import os
import re
import time
from datetime import datetime
import pytz
import requests
import logging
import sys

# =========================
# LOGGER
# =========================
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

# =========================
# CONFIG
# =========================
# PLATFORM: Deep Sky Cannabis (Luana St, Santa Fe) runs a TYMBER storefront backed by the BLAZE
# ecommerce API (ecom-api.blaze.me). Products come from a JSON:API endpoint that identifies the store
# by request headers (X-Store / X-Group), NOT a URL slug:
#   GET https://ecom-api.blaze.me/api/v1/products/?limit=100&offset=N&category=<slug>&delivery_type=pickup
#   headers: Accept: application/vnd.api+json, X-Store: <uuid>, X-Group: <uuid>, X-App-Mode: default
# Each product's `attributes` carry name, thc/cbd {amount,units}, flower_type (strain), on_sale, and a
# `weight_prices` array of variants ({weight_key, display_name, price.amount, discount_price.amount} —
# amounts in CENTS). Brand is a relationship resolved from the response's `included` list. The API
# needs ONLY the static X-Store/X-Group headers (verified 200 cross-origin with credentials omitted),
# so this is pure `requests` — no browser, no bot-block. One row per weight/variant; original_price is
# the OFFERED (discount) price, and a deal is flagged when it's marked down from the regular price.
# Requested URL: https://deepskycannabis.com/order-now/luana-st/categories/flowers/?s=1
# Run: pip install requests pandas pytz
company = "DeepSky"
location = "New Mexico"
dispensary_location = "Santa Fe - Luana St"

API = "https://ecom-api.blaze.me/api/v1/products/"
HEADERS = {
    "Accept": "application/vnd.api+json",
    "X-App-Mode": "default",
    "X-Store": "3cfc46c6-8aee-49ae-9710-a409bc7c92a8",
    "X-Group": "c4669e52-f908-4199-9612-61900fea9cb2",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
}
PAGE_SIZE = 100
MAX_PAGES = 40
TIMEOUT = 30
RETRIES = 2

# Blaze category slug -> affinity category label
CATEGORIES = {
    "flowers": "Flower",
    "preroll": "Pre-rolls",
    "vape-pens": "Vaporizers",
    "wax": "Concentrates",
    "hash": "Concentrates",
    "edibles": "Edibles",
    "drinks": "Edibles",
}

# Blaze weight_key -> normalized weight string
WEIGHT_KEYS = {
    "HALF_GRAM": "0.5g", "GRAM": "1g", "FULL_GRAM": "1g", "TWO_GRAMS": "2g",
    "THREE_GRAMS": "3g", "FOUR_GRAMS": "4g", "FIVE_GRAMS": "5g",
    "ONE_EIGHTTH": "3.5g", "EIGHTH": "3.5g", "TWO_EIGHTTHS": "7g",
    "QUARTER": "7g", "HALF": "14g", "OUNCE": "28g",
}

us_tz = pytz.timezone("America/Denver")
today = datetime.now(us_tz).strftime("%Y-%m-%d")

output_folder = "app/output/pending"
csv_folder = os.path.join(output_folder, "csv")
json_folder = os.path.join(output_folder, "json")
os.makedirs(csv_folder, exist_ok=True)
os.makedirs(json_folder, exist_ok=True)
output_file = os.path.join(csv_folder, f"{today}_{company}.csv")
json_file_path = os.path.join(json_folder, f"{today}_{company}.json")

products = []
all_json = []
seen = set()


def _potency(p):
    if not isinstance(p, dict):
        return ""
    amt = p.get("amount")
    if amt in (None, "", 0, "0"):
        return ""
    return f"{amt}{p.get('units') or ''}"


def _dollars(cents):
    return round(cents / 100, 2) if isinstance(cents, (int, float)) else None


def _parse_measure(text):
    m = re.search(r"(\d+(?:\.\d+)?)\s*(g|gram|grams|mg|oz|ounce)\b", text or "", re.IGNORECASE)
    if not m:
        return ""
    unit = m.group(2).lower()
    unit = "g" if unit in ("g", "gram", "grams") else ("oz" if unit in ("oz", "ounce") else "mg")
    return f"{float(m.group(1)):g}{unit}"


def normalize_weight(wp, attrs):
    key = (wp.get("weight_key") or "").upper()
    if key in WEIGHT_KEYS:
        return WEIGHT_KEYS[key]
    # try the variant label, then the product's base size, then the product name (edibles/pre-rolls
    # often put the weight only in the name, e.g. "... 100mg", "... 1 G - PRE ROLL").
    for text in (wp.get("display_name"), (attrs.get("size") or {}).get("display_text"), attrs.get("name")):
        w = _parse_measure(text)
        if w:
            return w
    return (wp.get("display_name") or "").strip()


def brand_lookup(included):
    """id -> brand name, from the response's included product_brands entries."""
    out = {}
    for x in included or []:
        if x.get("type") == "product_brands":
            out[x.get("id")] = (x.get("attributes") or {}).get("name") or ""
    return out


def handle(data, included, category):
    brands = brand_lookup(included)
    for d in data or []:
        a = d.get("attributes") or {}
        name = (a.get("name") or "").strip()
        if not name or a.get("is_deleted") or a.get("hide_from_menu"):
            continue
        pid = a.get("id") or d.get("id")
        rel = d.get("relationships") or {}
        brand_id = ((rel.get("brand") or {}).get("data") or {}).get("id")
        brand = brands.get(brand_id, "")
        strain = a.get("flower_type") or a.get("strain") or ""
        thc = _potency(a.get("thc"))
        cbd = _potency(a.get("cbd"))

        wps = a.get("weight_prices") or []
        if not wps:
            up = a.get("unit_price") or {}
            wps = [{"weight_key": "", "display_name": (a.get("size") or {}).get("display_text", ""),
                    "price": up, "discount_price": a.get("discount_price") or up}]

        for wp in wps:
            weight = normalize_weight(wp, a)
            reg = _dollars((wp.get("price") or {}).get("amount"))
            offered = _dollars((wp.get("discount_price") or {}).get("amount"))
            if offered is None:
                offered = reg
            key = (dispensary_location, pid, weight or (wp.get("weight_key") or ""))
            if key in seen:
                continue
            seen.add(key)
            row = {
                "date": today, "location": location, "company": company,
                "dispensary_location": dispensary_location, "name": name,
                "category": category, "subcategory": "", "brand": brand,
                "strain_type": strain, "THC": thc, "CBD": cbd,
                "weight": weight, "original_price": offered,
            }
            if reg is not None and offered is not None and reg > offered:
                row["deal_1"] = f"Sale (reg ${reg:g})"
            products.append(row)


session = requests.Session()
session.headers.update(HEADERS)


def get_page(slug, offset):
    params = {"limit": PAGE_SIZE, "offset": offset, "category": slug, "delivery_type": "pickup"}
    last = None
    for attempt in range(RETRIES + 1):
        try:
            r = session.get(API, params=params, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception as e:                                  # noqa: BLE001
            last = e
            if attempt < RETRIES:
                time.sleep(1.5)
    raise RuntimeError(f"{slug} offset={offset} failed: {last}")


# =========================
# FETCH (Blaze REST API — pure requests, no browser)
# =========================
try:
    logger.info(f"Starting {company} scrape | {dispensary_location} (Tymber/Blaze REST API)")
    for slug, cat in CATEGORIES.items():
        before = len(products)
        total = None
        for pg in range(MAX_PAGES):
            offset = pg * PAGE_SIZE
            try:
                body = get_page(slug, offset)
            except Exception as e:
                logger.warning(f"  {cat}: {str(e).splitlines()[0]}")
                break
            data = body.get("data") or []
            if not data:
                break
            total = (body.get("meta") or {}).get("total_count", total)
            all_json.append({"category": cat, "offset": offset, "count": len(data)})
            handle(data, body.get("included"), cat)
            if offset + len(data) >= (total or 0):
                break
            time.sleep(0.2)
        logger.info(f"  {cat}: +{len(products) - before} rows (total {len(products)})")
except Exception as e:
    logger.critical(f"❌ Scraper failed: {e}", exc_info=True)
    sys.exit(1)


# =========================
# SAVE JSON + CSV
# =========================
try:
    if not products:
        logger.critical("❌ No products collected — the store headers may have changed, or a gate appeared.")
        sys.exit(1)
    with open(json_file_path, "w", encoding="utf-8") as f:
        json.dump(products, f, indent=2)
    logger.info(f"JSON saved: {json_file_path}")

    df = pd.DataFrame(products).drop_duplicates(subset=["dispensary_location", "name", "weight"])
    if df.empty or len(df) < 5:
        logger.critical(f"❌ Invalid CSV output. Rows: {len(df)}")
        sys.exit(1)
    fixed_cols = ["date", "location", "company", "dispensary_location", "name",
                  "category", "subcategory", "brand", "strain_type",
                  "THC", "CBD", "weight", "original_price"]
    deal_cols = sorted([c for c in df.columns if c.startswith("deal_")],
                       key=lambda x: int(x.split("_")[1]))
    df = df[fixed_cols + deal_cols]
    df.to_csv(output_file, index=False, na_rep="")
    logger.info(f"CSV saved: {output_file} | Rows: {len(df)}")
except SystemExit:
    raise
except Exception as e:
    logger.critical(f"❌ Save failed: {e}", exc_info=True)
    sys.exit(1)

logger.info(f"✅ {company} scraper completed successfully")
sys.exit(0)
