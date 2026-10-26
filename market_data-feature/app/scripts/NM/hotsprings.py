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
# PLATFORM: Hot Springs Cannabis (Truth or Consequences, 420 N Date St) runs a GROWFLOW eCommerce
# storefront (shop.hotspringscannabistorc.com/hot-springs-cannabis-nm). The menu is a client-side
# ALGOLIA InstantSearch app — products live in the shared "prod_candid" index and are filtered per
# store by dispensary.slug. Each product record carries name, brand, productType/category/subcategory,
# lineage (strain), amountTotalThc/amountCbd, and a `productBatchDispensaryVariants` array holding the
# per-weight price ladder (1g $8 / 3.5g $25 / 7g $50 / 14g $100 / 28g $160). Single-weight items
# (edibles, etc.) carry no variants — their weight is in `weight` and the price in `retailPrice`.
#
# OUTPUT SHAPE: the standard affinity format — ONE ROW PER WEIGHT/VARIANT (a new row for each gram
# tier), with the weight in `weight` and its price in `original_price`. Flower expands to 1g / 3.5g /
# 7g / 14g / 28g rows; single-weight items (edibles, etc.) produce one row.
#
# We query Algolia directly (public search-only key, no bot-block), so this is pure `requests`.
# Requested URL: https://shop.hotspringscannabistorc.com/hot-springs-cannabis-nm/categories/flower
# Run: pip install requests pandas pytz
company = "HotSprings"
location = "New Mexico"
dispensary_location = "Truth or Consequences"

ALGOLIA_APP = "NXV6DGZFUV"
ALGOLIA_KEY = "0f7f53b49f876419423897a2be01fcdc"   # public search-only key (from the storefront bundle)
ALGOLIA_INDEX = "prod_candid"
DISPENSARY_SLUG = "hot-springs-cannabis-nm"
QUERY_URL = f"https://{ALGOLIA_APP}-dsn.algolia.net/1/indexes/{ALGOLIA_INDEX}/query"
HITS_PER_PAGE = 1000
TIMEOUT = 30
RETRIES = 2

# productType / category -> affinity category label
CAT_MAP = {
    "Flower": "Flower", "Pre-Roll": "Pre-rolls", "Preroll": "Pre-rolls", "Pre-Rolls": "Pre-rolls",
    "Vaporizer": "Vaporizers", "Vape": "Vaporizers", "Cartridge": "Vaporizers", "Vaporizers": "Vaporizers",
    "Concentrate": "Concentrates", "Concentrates": "Concentrates",
    "Edible": "Edibles", "Edibles": "Edibles", "Ingestible": "Edibles",
    "Tincture": "Tinctures", "Topical": "Topicals", "Accessory": "Accessories", "Accessories": "Accessories",
}

# weight-unit word -> short unit
UNIT_MAP = {
    "milligram": "mg", "milligrams": "mg", "mg": "mg",
    "gram": "g", "grams": "g", "g": "g",
    "ounce": "oz", "ounces": "oz", "oz": "oz",
    "each": "ea", "unit": "ea", "units": "ea", "pack": "pk",
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

session = requests.Session()
session.headers.update({
    "x-algolia-application-id": ALGOLIA_APP,
    "x-algolia-api-key": ALGOLIA_KEY,
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
})

rows = []
all_json = []
seen = set()


def _title(v):
    v = (v or "").strip()
    return v.title() if v else ""


def _pct(v):
    # Algolia stores potency as a FRACTION (0.1905 == 19.05%), so scale to a percentage to match
    # the storefront's "TOTAL THC 19.05%" display.
    if v in (None, "", 0, 0.0):
        return ""
    try:
        return f"{round(float(v) * 100, 2):g}%"
    except (TypeError, ValueError):
        return ""


def parse_weight_field(w):
    """'3.50Grams' -> '3.5g', '5.00Milligrams' -> '5mg'."""
    m = re.match(r"\s*([\d.]+)\s*([A-Za-z]+)", w or "")
    if not m:
        return ""
    unit = UNIT_MAP.get(m.group(2).lower(), m.group(2).lower())
    return f"{float(m.group(1)):g}{unit}"


def weight_price_tiers(hit):
    """Return {weight_label: price} for a product (variant ladder, or single retail tier)."""
    tiers = {}
    for v in (hit.get("productBatchDispensaryVariants") or []):
        try:
            wt = float(v.get("weight"))
        except (TypeError, ValueError):
            continue
        unit = UNIT_MAP.get((v.get("uom") or "").lower(), (v.get("uom") or "").lower())
        label = f"{wt:g}{unit}"
        price = v.get("price")
        if price is None:
            price = v.get("medicalPrice")
        if price is not None:
            tiers[label] = price
    if not tiers:
        label = parse_weight_field(hit.get("weight")) or "each"
        price = hit.get("retailPrice")
        if price is None:
            price = hit.get("msrp")
        if price is not None:
            tiers[label] = price
    return tiers


def handle(hits):
    for h in hits:
        if h.get("active") is False:
            continue
        status = (h.get("status") or "").upper()
        if status and status not in ("FOR_SALE", "ACTIVE", ""):
            continue
        name = (h.get("name") or "").strip()
        if not name:
            continue

        cat_raw = h.get("productType") or h.get("category") or ""
        category = CAT_MAP.get(cat_raw, _title(cat_raw))
        brand = (h.get("brand") or {}).get("name", "") if isinstance(h.get("brand"), dict) else (h.get("brand") or "")
        subcat = _title(h.get("subcategory"))
        strain = _title(h.get("lineage"))
        thc = _pct(h.get("amountTotalThc"))
        cbd = _pct(h.get("amountCbd"))
        pid = h.get("objectID") or h.get("productId") or name

        # One row per weight tier (1g / 3.5g / 7g / 14g / 28g for flower; one row otherwise).
        for weight, price in weight_price_tiers(h).items():
            key = (dispensary_location, pid, weight)
            if key in seen:
                continue
            seen.add(key)
            rows.append({
                "date": today, "location": location, "company": company,
                "dispensary_location": dispensary_location, "name": name,
                "category": category, "subcategory": subcat, "brand": brand,
                "strain_type": strain, "THC": thc, "CBD": cbd,
                "weight": weight, "original_price": price,
            })


# =========================
# FETCH (Algolia)
# =========================
def query(page):
    body = {
        "query": "",
        "hitsPerPage": HITS_PER_PAGE,
        "page": page,
        "facetFilters": [[f"dispensary.slug:{DISPENSARY_SLUG}"]],
    }
    last = None
    for attempt in range(RETRIES + 1):
        try:
            r = session.post(QUERY_URL, json=body, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception as e:                                  # noqa: BLE001
            last = e
            if attempt < RETRIES:
                time.sleep(1.5)
    raise RuntimeError(f"Algolia query failed (page={page}): {last}")


try:
    logger.info(f"Starting {company} scrape | {dispensary_location} (GrowFlow/Algolia {DISPENSARY_SLUG})")
    page = 0
    while True:
        data = query(page)
        hits = data.get("hits") or []
        nb_pages = data.get("nbPages", 1)
        total = data.get("nbHits")
        all_json.append({"page": page, "count": len(hits)})
        handle(hits)
        logger.info(f"  page {page}: {len(hits)} hits (rows {len(rows)}, total {total})")
        page += 1
        if page >= nb_pages or not hits:
            break
        time.sleep(0.3)
except Exception as e:
    logger.critical(f"❌ Scraper failed: {e}", exc_info=True)
    sys.exit(1)


# =========================
# SAVE JSON + CSV
# =========================
try:
    if not rows:
        logger.critical("❌ No products collected — check the dispensary slug / Algolia key.")
        sys.exit(1)
    with open(json_file_path, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2)
    logger.info(f"JSON saved: {json_file_path}")

    df = pd.DataFrame(rows).drop_duplicates(subset=["dispensary_location", "name", "weight"])
    if df.empty or len(df) < 10:
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
