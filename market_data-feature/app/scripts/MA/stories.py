import pandas as pd
import json
import os
import time
from datetime import datetime
import pytz
import requests
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
# PLATFORM: WordPress "CannaPlanners" plugin that proxies the DUTCHIE PLUS GraphQL
# API server-side at  storiescannabis.co/wp-json/cannaplanners/v1/graphql/  (same
# origin, NO Cloudflare). We POST the `menu` query (Dutchie Plus schema) per
# category and paginate via {limit, offset}; one row per product variant.
# Stories Cannabis is a single open MA store (Fall River); Attleboro is "coming
# soon" with no live menu. (retailerId is the Dutchie dispensary id.)
# Run: pip install requests pandas pytz
company  = "Stories"
location = "Massachusetts"
MENU_TYPE = "RECREATIONAL"          # "RECREATIONAL" or "MEDICAL"

us_tz = pytz.timezone("America/Phoenix")
today = datetime.now(us_tz).strftime("%Y-%m-%d")

output_folder = "app/output/pending"
csv_folder = os.path.join(output_folder, "csv")
json_folder = os.path.join(output_folder, "json")
os.makedirs(csv_folder, exist_ok=True)
os.makedirs(json_folder, exist_ok=True)

output_file = os.path.join(csv_folder, f"{today}_{company}.csv")
json_file_path = os.path.join(json_folder, f"{today}_{company}.json")

GQL_URL = "https://storiescannabis.co/wp-json/cannaplanners/v1/graphql/"
PAGE_SIZE = 100
TIMEOUT = 30
RETRIES = 3
REQUEST_DELAY_S = 0.3

# MASSACHUSETTS STORES (retailerId = Dutchie dispensary id).
stores = [
    {"retailerId": "8f8dadf6-4852-4ded-b01f-eb95db79ebfa", "dispensary_location": "Fall River"},
]

# affinity label -> Dutchie Plus Category enum
categories = {
    "Flower":       "FLOWER",
    "Pre-rolls":    "PRE_ROLLS",
    "Vaporizers":   "VAPORIZERS",
    "Edibles":      "EDIBLES",
    "Concentrates": "CONCENTRATES",
    "Tinctures":    "TINCTURES",
    "Topicals":     "TOPICALS",
    "Orals":        "ORALS",
    "CBD":          "CBD",
    "Accessories":  "ACCESSORIES",
}

MENU_QUERY = """
query Menu($retailerId: ID!, $menuType: MenuType, $filter: MenuFilter, $pagination: Pagination) {
  menu(retailerId: $retailerId, menuType: $menuType, filter: $filter, pagination: $pagination) {
    productsCount
    products {
      id name
      brand { name }
      category
      subcategory
      strainType
      potencyThc { formatted }
      potencyCbd { formatted }
      variants { option priceRec priceMed specialPriceRec specialPriceMed quantity }
    }
  }
}
"""

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Origin": "https://storiescannabis.co",
    "Referer": "https://storiescannabis.co/shop/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/123.0.0.0 Safari/537.36",
}

# =========================
# GLOBAL STORAGE
# =========================
products = []
all_json = []
seen_keys = set()

session = requests.Session()
session.headers.update(HEADERS)


# =========================
# HELPERS
# =========================
def _pretty(enum_val):
    if not enum_val or enum_val in ("DEFAULT", "NOT_APPLICABLE"):
        return ""
    return enum_val.replace("_", " ").title()


def gql_menu(retailer_id, category_enum, offset):
    payload = {
        "query": MENU_QUERY,
        "variables": {
            "retailerId": retailer_id,
            "menuType": MENU_TYPE,
            "filter": {"category": category_enum},
            "pagination": {"limit": PAGE_SIZE, "offset": offset},
        },
    }
    last = None
    for attempt in range(RETRIES + 1):
        try:
            r = session.post(GQL_URL, json=payload, timeout=TIMEOUT)
            r.raise_for_status()
            j = r.json()
            if j.get("errors"):
                raise RuntimeError(j["errors"])
            return j["data"]["menu"]
        except Exception as e:                                  # noqa: BLE001
            last = e
            if attempt < RETRIES:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"menu query failed (store={retailer_id}, {category_enum}, offset={offset}): {last}")


def handle_products(menu_products, store, category_label):
    for p in menu_products:
        name = p.get("name")
        brand = (p.get("brand") or {}).get("name") or ""
        subcat = _pretty(p.get("subcategory"))
        strain = _pretty(p.get("strainType"))
        thc = (p.get("potencyThc") or {}).get("formatted") or ""
        cbd = (p.get("potencyCbd") or {}).get("formatted") or ""

        for v in (p.get("variants") or []):
            weight = v.get("option") or ""
            price = v.get("priceRec")
            if price is None:
                price = v.get("priceMed")
            special = v.get("specialPriceRec")
            if special is None:
                special = v.get("specialPriceMed")

            key = (store["dispensary_location"], name, weight)
            if key in seen_keys:
                continue
            seen_keys.add(key)

            deals = []
            if special is not None and price is not None and special < price:
                deals = ["On Sale"]
            deal_columns = {f"deal_{i+1}": d for i, d in enumerate(deals)}

            product = {
                "date": today,
                "location": location,
                "company": company,
                "dispensary_location": store["dispensary_location"],
                "name": name,
                "category": category_label,
                "subcategory": subcat,
                "brand": brand,
                "strain_type": strain,
                "THC": thc,
                "CBD": cbd,
                "weight": weight,
                "original_price": price,
            }
            product.update(deal_columns)
            products.append(product)


# =========================
# FETCH LOOP
# =========================
try:
    logger.info(f"Starting {company} scrape (menuType={MENU_TYPE}) | stores={len(stores)}")

    for store in stores:
        logger.info(f"=== {store['dispensary_location']} ===")
        before = len(products)

        for category_label, category_enum in categories.items():
            offset, total = 0, None
            while True:
                menu = gql_menu(store["retailerId"], category_enum, offset)
                all_json.append({"store": store["dispensary_location"], "category": category_label,
                                 "offset": offset, "menu": menu})
                prods = menu.get("products") or []
                total = menu.get("productsCount") or 0
                handle_products(prods, store, category_label)

                if not prods:
                    if offset == 0:
                        logger.info(f"    {category_label}: 0")
                    break
                offset += PAGE_SIZE
                logger.info(f"    {category_label}: {min(offset, total)}/{total}")
                if total and offset >= total:
                    break
                time.sleep(REQUEST_DELAY_S)

        logger.info(f"  => {store['dispensary_location']}: {len(products) - before} product rows")

except Exception as e:
    logger.critical(f"❌ Scraper failed: {e}", exc_info=True)
    sys.exit(1)


# =========================
# SAVE JSON
# =========================
try:
    if not all_json:
        logger.critical("❌ No JSON data collected")
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
    deal_cols = sorted(
        [c for c in df.columns if c.startswith("deal_")],
        key=lambda x: int(x.split("_")[1]),
    )
    df = df[fixed_cols + deal_cols]

    df.to_csv(output_file, index=False, na_rep="")

    logger.info(f"CSV saved: {output_file} | Rows: {len(df)}")

except Exception as e:
    logger.critical(f"❌ CSV save failed: {e}", exc_info=True)
    sys.exit(1)


# =========================
# SUCCESS
# =========================
logger.info("✅ Stories scraper completed successfully")
sys.exit(0)