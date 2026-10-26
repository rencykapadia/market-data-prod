import pandas as pd
import json
import os
import re
import time
from datetime import datetime
import pytz
import requests
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
# PLATFORM: Pecos Valley Production runs a WordPress "LeafBridge" menu (Dutchie data) that serves
# products through admin-ajax.php — a plain JSON endpoint, NO browser needed:
#   POST /wp-admin/admin-ajax.php
#   body: action=wizard_show_products & wizard_data[retailer_id]=<id> & wizard_data[menu_type]=RECREATIONAL
#         & wizard_data[collection_method]=PICKUP & nonce_ajax=<nonce> & prods_pageNumber=<n>
#         & prod_categories[categories]=<CAT> & filter_attributes[categories]=<CAT> & ...
#   -> {success, data:{products_list:[{name, category, subcategory, strainType, brand{name},
#        potencyThc{formatted}, potencyCbd{formatted}, variants:[{option, priceRec, priceMed,
#        specialPriceRec, specialPriceMed}]}], total_products_count}}
# The nonce comes from the inline `leafbridge_public_ajax_obj` on any store page. We scrape ALL New
# Mexico locations by retailer_id. Pure `requests` (fast — no Playwright, no bot-block).
# Example URL: https://pecosvalleyproduction.com/categories/flower/?retailer_id=f97938ab-5827-4a95-a2ed-1aadd970eb46&menu_type=RECREATIONAL&collection_type=PICKUP
# Run: pip install requests pandas pytz
company = "PecosValley"
location = "New Mexico"

BASE = "https://pecosvalleyproduction.com"
AJAX_URL = f"{BASE}/wp-admin/admin-ajax.php"
MENU_TYPE = "RECREATIONAL"
COLLECTION_METHOD = "PICKUP"
MAX_PAGES = 60          # safety cap per store/category
TIMEOUT = 30
RETRIES = 2

# admin-ajax category token -> affinity category label
CATEGORIES = {
    "FLOWER": "Flower",
    "PRE_ROLLS": "Pre-rolls",
    "VAPORIZERS": "Vaporizers",
    "CONCENTRATES": "Concentrates",
    "EDIBLES": "Edibles",
    "TINCTURES": "Tinctures",
    "TOPICALS": "Topicals",
    "CBD": "CBD",
    "ACCESSORIES": "Accessories",
}

# All Pecos Valley Production NM locations (retailer_id -> store), from the site's store directory.
STORES = [
    {"retailer_id": "f97938ab-5827-4a95-a2ed-1aadd970eb46", "dispensary_location": "Alamogordo"},
    {"retailer_id": "17905632-0e09-4148-abdb-54a2156b9069", "dispensary_location": "Alameda"},
    {"retailer_id": "724250cb-8e9e-437b-9436-540042e24aa8", "dispensary_location": "Albuquerque - 98th St."},
    {"retailer_id": "1891973f-4a84-410c-aa3c-980631b00314", "dispensary_location": "Albuquerque - Central Ave."},
    {"retailer_id": "67737dbf-8533-4862-b715-d5d1e5e7ef28", "dispensary_location": "Albuquerque - Eubank"},
    {"retailer_id": "ad616e2a-9693-4bab-b7d5-39708e293bd6", "dispensary_location": "Albuquerque - Eubank & Central"},
    {"retailer_id": "53133f5d-bc38-4f13-b361-83cbb70c0255", "dispensary_location": "Albuquerque - Montgomery"},
    {"retailer_id": "aa4ce928-dcce-4748-824b-f4c374b8b5cc", "dispensary_location": "Artesia"},
    {"retailer_id": "c0a50d18-ea96-4008-9913-90d794fec442", "dispensary_location": "Carlsbad - Canal"},
    {"retailer_id": "00501d68-33db-44c6-bbf6-5c3650e1bb9f", "dispensary_location": "Carlsbad - Mermod"},
    {"retailer_id": "da758a71-e455-41ca-af17-eab394b76241", "dispensary_location": "Clovis"},
    {"retailer_id": "7cb21f97-8a7a-41f1-9e3e-c3779050f5ff", "dispensary_location": "Clovis - 2521 N. Prince St."},
    {"retailer_id": "954fed65-0a48-4d20-a39d-b1e07284c334", "dispensary_location": "Clovis - 5021 N. Prince St."},
    {"retailer_id": "a1e31149-19ee-4792-9d56-c517f85f889f", "dispensary_location": "Edgewood"},
    {"retailer_id": "c919b1ae-96fb-4529-b3d7-3adb2667ec0b", "dispensary_location": "Espanola"},
    {"retailer_id": "365d6b05-72fd-40b1-81ad-1268e057f82a", "dispensary_location": "Hobbs - W Broadway"},
    {"retailer_id": "8ae0d2f4-648f-422a-ab84-61cee2be84b7", "dispensary_location": "Hobbs - Navajo"},
    {"retailer_id": "4799eb4c-f1a0-44a4-a20c-a3c12d96bf91", "dispensary_location": "Las Cruces - Roadrunner"},
    {"retailer_id": "e2b63eba-c24c-42df-9d57-2ccb03613ace", "dispensary_location": "Las Cruces - Locust"},
    {"retailer_id": "5778db8f-3052-485d-8605-d4b3d5a1aa16", "dispensary_location": "Lovington Hobbs"},
    {"retailer_id": "503e276f-bc09-47fd-abd0-3f0aa0187154", "dispensary_location": "Roswell - W. Country Club Rd."},
    {"retailer_id": "1c9d7fd2-bbd8-4f92-9e6e-5c867d89d0e6", "dispensary_location": "Roswell - North Main St."},
    {"retailer_id": "0eede426-2c2c-49d0-8e64-ea22dd48e410", "dispensary_location": "Ruidoso"},
    {"retailer_id": "37ab5d14-a743-40b6-bb03-1f43b70840b7", "dispensary_location": "Santa Fe"},
    {"retailer_id": "d49d23e7-f465-4e4b-802a-bce1c7eecaa9", "dispensary_location": "Tularosa"},
]

us_tz = pytz.timezone("America/Denver")   # New Mexico (Mountain)
today = datetime.now(us_tz).strftime("%Y-%m-%d")

output_folder = "app/output/pending"
csv_folder = os.path.join(output_folder, "csv")
json_folder = os.path.join(output_folder, "json")
os.makedirs(csv_folder, exist_ok=True)
os.makedirs(json_folder, exist_ok=True)
output_file = os.path.join(csv_folder, f"{today}_{company}.csv")
json_file_path = os.path.join(json_folder, f"{today}_{company}.json")

session = requests.Session()
session.headers.update({
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Origin": BASE,
    "Referer": f"{BASE}/categories/flower/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
})

products = []
all_json = []
seen = set()


# =========================
# HELPERS
# =========================
def _pretty(v):
    if not v or str(v).upper() in ("NOT_APPLICABLE", "NONE", "DEFAULT", "N/A"):
        return ""
    return str(v).replace("_", " ").title()


def _num(v):
    return v if isinstance(v, (int, float)) else None


def get_nonce():
    """Read the LeafBridge ajax nonce from the inline `leafbridge_public_ajax_obj` on a store page."""
    url = (f"{BASE}/categories/flower/?retailer_id={STORES[0]['retailer_id']}"
           f"&menu_type={MENU_TYPE}&collection_type={COLLECTION_METHOD}")
    r = session.get(url, timeout=TIMEOUT, headers={"Accept": "text/html"})
    r.raise_for_status()
    # WordPress nonces are alphanumeric (e.g. "8a4c711828"), not just digits.
    m = re.search(r'leafbridge_public_ajax_obj\s*=\s*\{[^}]*?"nonce"\s*:\s*"([a-z0-9]+)"', r.text, re.IGNORECASE)
    if not m:
        m = re.search(r'"nonce"\s*:\s*"([a-z0-9]{6,20})"', r.text, re.IGNORECASE)
    return m.group(1) if m else ""


def build_body(retailer_id, cat_token, page, nonce):
    p = {
        "action": "wizard_show_products",
        "wizard_data[age_confirm]": "true",
        "wizard_data[default_set]": "true",
        "wizard_data[collection_method]": COLLECTION_METHOD,
        "wizard_data[menu_type]": MENU_TYPE,
        "wizard_data[retailer_id]": retailer_id,
        "wizard_data[sort]": "NAME_ASC",
        "wizard_data[event]": "DONT reset cart",
        "wizard_data[prod_prefix]": "",
        "nonce_ajax": nonce,
        "prods_pageNumber": str(page),
        "prod_categories[search_key_word]": "",
        "prod_categories[sort_order]": "NAME_ASC",
        "prod_categories[categories]": cat_token,
        "search_key_word": "",
        "filter_attributes[search_key_word]": "",
        "filter_attributes[sort_order]": "NAME_ASC",
        "filter_attributes[categories]": cat_token,
    }
    return p


def post_page(retailer_id, cat_token, page, nonce):
    last = None
    for attempt in range(RETRIES + 1):
        try:
            r = session.post(AJAX_URL, data=build_body(retailer_id, cat_token, page, nonce), timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception as e:                                  # noqa: BLE001
            last = e
            if attempt < RETRIES:
                time.sleep(1.0)
    raise RuntimeError(f"admin-ajax failed (retailer={retailer_id[:8]}, {cat_token}, page={page}): {last}")


def handle(items, cat_label, dispensary_location):
    for it in items:
        name = (it.get("name") or "").strip()
        if not name:
            continue
        category = CATEGORIES.get(it.get("category"), cat_label)
        subcat = _pretty(it.get("subcategory"))
        brand = (it.get("brand") or {}).get("name", "") if isinstance(it.get("brand"), dict) else ""
        strain = _pretty(it.get("strainType"))
        thc = (it.get("potencyThc") or {}).get("formatted", "") if isinstance(it.get("potencyThc"), dict) else ""
        cbd = (it.get("potencyCbd") or {}).get("formatted", "") if isinstance(it.get("potencyCbd"), dict) else ""

        for v in (it.get("variants") or []):
            weight = v.get("option") or ""
            price = _num(v.get("priceRec"))
            if price is None:
                price = _num(v.get("priceMed"))
            special = _num(v.get("specialPriceRec"))
            if special is None:
                special = _num(v.get("specialPriceMed"))

            key = (dispensary_location, it.get("id") or name, weight)
            if key in seen:
                continue
            seen.add(key)

            on_sale = special is not None and price is not None and special < price
            row = {
                "date": today, "location": location, "company": company,
                "dispensary_location": dispensary_location, "name": name,
                "category": category, "subcategory": subcat, "brand": brand,
                "strain_type": strain, "THC": thc, "CBD": cbd,
                "weight": weight, "original_price": special if on_sale else price,
            }
            if on_sale:
                row["deal_1"] = f"Sale (reg ${price:g})"
            products.append(row)


def scrape_store(store, nonce):
    disp = store["dispensary_location"]
    rid = store["retailer_id"]
    store_before = len(products)
    for cat_token, label in CATEGORIES.items():
        before = len(products)
        collected, total = 0, None
        for page in range(1, MAX_PAGES + 1):
            try:
                data = post_page(rid, cat_token, page, nonce)
            except Exception as e:
                logger.warning(f"  {disp}/{label}: {str(e).splitlines()[0]}")
                break
            d = data.get("data") or {}
            items = d.get("products_list") or []
            if total is None:
                total = d.get("total_products_count")
            if not items:
                break
            all_json.append({"store": disp, "category": label, "page": page, "count": len(items)})
            handle(items, label, disp)
            collected += len(items)
            if total is not None and collected >= total:
                break
            time.sleep(0.15)
        if len(products) - before:
            logger.info(f"  {disp}/{label}: +{len(products) - before} rows")
    logger.info(f"=> {disp}: {len(products) - store_before} rows")


# =========================
# MAIN
# =========================
try:
    logger.info(f"Starting {company} scrape | stores={len(STORES)} ({MENU_TYPE}, LeafBridge admin-ajax)")
    nonce = get_nonce()
    if not nonce:
        logger.critical("❌ Could not read the LeafBridge nonce from the menu page.")
        sys.exit(1)
    logger.info(f"nonce={nonce}")
    for store in STORES:
        scrape_store(store, nonce)
except Exception as e:
    logger.critical(f"❌ Scraper failed: {e}", exc_info=True)
    sys.exit(1)


# =========================
# SAVE JSON + CSV
# =========================
try:
    if not products:
        logger.critical("❌ No products collected — nonce/category tokens may have changed.")
        sys.exit(1)
    with open(json_file_path, "w", encoding="utf-8") as f:
        json.dump(all_json, f, indent=2)
    logger.info(f"JSON saved: {json_file_path}")

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

logger.info(f"✅ {company} scraper completed successfully ({len(STORES)} NM locations)")
sys.exit(0)
