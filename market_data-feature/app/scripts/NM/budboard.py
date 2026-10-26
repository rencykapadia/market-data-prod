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
# PLATFORM: Bud Board (Hobbs) runs a DUTCHIE menu hosted directly on dutchie.com
# (budboard.biz's "Click Here to Checkout" links to dutchie.com/dispensary/bud-board-dispensary-hobbs).
# Category pages are /dispensary/<slug>/products/<category>. We open each category in a real browser
# and INTERCEPT the `FilteredProducts` GraphQL RESPONSE (detected by body). dutchie.com is behind
# Cloudflare, so run HEADLESS=False (a visible real Chrome clears the challenge); if it 0-rows, re-run.
# Run: pip install playwright pandas pytz && playwright install chromium
company = "BudBoard"
location = "New Mexico"
dispensary_location = "Hobbs"

# Headed on local Windows (a visible Chrome clears the Cloudflare challenge); headless where there's
# no display — e.g. the scheduled Linux container, which has no X server for a headed browser.
HEADLESS = os.name != "nt" and not os.environ.get("DISPLAY")

us_tz = pytz.timezone("America/Denver")
today = datetime.now(us_tz).strftime("%Y-%m-%d")

output_folder = "app/output/pending"
csv_folder = os.path.join(output_folder, "csv")
json_folder = os.path.join(output_folder, "json")
os.makedirs(csv_folder, exist_ok=True)
os.makedirs(json_folder, exist_ok=True)
output_file = os.path.join(csv_folder, f"{today}_{company}.csv")
json_file_path = os.path.join(json_folder, f"{today}_{company}.json")

PAGE = "https://dutchie.com/dispensary/bud-board-dispensary-hobbs"
# Dutchie category slugs -> affinity category label
CATEGORY_SLUGS = {
    "flower": "Flower",
    "pre-rolls": "Pre-rolls",
    "vaporizers": "Vaporizers",
    "concentrates": "Concentrates",
    "edibles": "Edibles",
    "tinctures": "Tinctures",
    "topicals": "Topicals",
    "cbd": "CBD",
    "accessories": "Accessories",
}


def cat_url(slug):
    return f"{PAGE}/products/{slug}"


products = []
seen_ids = set()
all_json = []
current_category_global = None


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
    for lab in ("Yes I am", "Yes, I am 21", "Yes", "I am 21", "I'm 21", "Enter", "Accept", "Confirm"):
        for role in ("button", "link"):
            try:
                page.get_by_role(role, name=lab, exact=False).first.click(timeout=1000)
                logger.info(f"Passed age gate ({lab})")
                page.wait_for_timeout(1500)
                return
            except Exception:
                pass


def variants_from(item):
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
        key = (dispensary_location, current_category_global, pid)
        if key in seen_ids:
            continue
        seen_ids.add(key)
        deals = extract_deals(item)
        deal_columns = {f"deal_{i+1}": d for i, d in enumerate(deals)}
        for weight, price in variants_from(item):
            product = {
                "date": today, "location": location, "company": company,
                "dispensary_location": dispensary_location, "name": item.get("Name"),
                "category": current_category_global,
                "subcategory": (item.get("POSMetaData") or {}).get("canonicalCategory"),
                "brand": item.get("brandName"), "strain_type": item.get("strainType"),
                "THC": format_potency(item.get("THCContent")),
                "CBD": format_potency(item.get("CBDContent")),
                "weight": weight, "original_price": price,
            }
            product.update(deal_columns)
            products.append(product)


# =========================
# PLAYWRIGHT SCRAPER
# =========================
try:
    with sync_playwright() as p:
        launch_args = {"headless": HEADLESS, "args": ["--disable-blink-features=AutomationControlled"]}
        try:
            browser = p.chromium.launch(channel="chrome", **launch_args)
        except Exception:
            browser = p.chromium.launch(**launch_args)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
        )
        context.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        page = context.new_page()
        page.on("response", handle_response)

        try:
            page.goto(cat_url("flower"), timeout=60000)
            page.wait_for_timeout(6000)      # let Cloudflare clear
            dismiss_age_gate(page)
            page.wait_for_timeout(3000)
        except Exception as e:
            logger.warning(f"  warm-up load issue ({str(e).splitlines()[0]})")

        for slug, affinity_cat in CATEGORY_SLUGS.items():
            current_category_global = affinity_cat
            before = len(products)
            logger.info(f"Opening category: {affinity_cat}")
            try:
                page.goto(cat_url(slug), timeout=60000)
            except Exception as e:
                logger.warning(f"  skip {affinity_cat} ({str(e).splitlines()[0]})")
                continue
            page.wait_for_timeout(6000)
            while True:
                page.mouse.wheel(0, 4000)
                page.wait_for_timeout(2000)
                next_btn = page.locator('button[aria-label="go to next page"]')
                if next_btn.count() == 0 or next_btn.is_disabled():
                    break
                next_btn.click()
                page.wait_for_timeout(4000)
            logger.info(f"  {affinity_cat}: +{len(products) - before} rows")

        browser.close()
except Exception as e:
    logger.critical(f"❌ Playwright failed: {e}", exc_info=True)
    sys.exit(1)


# =========================
# SAVE JSON + CSV
# =========================
try:
    if not all_json:
        logger.critical("❌ No API responses captured — Cloudflare check likely; re-run (or HEADLESS=False).")
        sys.exit(1)
    with open(json_file_path, "w", encoding="utf-8") as f:
        json.dump(all_json, f, indent=2)
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
    logger.critical(f"❌ CSV save failed: {e}", exc_info=True)
    sys.exit(1)

logger.info(f"✅ {company} scraper completed successfully")
sys.exit(0)
