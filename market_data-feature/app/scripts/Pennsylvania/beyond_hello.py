"""
Beyond Hello (Pittsburgh, PA) scraper.

Uses iHeartJane's Boost widget API directly. No headless browser needed.

  Endpoint: POST https://dmerch.iheartjane.com/v2/multi
    ?jdm_api_key=ce5f15c9-3d09-441d-9bfd-26e87aff5925
    &jdm_source=monolith&jdm_version=2.24.1

  Body (per placement): { placements: [{ placement: "menu_inline_table",
    page_size: 200, search_filter: "" }], store_id: <int>, ... }

  Response: placements[0].products[].search_attributes has full product
  data (name, brand, category, potency, prices, weights, deals).

The api_key above is Jane's public storefront key -- same one their own
widget uses. No auth required beyond a US IP (Jane geo-blocks datacenter
egress from outside the US).

Env vars:
    JANE_PROXY / HTTPS_PROXY  -- residential US proxy (required on Azure)

Requirements:
    pip install curl_cffi pytz pandas requests
"""

import os
import re
import sys
import json
import time
import logging
from datetime import datetime
import pytz
import pandas as pd

# curl_cffi mimics Chrome's TLS fingerprint. Jane's edge doesn't always
# require it, but it's cheap insurance against future WAF rules.
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
company = "BeyondHello"
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

# Beyond Hello PA stores (all 18). Store ids captured from
# window.___JANE___.runtimeConfig.storeId on each store's Boost widget
# page (beyond-hello.com/pennsylvania-dispensaries/<slug>/medical-menu/).
# NOTE: the WordPress page's `store-id="XXXX"` attribute is the widget's
# embed-config id, NOT the Jane store_id -- always trust runtimeConfig.
STORES = [
    {"dispensary_location": "Ardmore",                        "store_id": 1852},
    {"dispensary_location": "Belle Vernon",                   "store_id": 1638},
    {"dispensary_location": "Bethlehem",                      "store_id": 1132},
    {"dispensary_location": "Bristol",                        "store_id": 1635},
    {"dispensary_location": "Dickson City",                   "store_id": 1115},
    {"dispensary_location": "Easton",                         "store_id": 3298},
    {"dispensary_location": "Irwin",                          "store_id": 2935},
    {"dispensary_location": "Linwood",                        "store_id": 3299},
    {"dispensary_location": "Mt Pocono",                      "store_id": 3263},
    {"dispensary_location": "Philadelphia - Center City",     "store_id": 1636},
    {"dispensary_location": "Philadelphia - Northern Liberties", "store_id": 1637},
    {"dispensary_location": "Philadelphia - University City", "store_id": 3300},
    {"dispensary_location": "Pittsburgh",                     "store_id": 3301},
    {"dispensary_location": "Pottsville",                     "store_id": 3368},
    {"dispensary_location": "Reading",                        "store_id": 1978},
    {"dispensary_location": "Scranton",                       "store_id": 1639},
    {"dispensary_location": "Stroudsburg",                    "store_id": 3371},
    {"dispensary_location": "West Chester",                   "store_id": 1640},
]

# =========================
# JANE API
# =========================
API_URL = "https://dmerch.iheartjane.com/v2/multi"
JDM_API_KEY = "ce5f15c9-3d09-441d-9bfd-26e87aff5925"   # Public storefront key
JDM_SOURCE = "monolith"
JDM_VERSION = "2.24.1"
DEFAULT_PAGE_SIZE = 200  # Jane's max; menu_inline_table returns up to this many

# Category filters -- Jane's Algolia-style filter syntax
CATEGORY_FILTERS = [
    ("flower",      '(kind:"flower" OR root_types:"flower")'),
    ("vape",        '(kind:"vape" OR root_types:"vape")'),
    ("extract",     '(kind:"extract" OR root_types:"extract")'),
    ("edible",      '(kind:"edible" OR root_types:"edible")'),
    ("pre-roll",    '(kind:"pre-roll" OR root_types:"pre-roll")'),
    ("tincture",    '(kind:"tincture" OR root_types:"tincture")'),
    ("topical",     '(kind:"topical" OR root_types:"topical")'),
    ("gear",        '(kind:"gear" OR root_types:"gear")'),
    ("merch",       '(kind:"merch" OR root_types:"merch")'),
]

REQUEST_DELAY_S = 0.4
HTTP_TIMEOUT = 30

# =========================
# HTTP SESSION / PROXY
# =========================
_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)


def _resolve_proxy():
    url = (
        os.environ.get("JANE_PROXY")
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


HEADERS = {
    "User-Agent": _CHROME_UA,
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Origin": "https://beyond-hello.com",
    "Referer": "https://beyond-hello.com/",
    "sec-fetch-site": "cross-site",
    "sec-fetch-mode": "cors",
    "sec-fetch-dest": "empty",
}


# =========================
# API CALL
# =========================
def fetch_category(session, store_id, category_filter):
    """
    Hit /v2/multi with a placement request for one category.
    Returns list of product dicts (each with .search_attributes).
    """
    params = {
        "jdm_api_key": JDM_API_KEY,
        "jdm_source": JDM_SOURCE,
        "jdm_version": JDM_VERSION,
    }
    body = {
        "app_mode": "framelessEmbed",
        "distinct_id": f"scraper-{today}",
        "jane_device_id": f"scraper-{today}",
        "num_columns": 6,
        "search_attributes": ["*"],
        "store_id": store_id,
        "placements": [{
            "disable_ads": True,
            "page_size": DEFAULT_PAGE_SIZE,
            "placement": "menu_inline_table",
            "search_filter": category_filter,
            "search_sort": "recommendation",
        }],
        "type": "custom",
    }
    r = session.post(API_URL, params=params, headers=HEADERS,
                     data=json.dumps(body), timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    placements = data.get("placements") or []
    if not placements:
        return []
    return placements[0].get("products") or []


# =========================
# PARSER
# =========================
_STRAIN_MAP = {
    "sativa": "Sativa", "indica": "Indica", "hybrid": "Hybrid",
    "sativa hybrid": "Sativa-Hybrid", "indica hybrid": "Indica-Hybrid",
    "sativa-hybrid": "Sativa-Hybrid", "indica-hybrid": "Indica-Hybrid",
    "hybrid_indica": "Indica-Hybrid", "hybrid_sativa": "Sativa-Hybrid",
    "cbd": "High CBD", "high-cbd": "High CBD", "high cbd": "High CBD",
}


def normalize_strain(raw):
    if not raw:
        return ""
    k = str(raw).strip().lower().replace("_", " ")
    if k in _STRAIN_MAP:
        return _STRAIN_MAP[k]
    for token in ("indica-hybrid", "sativa-hybrid", "indica", "sativa", "hybrid", "high cbd", "cbd"):
        if token in k:
            return _STRAIN_MAP[token]
    return str(raw).strip().title()


def format_potency_percent(v):
    if v is None or v == "":
        return ""
    try:
        return f"{float(v):g} %"
    except (TypeError, ValueError):
        return ""


def format_potency_mg(v):
    if v is None or v == "":
        return ""
    try:
        return f"{float(v):g} mg"
    except (TypeError, ValueError):
        return ""


# Jane's per-weight price fields we care about
_WEIGHT_KEYS = [
    ("half gram",     "price_half_gram",      "discounted_price_half_gram",     "0.5g"),
    ("gram",          "price_each_gram",      "discounted_price_each_gram",     "1g"),
    ("two gram",      "price_two_gram",       "discounted_price_two_gram",      "2g"),
    ("eighth ounce",  "price_eighth_ounce",   "discounted_price_eighth_ounce",  "3.5g"),
    ("quarter ounce", "price_quarter_ounce",  "discounted_price_quarter_ounce", "7g"),
    ("half ounce",    "price_half_ounce",     "discounted_price_half_ounce",    "14g"),
    ("ounce",         "price_ounce",          "discounted_price_ounce",         "28g"),
]


def resolve_variants(attrs):
    """
    Jane exposes per-weight prices on the product. Emit one variant tuple
    per available weight. Falls back to a single flat variant if the
    per-weight fields aren't populated.
    """
    variants = []
    for jane_weight, price_key, sale_key, weight_label in _WEIGHT_KEYS:
        price = attrs.get(price_key)
        sale = attrs.get(sale_key)
        if price in (None, 0) and sale in (None, 0):
            continue
        variants.append((weight_label, price, sale))
    if variants:
        return variants
    # Fallback: no per-weight breakdown, use bucket_price or per-unit
    price = attrs.get("bucket_price") or attrs.get("price_per_unit")
    if price:
        return [("", price, None)]
    return []


def parse_product(product):
    attrs = product.get("search_attributes") or {}
    if not attrs:
        return []

    name = attrs.get("name") or ""
    if not name:
        return []

    brand = attrs.get("brand") or ""
    subcategory = attrs.get("brand_subtype") or attrs.get("root_subtype") or ""
    strain = normalize_strain(attrs.get("category"))
    kind = (attrs.get("kind") or "").lower()

    # Jane percent_thc is percentage; thc_dosage_mg is mg for edibles
    thc = format_potency_percent(attrs.get("percent_thc"))
    if not thc:
        thc = format_potency_mg(attrs.get("thc_dosage_mg"))
    cbd = format_potency_percent(attrs.get("percent_cbd"))
    if not cbd:
        cbd = format_potency_mg(attrs.get("cbd_dosage_mg"))

    # Named deals: applicable_special_ids + applicable_special_types
    deals = []
    if attrs.get("is_special") or attrs.get("is_sale"):
        # Prefer human-readable name if the special is expanded in the response
        pass
    for special_type in (attrs.get("applicable_special_types") or []):
        deals.append(str(special_type).replace("_", " ").title())

    rows = []
    for weight, price, sale in resolve_variants(attrs):
        original_price = price if price not in (None, 0) else sale
        variant_deals = list(deals)
        if price and sale and price > sale:
            savings = price - sale
            pct = round(savings / price * 100)
            variant_deals.append(f"${savings:.2f} off ({pct}%)")
        rows.append({
            "name": name,
            "brand": brand,
            "category": kind or "unknown",
            "subcategory": (subcategory or kind or "DEFAULT").upper().replace(" ", "-") or "DEFAULT",
            "strain_type": strain,
            "thc": thc,
            "cbd": cbd,
            "weight": weight,
            "original_price": original_price,
            "sale_price": sale,
            "deals": variant_deals,
        })
    return rows


# =========================
# MAIN
# =========================
all_rows = []
seen_keys = set()
raw_json_dump = []

try:
    session = make_session()
    logger.info(f"Starting {company} scrape | stores={len(STORES)}")

    for store in STORES:
        logger.info(f"--- {store['dispensary_location']} (store_id={store['store_id']}) ---")
        store_rows_before = len(all_rows)

        for cat_name, cat_filter in CATEGORY_FILTERS:
            try:
                products = fetch_category(session, store["store_id"], cat_filter)
            except Exception as e:
                logger.warning(f"  {cat_name}: fetch failed: {e}")
                continue

            raw_json_dump.append({
                "store": store["dispensary_location"], "category": cat_name,
                "count": len(products),
            })

            if not products:
                logger.info(f"  {cat_name}: 0")
                continue

            cat_rows = 0
            for p in products:
                for row in parse_product(p):
                    dedupe = (store["dispensary_location"], row["name"],
                              row["weight"], row["original_price"])
                    if dedupe in seen_keys:
                        continue
                    seen_keys.add(dedupe)
                    deal_columns = {f"deal_{i+1}": d for i, d in enumerate(row["deals"])}
                    csv_row = {
                        "date": today,
                        "location": state,
                        "company": company,
                        "dispensary_location": store["dispensary_location"],
                        "name": row["name"],
                        "category": row["category"],
                        "subcategory": row["subcategory"],
                        "brand": row["brand"],
                        "strain_type": row["strain_type"],
                        "THC": row["thc"],
                        "CBD": row["cbd"],
                        "weight": row["weight"],
                        "original_price": row["original_price"],
                    }
                    csv_row.update(deal_columns)
                    all_rows.append(csv_row)
                    cat_rows += 1
            logger.info(f"  {cat_name}: {cat_rows} rows")
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
        json.dump({"per_store_category_counts": raw_json_dump, "total_rows": len(all_rows)}, f, indent=2)
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
            f"Likely 403 from Jane's US-geo-fence. Set HTTPS_PROXY to a "
            f"US residential proxy."
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