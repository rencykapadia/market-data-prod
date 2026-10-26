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
store_slug = {
    "the-downtown-dispensary": "Downtown",
    "d2-dispensary": "Eastside"
}

company = "D2dispensary"
location = "Tucson"

us_tz = pytz.timezone("America/Phoenix")
today = datetime.now(us_tz).strftime("%Y-%m-%d")

# Output folders
output_folder = "app/output/pending"
csv_folder = os.path.join(output_folder, "csv")
json_folder = os.path.join(output_folder, "json")

os.makedirs(csv_folder, exist_ok=True)
os.makedirs(json_folder, exist_ok=True)

output_file = os.path.join(csv_folder, f"{today}_{company}.csv")
json_file_path = os.path.join(json_folder, f"{today}_{company}.json")

categories = [
    "flower",
    "pre-rolls",
    "vaporizers",
    "edibles",
    "concentrates"
]

# =========================
# GLOBAL STORAGE
# =========================
products = []
seen_ids = set()
all_json = []

logger.info("D2dispensary Script started")

# ✅ FIX VARIABLES
current_store = None
current_category = None

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
def handle_response(response, current_store, current_category):
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

        if pid in seen_ids:
            continue

        seen_ids.add(pid)

        deals = extract_deals(item)
        deal_columns = {f"deal_{i+1}": d for i, d in enumerate(deals)}

        variants = item.get("POSMetaData", {}).get("children") or []

        if not variants:
            logger.warning(f"⚠️ No variants for {item.get('Name')}")

        for variant in variants:
            try:
                product = {
                    "date": today,
                    "location": location,
                    "company": company,
                    "dispensary_location": current_store,
                    "name": item.get("Name"),
                    "category": current_category,
                    "subcategory": item.get("POSMetaData", {}).get("canonicalCategory"),
                    "brand": item.get("brandName"),
                    "strain_type": item.get("strainType"),
                    "THC": format_potency(item.get("THCContent")),
                    "CBD": format_potency(item.get("CBDContent")),
                    "weight": variant.get("option"),
                    "original_price": variant.get("price"),
                }

                product.update(deal_columns)
                products.append(product)

            except Exception as e:
                logger.critical(f"❌ Variant processing failed: {e}", exc_info=True)
                sys.exit(1)


# ✅ SINGLE GLOBAL LISTENER
def route_handler(response):
    handle_response(response, current_store, current_category)


# =========================
# PLAYWRIGHT
# =========================
try:
    with sync_playwright() as p:

        browser = p.chromium.launch(headless=True)
        
        # 🔥 FIX: Pass a real-world User-Agent
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            viewport={'width': 1920, 'height': 1080}
        )
        
        page = context.new_page()
        page.on("response", route_handler)


        for slug, store_name in store_slug.items():

            current_store = store_name
            logger.info(f"Store: {store_name} ({slug})")

            base_url = f"https://dutchie.com/embedded-menu/{slug}/products"

            for cat in categories:

                current_category = cat
                url = f"{base_url}/{cat}"

                logger.info(f"Opening: {cat}")

                page.goto(url, timeout=60000)
                page.wait_for_timeout(6000)

                while True:
                    page.mouse.wheel(0, 4000)
                    page.wait_for_timeout(2000)

                    next_btn = page.locator('button[aria-label="go to next page"]')

                    if next_btn.count() == 0:
                        logger.info("No pagination")
                        break

                    if next_btn.is_disabled():
                        logger.info("Last page reached")
                        break

                    next_btn.click()
                    page.wait_for_timeout(4000)

        browser.close()

except Exception as e:
    logger.critical(f"❌ Playwright failed: {e}", exc_info=True)
    sys.exit(1)


# =========================
# SAVE JSON
# =========================
try:
    if not all_json:
        logger.critical("❌ No API responses captured")
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
    df = pd.DataFrame(products).drop_duplicates(subset=["name", "weight"])

    if df.empty or len(df) < 10:
        logger.critical(f"❌ Invalid CSV output. Rows: {len(df)}")
        sys.exit(1)

    df.to_csv(output_file, index=False, na_rep="")

    logger.info(f"CSV saved: {output_file} | Rows: {len(df)}")

except Exception as e:
    logger.critical(f"❌ CSV save failed: {e}", exc_info=True)
    sys.exit(1)


# =========================
# SUCCESS
# =========================
logger.info("✅ D2dispensary scraper completed successfully")
sys.exit(0)