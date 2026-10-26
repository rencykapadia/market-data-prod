import requests
import pandas as pd
import json
import os
import sys
from datetime import datetime
import pytz
import logging

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
company = "Nirvana"
location = "Tucson"

us_tz = pytz.timezone("America/Phoenix")
today = datetime.now(us_tz).strftime("%Y-%m-%d")

output_folder = "app/output/pending"
csv_folder = os.path.join(output_folder, "csv")
json_folder = os.path.join(output_folder, "json")

os.makedirs(csv_folder, exist_ok=True)
os.makedirs(json_folder, exist_ok=True)

output_file = os.path.join(csv_folder, f"{today}_{company}.csv")
json_file_path = os.path.join(json_folder, f"{today}_{company}.json")

url = "https://nirvanacannabis.com/wp-json/joint-ecommerce/v1/products/ecommerce-production/_search"

headers = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0",
    "Origin": "https://nirvanacannabis.com",
    "Referer": "https://nirvanacannabis.com/"
}

business_map = {
    "5930": "Tucson"
}

products = []
all_json = []

logger.info("Nirvana Script started")

# =========================
# SCRAPER
# =========================
try:
    for store_id, dispensary_location in business_map.items():

        logger.info(f"Store: {dispensary_location} ({store_id})")

        offset = 0
        has_data = False

        while True:

            # NOTE: the joint-ecommerce proxy validates that the businessId filter
            # is nested as query.bool.filter[].bool.should[].term.businessId.
            # A flat {"term": {"businessId": ...}} returns HTTP 400
            # "Missing businessId filter".
            payload = {
                "size": 20,
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
                response = requests.post(url, json=payload, headers=headers, timeout=20)

                if response.status_code != 200:
                    raise Exception(f"HTTP {response.status_code} | {response.text[:300]}")

                data = response.json()
                all_json.append(data)

            except Exception as e:
                logger.critical(f"❌ API request failed: {e}", exc_info=True)
                sys.exit(1)

            hits = data.get("hits", {}).get("hits", [])

            if not hits:
                if not has_data:
                    logger.warning("⚠️ No data found for store")
                else:
                    logger.info("Store completed")
                break

            has_data = True

            for item in hits:
                try:
                    source = item["_source"]
                    variants = source.get("variants") or [{}]

                    specials = source.get("applicableSpecials", [])
                    deal_names = list(set(
                        s.get("specialName") for s in specials if s.get("specialName")
                    ))

                    deal_columns = {f"deal_{i+1}": d for i, d in enumerate(deal_names)}

                    for variant in variants:
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
                    logger.critical(f"❌ Item processing failed: {e}", exc_info=True)
                    sys.exit(1)

            offset += 20

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
        json.dump(all_json, f, indent=2)

    logger.info(f"JSON saved: {json_file_path}")

except Exception as e:
    logger.critical(f"❌ JSON save failed: {e}", exc_info=True)
    sys.exit(1)


# =========================
# SAVE CSV
# =========================
try:
    df = pd.DataFrame(products).drop_duplicates(subset=["name", "weight"])

    if df.empty or len(df) < 10:
        logger.critical(f"❌ Invalid CSV output. Rows: {len(df)}")
        sys.exit(1)

    df.to_csv(output_file, index=False, na_rep="")

    logger.info(f"CSV saved: {output_file} | Rows: {len(df)}")

except Exception as e:
    logger.critical(f"❌ CSV save failed: {e}", exc_info=True)
    sys.exit(1)


# =========================
# SUCCESS
# =========================
logger.info("✅ Nirvana scraper completed successfully")
sys.exit(0)