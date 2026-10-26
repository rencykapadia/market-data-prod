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
# PLATFORM: Dark Matter (New Mexico) runs a DUTCHIE embedded menu proxied on its own domain at
#   https://darkmattercanna.com/stores/<store-slug>/products/<category>
# Same technique as JARS: Playwright opens each store's category and we intercept the
# `FilteredProducts` GraphQL RESPONSE (detected by BODY). If Cloudflare challenges, no
# FilteredProducts fires and that store/category reports 0 rows — just re-run.
#
# STORES (NM only): Albuquerque, Sunland Park, Hobbs.
#
# PRICE NOTE: original_price is the OFFERED (sale) price when a product is on special. Dutchie keeps
# the discounted price in the index-aligned `recSpecialPrices` array (NOT in POSMetaData.children,
# which only carry the regular price), so variants_from() maps each option to its special price and
# falls back to the regular price when there's no special. E.g. "Han Solo Burger" reg $16 -> $12.80.
# Run: pip install playwright pandas pytz && playwright install chromium
company = "DarkMatter"
location = "New Mexico"

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

STORE_BASE = "https://darkmattercanna.com/stores"
# NM Dark Matter stores (store-slug -> dispensary_location).
STORES = [
    {"slug": "dark-matter-albuquerque", "dispensary_location": "Albuquerque"},
    {"slug": "dark-matter-sunland-park", "dispensary_location": "Sunland Park"},
    {"slug": "dark-matter-hobbs", "dispensary_location": "Hobbs"},
]
# affinity category label -> Dutchie category slug
CATEGORY_SLUGS = {
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


def store_cat_url(slug, cat_slug):
    return f"{STORE_BASE}/{slug}/products/{cat_slug}"


products = []
seen_ids = set()
all_json = []
current_category_global = None
current_dispensary = None      # set per-store in the loop; used by handle_response


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


def _first_positive(arr, i):
    return arr[i] if (0 <= i < len(arr) and arr[i] not in (None, 0)) else None


def variants_from(item):
    """Yield (weight_option, offered_price) — the SALE price when a special exists, else regular.

    Dutchie stores the discounted price in the index-aligned recSpecialPrices/medSpecialPrices
    arrays (parallel to Options / recPrices), while POSMetaData.children only carry the regular
    price. So we build option -> special/regular maps and prefer the special.
    """
    options = item.get("Options") or []
    rec_special = item.get("recSpecialPrices") or []
    med_special = item.get("medSpecialPrices") or []
    rec_prices = item.get("recPrices") or item.get("Prices") or []
    med_prices = item.get("medPrices") or []

    special_by_opt, reg_by_opt = {}, {}
    for i, opt in enumerate(options):
        sp = _first_positive(rec_special, i)
        if sp is None:
            sp = _first_positive(med_special, i)
        if sp is not None:
            special_by_opt[opt] = sp
        rp = _first_positive(rec_prices, i)
        if rp is None:
            rp = _first_positive(med_prices, i)
        if rp is not None:
            reg_by_opt[opt] = rp

    children = (item.get("POSMetaData") or {}).get("children") or []
    if children:
        for v in children:
            opt = v.get("option")
            price = special_by_opt.get(opt)
            if price is None:
                price = v.get("specialPrice") or v.get("recSpecialPrice")
            if price is None:
                price = v.get("price")
                if price is None:
                    price = v.get("recPrice")
                if price is None:
                    price = v.get("medPrice")
            yield opt, price
        return

    if options:
        for i, opt in enumerate(options):
            price = special_by_opt.get(opt)
            if price is None:
                price = reg_by_opt.get(opt)
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
        key = (current_dispensary, current_category_global, pid)
        if key in seen_ids:
            continue
        seen_ids.add(key)
        deals = extract_deals(item)
        deal_columns = {f"deal_{i+1}": d for i, d in enumerate(deals)}
        for weight, price in variants_from(item):
            product = {
                "date": today, "location": location, "company": company,
                "dispensary_location": current_dispensary, "name": item.get("Name"),
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
    logger.info(f"Starting {company} scrape | stores={len(STORES)} (Dutchie via browser interception)")
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

        age_gate_done = False
        for store in STORES:
            current_dispensary = store["dispensary_location"]
            slug = store["slug"]
            store_before = len(products)
            logger.info(f"=== {current_dispensary} ({slug}) ===")

            # Warm up on this store's flower page (dismiss the domain-wide age gate once).
            try:
                page.goto(store_cat_url(slug, "flower"), timeout=60000)
                page.wait_for_timeout(5000)
                if not age_gate_done:
                    dismiss_age_gate(page)
                    age_gate_done = True
                    page.wait_for_timeout(3000)
            except Exception as e:
                logger.warning(f"  {current_dispensary}: warm-up issue ({str(e).splitlines()[0]})")

            for cat_label, cat_slug in CATEGORY_SLUGS.items():
                current_category_global = cat_label
                before = len(products)
                logger.info(f"Opening category: {cat_label}")
                try:
                    page.goto(store_cat_url(slug, cat_slug), timeout=60000)
                except Exception as e:
                    logger.warning(f"  skip {cat_label} ({str(e).splitlines()[0]})")
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
                logger.info(f"  {cat_label}: +{len(products) - before} rows")

            logger.info(f"=> {current_dispensary}: {len(products) - store_before} rows")

        browser.close()
except Exception as e:
    logger.critical(f"❌ Playwright failed: {e}", exc_info=True)
    sys.exit(1)


# =========================
# SAVE JSON + CSV
# =========================
try:
    if not all_json:
        logger.critical("❌ No API responses captured — Cloudflare check likely; re-run (or set HEADLESS=False locally).")
        sys.exit(1)
    with open(json_file_path, "w", encoding="utf-8") as f:
        json.dump(all_json, f, indent=2)
    logger.info(f"JSON saved: {json_file_path}")

    df = pd.DataFrame(products).drop_duplicates(subset=["dispensary_location", "name", "weight"])
    if df.empty or len(df) < 10:
        logger.critical(f"❌ Invalid CSV output. Rows: {len(df)}")
        sys.exit(1)
    fixed_cols = ["date", "location", "company", "dispensary_location", "name",
                  "category", "subcategory", "brand", "strain_type",
                  "THC", "CBD", "weight", "original_price"]
    deal_cols = sorted([c for c in df.columns if c.startswith("deal_")],
                       key=lambda x: int(x.split("_")[1]))
    df = df[fixed_cols + deal_cols]
    try:
        df.to_csv(output_file, index=False, na_rep="")
        logger.info(f"CSV saved: {output_file} | Rows: {len(df)}")
    except PermissionError:
        alt = output_file.replace(".csv", f"_{datetime.now(us_tz).strftime('%H%M%S')}.csv")
        df.to_csv(alt, index=False, na_rep="")
        logger.warning(f"⚠ {output_file} is locked (open in Excel?). Saved to {alt} | Rows: {len(df)}")
except SystemExit:
    raise
except Exception as e:
    logger.critical(f"❌ CSV save failed: {e}", exc_info=True)
    sys.exit(1)

logger.info(f"✅ {company} scraper completed successfully ({len(STORES)} NM stores)")
sys.exit(0)
