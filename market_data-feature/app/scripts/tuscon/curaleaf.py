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
# CONFIG
# =========================
company = "Curaleaf"
location = "Tucson"
dispensary_location = ""

us_tz = pytz.timezone("America/Phoenix")
today = datetime.now(us_tz).strftime("%Y-%m-%d")

# =========================
# OUTPUT PATHS
# =========================
output_folder = "app/output/pending"
csv_folder = os.path.join(output_folder, "csv")
json_folder = os.path.join(output_folder, "json")

os.makedirs(csv_folder, exist_ok=True)
os.makedirs(json_folder, exist_ok=True)

output_file = os.path.join(csv_folder, f"{today}_{company}.csv")
json_file_path = os.path.join(json_folder, f"{today}_{company}.json")

# =========================
# API CONFIG
# =========================
BASE_URL = "https://web-ui-curaleaf.sweedpos.com/_api/proxy/Products"

# =========================
# US EGRESS / PROXY
# =========================
# Curaleaf's shop pages + the SweedPOS product API are geo-restricted to US traffic.
# Non-US IPs get a stock nginx 403. Route through a US proxy (or run this from a
# US-based server and leave PROXY_URL unset).
#
#   PowerShell:  $env:PROXY_URL = "http://user:pass@us-proxy-host:port"
#   bash:        export PROXY_URL="http://user:pass@us-proxy-host:port"
#
# Residential US proxies work best; datacenter proxies may themselves be blocked.
PROXY_URL = os.getenv("PROXY_URL", "").strip()
PROXIES = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None

# Fail fast + verify egress country before scraping, so a dead/leaking proxy
# doesn't masquerade as a scraper bug.
VERIFY_EGRESS = True

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

API_HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Origin": "https://curaleaf.com",
    "Referer": "https://curaleaf.com/",
    "storeid": "86",
}

# =========================
# SESSION
# =========================
session = requests.Session()
session.headers.update({"User-Agent": UA})
if PROXIES:
    session.proxies.update(PROXIES)


def check_egress():
    """Confirm we're actually exiting the US before hitting Curaleaf."""
    try:
        r = session.get("https://ipinfo.io/json", timeout=15)
        info = r.json()
        ip = info.get("ip")
        country = info.get("country")
        logger.info(f"Egress IP {ip} | country={country} | proxy={'yes' if PROXIES else 'no'}")
        if country and country != "US":
            logger.critical(
                f"❌ Egress country is {country}, not US. Curaleaf will 403. "
                f"Set PROXY_URL to a US proxy or run from a US server."
            )
            sys.exit(2)
    except Exception as e:
        logger.warning(f"Could not verify egress (continuing): {e}")


def post_api(path, payload, max_retries=3):
    """POST with retry/backoff. On failure, LOG THE BODY so we can see WHY."""
    last = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = session.post(f"{BASE_URL}/{path}", headers=API_HEADERS,
                                json=payload, timeout=25)
        except requests.exceptions.ProxyError as e:
            logger.critical(f"❌ Proxy connection failed: {e}")
            sys.exit(2)

        last = resp
        if resp.status_code == 200:
            return resp

        logger.error(f"{path} -> HTTP {resp.status_code} (attempt {attempt}/{max_retries})")
        logger.error(f"  server={resp.headers.get('server')} "
                     f"cf-ray={resp.headers.get('cf-ray')} "
                     f"cf-mitigated={resp.headers.get('cf-mitigated')}")
        logger.error(f"  body[:600]: {resp.text[:600]!r}")

        if resp.status_code in (403, 429, 503):
            time.sleep(2 * attempt)
            continue
        break
    return last


products = []
all_json = []

logger.info("Curaleaf Script started")
if VERIFY_EGRESS:
    check_egress()


# =========================
# HELPER: Fetch missing THC/CBD
# =========================
def get_variant_details(variant_id):
    try:
        payload = {
            "variantId": str(variant_id),
            "saleType": "Recreational",
            "platformOs": "web",
            "stockType": "Default"
        }
        response = post_api("GetProductByVariantId", payload)
        if response is None or response.status_code != 200:
            return None, None

        try:
            data = response.json()
        except Exception:
            logger.error(f"Invalid variant JSON: {response.text[:300]}")
            return None, None

        for v in data.get("variants", []):
            if v.get("id") == variant_id:
                lab = v.get("labTests") or {}
                thc = cbd = None
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
# MAIN SCRAPER
# =========================
try:
    page = 1
    while True:
        logger.info(f"Scraping page {page}")

        payload = {"page": page, "pageSize": 24, "filters": {}, "saleType": "Recreational"}
        response = post_api("GetProductList", payload)

        if response is None or response.status_code != 200:
            code = response.status_code if response is not None else "no-response"
            raise Exception(f"API blocked/error: {code} (see logged body above for reason)")

        try:
            data = response.json()
        except Exception:
            logger.critical(f"Invalid JSON response: {response.text[:500]}")
            raise

        if "list" not in data:
            raise Exception("Invalid API structure: 'list' missing")

        all_json.append(data)
        items = data["list"]
        if not items:
            logger.info("No more data -> stopping pagination")
            break

        for item in items:
            strain = item.get("strain") or {}
            strain_type = (strain.get("prevalence") or {}).get("name")

            for variant in item.get("variants", []):
                lab = variant.get("labTests") or {}

                thc = None
                if lab.get("thc") and lab["thc"].get("value"):
                    thc = f"{lab['thc']['value'][0]} {lab['thc'].get('unitAbbr', '%')}"

                cbd = None
                if lab.get("cbd") and lab["cbd"].get("value"):
                    val = lab["cbd"]["value"][0]
                    if val != 0:
                        cbd = f"{val} {lab['cbd'].get('unitAbbr', '%')}"

                if not thc and not cbd:
                    thc_detail, cbd_detail = get_variant_details(variant.get("id"))
                    thc = thc or thc_detail
                    cbd = cbd or cbd_detail
                    time.sleep(0.2)

                promos = variant.get("promos", [])
                deal_columns = {
                    f"deal_{i+1}": p.get("shortName")
                    for i, p in enumerate(promos)
                    if p.get("shortName")
                }

                product = {
                    "date": today,
                    "location": location,
                    "company": company,
                    "dispensary_location": dispensary_location,
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
                products.append(product)

        page += 1

except Exception as e:
    logger.critical(f"❌ Scraper failed: {e}", exc_info=True)
    sys.exit(1)


# =========================
# VALIDATION + SAVE
# =========================
if not all_json:
    logger.critical("❌ No JSON collected")
    sys.exit(1)
if not products:
    logger.critical("❌ No products scraped")
    sys.exit(1)

try:
    with open(json_file_path, "w", encoding="utf-8") as f:
        json.dump(all_json, f, indent=2)
    if os.path.getsize(json_file_path) == 0:
        raise Exception("JSON file is empty")
except Exception as e:
    logger.critical(f"❌ JSON save failed: {e}", exc_info=True)
    sys.exit(1)

try:
    df = pd.DataFrame(products).drop_duplicates(subset=["name", "weight", "original_price"])
    if df.empty:
        raise Exception("CSV is empty")
    df.to_csv(output_file, index=False, na_rep="")
except Exception as e:
    logger.critical(f"❌ CSV save failed: {e}", exc_info=True)
    sys.exit(1)

logger.info("✅ Curaleaf completed successfully")
sys.exit(0)