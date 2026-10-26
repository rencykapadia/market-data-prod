import requests
import pandas as pd
import time
import logging
import json
import os
from datetime import datetime
import pytz
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
# CONFIG & TARGET LOCATIONS
# =========================
company = "CuraleafCT"
us_tz = pytz.timezone("America/Phoenix")
today = datetime.now(us_tz).strftime("%Y-%m-%d")

STORES = [
    {"id": "89", "name": "Groton, CT"},
    {"id": "90", "name": "Hartford, CT"},
    {"id": "91", "name": "Manchester, CT"},
    {"id": "92", "name": "Stamford, CT"}
]

# =========================
# OUTPUT PATHS
# =========================
output_folder = "app/output/pending"
csv_folder = os.path.join(output_folder, "csv")
json_folder = os.path.join(output_folder, "json")

os.makedirs(csv_folder, exist_ok=True)
os.makedirs(json_folder, exist_ok=True)

# ✅ Single combined output file name instead of store-specific ones
output_file = os.path.join(csv_folder, f"{today}_{company}.csv")

# =========================
# API CONFIG
# =========================
BASE_URL = "https://web-ui-curaleaf.sweedpos.com/_api/proxy/Products"

BASE_HEADERS = {
    "Content-Type": "application/json",
    "Origin": "https://curaleaf.com",
    "Referer": "https://curaleaf.com/",
    "User-Agent": "Mozilla/5.0",
}

logger.info("Curaleaf Multi-Location Script started")

# =========================
# HELPER: Fetch missing THC/CBD
# =========================
def get_variant_details(variant_id, store_headers):
    try:
        payload = {
            "variantId": str(variant_id),
            "saleType": "Recreational",
            "platformOs": "web",
            "stockType": "Default"
        }

        response = requests.post(
            f"{BASE_URL}/GetProductByVariantId",
            headers=store_headers, 
            json=payload,
            timeout=200
        )

        if response.status_code != 200:
            raise Exception(f"Variant API failed: {response.status_code}")

        try:
            data = response.json()
        except Exception:
            logger.error(f"Invalid variant JSON: {response.text[:300]}")
            return None, None

        for v in data.get("variants", []):
            if v.get("id") == variant_id:
                lab = v.get("labTests") or {}

                thc = None
                cbd = None

                if lab.get("thc") and lab["thc"].get("value"):
                    thc = f"{lab['thc']['value'][0]} {lab['thc'].get('unitAbbr', '%')}"

                if lab.get("cbd") and lab["cbd"].get("value"):
                    val = lab["cbd"]["value"][0]
                    if val != 0:
                        cbd = f"{val} {lab['cbd'].get('unitAbbr', '%')}"

                return thc, cbd

    except Exception as e:
        logger.error(f"Variant detail error (ID {variant_id}): {e}")

    return None, None


# =========================
# GLOBAL STORAGE
# =========================
# ✅ Moved outside the loop so data builds up across all stores
all_products = []

# =========================
# MAIN LOOP OVER STORES
# =========================
for store in STORES:
    store_id = store["id"]
    store_name = store["name"]
    
    logger.info(f"=== Starting scrape for Location: {store_name} (ID: {store_id}) ===")
    dispensary_city = store_name.split(",")[0].strip()
    
    current_headers = BASE_HEADERS.copy()
    current_headers["storeid"] = str(store_id)
    
    page = 1
    
    try:
        while True:
            logger.info(f"[{store_name}] Scraping page {page}")

            payload = {
                "page": page,
                "pageSize": 24,
                "filters": {},
                "saleType": "Recreational"
            }

            response = requests.post(
                f"{BASE_URL}/GetProductList",
                headers=current_headers,
                json=payload,
                timeout=20
            )

            if response.status_code != 200:
                logger.error(f"❌ API blocked/error for {store_name} on page {page}: {response.status_code}")
                break  

            try:
                data = response.json()
            except Exception:
                logger.error(f"❌ Invalid JSON response for {store_name} on page {page}")
                break

            if "list" not in data:
                logger.error(f"❌ Invalid API structure for {store_name}: 'list' missing")
                break

            items = data["list"]

            if not items:
                logger.info(f"No more data for {store_name} → stopping pagination")
                break

            for item in items:
                strain = item.get("strain") or {}
                strain_type = (strain.get("prevalence") or {}).get("name")

                for variant in item.get("variants", []):
                    lab = variant.get("labTests") or {}

                    # THC
                    thc = None
                    if lab.get("thc") and lab["thc"].get("value"):
                        thc = f"{lab['thc']['value'][0]} {lab['thc'].get('unitAbbr', '%')}"

                    # CBD
                    cbd = None
                    if lab.get("cbd") and lab["cbd"].get("value"):
                        val = lab["cbd"]["value"][0]
                        if val != 0:
                            cbd = f"{val} {lab['cbd'].get('unitAbbr', '%')}"

                    # Fallback details
                    if not thc and not cbd:
                        thc_detail, cbd_detail = get_variant_details(variant.get("id"), current_headers)
                        thc = thc or thc_detail
                        cbd = cbd or cbd_detail
                        time.sleep(0.2)

                    # DEALS
                    promos = variant.get("promos", [])
                    deal_columns = {
                        f"deal_{i+1}": p.get("shortName")
                        for i, p in enumerate(promos)
                        if p.get("shortName")
                    }

                    product = {
                        "date": today,
                        "location": store_name, 
                        "company": company,
                        "dispensary_location": dispensary_city, 
                        "name": item.get("name"),
                        "category": item.get("category", {}).get("name"),
                        "subcategory": item.get("subcategory", {}).get("name"),
                        "brand": item.get("brand", {}).get("name"),
                        "strain_type": strain_type,
                        "THC": thc,
                        "CBD": cbd,
                        "weight": variant.get("name"),
                        "original_price": variant.get("price"),
                    }

                    product.update(deal_columns)
                    # ✅ Append to global list instead of a temporary local list
                    all_products.append(product)

            page += 1
            time.sleep(0.5) 

    except Exception as e:
        logger.error(f"❌ Critical error during processing of {store_name}: {e}", exc_info=True)
        continue 

    # Small break before cycling to the next store
    time.sleep(2)

# =========================
# SAVE COMBINED DATA
# =========================
# ✅ Export logic moved completely out of the store loop
if all_products:
    try:
        # Note: We now drop duplicates using 'location' as well, ensuring items 
        # carrying identical names across different locations aren't accidentally removed.
        df = pd.DataFrame(all_products).drop_duplicates(subset=["location", "name", "weight", "original_price"])
        
        if not df.empty:
            df.to_csv(output_file, index=False, na_rep="")
            logger.info(f"💾 Combined CSV saved successfully! Total items across all locations: {len(df)}")
            logger.info(f"File location: {output_file}")
        else:
            logger.warning("⚠️ Combined DataFrame was unexpectedly empty.")
    except Exception as e:
        logger.error(f"❌ Combined CSV save failed: {e}")
else:
    logger.warning("⚠️ No data collected from any stores. Combined CSV was not generated.")

logger.info("🎉 All Curaleaf store locations have been processed successfully")
sys.exit(0)