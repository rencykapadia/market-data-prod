import pandas as pd
import json
import os
import re
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
# PLATFORM: Amnesia Dispensary runs a WIX website with a native Wix Stores menu (no third-party
# ordering embed). Each category is its own page (/flower, /cartridges, /vapes, /concentrates,
# /recreational-edibles-drinks) and products render as `div.item-link-wrapper` cards carrying
# "<name> $<price>/<unit>  <thc>% THC (<strain>)". Wix hydrates the grid client-side, so we load
# each category in a real browser and parse the rendered cards. One row per product.
# Run: pip install playwright pandas pytz && playwright install chromium
company = "Amnesia"
location = "New Mexico"
dispensary_location = "Albuquerque - San Mateo"

BASE = "https://www.amnesiadispensary.com"
HEADLESS = True   # flip to False if Wix throttles the headless browser
MAX_SCROLLS = 40

# page slug -> affinity category.
# NOTE: as of this build only /flower is a real Wix Stores product grid. The other category pages
# (/cartridges, /vapes, /concentrates, /recreational-edibles-drinks) are descriptive placeholders
# marked "PRODUCT IMAGES COMING SOON" with no product listing — add them back here once Amnesia
# populates them.
CATEGORIES = {
    "flower": "Flower",
    # "cartridges": "Vaporizers", "vapes": "Vaporizers",
    # "concentrates": "Concentrates", "recreational-edibles-drinks": "Edibles",
}
STRAIN_RE = re.compile(r"\b(Indica Dominant|Sativa Dominant|Hybrid|Indica|Sativa|CBD)\b", re.IGNORECASE)
UNIT_MAP = {"gram": "1g", "g": "1g", "gm": "1g", "each": "", "ea": "", "unit": "",
            "oz": "1oz", "ounce": "1oz", "pk": "", "ct": ""}

us_tz = pytz.timezone("America/Denver")
today = datetime.now(us_tz).strftime("%Y-%m-%d")

output_folder = "app/output/pending"
csv_folder = os.path.join(output_folder, "csv")
json_folder = os.path.join(output_folder, "json")
os.makedirs(csv_folder, exist_ok=True)
os.makedirs(json_folder, exist_ok=True)
output_file = os.path.join(csv_folder, f"{today}_{company}.csv")
json_file_path = os.path.join(json_folder, f"{today}_{company}.json")

products = []
seen = set()

_DOM_EXTRACT = r"""() => {
  const out = [], seen = new Set();
  let cards = [...document.querySelectorAll('.item-link-wrapper')];
  if (cards.length < 3) {
    cards = [...document.querySelectorAll('div,li,a')].filter(e => {
      const t = e.innerText || '';
      return /\$\d/.test(t) && /(THC|Hybrid|Indica|Sativa|CBD|\/\s*gram|\/\s*each)/i.test(t)
             && t.length < 220 && e.children.length < 12;
    });
  }
  for (const e of cards) {
    const t = (e.innerText || '').trim();
    if (!t || seen.has(t)) continue;
    seen.add(t);
    out.push(t);
  }
  return out;
}"""


def parse_cards(cards, category):
    for text in cards:
        flat = re.sub(r"\s+", " ", text).strip()
        if "$" not in flat:
            continue
        name = flat.split("$")[0].strip(" -–")
        if not name or len(name) < 2:
            continue
        pm = re.search(r"\$([\d.]+)\s*/?\s*([a-zA-Z]+)?", flat)
        price = float(pm.group(1)) if pm else None
        unit = (pm.group(2) or "").lower() if pm else ""
        weight = UNIT_MAP.get(unit, "" if unit in ("", "thc") else "")
        thcm = re.search(r"([\d.]+)\s*%\s*THC", flat, re.IGNORECASE) or \
            re.search(r"THC[:\s]*([\d.]+)\s*%", flat, re.IGNORECASE) or \
            re.search(r"([\d.]+)\s*%", flat)
        thc = f"{thcm.group(1)}%" if thcm else ""
        sm = STRAIN_RE.search(flat)
        strain = sm.group(1).title() if sm else ""

        key = (dispensary_location, name, weight, category)
        if key in seen:
            continue
        seen.add(key)
        products.append({
            "date": today, "location": location, "company": company,
            "dispensary_location": dispensary_location, "name": name,
            "category": category, "subcategory": "", "brand": "",
            "strain_type": strain, "THC": thc, "CBD": "",
            "weight": weight, "original_price": price,
        })


def scrape_category(page, slug, category):
    before = len(products)
    try:
        page.goto(f"{BASE}/{slug}", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4500)
    except Exception as e:
        logger.warning(f"  {slug}: nav failed ({e})")
        return
    for _ in range(MAX_SCROLLS):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(900)
        # Wix loads all grid items on one page; a few scrolls settle lazy images/rows
    try:
        cards = page.evaluate(_DOM_EXTRACT)
    except Exception as e:
        logger.warning(f"  {slug}: DOM extract failed ({e})")
        cards = []
    parse_cards(cards, category)
    logger.info(f"  {slug} ({category}): {len(cards)} cards -> +{len(products) - before} rows")


# =========================
# PLAYWRIGHT
# =========================
try:
    logger.info(f"Starting {company} scrape | {dispensary_location} (Wix Stores via browser)")
    with sync_playwright() as p:
        launch_args = {"headless": HEADLESS, "args": ["--disable-blink-features=AutomationControlled"]}
        try:
            browser = p.chromium.launch(channel="chrome", **launch_args)
        except Exception:
            browser = p.chromium.launch(**launch_args)
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            viewport={"width": 1440, "height": 2200},
        )
        ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        page = ctx.new_page()
        for slug, category in CATEGORIES.items():
            scrape_category(page, slug, category)
        browser.close()
except Exception as e:
    logger.critical(f"❌ Scraper failed: {e}", exc_info=True)
    sys.exit(1)


# =========================
# SAVE JSON + CSV
# =========================
try:
    if not products:
        logger.critical("❌ No products collected (try HEADLESS=False).")
        sys.exit(1)
    with open(json_file_path, "w", encoding="utf-8") as f:
        json.dump(products, f, indent=2)
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
    df.to_csv(output_file, index=False, na_rep="")
    logger.info(f"CSV saved: {output_file} | Rows: {len(df)}")
except SystemExit:
    raise
except Exception as e:
    logger.critical(f"❌ Save failed: {e}", exc_info=True)
    sys.exit(1)

logger.info(f"✅ {company} scraper completed successfully")
sys.exit(0)
