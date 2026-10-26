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
# PLATFORM: Higher Collective migrated off the old SweedPOS `GetProductList` XHR (now 403)
# to a Next.js front-end that SERVER-RENDERS the menu. Every product is in the page HTML,
# and the "All Products" menu paginates with ?page=N. So this scraper is plain `requests`
# + BeautifulSoup over the SSR pages — no API token, no browser.
#   https://highercollective.com/locations/<city>/menu/menu?page=N
# Run: pip install requests beautifulsoup4 pandas pytz
company = "HigherCollective"
state = "Connecticut"

us_tz = pytz.timezone("America/New_York")
today = datetime.now(us_tz).strftime("%Y-%m-%d")

output_folder = "app/output/pending"
csv_folder = os.path.join(output_folder, "csv")
json_folder = os.path.join(output_folder, "json")
os.makedirs(csv_folder, exist_ok=True)
os.makedirs(json_folder, exist_ok=True)
output_file = os.path.join(csv_folder, f"{today}_{company}.csv")
json_file_path = os.path.join(json_folder, f"{today}_{company}.json")

BASE = "https://highercollective.com/locations/{city}/menu/menu"
MAX_PAGES = 60
REQUEST_DELAY_S = 0.4
HTTP_TIMEOUT = 30

# CONNECTICUT stores — city slug in the menu URL.
stores = [
    {"city": "killingly", "dispensary_location": "Killingly"},
    {"city": "new-london", "dispensary_location": "New London"},
    {"city": "bridgeport", "dispensary_location": "Bridgeport"},
    {"city": "hamden", "dispensary_location": "Hamden"},
    {"city": "torrington", "dispensary_location": "Torrington"},
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

# product link: /menu/menu/<catslug>-<catid>/<productslug>-<sku>?...
PRODUCT_HREF_RE = re.compile(r"/menu/menu/([a-z0-9-]+?)-\d+/[a-z0-9.'\-]+?-(\d+)(?:\?|$)", re.IGNORECASE)
_STRAIN_RE = re.compile(r"0(?:SALE)?\s*(Indica Dominant|Sativa Dominant|Hybrid|Indica|Sativa)(?=[A-Z]|\s|$)")
_HEADER_RE = re.compile(
    r"0(?:SALE)?\s*(?:Indica Dominant|Sativa Dominant|Hybrid|Indica|Sativa|Accessories|Flower|"
    r"Vapes|Edibles|Pre-Rolls|Concentrates|Drinks|Hemp Derived|Apparel|Gift Cards|"
    r"Infused Pre-Rolls|Tinctures|Topicals)"
)
_POT_END_RE = re.compile(r"(?:THC|CBD):\s*[\d.]+\s*(?:%|MG|mg)")
_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?(?:g|mg|oz|ml)|\d+pk|Each)", re.IGNORECASE)
_DEAL_RE = re.compile(r"\d+\s*[Ff]or\s*\$\d+(?:\.\d+)?|\d+%\s*[Oo]ff|BOGO", re.IGNORECASE)


def _pot(cann, blob):
    m = re.search(rf"{cann}:\s*([\d.]+)\s*(%|MG|mg)", blob)
    if m:
        return f"{m.group(1)} {m.group(2).lower().replace('mg','mg') if m.group(2).upper()=='MG' else m.group(2)}"
    return None


def _longest_shared(header, region):
    """Brand = longest suffix of the header that is also a prefix of the after-'by' region."""
    hwords = header.split()
    for i in range(len(hwords)):
        cand = " ".join(hwords[i:])
        if cand and region.startswith(cand):
            return cand
    return ""


def parse_card(anchor, store):
    href = anchor.get("href", "")
    m = PRODUCT_HREF_RE.search(href)
    if not m:
        return None
    cat_slug, sku = m.group(1), m.group(2)
    blob = anchor.get_text(" ", strip=True)
    blob = re.sub(r"\s+", " ", blob)
    if not blob or " by " not in blob:
        return None

    category = cat_slug.replace("-", " ").title()

    # strain
    sm = _STRAIN_RE.search(blob)
    strain_type = sm.group(1).title() if sm else ""

    # header (text before the rating "0")
    hm = _HEADER_RE.search(blob)
    header = blob[:hm.start()].strip() if hm else ""

    # brand + product name from the "<Category> by <Brand><Name>THC/$..." region
    after_by = blob.split(" by ", 1)[1]
    name_region = re.split(r"THC:|CBD:|\$", after_by)[0].strip()
    brand = _longest_shared(header, name_region)
    name = name_region[len(brand):].strip() if brand else name_region
    # strip a trailing size word glued to the name (e.g. "...AssortedEach")
    name = re.sub(r"(Each)$", "", name).strip()
    if not name:
        name = header
    if not brand:
        brand = header.split()[-1] if header else ""

    # potency
    thc = _pot("THC", blob)
    cbd = _pot("CBD", blob)

    # size — first measurement AFTER the potency block (so edible "THC: 91.4MG" isn't misread)
    pot_ends = [x.end() for x in _POT_END_RE.finditer(blob)]
    after_pot = blob[pot_ends[-1]:] if pot_ends else blob
    szm = _SIZE_RE.search(after_pot)
    weight = szm.group(1) if szm else ""

    # deals (strip stock notes; "N For $X" / "N% Off" / BOGO / from-pricing)
    deals = _DEAL_RE.findall(blob)
    if not deals and re.search(r"\bfrom \$", blob):
        deals = ["Deal"]
    if not deals and re.search(r"0SALE\b", blob):
        deals = ["Sale"]
    deals = list(dict.fromkeys(d.strip() for d in deals))
    deal_columns = {f"deal_{i+1}": d for i, d in enumerate(deals)}

    # price — remove deal-embedded $ amounts first, then take trailing price(s)
    cleaned = _DEAL_RE.sub("", blob)
    cleaned = re.sub(r"\bfrom\b", "", cleaned)
    prices = re.findall(r"\$(\d+(?:\.\d+)?)", cleaned)
    price = float(prices[0]) if prices else None    # displayed/sale price

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
        url = BASE.format(city=store["city"]) + f"?page={page}"
        try:
            r = session.get(url, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
        except Exception as e:
            logger.warning(f"  {store['dispensary_location']} page {page}: request failed ({e})")
            break

        soup = BeautifulSoup(r.text, "html.parser")
        anchors, seen_anchor = [], set()
        for a in soup.find_all("a", href=True):
            mm = PRODUCT_HREF_RE.search(a["href"])
            if not mm:
                continue
            sku = mm.group(2)
            if sku in seen_anchor or not a.get_text(strip=True):
                continue
            seen_anchor.add(sku)
            anchors.append(a)

        if not anchors:
            logger.info(f"  {store['dispensary_location']} page {page}: 0 products — stopping")
            break

        added = 0
        for a in anchors:
            try:
                row = parse_card(a, store)
            except Exception as e:
                logger.warning(f"    card parse skipped: {e}")
                row = None
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
    logger.info(f"Starting {company} scrape | stores={len(stores)}")
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
