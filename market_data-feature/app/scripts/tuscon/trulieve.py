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
# PLATFORM: The Trulieve-owned Tucson store (Harvest of Tucson) runs a standard DUTCHIE
# menu (dutchie.com/dispensary/harvest-of-tucson). Same approach as the Botanica scraper:
# Playwright opens each category and we intercept the `FilteredProducts` GraphQL RESPONSE
# (direct POSTs to dutchie.com/graphql now return 403 Cloudflare, but a real browser
# passes). Runs HEADLESS — no store id, no display/Xvfb needed. When Cloudflare
# intermittently challenges, no FilteredProducts fires and the run reports 0 rows —
# just re-run.
# Run: pip install playwright pandas pytz && playwright install chromium
store_slug = "harvest-of-tucson"
company = "Trulieve"
location = "Tucson"
dispensary_location = "Harvest of Tucson"

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

base_url = f"https://dutchie.com/embedded-menu/{store_slug}/products"

categories = {
    "Flower": f"{base_url}/flower",
    "Pre-rolls": f"{base_url}/pre-rolls",
    "Vaporizers": f"{base_url}/vaporizers",
    "Concentrates": f"{base_url}/concentrates",
    "Edibles": f"{base_url}/edibles",
    "Tinctures": f"{base_url}/tinctures",
    "Topicals": f"{base_url}/topicals",
    "CBD": f"{base_url}/cbd",
    "Accessories": f"{base_url}/accessories",
}

# =========================
# GLOBAL STORAGE
# =========================
products = []
seen_ids = set()
all_json = []

current_category_global = None

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


def dismiss_age_gate(page):
    """Full dispensary pages sometimes show a 21+ age gate before the menu loads."""
    for lab in ("Yes", "I am 21", "I'm 21", "Yes, I am 21", "Enter", "Accept"):
        try:
            page.get_by_role("button", name=lab, exact=False).first.click(timeout=1200)
            logger.info(f"Passed age gate ({lab})")
            page.wait_for_timeout(2000)
            return
        except Exception:
            pass


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
        # body can be evicted when the page navigates to the next category — skip, don't abort
        logger.warning(f"⚠️ Skipped a FilteredProducts response (body unavailable): {e}")
        return

    for item in items:
        pid = item.get("id")
        key = (dispensary_location, pid)
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
                product = {
                    "date": today,
                    "location": location,
                    "company": company,
                    "dispensary_location": dispensary_location,
                    "name": item.get("Name"),
                    "category": current_category_global,
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
                logger.warning(f"⚠️ Variant skipped: {e}")
                continue


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

        first = True
        for current_category, url in categories.items():
            current_category_global = current_category
            logger.info(f"Opening category: {current_category}")

            try:
                page.goto(url, timeout=60000)
            except Exception as e:                          # e.g. a category the store doesn't have -> ERR_TOO_MANY_REDIRECTS
                logger.warning(f"  skip {current_category} ({str(e).splitlines()[0]})")
                continue
            page.wait_for_timeout(5000)

            if first:
                dismiss_age_gate(page)
                first = False

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
logger.info("✅ Trulieve (Harvest of Tucson) scraper completed successfully")
sys.exit(0)