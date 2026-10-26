import pandas as pd
import json
import os
from datetime import datetime
import pytz
from playwright.sync_api import sync_playwright
import logging
import sys

# =========================
# LOGGER
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger(__name__)

# =========================
# CONFIG
# =========================
# PLATFORM: Good Nature uses the DUTCHIE EMBEDDED menu on dutchie.com. Same approach
# as the Botanica scraper: Playwright opens each category, we intercept the
# `FilteredProducts` GraphQL RESPONSE and read products straight off it (incl.
# POSMetaData.children variants). dutchie.com is sometimes behind a Cloudflare
# check; when it is, no FilteredProducts response fires and the run exits with
# "No API responses captured" — just re-run (or set HEADLESS=False) until it clears.
# Run: pip install playwright pandas pytz && playwright install chromium
company  = "GoodNature"
location = "Massachusetts"

HEADLESS = True

us_tz = pytz.timezone("America/Phoenix")
today = datetime.now(us_tz).strftime("%Y-%m-%d")

output_folder = "app/output/pending"
csv_folder = os.path.join(output_folder, "csv")
json_folder = os.path.join(output_folder, "json")

os.makedirs(csv_folder, exist_ok=True)
os.makedirs(json_folder, exist_ok=True)

output_file = os.path.join(csv_folder, f"{today}_{company}.csv")
json_file_path = os.path.join(json_folder, f"{today}_{company}.json")

# MASSACHUSETTS RECREATIONAL STORES (Dutchie embedded-menu slug).
# Only Wareham is open; Central Square & Harvard Square (Cambridge) open 2026.
stores = [
    {"slug": "good-nature-wareham-inc", "dispensary_location": "Wareham"},
    # {"slug": "PASTE_CENTRAL_SQUARE_SLUG", "dispensary_location": "Central Square"},  # Opening 2026
    # {"slug": "PASTE_HARVARD_SQUARE_SLUG", "dispensary_location": "Harvard Square"},  # Opening 2026
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
seen_ids = set()
all_json = []

current_category_global = None
current_store_global = None          # dispensary_location of the store being scraped

# =========================
# HELPERS
# =========================
def format_potency(content):
    if not content:
        return None
    value = (content.get("range") or [None])[0]
    if value in [None, 0]:
        return None
    return value


def extract_deals(item):
    deals = []
    special = item.get("specialData")
    if not special:
        return deals

    for key in ["saleSpecials", "bundleSpecials", "rewardSpecials", "conditionSpecials", "bogoSpecials"]:
        for s in special.get(key, []) or []:
            if s.get("specialName"):
                deals.append(s["specialName"])

    return list(set(deals))


# =========================
# RESPONSE HANDLER
# =========================
def handle_response(response):
    if "FilteredProducts" not in response.url:
        return

    try:
        data = response.json()
        all_json.append(data)
        items = data["data"]["filteredProducts"]["products"]
    except Exception as e:
        logger.critical(f"❌ Response parse failed: {e}", exc_info=True)
        sys.exit(1)

    for item in items:
        pid = item.get("id")
        key = (current_store_global, pid)
        if key in seen_ids:
            continue
        seen_ids.add(key)

        deals = extract_deals(item)
        deal_columns = {f"deal_{i+1}": d for i, d in enumerate(deals)}

        variants = item.get("POSMetaData", {}).get("children") or []
        if not variants:
            logger.warning(f"⚠️ No variants for: {item.get('Name')}")

        for variant in variants:
            try:
                price = variant.get("price")
                if price is None:
                    price = variant.get("recPrice")
                if price is None:
                    price = variant.get("medPrice")

                product = {
                    "date": today,
                    "location": location,
                    "company": company,
                    "dispensary_location": current_store_global,
                    "name": item.get("Name"),
                    "category": current_category_global,
                    "subcategory": item.get("POSMetaData", {}).get("canonicalCategory"),
                    "brand": item.get("brandName"),
                    "strain_type": item.get("strainType"),
                    "THC": format_potency(item.get("THCContent")),
                    "CBD": format_potency(item.get("CBDContent")),
                    "weight": variant.get("option"),
                    "original_price": price,
                }
                product.update(deal_columns)
                products.append(product)

            except Exception as e:
                logger.critical(f"❌ Variant processing failed: {e}", exc_info=True)
                sys.exit(1)


# =========================
# PLAYWRIGHT SCRAPER
# =========================
try:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
        )
        page = context.new_page()
        page.on("response", handle_response)

        for store in stores:
            current_store_global = store["dispensary_location"]
            base_url = f"https://dutchie.com/embedded-menu/{store['slug']}/products"
            logger.info(f"=== {store['dispensary_location']} ({store['slug']}) ===")
            before = len(products)

            for current_category, path in CATEGORY_PATHS.items():
                current_category_global = current_category
                logger.info(f"Opening category: {current_category}")

                page.goto(f"{base_url}/{path}", timeout=60000)
                page.wait_for_timeout(5000)

                while True:
                    page.mouse.wheel(0, 4000)
                    page.wait_for_timeout(2000)

                    next_btn = page.locator('button[aria-label="go to next page"]')
                    if next_btn.count() == 0:
                        logger.info("No pagination found")
                        break
                    if next_btn.is_disabled():
                        logger.info("Last page reached")
                        break

                    next_btn.click()
                    page.wait_for_timeout(4000)

            logger.info(f"  => {store['dispensary_location']}: {len(products) - before} product rows")

        browser.close()

except Exception as e:
    logger.critical(f"❌ Playwright failed: {e}", exc_info=True)
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

    if df.empty or len(df) < 10:
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

    logger.info(f"CSV saved: {output_file} | Rows: {len(df)}")

except Exception as e:
    logger.critical(f"❌ CSV save failed: {e}", exc_info=True)
    sys.exit(1)


# =========================
# SUCCESS
# =========================
logger.info("✅ GoodNature scraper completed successfully")
sys.exit(0)