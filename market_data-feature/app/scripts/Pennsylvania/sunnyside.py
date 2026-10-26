"""
Sunnyside (Cresco Labs) Pennsylvania scraper -- 18 stores.

Uses Cresco's fifo-inventory API directly. No headless browser needed.

  GET https://api.crescolabs.com/p/inventory/op/fifo-inventory
      ?category=<slug>&limit=500&usage_type=medical
      &inventory_type=retail&sellable=true&include_specials=true

  Required headers:
      X-API-Key:          hE1gQuwYcO54382jYNH0c9W0w4fEC3dJ8ljnwVau
      X-Client-Version:   4.49.0
      Ordering_App_Id:    9ha3c289-1260-4he2-nm62-4598bca34naa
      Store_Id:           <int>

`limit=500` returns full inventory per category in one call -- no
pagination needed. Cresco's edge geo-blocks non-US IPs at the CDN, so
set HTTPS_PROXY / CRESCO_PROXY to a US residential proxy on Azure.

Requirements:
    pip install curl_cffi pytz pandas requests
"""

import os
import sys
import json
import time
import logging
from datetime import datetime
import pytz
import pandas as pd

try:
    from curl_cffi import requests as _http
    _HTTP_BACKEND = "curl_cffi"
except ImportError:
    import requests as _http
    _HTTP_BACKEND = "requests"

# =========================
# LOGGER
# =========================
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

# =========================
# CONFIG
# =========================
company = "Sunnyside"
state = "Pennsylvania"

us_tz = pytz.timezone("America/Phoenix")
today = datetime.now(us_tz).strftime("%Y-%m-%d")

output_folder = "app/output/pending"
csv_folder = os.path.join(output_folder, "csv")
json_folder = os.path.join(output_folder, "json")
os.makedirs(csv_folder, exist_ok=True)
os.makedirs(json_folder, exist_ok=True)
output_file = os.path.join(csv_folder, f"{today}_{company}.csv")
json_file_path = os.path.join(json_folder, f"{today}_{company}.json")

API_URL = "https://api.crescolabs.com/p/inventory/op/fifo-inventory"
API_KEY = "hE1gQuwYcO54382jYNH0c9W0w4fEC3dJ8ljnwVau"
CLIENT_VERSION = "4.49.0"
ORDERING_APP_ID = "9ha3c289-1260-4he2-nm62-4598bca34naa"

PAGE_LIMIT = 500
REQUEST_DELAY_S = 0.35
HTTP_TIMEOUT = 30

CATEGORIES = [
    "flower", "vapes", "concentrates", "edibles", "topicals",
    "tinctures", "capsules", "accessories", "pre-rolls", "troches",
    "cbd", "clones", "apparel",
]

# All 18 Sunnyside PA stores (from /p/stores enumeration).
STORES = [
    {"dispensary_location": "Butler",                       "store_id": 202},
    {"dispensary_location": "New Kensington",               "store_id": 229},
    {"dispensary_location": "Pittsburgh",                   "store_id": 203},
    {"dispensary_location": "Pittsburgh (Lawrenceville)",   "store_id": 899},
    {"dispensary_location": "Philadelphia",                 "store_id": 619},
    {"dispensary_location": "Philadelphia (City Ave)",      "store_id": 634},
    {"dispensary_location": "Lancaster",                    "store_id": 633},
    {"dispensary_location": "Altoona",                      "store_id": 816},
    {"dispensary_location": "Phoenixville",                 "store_id": 635},
    {"dispensary_location": "Gettysburg",                   "store_id": 814},
    {"dispensary_location": "Erie",                         "store_id": 785},
    {"dispensary_location": "Ambler",                       "store_id": 650},
    {"dispensary_location": "Somerset",                     "store_id": 815},
    {"dispensary_location": "Greensburg",                   "store_id": 898},
    {"dispensary_location": "Montgomeryville",              "store_id": 636},
    {"dispensary_location": "Wyomissing",                   "store_id": 624},
    {"dispensary_location": "Washington",                   "store_id": 813},
    {"dispensary_location": "Beaver Falls",                 "store_id": 964},
]


# =========================
# HTTP / SESSION
# =========================
_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)


def _resolve_proxy():
    url = (
        os.environ.get("CRESCO_PROXY")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("HTTP_PROXY")
    )
    if not url:
        return None
    return {"http": url, "https": url}


def make_session():
    proxies = _resolve_proxy()
    if _HTTP_BACKEND == "curl_cffi":
        s = _http.Session(impersonate="chrome120")
    else:
        s = _http.Session()
    if proxies:
        s.proxies.update(proxies)
    logger.info(f"backend={_HTTP_BACKEND} {'via proxy' if proxies else 'DIRECT (no proxy)'}")
    return s


def build_headers(store_id):
    return {
        "User-Agent": _CHROME_UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://www.sunnyside.shop",
        "Referer": "https://www.sunnyside.shop/",
        "x-api-key": API_KEY,
        "x-client-version": CLIENT_VERSION,
        "ordering_app_id": ORDERING_APP_ID,
        "store_id": str(store_id),
    }


# =========================
# FIELD RESOLVERS
# =========================
_STRAIN_MAP = {
    "sativa": "Sativa", "indica": "Indica", "hybrid": "Hybrid",
    "sativa hybrid": "Sativa-Hybrid", "indica hybrid": "Indica-Hybrid",
    "sativa-hybrid": "Sativa-Hybrid", "indica-hybrid": "Indica-Hybrid",
    "hybrid indica": "Indica-Hybrid", "hybrid sativa": "Sativa-Hybrid",
    "cbd": "High CBD", "high cbd": "High CBD", "high-cbd": "High CBD",
}


def normalize_strain(raw):
    if not raw:
        return ""
    k = str(raw).strip().lower().replace("_", " ")
    if k in _STRAIN_MAP:
        return _STRAIN_MAP[k]
    for tok in ("indica-hybrid", "sativa-hybrid", "indica", "sativa", "hybrid", "high cbd", "cbd"):
        if tok in k:
            return _STRAIN_MAP[tok]
    return str(raw).strip().title()


def _num(v):
    if v in (None, "", 0):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def format_percent(v):
    n = _num(v)
    return f"{n:g} %" if n else ""


def format_mg(v):
    n = _num(v)
    return f"{n:g} mg" if n else ""


def resolve_potency(product):
    thc = format_percent(product.get("bt_potency_thc") or product.get("potency_thc"))
    cbd = format_percent(product.get("bt_potency_cbd") or product.get("potency_cbd"))
    if not thc:
        thc = format_mg(product.get("bt_potency_thc_total") or product.get("potency_thc_total"))
    if not cbd:
        cbd = format_mg(product.get("bt_potency_cbd_total") or product.get("potency_cbd_total"))
    return thc, cbd


def resolve_weight(product):
    w = product.get("bt_weight") or product.get("bt_usable_weight")
    if w in (None, "", 0):
        return ""
    try:
        f = float(w)
        if f >= 28:
            oz = round(f / 28)
            return f"{oz}oz" if abs(f - oz * 28) < 0.5 else f"{f:g}g"
        return f"{f:g}g"
    except (TypeError, ValueError):
        return str(w)


def resolve_price(product):
    price = _num(product.get("price"))
    discount = _num(product.get("discount_price"))
    if price and discount and discount < price:
        return price, discount
    return price, None


def resolve_deals(product):
    specials = product.get("specials") or []
    out = []
    seen = set()
    for s in specials:
        if not isinstance(s, dict):
            continue
        label = s.get("caption") or s.get("title") or s.get("description")
        if label and label not in seen:
            seen.add(label)
            out.append(label)
    return out


# =========================
# API CALL
# =========================
def fetch_category(session, store, category):
    params = {
        "category": category,
        "inventory_type": "retail",
        "require_sellable_quantity": "true",
        "include_specials": "true",
        "sellable": "true",
        "order_by": "brand",
        "limit": PAGE_LIMIT,
        "usage_type": "medical",
        "hob_first": "true",
    }
    r = session.get(API_URL, params=params, headers=build_headers(store["store_id"]), timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    return data.get("data") or []


# =========================
# MAIN
# =========================
all_rows = []
seen_keys = set()
debug_counts = []

try:
    session = make_session()
    logger.info(f"Starting {company} scrape | stores={len(STORES)}")

    for store in STORES:
        logger.info(f"--- {store['dispensary_location']} (store_id={store['store_id']}) ---")
        store_rows_before = len(all_rows)

        for category in CATEGORIES:
            try:
                products = fetch_category(session, store, category)
            except Exception as e:
                logger.warning(f"  {category}: fetch failed: {e}")
                continue

            debug_counts.append({
                "location": store["dispensary_location"],
                "category": category, "count": len(products),
            })
            if not products:
                continue

            cat_rows = 0
            for p in products:
                name = p.get("name") or ""
                if not name:
                    continue

                brand_name = p.get("brand") or p.get("cultivator") or ""
                strain = normalize_strain(p.get("strain_type"))
                thc, cbd = resolve_potency(p)
                weight = resolve_weight(p)
                price, sale_price = resolve_price(p)
                original_price = price if price is not None else sale_price
                deals = resolve_deals(p)

                if price and sale_price and price > sale_price:
                    savings = price - sale_price
                    pct = round(savings / price * 100)
                    deals.append(f"${savings:.2f} off ({pct}%)")

                dedupe = (store["dispensary_location"], p.get("id") or name, weight, original_price)
                if dedupe in seen_keys:
                    continue
                seen_keys.add(dedupe)

                subcategory = str(p.get("subcategory") or category).strip().upper().replace(" ", "-") or "DEFAULT"
                deal_columns = {f"deal_{i+1}": d for i, d in enumerate(deals)}
                row = {
                    "date": today,
                    "location": state,
                    "company": company,
                    "dispensary_location": store["dispensary_location"],
                    "name": name,
                    "category": category,
                    "subcategory": subcategory,
                    "brand": brand_name,
                    "strain_type": strain,
                    "THC": thc,
                    "CBD": cbd,
                    "weight": weight,
                    "original_price": original_price,
                }
                row.update(deal_columns)
                all_rows.append(row)
                cat_rows += 1
            logger.info(f"  {category}: {cat_rows} rows")
            time.sleep(REQUEST_DELAY_S)

        logger.info(f"=> {store['dispensary_location']}: {len(all_rows) - store_rows_before} rows")

except Exception as e:
    logger.critical(f"❌ {company} fetch failed: {e}", exc_info=True)
    sys.exit(1)


# =========================
# SAVE JSON
# =========================
try:
    with open(json_file_path, "w", encoding="utf-8") as f:
        json.dump({"per_store_category_counts": debug_counts, "total_rows": len(all_rows)}, f, indent=2, default=str)
    logger.info(f"JSON saved: {json_file_path}")
except Exception as e:
    logger.critical(f"❌ JSON save failed: {e}", exc_info=True)
    sys.exit(1)


# =========================
# SAVE CSV
# =========================
try:
    df = pd.DataFrame(all_rows).drop_duplicates(
        subset=["dispensary_location", "name", "weight", "original_price"]
    )
    if df.empty or len(df) < 5:
        logger.critical(
            f"❌ Invalid CSV output. Rows: {len(df)}. "
            f"Likely 403 from Cresco's US-geo-fence. Set HTTPS_PROXY to "
            f"a US residential proxy."
        )
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

    per_store = df.groupby(["dispensary_location"]).size().to_dict()
    cat_counts = df.groupby("category").size().to_dict()
    logger.info(f"CSV saved: {output_file} | Rows: {len(df)} | Deal cols: {len(deal_cols)}")
    logger.info(f"Rows per dispensary: {per_store}")
    logger.info(f"Rows per category: {cat_counts}")
except Exception as e:
    logger.critical(f"❌ CSV save failed: {e}", exc_info=True)
    sys.exit(1)

logger.info(f"✅ {company} scraper completed successfully")
sys.exit(0)