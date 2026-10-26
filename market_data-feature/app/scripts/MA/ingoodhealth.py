#!/usr/bin/env python3
"""
In Good Health (Sandwich + Brockton + Taunton, MA) -- Botanica-pattern scraper.

Three MA rec stores, all Dutchie-embedded (white-labeled at
ingoodhealthma.com/stores/<slug> but the underlying menu is dutchie.com):
    Sandwich (Rec)  -> in-good-health-sandwich-rec-retail
    Brockton (Rec)  -> in-good-health-brockton
    Taunton  (Rec)  -> in-good-health-taunton-rec-dispensary

In Good Health is a licensed cultivator, so they carry a large family of
in-house brand names. HOUSE_BRAND_KEYWORDS covers everything that showed up
in the #1 cheapest tier consistently across all three rec stores.
"""
import json
import logging
import os
import sys
from datetime import datetime

import pandas as pd
import pytz
from playwright.sync_api import sync_playwright


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)


# === CONFIG =================================================================

company             = "InGoodHealth"
location            = "Massachusetts"
PRICING_TYPE        = "rec"
TIMEZONE            = "America/New_York"

# In Good Health is a cultivator-retailer. These are the internal brand
# families identified by the cheapest-per-category probe -- all appear as
# #1 cheapest at all three rec stores (Sandwich, Brockton, Taunton).
HOUSE_BRAND_KEYWORDS = [
    # Flower / pre-roll house family
    "champ city",
    "good buzz",
    "slacker",
    "sweet juanita",
    "moon-lit", "moon lit",
    # Vape house family
    "electric underground",
    "pentagram",
    "infinity",
    "cann1gram", "cannigram",
    "holiday",
    # Concentrate / tincture house family
    "scatterbrain",
    "cloud cover",
    "galactic",
    # Edible house family
    "grandma georgia",
    "lullaby",
    "oblio",
    # Topical
    "honah-lee", "honah lee",
    # Safety net if any direct-branded SKU appears
    "in good health",
]

stores = [
    {"slug": "in-good-health-sandwich-rec-retail",   "dispensary_location": "Sandwich"},
    {"slug": "in-good-health-brockton",              "dispensary_location": "Brockton"},
    {"slug": "in-good-health-taunton-rec-dispensary","dispensary_location": "Taunton"},
]

us_tz = pytz.timezone(TIMEZONE)
today = datetime.now(us_tz).strftime("%Y-%m-%d")

output_folder = "app/output/pending"
csv_folder = os.path.join(output_folder, "csv")
json_folder = os.path.join(output_folder, "json")
os.makedirs(csv_folder, exist_ok=True)
os.makedirs(json_folder, exist_ok=True)
output_file = os.path.join(csv_folder, f"{today}_{company}.csv")
json_file_path = os.path.join(json_folder, f"{today}_{company}.json")

CATEGORY_SLUGS = {
    "Flower":       "flower",
    "Pre-Roll":     "pre-rolls",
    "Vaporizers":   "vaporizers",
    "Concentrate":  "concentrates",
    "Edible":       "edibles",
    "Tincture":     "tinctures",
    "Topical":      "topicals",
    "Accessories":  "accessories",
    "Apparel":      "apparel",
}


# === STATE =================================================================

products = []
seen_keys = set()
all_json = []
current_category_global = None
current_store_global = None


# === HELPERS ================================================================

_STRAIN_MAP = {
    "hybrid": "Hybrid", "sativa": "Sativa", "indica": "Indica",
    "cbd": "High CBD", "high cbd": "High CBD", "high_cbd": "High CBD",
    "1 to 1": "1:1", "1:1": "1:1", "2 to 1": "2:1", "1 to 2": "1:2",
}
_UNIT_MAP = {"PERCENTAGE": "%", "PERCENT": "%", "MILLIGRAMS": "mg", "MG": "mg"}


def _format_one_lab_value(potency_obj):
    if not isinstance(potency_obj, dict):
        return ""
    rng = potency_obj.get("range") or []
    nums = [x for x in rng if isinstance(x, (int, float)) and x > 0]
    if not nums:
        return ""
    val = max(nums)
    unit_raw = (potency_obj.get("unit") or "").strip().upper()
    unit = _UNIT_MAP.get(unit_raw, unit_raw or "")
    try:
        val_str = f"{float(val):g}"
    except (TypeError, ValueError):
        val_str = str(val)
    return f"{val_str} {unit}".strip()


def resolve_potency(product, key):
    key = key.lower()
    if key == "thc":
        return _format_one_lab_value(product.get("THCContent"))
    if key == "cbd":
        return _format_one_lab_value(product.get("CBDContent"))
    return ""


def resolve_strain_type(product):
    raw = (product.get("strainType") or "").strip()
    if not raw:
        return ""
    key = raw.lower()
    if key in _STRAIN_MAP:
        return _STRAIN_MAP[key]
    for token in ("indica", "sativa", "hybrid", "high cbd", "high_cbd", "cbd"):
        if token in key:
            return _STRAIN_MAP[token if token != "high_cbd" else "high cbd"]
    return raw


def resolve_subcategory(product):
    raw = (product.get("subcategory") or "").strip()
    return raw.upper().replace(" ", "-") if raw else "DEFAULT"


def resolve_brand(product):
    return product.get("brandName") or ""


def is_house_brand(brand):
    if not brand:
        return False
    b = brand.lower()
    return any(kw in b for kw in HOUSE_BRAND_KEYWORDS)


def _pick_num(arr, idx):
    if isinstance(arr, list) and idx < len(arr) and arr[idx] not in (None, 0):
        try:
            return float(arr[idx])
        except (TypeError, ValueError):
            return None
    return None


def card_price_and_deals(product, idx):
    specials_key = "recSpecialPrices" if PRICING_TYPE == "rec" else "medicalSpecialPrices"
    prices_key = "recPrices" if PRICING_TYPE == "rec" else "medicalPrices"

    discounted = _pick_num(product.get(specials_key), idx)
    regular = _pick_num(product.get(prices_key), idx)
    if discounted is None:
        return regular, []

    deals = []
    seen = set()
    sd = product.get("specialData") or {}
    if isinstance(sd, dict):
        for k in ("bogoSpecials", "saleSpecials"):
            for entry in (sd.get(k) or []):
                if isinstance(entry, dict):
                    name = (entry.get("specialName") or "").strip()
                    if name and name not in seen:
                        seen.add(name)
                        deals.append(name)
    return discounted, deals


# === RESPONSE HANDLER (Botanica pattern) ====================================

def handle_response(response):
    if "FilteredProducts" not in response.url:
        return
    try:
        data = response.json()
        all_json.append(data)
        fp = (data.get("data") or {}).get("filteredProducts") or {}
        items = fp.get("products") or fp.get("queue") or []
    except Exception as e:
        logger.warning(f"Response parse failed: {e}")
        return

    for item in items:
        try:
            options = item.get("Options") or [None]
            subcategory = resolve_subcategory(item)
            strain_type = resolve_strain_type(item)
            thc = resolve_potency(item, "thc")
            cbd = resolve_potency(item, "cbd")
            brand = resolve_brand(item)
            name = item.get("Name") or item.get("name")
            pid = item.get("_id") or item.get("id") or name

            for i, opt in enumerate(options):
                weight = opt.strip() if isinstance(opt, str) else ""
                price, deals = card_price_and_deals(item, i)
                deal_columns = {f"deal_{n+1}": d for n, d in enumerate(deals)}

                dedupe_key = (current_store_global, pid, weight)
                if dedupe_key in seen_keys:
                    continue
                seen_keys.add(dedupe_key)

                row = {
                    "date": today,
                    "location": location,
                    "company": company,
                    "dispensary_location": current_store_global,
                    "name": name,
                    "category": current_category_global,
                    "subcategory": subcategory,
                    "brand": brand,
                    "strain_type": strain_type,
                    "THC": thc,
                    "CBD": cbd,
                    "weight": weight,
                    "original_price": price,
                }
                row.update(deal_columns)
                products.append(row)
        except Exception as e:
            logger.warning(f"Variant processing failed: {e}")
            continue


# === PLAYWRIGHT MAIN LOOP ===================================================

try:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        
        # 🔥 FIX: Pass a real-world User-Agent
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            viewport={'width': 1920, 'height': 1080}
        )
        
        page = context.new_page()
        page.on("response", handle_response)

        for store in stores:
            current_store_global = store["dispensary_location"]
            base_url = f"https://dutchie.com/embedded-menu/{store['slug']}/products"
            logger.info(f"--- {store['dispensary_location']} (slug={store['slug']}) ---")

            for cat_name, cat_slug in CATEGORY_SLUGS.items():
                current_category_global = cat_name
                url = f"{base_url}/{cat_slug}"
                logger.info(f"Opening category: {cat_name}  ({url})")

                try:
                    page.goto(url, timeout=60000)
                except Exception as e:
                    logger.warning(f"  goto failed for {cat_name}: {e}")
                    continue
                page.wait_for_timeout(5000)

                while True:
                    page.mouse.wheel(0, 4000)
                    page.wait_for_timeout(2000)
                    next_btn = page.locator('button[aria-label="go to next page"]')
                    if next_btn.count() == 0:
                        break
                    try:
                        if next_btn.first.is_disabled():
                            break
                        next_btn.first.click()
                        page.wait_for_timeout(4000)
                    except Exception:
                        break

        browser.close()

except Exception as e:
    logger.critical(f"Playwright failed: {e}", exc_info=True)
    sys.exit(1)


# === SAVE JSON ==============================================================

try:
    if not all_json:
        logger.critical("No API responses captured (JSON empty)")
        sys.exit(1)
    with open(json_file_path, "w", encoding="utf-8") as f:
        json.dump(all_json, f, indent=2)
    logger.info(f"JSON saved: {json_file_path}")
except Exception as e:
    logger.critical(f"JSON save failed: {e}", exc_info=True)
    sys.exit(1)


# === SAVE CSV ===============================================================

try:
    df = pd.DataFrame(products).drop_duplicates(
        subset=["dispensary_location", "name", "weight"]
    )
    if df.empty or len(df) < 5:
        logger.critical(f"Invalid CSV output. Rows: {len(df)}")
        sys.exit(1)

    fixed_cols = [
        "date", "location", "company", "dispensary_location", "name",
        "category", "subcategory", "brand", 
        "strain_type", "THC", "CBD",
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
    logger.info(f"CSV saved: {output_file} | Rows: {len(df)} | Deal columns: {len(deal_cols)}")
    logger.info(f"Rows per dispensary: {per_store}")
    logger.info(f"Rows per category: {cat_counts}")


    top_brands = df["brand"].value_counts().head(20).to_dict()
    logger.info("Top brands (verify HOUSE_BRAND_KEYWORDS covers the intended ones):")
    for b, n in top_brands.items():
        marker = "* HOUSE" if is_house_brand(b) else ""
        logger.info(f"    {n:4d}  {b}  {marker}")
except Exception as e:
    logger.critical(f"CSV save failed: {e}", exc_info=True)
    sys.exit(1)

logger.info(f"{company} scraper completed successfully")
sys.exit(0)