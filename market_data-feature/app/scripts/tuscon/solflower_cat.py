import requests
import pandas as pd
import json
import os
import sys
import time
from datetime import datetime
import pytz
import logging

from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# =========================
# LOGGER
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# =========================
# CONFIG
# =========================
company = "Solflower"
location = "Tucson"

us_tz = pytz.timezone("America/Phoenix")
today = datetime.now(us_tz).strftime("%Y-%m-%d")

output_folder = "app/output/pending"
csv_folder = os.path.join(output_folder, "csv")
json_folder = os.path.join(output_folder, "json")

os.makedirs(csv_folder, exist_ok=True)
os.makedirs(json_folder, exist_ok=True)

output_file   = os.path.join(csv_folder, f"{today}_{company}.csv")
json_file_path = os.path.join(json_folder, f"{today}_{company}.json")

url = "https://www.livewithsol.com/wp-json/joint-ecommerce/v1/products/ecommerce-production/_search"

headers = {
    "Content-Type": "application/json",
    "Origin": "https://www.livewithsol.com",
    "Referer": "https://www.livewithsol.com/menu/",
    "User-Agent": "Mozilla/5.0"
}

business_map = {
    "5804": "Casas Adobes",
    "5802": "Foothills Tucson",
    "5803": "North Tucson",
    "5801": "South Tucson"
}

size = 50

# Network resilience knobs
TIMEOUT = 30
MAX_RETRIES = 4          # per-request attempts before giving up
BACKOFF_BASE = 2         # seconds: 2, 4, 8 ...
RETRY_STATUSES = {429, 500, 502, 503, 504}

products = []
all_json = []

# =========================
# HTTP SESSION (connection-pool level retries for transient drops)
# =========================
session = requests.Session()
_pool_retry = Retry(
    total=5, connect=5, read=5, backoff_factor=1.0,
    status_forcelist=list(RETRY_STATUSES),
    allowed_methods=frozenset(["POST", "GET"]),  # allow retrying POST
    raise_on_status=False,
)
session.mount("https://", HTTPAdapter(max_retries=_pool_retry))
session.mount("http://", HTTPAdapter(max_retries=_pool_retry))


def post_with_retry(payload, store_id):
    """POST with explicit retry/backoff. Raises only after MAX_RETRIES transient failures."""
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = session.post(url, headers=headers, json=payload, timeout=TIMEOUT)

            if response.status_code == 200:
                return response.json()

            # Retry transient server/rate-limit statuses; fail fast on real client errors (e.g. 400)
            if response.status_code in RETRY_STATUSES and attempt < MAX_RETRIES:
                wait = BACKOFF_BASE ** attempt
                logger.warning(f"⚠️ store {store_id}: HTTP {response.status_code} (attempt {attempt}/{MAX_RETRIES}); retrying in {wait}s")
                time.sleep(wait)
                continue

            raise Exception(f"Bad status code: {response.status_code} | {response.text[:300]}")

        except requests.exceptions.RequestException as e:
            # Connection reset / read timeout / DNS / etc. -> transient, retry
            last_err = e
            if attempt < MAX_RETRIES:
                wait = BACKOFF_BASE ** attempt
                logger.warning(f"⚠️ store {store_id}: network error '{e}' (attempt {attempt}/{MAX_RETRIES}); retrying in {wait}s")
                time.sleep(wait)
                continue
            raise Exception(f"Network error after {MAX_RETRIES} attempts: {e}") from e

    # Should not reach here
    raise Exception(f"Request failed: {last_err}")


# =========================
# FETCH DATA
# =========================
def fetch_store_products(store_id, dispensary_location):
    offset = 0
    has_data = False

    while True:
        # The joint-ecommerce proxy requires the businessId filter nested as
        # query.bool.filter[].bool.should[].term.businessId. A flat
        # {"term": {"businessId": ...}} returns HTTP 400 "Missing businessId filter".
        payload = {
            "size": size,
            "from": offset,
            "query": {
                "bool": {
                    "filter": [
                        {"bool": {"should": [{"term": {"businessId": store_id}}]}}
                    ]
                }
            }
        }

        try:
            data = post_with_retry(payload, store_id)
            all_json.append(data)
        except Exception as e:
            logger.critical(f"❌ API FAILED for store {store_id}: {e}", exc_info=True)
            sys.exit(1)  # 🔴 HARD FAIL only after retries exhausted

        hits = data.get("hits", {}).get("hits", [])

        if not hits:
            if not has_data:
                logger.critical(f"❌ No data found for store {store_id}")
                sys.exit(1)  # 🔴 HARD FAIL
            else:
                logger.info(f"Completed store {store_id}")
            break

        has_data = True

        for item in hits:
            process_item(item, dispensary_location)

        offset += size
        time.sleep(0.3)  # be polite between pages


# =========================
# PROCESS ITEM
# =========================
def process_item(item, dispensary_location):
    try:
        source = item["_source"]
        variants = source.get("variants") or []

        specials = source.get("applicableSpecials", [])
        deals = [d.get("specialName") for d in specials if d.get("specialName")]

        deal_columns = {f"deal_{i+1}": d for i, d in enumerate(deals)}

        for variant in variants:
            process_variant(source, variant, dispensary_location, deal_columns)

    except Exception as e:
        logger.critical(f"❌ Item processing failed: {e}", exc_info=True)
        sys.exit(1)


# =========================
# PROCESS VARIANT
# =========================
def process_variant(source, variant, dispensary_location, deal_columns):
    try:
        product = {
            "date": today,
            "location": location,
            "company": company,
            "dispensary_location": dispensary_location,
            "name": source.get("name"),
            "category": source.get("category"),
            "subcategory": source.get("subCategory"),
            "brand": source.get("brandName"),
            "strain_type": source.get("strainType"),
            "THC": source.get("potencyThcDisplayValue"),
            "CBD": source.get("potencyCbdDisplayValue"),
            "weight": variant.get("option"),
            "original_price": variant.get("price"),
        }

        product.update(deal_columns)
        products.append(product)

    except Exception as e:
        logger.critical(f"❌ Variant processing failed: {e}", exc_info=True)
        sys.exit(1)


# =========================
# SAVE OUTPUTS
# =========================
def save_outputs():

    # 🔴 JSON must exist + not empty
    if not all_json:
        logger.critical("❌ No JSON data collected")
        sys.exit(1)

    try:
        with open(json_file_path, "w", encoding="utf-8") as f:
            json.dump(all_json, f, indent=2)
        logger.info(f"JSON saved: {json_file_path}")

    except Exception as e:
        logger.critical(f"❌ JSON save failed: {e}", exc_info=True)
        sys.exit(1)

    # 🔴 CSV must exist + not empty
    try:
        df = pd.DataFrame(products).drop_duplicates(subset=["name", "weight"])

        if df.empty:
            raise Exception("CSV is empty")

        df.to_csv(output_file, index=False, na_rep="")

        logger.info(f"CSV saved: {output_file} | Rows: {len(df)}")

    except Exception as e:
        logger.critical(f"❌ CSV save failed: {e}", exc_info=True)
        sys.exit(1)


# =========================
# MAIN EXECUTION
# =========================
try:
    logger.info("🚀 Solflower script started")

    for store_id, dispensary_location in business_map.items():
        logger.info(f"Processing store: {dispensary_location}")
        fetch_store_products(store_id, dispensary_location)

    save_outputs()

    logger.info("✅ Solflower completed successfully")
    sys.exit(0)

except Exception as e:
    logger.critical(f"❌ Fatal script failure: {e}", exc_info=True)
    sys.exit(1)