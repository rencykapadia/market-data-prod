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
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

# =========================
# CONFIG
# =========================
# PLATFORM: KURE runs the DUTCHIE menu (dutchie.com/embedded-menu/<slug>), embedded on
# kureforlife.com. Same approach as Botanica / The Vault: Playwright opens each category
# and we intercept the `FilteredProducts` GraphQL RESPONSE. Runs HEADLESS. When Cloudflare
# intermittently challenges, no FilteredProducts fires and the run reports 0 rows — re-run.
# KURE is a Santa Fe chain; `kure-cerrillos` is the Cerrillos Rd drive-thru. Add the other
# KURE slugs (kure-downtown / kure-eldorado, verify in the site's menu iframe) for more stores.
# Run: pip install playwright pandas pytz && playwright install chromium
company = "KURE"
location = "New Mexico"

HEADLESS = True
NAV_TIMEOUT = 90000

us_tz = pytz.timezone("America/Denver")   # New Mexico (Mountain)
today = datetime.now(us_tz).strftime("%Y-%m-%d")

output_folder = "app/output/pending"
csv_folder = os.path.join(output_folder, "csv")
json_folder = os.path.join(output_folder, "json")
os.makedirs(csv_folder, exist_ok=True)
os.makedirs(json_folder, exist_ok=True)
output_file = os.path.join(csv_folder, f"{today}_{company}.csv")
json_file_path = os.path.join(json_folder, f"{today}_{company}.json")

# Dutchie embedded-menu slug -> dispensary label.
stores = [
    {"slug": "kure-cerrillos", "dispensary_location": "Santa Fe - Cerrillos"},
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
current_store_global = None


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


def variants_from(item):
    """Yield (weight, price). Handles both Dutchie structures: POSMetaData.children and the
    top-level Options[]/recPrices[] arrays (older/bulk items like flower)."""
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
# RESPONSE HANDLER (detect by body, not URL)
# =========================
def handle_response(response):
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
        key = (current_store_global, current_category_global, pid)
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
                    "dispensary_location": current_store_global,
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


def dismiss_age_gate(page):
    for lab in ("Yes", "I am 21", "I'm 21", "Yes, I am 21", "Enter", "Accept", "Agree"):
        try:
            page.get_by_role("button", name=lab, exact=False).first.click(timeout=1200)
            page.wait_for_timeout(1500)
            return
        except Exception:
            pass


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

            # warm up (age gate + boot) so the first category (Flower) isn't captured empty
            try:
                page.goto(f"{base_url}/flower", timeout=60000)
                page.wait_for_timeout(5000)
                dismiss_age_gate(page)
                page.wait_for_timeout(2000)
            except Exception as e:
                logger.warning(f"  warm-up issue ({str(e).splitlines()[0]})")

            for current_category, path in CATEGORY_PATHS.items():
                current_category_global = current_category
                cbefore = len(products)
                logger.info(f"Opening category: {current_category}")
                try:
                    page.goto(f"{base_url}/{path}", timeout=60000)
                except Exception as e:                       # missing category -> ERR_TOO_MANY_REDIRECTS
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

                logger.info(f"  {current_category}: +{len(products) - cbefore} rows")

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
    deal_cols = sorted([c for c in df.columns if c.startswith("deal_")],
                       key=lambda x: int(x.split("_")[1]))
    df = df[fixed_cols + deal_cols]

    df.to_csv(output_file, index=False, na_rep="")
    logger.info(f"CSV saved: {output_file} | Rows: {len(df)}")
except Exception as e:
    logger.critical(f"❌ CSV save failed: {e}", exc_info=True)
    sys.exit(1)


logger.info(f"✅ {company} scraper completed successfully")
sys.exit(0)
