import pandas as pd
import json
import os
import base64
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
# PLATFORM: High Desert Relief runs the iHeartJane ("Jane") menu (highdesertrelief.org carries the
# jane:version plugin meta; stores live at iheartjane.com/stores/<id>). Same backend as the RISE /
# FineFettle scrapers, so this uses the proven Jane/Algolia request: POST to search.iheartjane.com
# with the public app id + search key and a per-store facet filter. Pure `requests`.
# Jane store ids: Coors 3039 (3320 Coors NW / ABQ West / Taylor Ranch), Pan American 3040,
# Alameda 6470, Santa Fe 3830.
company = "HighDesertRelief"
location = "New Mexico"

us_tz = pytz.timezone("America/Denver")
today = datetime.now(us_tz).strftime("%Y-%m-%d")

output_folder = "app/output/pending"
csv_folder = os.path.join(output_folder, "csv")
json_folder = os.path.join(output_folder, "json")
os.makedirs(csv_folder, exist_ok=True)
os.makedirs(json_folder, exist_ok=True)
output_file = os.path.join(csv_folder, f"{today}_{company}.csv")
json_file_path = os.path.join(json_folder, f"{today}_{company}.json")

ALGOLIA_URL = "https://search.iheartjane.com/1/indexes/*/queries"
ALGOLIA_INDEX = "menu-products-production"
ALGOLIA_APP_ID = base64.b64decode("VkZNNFgwTjIzQQ==").decode("ascii")
ALGOLIA_API_KEY = base64.b64decode("MjJiYmJhOGUxZWRmMjgwYjM0ZjQyYzE0NzUzODczNDM=").decode("ascii")

PAGE_SIZE = 500
REQUEST_DELAY_S = 0.5
RETRY_ON_FAIL = 1
HTTP_TIMEOUT = 30
SALE_TYPE = "recreational"

stores = [
    {"id": "3039", "dispensary_location": "Albuquerque - Coors", "sale_types": ["recreational", "medical"]},
    {"id": "3040", "dispensary_location": "Albuquerque - Pan American", "sale_types": ["recreational", "medical"]},
    {"id": "6470", "dispensary_location": "Albuquerque - Alameda", "sale_types": ["recreational", "medical"]},
    {"id": "3830", "dispensary_location": "Santa Fe", "sale_types": ["recreational", "medical"]},
]

products = []
seen_keys = set()
all_json = []

_WEIGHT_LABEL = {"each": "", "half_gram": "0.5g", "gram": "1g", "two_gram": "2g",
                 "eighth_ounce": "1/8oz", "quarter_ounce": "1/4oz", "half_ounce": "1/2oz", "ounce": "1oz"}
_AVAILABLE_WEIGHT_TO_BUCKET = {
    "each": "each", "half gram": "half_gram", "0.5g": "half_gram", ".5g": "half_gram",
    "gram": "gram", "1g": "gram", "two gram": "two_gram", "2g": "two_gram",
    "eighth ounce": "eighth_ounce", "1/8 ounce": "eighth_ounce", "1/8oz": "eighth_ounce",
    "quarter ounce": "quarter_ounce", "1/4 ounce": "quarter_ounce", "1/4oz": "quarter_ounce",
    "half ounce": "half_ounce", "1/2 ounce": "half_ounce", "1/2oz": "half_ounce",
    "ounce": "ounce", "1 ounce": "ounce", "1oz": "ounce",
}
_NAME_WEIGHT_RE = re.compile(
    r"(?<![A-Za-z0-9])(\d+\s*/\s*\d+\s*oz|\d+(?:\.\d+)?\s*(?:grams?|gm|g)\b|"
    r"\d+(?:\.\d+)?\s*(?:ounces?|oz)\b|\d+(?:\.\d+)?\s*(?:milligrams?|mg)\b|"
    r"\d+(?:\.\d+)?\s*(?:milliliters?|ml)\b|\d+(?:\.\d+)?\s*(?:kilograms?|kg)\b)(?![A-Za-z0-9])",
    re.IGNORECASE)
_STRAIN_MAP = {"hybrid": "Hybrid", "sativa": "Sativa", "indica": "Indica",
               "cbd": "High CBD", "high cbd": "High CBD", "high_cbd": "High CBD", "thc": "THC"}
_NAME_TYPE_PATTERNS = [
    re.compile(r"\((H|I|S|CBD)\)\s*\d*\s*$", re.IGNORECASE),
    re.compile(r"\bT\d+(?:\.\d+)?%?\s+(H|I|S|CBD)\s+\d{3,}\s*$", re.IGNORECASE),
    re.compile(r"\s+(H|I|S|CBD)\s+\d{3,}\s*$", re.IGNORECASE),
]
_SUFFIX_TO_TYPE = {"H": "Hybrid", "I": "Indica", "S": "Sativa", "CBD": "High CBD"}
_POTENCY_TOKEN_RE = re.compile(r"\b(THCA|CBDA|THC|CBD|CBG|CBN|CBC|TAC)\s*:\s*([\d.]+)\s*(mg|%)", re.IGNORECASE)


def normalize_weight_label(label):
    s = re.sub(r"\s+", "", str(label).strip().lower())
    for a, b in (("grams", "g"), ("gram", "g"), ("gm", "g"), ("ounces", "oz"), ("ounce", "oz"),
                 ("milligrams", "mg"), ("milligram", "mg"), ("milliliters", "ml"), ("milliliter", "ml"),
                 ("kilograms", "kg"), ("kilogram", "kg"), ("packs", "pk"), ("pack", "pk"),
                 ("counts", "ct"), ("count", "ct"), ("each", "ea")):
        s = s.replace(a, b)
    return s


def weight_from_name(name):
    if not name:
        return ""
    m = _NAME_WEIGHT_RE.search(name)
    return normalize_weight_label(m.group(1)) if m else ""


def resolve_weight(bucket, raw_label, name):
    if bucket and bucket in _WEIGHT_LABEL:
        label = _WEIGHT_LABEL[bucket]
        return label if label else weight_from_name(name)
    if raw_label:
        norm = normalize_weight_label(raw_label)
        if norm and not re.search(r"(pk|ct|ea)$", norm):
            return norm
    return weight_from_name(name)


def normalize_bucket(label):
    if not isinstance(label, str):
        return None
    return _AVAILABLE_WEIGHT_TO_BUCKET.get(label.strip().lower())


def format_potency(value, unit="%"):
    if value in (None, ""):
        return None
    try:
        return f"{float(value):g} {unit}"
    except (TypeError, ValueError):
        return str(value)


def _parse_store_notes_potency(store_notes):
    found = {}
    if not store_notes or not isinstance(store_notes, str):
        return found
    for m in _POTENCY_TOKEN_RE.finditer(store_notes):
        found.setdefault(m.group(1).upper(), f"{m.group(2)} {m.group(3)}")
    return found


def resolve_potency(hit, base):
    tokens = _parse_store_notes_potency(hit.get("store_notes"))
    if (base.upper() + "A") in tokens:
        return tokens[base.upper() + "A"]
    if base.upper() in tokens:
        return tokens[base.upper()]
    for key in (f"percent_{base}a", f"percent_{base}"):
        v = hit.get(key)
        if v not in (None, "", 0):
            return format_potency(v, "%")
    return None


def resolve_strain_type(hit):
    for raw in (hit.get("category"), hit.get("aggregate_kind"), hit.get("kind_subtype"),
                hit.get("root_subtype"), hit.get("lineage"), hit.get("strain_subtype"), hit.get("kind")):
        if not raw or not isinstance(raw, str):
            continue
        key = raw.strip().lower()
        if not key or key in {"not_applicable", "n/a", "none", "no strain"}:
            continue
        if key in _STRAIN_MAP:
            return _STRAIN_MAP[key]
        for token in ("indica", "sativa", "hybrid", "high cbd", "high_cbd", "cbd"):
            if token in key:
                return _STRAIN_MAP["high cbd" if token in ("high_cbd", "cbd") else token]
    name = hit.get("name") or ""
    for pat in _NAME_TYPE_PATTERNS:
        m = pat.search(name)
        if m:
            return _SUFFIX_TO_TYPE[m.group(1).upper()]
    return ""


def resolve_category(hit):
    return (hit.get("custom_product_type") or hit.get("kind") or hit.get("category") or "").lower()


def resolve_subcategory(hit):
    raw = (hit.get("custom_product_subtype") or hit.get("kind_subtype") or hit.get("root_subtype") or "")
    return raw.upper().replace(" ", "-") if raw else "DEFAULT"


def extract_deals(hit):
    deals, seen = [], set()

    def push(t):
        if not t:
            return
        t = str(t).strip()
        if t and t not in seen:
            seen.add(t)
            deals.append(t)
    push(hit.get("special_title"))
    bsp = hit.get("brand_special_prices") or {}
    if isinstance(bsp, dict):
        for entry in bsp.values():
            if isinstance(entry, dict):
                push(entry.get("title"))
                push(entry.get("discount_label"))
    for t in (hit.get("applicable_special_types") or []):
        if isinstance(t, str):
            push(t)
    return deals


def price_for(hit, bucket):
    if bucket:
        p = hit.get(f"price_{bucket}")
        if p not in (None, "", 0):
            return p
    for key in ("price_each", "price"):
        p = hit.get(key)
        if p not in (None, "", 0):
            return p
    return None


def algolia_query(store_id, page, sale_type):
    params = "&".join(
        f"{k}={requests.utils.quote(str(v), safe='')}"
        for k, v in {
            "hitsPerPage": PAGE_SIZE, "page": page, "query": "",
            "facetFilters": json.dumps([f"store_id:{store_id}"]),
            "filters": f"store_types:{sale_type}",
        }.items())
    body = {"requests": [{"indexName": ALGOLIA_INDEX, "params": params}]}
    headers = {
        "Content-Type": "application/json",
        "x-algolia-api-key": ALGOLIA_API_KEY,
        "x-algolia-application-id": ALGOLIA_APP_ID,
        "User-Agent": "Mozilla/5.0 (hdr-scraper/1.0)",
    }
    last = None
    for attempt in range(RETRY_ON_FAIL + 1):
        try:
            r = requests.post(ALGOLIA_URL, headers=headers, json=body, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            data = r.json()
            if "results" not in data:
                raise RuntimeError(f"unexpected response: {data!r}")
            return data
        except Exception as e:
            last = e
            if attempt < RETRY_ON_FAIL:
                time.sleep(1.0)
    raise RuntimeError(f"Algolia query failed: {last}")


def handle_response(data, store):
    all_json.append({"store": store["id"]})
    hits = (data["results"][0] or {}).get("hits", [])
    for hit in hits:
        deals = extract_deals(hit)
        deal_columns = {f"deal_{i+1}": d for i, d in enumerate(deals)}
        available_weights = hit.get("available_weights") or []
        variants = [(None, None)] if not available_weights else [(normalize_bucket(w), w) for w in available_weights]
        for bucket, raw_label in variants:
            price = price_for(hit, bucket)
            weight = resolve_weight(bucket, raw_label, hit.get("name", ""))
            key = (store["id"], hit.get("product_id") or hit.get("objectID") or hit.get("name"),
                   bucket or raw_label or weight)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            row = {
                "date": today, "location": location, "company": company,
                "dispensary_location": store["dispensary_location"], "name": hit.get("name"),
                "category": resolve_category(hit), "subcategory": resolve_subcategory(hit),
                "brand": hit.get("brand"), "strain_type": resolve_strain_type(hit),
                "THC": resolve_potency(hit, "thc"), "CBD": resolve_potency(hit, "cbd"),
                "weight": weight, "original_price": price,
            }
            row.update(deal_columns)
            products.append(row)


# =========================
# FETCH LOOP
# =========================
try:
    targets = [s for s in stores if SALE_TYPE in s["sale_types"]]
    logger.info(f"Starting {company} scrape | sale_type={SALE_TYPE} | stores={len(targets)}")
    for store in targets:
        logger.info(f"=== {store['dispensary_location']} (id={store['id']}) ===")
        page_idx = 0
        while True:
            data = algolia_query(store["id"], page_idx, SALE_TYPE)
            handle_response(data, store)
            result = data["results"][0]
            hits = result.get("hits", [])
            n_pages = result.get("nbPages", 0)
            logger.info(f"  page {page_idx+1}/{n_pages}: +{len(hits)} hits (rows {len(products)})")
            if not hits or page_idx + 1 >= n_pages:
                break
            page_idx += 1
            time.sleep(REQUEST_DELAY_S)
except Exception as e:
    logger.critical(f"❌ Algolia fetch failed: {e}", exc_info=True)
    sys.exit(1)


# =========================
# SAVE JSON + CSV
# =========================
try:
    if not products:
        logger.critical("❌ No products collected")
        sys.exit(1)
    with open(json_file_path, "w", encoding="utf-8") as f:
        json.dump(products, f, indent=2)
    logger.info(f"JSON saved: {json_file_path}")

    df = pd.DataFrame(products).drop_duplicates(subset=["dispensary_location", "name", "weight", "original_price"])
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

logger.info(f"✅ {company} scraper completed successfully")
sys.exit(0)
