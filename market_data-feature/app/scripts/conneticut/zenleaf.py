import pandas as pd
import json
import os
import re
import time
from datetime import datetime
import pytz
import requests
from bs4 import BeautifulSoup
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
# PLATFORM: Zen Leaf migrated off the old SweedPOS `GetProductList` XHR (now 403) to a
# Next.js front-end that SERVER-RENDERS the menu. Every product is in the page HTML, and
# the "All Products" menu paginates with ?page=N. So this scraper is plain `requests` +
# BeautifulSoup over the SSR pages — no API token, no browser.
#   https://zenleafdispensaries.com/locations/<city>/menu/<saleType>?page=N
# Run: pip install requests beautifulsoup4 pandas pytz
company = "ZenLeaf"
state = "Connecticut"
SALE_TYPE = "recreational"

us_tz = pytz.timezone("America/New_York")
today = datetime.now(us_tz).strftime("%Y-%m-%d")

output_folder = "app/output/pending"
csv_folder = os.path.join(output_folder, "csv")
json_folder = os.path.join(output_folder, "json")
os.makedirs(csv_folder, exist_ok=True)
os.makedirs(json_folder, exist_ok=True)
output_file = os.path.join(csv_folder, f"{today}_{company}.csv")
json_file_path = os.path.join(json_folder, f"{today}_{company}.json")

BASE = "https://zenleafdispensaries.com/locations/{city}/menu/{sale}"
MAX_PAGES = 60
REQUEST_DELAY_S = 0.4
HTTP_TIMEOUT = 30

# CONNECTICUT stores — city slug in the menu URL.
stores = [
    {"city": "newington", "dispensary_location": "Newington"},
    {"city": "enfield", "dispensary_location": "Enfield"},
    {"city": "meriden", "dispensary_location": "Meriden"},
    {"city": "waterbury", "dispensary_location": "Waterbury"},
    {"city": "norwich", "dispensary_location": "Norwich"},
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# =========================
# GLOBAL STORAGE
# =========================
products = []
seen_keys = set()
all_json = []
session = requests.Session()
session.headers.update(HEADERS)

# product detail link: /menu/<catslug>-<catid>/<productslug>-<sku>
PRODUCT_HREF_RE = re.compile(r"/menu/([a-z0-9-]+?)-\d+/([a-z0-9-]+?)-(\d+)/?$", re.IGNORECASE)
_STRAINS = {"indica", "hybrid", "sativa", "indica dominant", "sativa dominant", "cbd", "high cbd"}
_MEASURE_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:mg|g|oz|ml)\b", re.IGNORECASE)
_PRICE_RE = re.compile(r"\$[\d,]+(?:\.\d+)?")
_POT_RE = lambda cann: re.compile(rf"{cann}\s*:?\s*([\d.]+)\s*(%|mg)", re.IGNORECASE)


def _skus_in(node):
    out = set()
    for a in node.find_all("a", href=True):
        m = PRODUCT_HREF_RE.search(a["href"])
        if m:
            out.add(m.group(3))
    return out


def _card_for(anchor):
    """Climb to the LARGEST ancestor that still contains only THIS product (one sku),
    i.e. the full product card — so a sibling product or the page's promo/filter prices
    can't bleed into this row."""
    best = anchor.parent or anchor
    node = anchor.parent
    for _ in range(10):
        if node is None:
            break
        if len(_skus_in(node)) <= 1:
            best = node
            node = node.parent
        else:
            break
    return best


def _pretty_cat(slug):
    return slug.replace("-", " ").title() if slug else ""


def parse_card(anchor, card, store):
    href = anchor.get("href", "")
    m = PRODUCT_HREF_RE.search(href)
    if not m:
        return None
    cat_slug, _, sku = m.group(1), m.group(2), m.group(3)

    name = anchor.get_text(" ", strip=True)
    if not name:
        return None

    lines = [ln.strip() for ln in card.get_text("\n", strip=True).split("\n") if ln.strip()]

    # strain type
    strain_type = ""
    for ln in lines:
        low = ln.lower()
        for s in _STRAINS:
            if low == s or low.startswith(s):
                strain_type = ln.split("Bold")[0].strip().title() if ln else ""
                break
        if strain_type:
            break

    # brand + category from "<Category> by <Brand>"
    brand, category = "", _pretty_cat(cat_slug)
    for ln in lines:
        if " by " in ln and len(ln) < 60:
            left, right = ln.split(" by ", 1)
            category = left.strip() or category
            brand = right.strip()
            break

    # potency
    def pot(cann):
        mm = _POT_RE(cann).search(" ".join(lines))
        if mm:
            unit = mm.group(2).upper().replace("MG", "mg").replace("%", "%")
            return f"{mm.group(1)} {unit}"
        return None
    thc = pot("THC")
    cbd = pot("CBD")

    # weight/size — the line with a measurement that isn't the potency line
    weight = ""
    for ln in lines:
        if ("thc" in ln.lower()) or ("cbd" in ln.lower()) or ("terp" in ln.lower()):
            continue
        mm = _MEASURE_RE.search(ln)
        if mm and "$" not in ln:
            weight = ln.strip()
            break

    # prices — first $ is the current/displayed price (sale price when on sale)
    prices = _PRICE_RE.findall(" ".join(lines))
    prices = [p for p in prices if "OFF" not in p]
    price = None
    if prices:
        try:
            price = float(prices[0].replace("$", "").replace(",", ""))
        except ValueError:
            price = None

    # deals — sale badges / discount labels
    deals = []
    for ln in lines:
        low = ln.lower()
        if low == "sale":
            deals.append("Sale")
        elif re.search(r"\d+%\s*off", low) or re.search(r"\$\d+\s*off", low) \
                or re.search(r"\d+-for-\$", low) or "bogo" in low:
            deals.append(ln.strip())
    deals = list(dict.fromkeys(deals))
    deal_columns = {f"deal_{i+1}": d for i, d in enumerate(deals)}

    key = (store["dispensary_location"], sku, weight)
    if key in seen_keys:
        return None
    seen_keys.add(key)

    row = {
        "date": today,
        "location": state,
        "company": company,
        "dispensary_location": store["dispensary_location"],
        "name": name,
        "category": category,
        "subcategory": "",
        "brand": brand,
        "strain_type": strain_type,
        "THC": thc,
        "CBD": cbd,
        "weight": weight,
        "original_price": price,
    }
    row.update(deal_columns)
    return row


def scrape_store(store):
    before = len(products)
    for page in range(1, MAX_PAGES + 1):
        url = BASE.format(city=store["city"], sale=SALE_TYPE) + (f"?page={page}" if page > 1 else "")
        try:
            r = session.get(url, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
        except Exception as e:
            logger.warning(f"  {store['dispensary_location']} page {page}: request failed ({e})")
            break

        soup = BeautifulSoup(r.text, "html.parser")
        anchors, seen_anchor = [], set()
        for a in soup.find_all("a", href=True):
            m = PRODUCT_HREF_RE.search(a["href"])
            if not m:
                continue
            sku = m.group(3)
            if sku in seen_anchor:
                continue
            # prefer the anchor that carries the product NAME (non-empty text)
            if not a.get_text(strip=True):
                continue
            seen_anchor.add(sku)
            anchors.append(a)

        if not anchors:
            logger.info(f"  {store['dispensary_location']} page {page}: 0 products — stopping")
            break

        added = 0
        for a in anchors:
            row = parse_card(a, _card_for(a), store)
            if row:
                products.append(row)
                added += 1
        all_json.append({"store": store["dispensary_location"], "page": page, "count": added})
        logger.info(f"  {store['dispensary_location']} page {page}: +{added} (total {len(products)})")

        if added == 0:
            break
        time.sleep(REQUEST_DELAY_S)

    logger.info(f"=> {store['dispensary_location']}: {len(products) - before} rows")


# =========================
# MAIN
# =========================
try:
    logger.info(f"Starting {company} scrape | sale_type={SALE_TYPE} | stores={len(stores)}")
    for store in stores:
        logger.info(f"=== {store['dispensary_location']} ({store['city']}) ===")
        scrape_store(store)
except Exception as e:
    logger.critical(f"❌ Scraper failed: {e}", exc_info=True)
    sys.exit(1)


# =========================
# SAVE JSON
# =========================
try:
    if not products:
        logger.critical("❌ No products collected")
        sys.exit(1)
    with open(json_file_path, "w", encoding="utf-8") as f:
        json.dump(products, f, indent=2)
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
