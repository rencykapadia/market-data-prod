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
# PLATFORM: JARS East Tucson also has a standard DUTCHIE menu
# (dutchie.com/dispensary/jars-east-tucson-green-med-wellness). Same approach as the
# Botanica scraper: Playwright opens each category and we intercept the
# `FilteredProducts` GraphQL RESPONSE (direct POSTs to dutchie.com/graphql now return
# 403 Cloudflare, but a real browser passes). Runs HEADLESS — no display/Xvfb needed.
# When Cloudflare intermittently challenges, no FilteredProducts fires and the run
# reports 0 rows — just re-run.
# Run: pip install playwright pandas pytz && playwright install chromium
store_slug = "jars-east-tucson-green-med-wellness"
company = "JARS"
location = "Tucson"
dispensary_location = "East Tucson"

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
# VARIANT EXTRACTION (robust to both Dutchie structures)
# =========================
def variants_from(item):
    """Yield (weight, price) pairs. Newer menus carry weights in
    POSMetaData.children[{option, price}]; older/bulk items (esp. FLOWER, sold by
    weight) instead expose top-level Options[] with parallel recPrices[]/recSpecialPrices[].
    Reading only children silently drops those — so we fall back to Options."""
    children = (item.get("POSMetaData") or {}).get("children") or []
    if children:
        for v in children:
            price = v.get("price")
            if price is None:
                price = v.get("recPrice")
            if price is None:
                price = v.get("medPrice")
            yield v.get("option"), price
        return

    options = item.get("Options") or []
    rec_prices = item.get("recPrices") or item.get("Prices") or []
    rec_special = item.get("recSpecialPrices") or []
    if options:
        for i, opt in enumerate(options):
            price = None
            if i < len(rec_special) and rec_special[i] not in (None, 0):
                price = rec_special[i]
            elif i < len(rec_prices) and rec_prices[i] not in (None, 0):
                price = rec_prices[i]
            yield opt, price
    else:
        yield "", item.get("price")


# =========================
# RESPONSE HANDLER
# =========================
def handle_response(response):
    # Detect by BODY, not URL: embedded menus put FilteredProducts in the URL, but full
    # /dispensary/ pages POST to a bare /graphql, so keying on the URL misses them.
    url = response.url.lower()
    if "graphql" not in url and "filteredproducts" not in url:
        return
    try:
        data = response.json()
    except Exception:
        return
    fp = (data.get("data") or {}).get("filteredProducts") or {}
    items = fp.get("products")
    if not items:
        return
    all_json.append(data)

    for item in items:
        pid = item.get("id") or item.get("_id") or item.get("Name")
        key = (dispensary_location, current_category_global, pid)
        if key in seen_ids:
            continue
        seen_ids.add(key)

        deals = extract_deals(item)
        deal_columns = {f"deal_{i+1}": d for i, d in enumerate(deals)}

        for weight, price in variants_from(item):
            try:
                product = {
                    "date": today,
                    "location": location,
                    "company": company,
                    "dispensary_location": dispensary_location,
                    "name": item.get("Name"),
                    "category": current_category_global,
                    "subcategory": (item.get("POSMetaData") or {}).get("canonicalCategory"),
                    "brand": item.get("brandName"),
                    "strain_type": item.get("strainType"),
                    "THC": format_potency(item.get("THCContent")),
                    "CBD": format_potency(item.get("CBDContent")),
                    "weight": weight,
                    "original_price": price,
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

        # Warm up: load the menu once, clear any 21+ age gate, and let the app fully boot
        # BEFORE the first real category. Otherwise the first category (Flower) fires its
        # product query during the cold boot / while the modal is up and comes back empty
        # (that was the Flower: +0). Rows captured here still count — dedupe handles overlap.
        try:
            page.goto(base_url + "/flower", timeout=60000)
            page.wait_for_timeout(5000)
            dismiss_age_gate(page)
            page.wait_for_timeout(3000)
        except Exception as e:
            logger.warning(f"  warm-up load issue ({str(e).splitlines()[0]})")

        for current_category, url in categories.items():
            current_category_global = current_category
            before = len(products)
            logger.info(f"Opening category: {current_category}")

            try:
                page.goto(url, timeout=60000)
            except Exception as e:                          # e.g. a category the store doesn't have -> ERR_TOO_MANY_REDIRECTS
                logger.warning(f"  skip {current_category} ({str(e).splitlines()[0]})")
                continue
            page.wait_for_timeout(6000)

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

            logger.info(f"  {current_category}: +{len(products) - before} rows")

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
logger.info("✅ JARS scraper completed successfully")
sys.exit(0)