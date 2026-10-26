import pandas as pd
import json
import os
import time
from datetime import datetime
import pytz
from playwright.sync_api import sync_playwright
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
# Backend: Dutchie classic EMBEDDED menu (dutchie.com/embedded-menu/<slug>).
#
# NOTE (2026-07): direct server-side POSTs to https://dutchie.com/graphql now return
# 403 (Cloudflare bot protection). So instead of calling the API with `requests`, we
# drive a real browser (Playwright) through the embedded menu and INTERCEPT the same
# `FilteredProducts` GraphQL RESPONSE. A real browser passes the Cloudflare check;
# when it's intermittently challenged, no response fires and the run reports 0 rows —
# just re-run (or set HEADLESS=False).
#
# The intercepted product objects carry the SAME fields the old query selected
# (Options, recPrices, recSpecialPrices, specialData, THCContent, CBDContent,
# brandName, strainType, type, subcategory) so all the card-mirror parsing below is
# unchanged.
#
# Card mirror rules (unchanged):
#   - price column shows whatever number the card displays (green/bold): when
#     recSpecialPrices[i] is set -> discounted; else recPrices[i].
#   - deal_N columns appear ONLY for variants that carry a strike-through
#     (recSpecialPrices[i] set). If no product has a deal, no deal_* columns.
#   - weight is whatever Options[i] returns, byte-for-byte.
# Run: pip install playwright pandas pytz && playwright install chromium
company = "JDMCannabis"
state = "Massachusetts"

HEADLESS = True
PRICING_TYPE = "rec"            # "rec" or "med"
NAV_TIMEOUT = 60000

us_tz = pytz.timezone("America/New_York")
today = datetime.now(us_tz).strftime("%Y-%m-%d")

output_folder = "app/output/pending"
csv_folder = os.path.join(output_folder, "csv")
json_folder = os.path.join(output_folder, "json")
os.makedirs(csv_folder, exist_ok=True)
os.makedirs(json_folder, exist_ok=True)
output_file = os.path.join(csv_folder, f"{today}_{company}.csv")
json_file_path = os.path.join(json_folder, f"{today}_{company}.json")

stores = [
    {
        "id": "ddm-cannabis-jdm-sales-inc",
        "slug": "ddm-cannabis-jdm-sales-inc",
        "dispensary_location": "Mendon",
    },
]

CATEGORY_PATHS = {
    "Flower": "flower",
    "Pre-rolls": "pre-rolls",
    "Vaporizers": "vaporizers",
    "Concentrates": "concentrates",
    "Edibles": "edibles",
    "Tinctures": "tinctures",
    "Topicals": "topicals",
    "CBD": "cbd",
    "Accessories": "accessories",
}

# =========================
# GLOBAL STORAGE
# =========================
products = []
seen_keys = set()
all_json = []
current_store = None


# =========================
# HELPERS  (unchanged parsing)
# =========================
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


def resolve_category(product):
    return (product.get("type") or "").strip().lower()


def resolve_subcategory(product):
    raw = (product.get("subcategory") or "").strip()
    return raw.upper().replace(" ", "-") if raw else "DEFAULT"


def resolve_brand(product):
    return product.get("brandName") or ""


def resolve_weight(option):
    return option.strip() if isinstance(option, str) else ""


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
        for key in ("bogoSpecials", "saleSpecials"):
            arr = sd.get(key) or []
            for entry in arr:
                if isinstance(entry, dict):
                    name = (entry.get("specialName") or "").strip()
                    if name and name not in seen:
                        seen.add(name)
                        deals.append(name)
    return discounted, deals


# =========================
# RESPONSE HANDLER  (fed by Playwright interception)
# =========================
def handle_response(data, store):
    try:
        all_json.append({"store_id": store["id"], "data": data})
        fp = (data.get("data") or {}).get("filteredProducts") or {}
        items = fp.get("products") or []
    except Exception as e:                                       # noqa: BLE001
        logger.warning(f"⚠️ Skipped a response (parse failed): {e}")
        return

    for product in items:
        try:
            options = product.get("Options") or [None]
            category = resolve_category(product)
            subcategory = resolve_subcategory(product)
            strain_type = resolve_strain_type(product)
            thc = resolve_potency(product, "thc")
            cbd = resolve_potency(product, "cbd")
            brand = resolve_brand(product)

            for i, opt in enumerate(options):
                weight = resolve_weight(opt)
                price, deals = card_price_and_deals(product, i)
                deal_columns = {f"deal_{n+1}": d for n, d in enumerate(deals)}

                dedupe_key = (store["id"], product.get("_id") or product.get("Name"), weight)
                if dedupe_key in seen_keys:
                    continue
                seen_keys.add(dedupe_key)

                row = {
                    "date": today,
                    "location": state,
                    "company": company,
                    "dispensary_location": store["dispensary_location"],
                    "name": product.get("Name"),
                    "category": category,
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
        except Exception as e:                                  # noqa: BLE001
            logger.warning(f"⚠️ Variant skipped: {e}")
            continue


def on_response(response):
    if "FilteredProducts" not in response.url:
        return
    try:
        data = response.json()
    except Exception as e:                                      # noqa: BLE001
        logger.warning(f"⚠️ Skipped a FilteredProducts response (body unavailable): {e}")
        return
    handle_response(data, current_store)


# =========================
# PLAYWRIGHT SCRAPER
# =========================
try:
    logger.info(f"Starting {company} scrape | pricingType={PRICING_TYPE} | stores={len(stores)}")
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=HEADLESS, channel="chrome")
        except Exception:
            browser = p.chromium.launch(headless=HEADLESS)
        context = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"),
            viewport={"width": 1920, "height": 1080},
        )
        page = context.new_page()
        page.on("response", on_response)

        for store in stores:
            current_store = store
            base_url = f"https://dutchie.com/embedded-menu/{store['slug']}/products"
            logger.info(f"--- {store['dispensary_location']} (id={store['id']}) ---")
            rows_before = len(products)

            for label, path in CATEGORY_PATHS.items():
                logger.info(f"  opening category: {label}")
                page.goto(f"{base_url}/{path}", timeout=NAV_TIMEOUT)
                page.wait_for_timeout(5000)

                while True:
                    page.mouse.wheel(0, 4000)
                    page.wait_for_timeout(2000)
                    next_btn = page.locator('button[aria-label="go to next page"]')
                    if next_btn.count() == 0:
                        break
                    if next_btn.is_disabled():
                        break
                    next_btn.click()
                    page.wait_for_timeout(4000)

            logger.info(f"=> {store['dispensary_location']}: {len(products) - rows_before} rows added")

        browser.close()

except Exception as e:
    logger.critical(f"❌ Playwright fetch failed: {e}", exc_info=True)
    sys.exit(1)


# =========================
# SAVE JSON
# =========================
try:
    if not all_json:
        logger.critical("❌ No API responses captured (JSON empty) — Cloudflare check likely; re-run.")
        sys.exit(1)
    with open(json_file_path, "w", encoding="utf-8") as f:
        json.dump(all_json, f, indent=2)
    logger.info(f"JSON saved: {json_file_path}")
except Exception as e:
    logger.critical(f"❌ JSON save failed: {e}", exc_info=True)
    sys.exit(1)


# =========================
# SAVE CSV
# =========================
try:
    df = pd.DataFrame(products).drop_duplicates(subset=["dispensary_location", "name", "weight"])
    if df.empty or len(df) < 5:
        logger.critical(f"❌ Invalid CSV output. Rows: {len(df)}")
        sys.exit(1)

    fixed_cols = [
        "date", "location", "company", "dispensary_location", "name",
        "category", "subcategory", "brand", "strain_type",
        "THC", "CBD", "weight", "original_price",
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
except Exception as e:
    logger.critical(f"❌ CSV save failed: {e}", exc_info=True)
    sys.exit(1)

logger.info(f"✅ {company} scraper completed successfully")
sys.exit(0)