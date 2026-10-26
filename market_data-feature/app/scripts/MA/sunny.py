import pandas as pd
import json
import os
import time
from datetime import datetime
import pytz
import requests
from playwright.sync_api import sync_playwright
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
# PLATFORM: Sunnyside (Cresco Labs). Clean public REST API at api.crescolabs.com.
# Products: GET /p/inventory/op/fifo-inventory  (store_id is a HEADER).
# Every request carries public client keys (x-api-key, X-Client-Version). They are
# NOT secret (every visitor's browser sends them) but they aren't hard-coded here.
# AUTO-CAPTURE: a headless browser opens the menu once and reads those keys off a
# live request; the rest of the scrape is plain `requests`.
# Run: pip install playwright requests pandas pytz && playwright install chromium
company  = "Sunnyside"
location = "Massachusetts"

us_tz = pytz.timezone("America/Phoenix")
today = datetime.now(us_tz).strftime("%Y-%m-%d")

output_folder = "app/output/pending"
csv_folder = os.path.join(output_folder, "csv")
json_folder = os.path.join(output_folder, "json")
os.makedirs(csv_folder, exist_ok=True)
os.makedirs(json_folder, exist_ok=True)

output_file = os.path.join(csv_folder, f"{today}_{company}.csv")
json_file_path = os.path.join(json_folder, f"{today}_{company}.json")

BASE = "https://api.crescolabs.com"
INV_URL = BASE + "/p/inventory/op/fifo-inventory"
KEY_PAGE = "https://www.sunnyside.shop/products/flower"
HEADLESS = True
PAGE_SIZE = 100
TIMEOUT = 30
RETRIES = 3
REQUEST_DELAY_S = 0.3

# MASSACHUSETTS STORES (store_id is sent as a header). Fall River is medical-only,
# Worcester recreational-only, Framingham/Leicester serve both -> query both usage
# types per store and de-duplicate.
stores = [
    {"id": 318, "dispensary_location": "Fall River"},
    {"id": 625, "dispensary_location": "Framingham"},
    {"id": 627, "dispensary_location": "Leicester"},
    {"id": 629, "dispensary_location": "Worcester"},
]
USAGE_TYPES = ["medical", "recreational"]

categories = ["flower", "vaporizers", "edibles", "concentrates",
              "pre-rolls", "tinctures", "topicals", "accessories"]

# Filled in automatically by capture_keys()
AUTH = {"x-api-key": None, "X-Client-Version": None, "ordering_app_id": None}

# =========================
# GLOBAL STORAGE
# =========================
products = []
all_json = []
seen_keys = set()

session = requests.Session()


# =========================
# AUTO-CAPTURE PUBLIC CLIENT KEYS (headless browser, one page load)
# =========================
def capture_keys():
    captured = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=HEADLESS)
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"))
        page = ctx.new_page()

        def on_request(req):
            if captured:
                return
            if "api.crescolabs.com" in req.url:
                h = req.headers  # keys are lower-cased by Playwright
                if h.get("x-api-key"):
                    captured["x-api-key"] = h.get("x-api-key")
                    captured["X-Client-Version"] = h.get("x-client-version")
                    captured["ordering_app_id"] = h.get("ordering_app_id")

        ctx.on("request", on_request)
        page.goto(KEY_PAGE, wait_until="domcontentloaded", timeout=90_000)
        for _ in range(30):
            if captured:
                break
            page.wait_for_timeout(1000)
        browser.close()

    if not captured.get("x-api-key"):
        raise RuntimeError("could not capture x-api-key from sunnyside.shop (try HEADLESS=False)")
    return captured


def build_headers(store_id):
    return {
        "Accept": "application/json, text/plain, */*",
        "x-api-key": AUTH["x-api-key"],
        "X-Client-Version": AUTH["X-Client-Version"],
        "ordering_app_id": AUTH["ordering_app_id"],
        "store_id": str(store_id),
        "Origin": "https://www.sunnyside.shop",
        "Referer": "https://www.sunnyside.shop/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/123.0.0.0 Safari/537.36",
    }


# =========================
# HELPERS
# =========================
def _potency_total(p, base):
    """Total THC/CBD = neutral + 0.877 * acid form, from the `potency` object
    (falls back to the bt_potency_* fields). Returns 'X.XX%' or ''."""
    pot = p.get("potency") or {}
    neutral = pot.get(base)
    acid = pot.get(base + "a")
    if neutral is None:
        neutral = p.get(f"bt_potency_{base}")
    if acid is None:
        acid = p.get(f"bt_potency_{base}a")
    neutral = neutral or 0
    acid = acid or 0
    total = neutral + 0.877 * acid
    return f"{round(total, 2)}%" if total else ""


def _deals(p):
    out, seen = [], set()
    for s in (p.get("specials") or []):
        nm = (s.get("name") or s.get("special_name") or s.get("title")) if isinstance(s, dict) else str(s)
        if nm and nm not in seen:
            seen.add(nm)
            out.append(nm)
    ap = p.get("applied_special")
    if isinstance(ap, dict):
        nm = ap.get("name") or ap.get("special_name")
        if nm and nm not in seen:
            out.append(nm)
    return out


def get_inventory(store_id, category, usage_type, offset):
    params = {
        "category": category,
        "inventory_type": "retail",
        "usage_type": usage_type,
        "sellable": "true",
        "require_sellable_quantity": "true",
        "include_specials": "true",
        "order_by": "brand",
        "limit": PAGE_SIZE,
        "offset": offset,
    }
    last = None
    for attempt in range(RETRIES + 1):
        try:
            r = session.get(INV_URL, params=params, headers=build_headers(store_id), timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception as e:                                  # noqa: BLE001
            last = e
            if attempt < RETRIES:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"inventory fetch failed (store={store_id}, {category}/{usage_type}, offset={offset}): {last}")


def handle_rows(rows, store, category):
    for p in rows:
        name = p.get("name")
        if not name:
            continue
        weight = f"{p.get('bt_weight')}g" if p.get("bt_weight") not in (None, "") else ""

        key = (store["dispensary_location"], name, weight)
        if key in seen_keys:
            continue
        seen_keys.add(key)

        deals = _deals(p)
        deal_columns = {f"deal_{i+1}": d for i, d in enumerate(deals)}

        price = p.get("price")
        if price is None:
            price = p.get("price_med")

        product = {
            "date": today,
            "location": location,
            "company": company,
            "dispensary_location": store["dispensary_location"],
            "name": name,
            "category": category,
            "subcategory": "",
            "brand": p.get("brand") or "",
            "strain_type": "",
            "THC": _potency_total(p, "thc"),
            "CBD": _potency_total(p, "cbd"),
            "weight": weight,
            "original_price": price,
        }
        product.update(deal_columns)
        products.append(product)


# =========================
# FETCH LOOP
# =========================
try:
    logger.info("Capturing public client keys from sunnyside.shop ...")
    AUTH.update(capture_keys())
    logger.info("Keys captured. Starting %s scrape | stores=%d", company, len(stores))

    for store in stores:
        logger.info(f"=== {store['dispensary_location']} (id={store['id']}) ===")
        before = len(products)

        for usage_type in USAGE_TYPES:
            for category in categories:
                offset = 0
                while True:
                    data = get_inventory(store["id"], category, usage_type, offset)
                    all_json.append({"store_id": store["id"], "usage_type": usage_type,
                                     "category": category, "offset": offset, "data": data})
                    rows = data.get("data") or data.get("rows") or []
                    total = data.get("total_rows") or data.get("total_count") or 0
                    handle_rows(rows, store, category)

                    if not rows:
                        break
                    offset += PAGE_SIZE
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
logger.info("✅ Sunnyside scraper completed successfully")
sys.exit(0)