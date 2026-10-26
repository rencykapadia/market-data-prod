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
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

# =========================
# CONFIG
# =========================
# PLATFORM: Urban Wellness serves an iHeartJane-backed menu through a WordPress
# "joint-ecommerce" Elasticsearch proxy:
#   POST /wp-json/joint-ecommerce/v1/products/ecommerce-production/_search
# Filter by `businessId` (per store) and `menuType` ("RECREATIONAL"). Each hit's _source
# carries name, category, subCategory, brandName, strainType, potencyThc/CbdDisplayValue,
# and variants[{option, price, specialPrice, appliedSpecials}]. Pure `requests`.
# NOTE: businessId 6357 is the store captured from the site's network tab; change it if a
# different Urban Wellness location is wanted.
# Run: pip install requests pandas pytz
company = "UrbanWellness"
location = "New Mexico"
MENU_TYPE = "RECREATIONAL"

# Urban Wellness stores (joint_business_id from window.jointEcommerce.stores). San Mateo Blvd = 6207.
stores = [
    {"business_id": "6207", "dispensary_location": "Albuquerque - San Mateo"},
    {"business_id": "6214", "dispensary_location": "Bernalillo"},
    {"business_id": "6211", "dispensary_location": "Las Cruces"},
    # Others: Montano 6205, Sunland Park 6206, Rio Rancho 6208, Paseo 6209, Paradise 6210,
    # Isleta 6212, Hobbs 6213, Academy 6215, 98th St 6216, Coors 6217, Taos 6357, Los Lunas 6365.
]

SEARCH_URL = "https://www.urbanwellnessnm.com/wp-json/joint-ecommerce/v1/products/ecommerce-production/_search"
PAGE_SIZE = 100
TIMEOUT = 30
RETRIES = 2

us_tz = pytz.timezone("America/Denver")
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
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Origin": "https://www.urbanwellnessnm.com",
    "Referer": "https://www.urbanwellnessnm.com/menu/",
})

products = []
all_json = []
seen = set()


def _pretty(v):
    if not v or str(v).upper() in ("NOT_APPLICABLE", "NONE", "DEFAULT"):
        return ""
    return str(v).replace("_", " ").title()


def search(from_, business_id):
    body = {
        "from": from_,
        "size": PAGE_SIZE,
        "query": {"bool": {"filter": [
            {"bool": {"should": [{"term": {"businessId": business_id}}]}},
            {"bool": {"should": [{"term": {"menuType": MENU_TYPE}}]}},
        ]}},
    }
    last = None
    for attempt in range(RETRIES + 1):
        try:
            r = session.post(SEARCH_URL, json=body, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception as e:                                  # noqa: BLE001
            last = e
            if attempt < RETRIES:
                time.sleep(1.5)
    raise RuntimeError(f"_search failed (from={from_}): {last}")


def handle_hits(hits, dispensary_location):
    for h in hits:
        p = h.get("_source") or {}
        name = (p.get("name") or "").strip()
        if not name:
            continue
        category = _pretty(p.get("category"))
        subcat = _pretty(p.get("subCategory"))
        brand = p.get("brandName") or ""
        strain = _pretty(p.get("strainType"))
        thc = p.get("potencyThcDisplayValue") or ""
        cbd = p.get("potencyCbdDisplayValue") or ""

        for v in (p.get("variants") or []):
            weight = v.get("option") or ""
            try:
                price = float(v.get("price")) if v.get("price") is not None else None
            except (TypeError, ValueError):
                price = None
            special = v.get("specialPrice")
            try:
                special = float(special) if special is not None else None
            except (TypeError, ValueError):
                special = None

            key = (dispensary_location, p.get("jointId") or p.get("posId") or name, weight)
            if key in seen:
                continue
            seen.add(key)

            deals = []
            for s in (v.get("appliedSpecials") or []) + (p.get("applicableSpecials") or []):
                nm = s.get("specialName") if isinstance(s, dict) else None
                if nm:
                    deals.append(nm)
            deals = list(dict.fromkeys(deals))
            on_sale = special is not None and price is not None and special < price
            deal_columns = {f"deal_{i+1}": d for i, d in enumerate(deals)}

            row = {
                "date": today, "location": location, "company": company,
                "dispensary_location": dispensary_location, "name": name,
                "category": category, "subcategory": subcat, "brand": brand,
                "strain_type": strain, "THC": thc, "CBD": cbd,
                "weight": weight, "original_price": special if on_sale else price,
            }
            row.update(deal_columns)
            products.append(row)


# =========================
# MAIN
# =========================
try:
    logger.info(f"Starting {company} scrape | stores={len(stores)} | menuType={MENU_TYPE}")
    for store in stores:
        biz = store["business_id"]
        disp = store["dispensary_location"]
        logger.info(f"=== {disp} (businessId={biz}) ===")
        before = len(products)
        frm, total = 0, None
        while frm < 8000:
            data = search(frm, biz)
            hits = ((data.get("hits") or {}).get("hits")) or []
            total = ((data.get("hits") or {}).get("total") or {})
            total = total.get("value") if isinstance(total, dict) else total
            all_json.append({"store": disp, "from": frm, "count": len(hits)})
            handle_hits(hits, disp)
            logger.info(f"  from={frm}: +{len(hits)} (rows {len(products)}, total {total})")
            if not hits or (total is not None and frm + PAGE_SIZE >= total):
                break
            frm += PAGE_SIZE
            time.sleep(0.3)
        logger.info(f"=> {disp}: {len(products) - before} rows")
except Exception as e:
    logger.critical(f"❌ Scraper failed: {e}", exc_info=True)
    sys.exit(1)


# =========================
# SAVE JSON + CSV
# =========================
try:
    if not products:
        logger.critical("❌ No products collected — check businessId / menuType.")
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

logger.info(f"✅ {company} (Bernalillo) scraper completed successfully")
sys.exit(0)
