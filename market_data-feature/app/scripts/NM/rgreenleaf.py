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
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger(__name__)

# =========================
# CONFIG
# =========================
# PLATFORM: R. Greenleaf runs the iHeartJane ("Jane") menu (images on
# uploads.iheartjane.com, product URLs /shop/products/<id>). Same backend as the RISE
# scraper, so this uses the identical, proven Jane/Algolia request: POST to
# search.iheartjane.com with the public app id + search key and a per-store facet
# filter. Pure `requests` — no browser, no data-testid selectors, no timeouts.
# The finefettle.com `?storeid=NNNN` values ARE Jane store ids, used directly here.
company = "RGreenleaf"
state = "New Mexico"

us_tz = pytz.timezone("America/Denver")
today = datetime.now(us_tz).strftime("%Y-%m-%d")

output_folder = "app/output/pending"
csv_folder = os.path.join(output_folder, "csv")
json_folder = os.path.join(output_folder, "json")

os.makedirs(csv_folder, exist_ok=True)
os.makedirs(json_folder, exist_ok=True)

output_file = os.path.join(csv_folder, f"{today}_{company}.csv")
json_file_path = os.path.join(json_folder, f"{today}_{company}.json")

# Algolia / iHeartJane backend (same endpoint + key the working RISE scraper uses)
ALGOLIA_URL = "https://search.iheartjane.com/1/indexes/*/queries"
ALGOLIA_INDEX = "menu-products-production"

_APP_ID_B64 = "VkZNNFgwTjIzQQ=="
_API_KEY_B64 = "MjJiYmJhOGUxZWRmMjgwYjM0ZjQyYzE0NzUzODczNDM="
ALGOLIA_APP_ID = base64.b64decode(_APP_ID_B64).decode("ascii")
ALGOLIA_API_KEY = base64.b64decode(_API_KEY_B64).decode("ascii")

PAGE_SIZE = 500
REQUEST_DELAY_S = 0.5
RETRY_ON_FAIL = 1
HTTP_TIMEOUT = 30
SALE_TYPE = "recreational"

# NEW MEXICO STORES (Jane store_id from rgreenleaf.com store pages / schwazze-mini-menu store_id).
# Midtown = 4910 (4414 Menaul Blvd NE — the Menaul/Midtown store).
stores = [
    {"id": "4918", "dispensary_location": "Bernalillo", "sale_types": ["recreational", "medical"]},
    {"id": "4910", "dispensary_location": "Albuquerque - Midtown", "sale_types": ["recreational", "medical"]},
]

# =========================
# GLOBAL STORAGE
# =========================
products = []
seen_keys = set()
all_json = []

# =========================
# HELPERS
# =========================
_WEIGHT_LABEL = {
    "each": "",
    "half_gram": "0.5g",
    "gram": "1g",
    "two_gram": "2g",
    "eighth_ounce": "1/8oz",
    "quarter_ounce": "1/4oz",
    "half_ounce": "1/2oz",
    "ounce": "1oz",
}

_AVAILABLE_WEIGHT_TO_BUCKET = {
    "each": "each",
    "half gram": "half_gram", "0.5g": "half_gram", ".5g": "half_gram",
    "gram": "gram", "1g": "gram",
    "two gram": "two_gram", "2g": "two_gram",
    "eighth ounce": "eighth_ounce", "1/8 ounce": "eighth_ounce", "1/8oz": "eighth_ounce",
    "quarter ounce": "quarter_ounce", "1/4 ounce": "quarter_ounce", "1/4oz": "quarter_ounce",
    "half ounce": "half_ounce", "1/2 ounce": "half_ounce", "1/2oz": "half_ounce",
    "ounce": "ounce", "1 ounce": "ounce", "1oz": "ounce",
}

_NAME_WEIGHT_RE = re.compile(
    r"""
    (?<![A-Za-z0-9])
    (
        \d+\s*/\s*\d+\s*oz                        |
        \d+(?:\.\d+)?\s*(?:grams?|gm|g)\b         |
        \d+(?:\.\d+)?\s*(?:ounces?|oz)\b          |
        \d+(?:\.\d+)?\s*(?:milligrams?|mg)\b      |
        \d+(?:\.\d+)?\s*(?:milliliters?|ml)\b     |
        \d+(?:\.\d+)?\s*(?:kilograms?|kg)\b
    )
    (?![A-Za-z0-9])
    """,
    re.IGNORECASE | re.VERBOSE,
)

_STRAIN_MAP = {
    "hybrid": "Hybrid",
    "sativa": "Sativa",
    "indica": "Indica",
    "cbd": "High CBD",
    "high cbd": "High CBD",
    "high_cbd": "High CBD",
    "thc": "THC",
}

_NAME_TYPE_PATTERNS = [
    re.compile(r"\((H|I|S|CBD)\)\s*\d*\s*$", re.IGNORECASE),
    re.compile(r"\bT\d+(?:\.\d+)?%?\s+(H|I|S|CBD)\s+\d{3,}\s*$", re.IGNORECASE),
    re.compile(r"\s+(H|I|S|CBD)\s+\d{3,}\s*$", re.IGNORECASE),
]
_SUFFIX_TO_TYPE = {"H": "Hybrid", "I": "Indica", "S": "Sativa", "CBD": "High CBD"}


def normalize_weight_label(label):
    s = re.sub(r"\s+", "", str(label).strip().lower())
    s = s.replace("grams", "g").replace("gram", "g").replace("gm", "g")
    s = s.replace("ounces", "oz").replace("ounce", "oz")
    s = s.replace("milligrams", "mg").replace("milligram", "mg")
    s = s.replace("milliliters", "ml").replace("milliliter", "ml")
    s = s.replace("kilograms", "kg").replace("kilogram", "kg")
    s = s.replace("packs", "pk").replace("pack", "pk")
    s = s.replace("counts", "ct").replace("count", "ct")
    s = s.replace("each", "ea")
    return s


def weight_from_name(name):
    if not name:
        return ""
    m = _NAME_WEIGHT_RE.search(name)
    return normalize_weight_label(m.group(1)) if m else ""


def resolve_weight(bucket, raw_label, name):
    if bucket and bucket in _WEIGHT_LABEL:
        label = _WEIGHT_LABEL[bucket]
        if label:
            return label
        return weight_from_name(name)
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
    if value is None or value == "":
        return None
    try:
        return f"{float(value):g} {unit}"
    except (TypeError, ValueError):
        return str(value)


_POTENCY_TOKEN_RE = re.compile(
    r"\b(THCA|CBDA|THC|CBD|CBG|CBN|CBC|TAC)\s*:\s*([\d.]+)\s*(mg|%)",
    re.IGNORECASE,
)


def _parse_store_notes_potency(store_notes):
    found = {}
    if not store_notes or not isinstance(store_notes, str):
        return found
    for m in _POTENCY_TOKEN_RE.finditer(store_notes):
        cann = m.group(1).upper()
        if cann not in found:
            found[cann] = f"{m.group(2)} {m.group(3)}"
    return found


def resolve_potency(hit, base):
    base_u = base.upper()
    acid_u = base_u + "A"

    tokens = _parse_store_notes_potency(hit.get("store_notes"))
    if acid_u in tokens:
        return tokens[acid_u]
    if base_u in tokens:
        return tokens[base_u]

    for key in (f"percent_{base}a", f"percent_{base}"):
        v = hit.get(key)
        if v not in (None, "", 0):
            return format_potency(v, "%")
    return None


def resolve_strain_type(hit):
    candidates = [
        hit.get("category"),
        hit.get("aggregate_kind"),
        hit.get("kind_subtype"),
        hit.get("root_subtype"),
        hit.get("lineage"),
        hit.get("strain_subtype"),
        hit.get("kind"),
    ]
    for raw in candidates:
        if not raw or not isinstance(raw, str):
            continue
        key = raw.strip().lower()
        if not key or key in {"not_applicable", "n/a", "none", "no strain"}:
            continue
        if key in _STRAIN_MAP:
            return _STRAIN_MAP[key]
        for token in ("indica", "sativa", "hybrid", "high cbd", "high_cbd", "cbd"):
            if token in key:
                return _STRAIN_MAP[token if token != "high_cbd" else "high cbd"]

    name = hit.get("name") or ""
    for pat in _NAME_TYPE_PATTERNS:
        m = pat.search(name)
        if m:
            return _SUFFIX_TO_TYPE[m.group(1).upper()]
    return ""


def resolve_category(hit):
    return (
        hit.get("custom_product_type")
        or hit.get("kind")
        or hit.get("category")
        or ""
    ).lower()


def resolve_subcategory(hit):
    raw = (
        hit.get("custom_product_subtype")
        or hit.get("kind_subtype")
        or hit.get("root_subtype")
        or ""
    )
    return raw.upper().replace(" ", "-") if raw else "DEFAULT"


def extract_deals(hit):
    deals = []
    seen = set()

    def push(t):
        if not t:
            return
        t = str(t).strip()
        if not t or t in seen:
            return
        seen.add(t)
        deals.append(t)

    push(hit.get("special_title"))
    bsp = hit.get("brand_special_prices") or {}
    if isinstance(bsp, dict):
        for entry in bsp.values():
            if isinstance(entry, dict):
                push(entry.get("title"))
                push(entry.get("discount_label"))
    types = hit.get("applicable_special_types") or []
    if isinstance(types, list):
        for t in types:
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


# =========================
# ALGOLIA FETCHER
# =========================
def algolia_query(store_id, page, sale_type):
    params = "&".join(
        f"{k}={requests.utils.quote(str(v), safe='')}"
        for k, v in {
            "hitsPerPage": PAGE_SIZE,
            "page": page,
            "query": "",
            "facetFilters": json.dumps([f"store_id:{store_id}"]),
            "filters": f"store_types:{sale_type}",
        }.items()
    )
    body = {"requests": [{"indexName": ALGOLIA_INDEX, "params": params}]}
    headers = {
        "Content-Type": "application/json",
        "x-algolia-api-key": ALGOLIA_API_KEY,
        "x-algolia-application-id": ALGOLIA_APP_ID,
        "User-Agent": "Mozilla/5.0 (finefettle-scraper/1.0)",
    }
    last_err = None
    for attempt in range(RETRY_ON_FAIL + 1):
        try:
            r = requests.post(ALGOLIA_URL, headers=headers, json=body, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            data = r.json()
            if "results" not in data:
                raise RuntimeError(f"unexpected response: {data!r}")
            return data
        except Exception as e:
            last_err = e
            if attempt < RETRY_ON_FAIL:
                time.sleep(1.0)
    raise RuntimeError(f"Algolia query failed after retries: {last_err}")


def handle_response(data, store):
    try:
        all_json.append(data)
        result = data["results"][0]
        hits = result.get("hits", [])
    except Exception as e:
        logger.critical(f"❌ Response parse failed: {e}", exc_info=True)
        sys.exit(1)

    for hit in hits:
        deals = extract_deals(hit)
        deal_columns = {f"deal_{i+1}": d for i, d in enumerate(deals)}

        available_weights = hit.get("available_weights") or []
        if not available_weights:
            variants = [(None, None)]
        else:
            variants = [(normalize_bucket(w), w) for w in available_weights]

        for bucket, raw_label in variants:
            try:
                price = price_for(hit, bucket)
                weight = resolve_weight(bucket, raw_label, hit.get("name", ""))

                dedupe_key = (
                    store["id"],
                    hit.get("product_id") or hit.get("objectID") or hit.get("name"),
                    bucket or raw_label or weight,
                )
                if dedupe_key in seen_keys:
                    continue
                seen_keys.add(dedupe_key)

                product = {
                    "date": today,
                    "location": state,
                    "company": company,
                    "dispensary_location": store["dispensary_location"],
                    "name": hit.get("name"),
                    "category": resolve_category(hit),
                    "subcategory": resolve_subcategory(hit),
                    "brand": hit.get("brand"),
                    "strain_type": resolve_strain_type(hit),
                    "THC": resolve_potency(hit, "thc"),
                    "CBD": resolve_potency(hit, "cbd"),
                    "weight": weight,
                    "original_price": price,
                }
                product.update(deal_columns)
                products.append(product)

            except Exception as e:
                logger.critical(f"❌ Variant processing failed: {e}", exc_info=True)
                sys.exit(1)


# =========================
# FETCH LOOP
# =========================
try:
    targets = [s for s in stores if SALE_TYPE in s["sale_types"]]
    logger.info(f"Starting {company} scrape | sale_type={SALE_TYPE} | stores={len(targets)}")

    for store in targets:
        logger.info(f"Opening store: {store['dispensary_location']} (id={store['id']})")
        page_idx = 0
        while True:
            data = algolia_query(store["id"], page_idx, SALE_TYPE)
            handle_response(data, store)

            result = data["results"][0]
            hits = result.get("hits", [])
            n_pages = result.get("nbPages", 0)
            total = result.get("nbHits", 0)
            logger.info(f"  page {page_idx+1}/{n_pages}: +{len(hits)} hits (total {total})")

            if not hits or page_idx + 1 >= n_pages:
                logger.info("Last page reached")
                break
            page_idx += 1
            time.sleep(REQUEST_DELAY_S)

except Exception as e:
    logger.critical(f"❌ Algolia fetch failed: {e}", exc_info=True)
    sys.exit(1)


# =========================
# SAVE JSON
# =========================
try:
    if not all_json:
        logger.critical("❌ No API responses captured (JSON empty)")
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
    df = pd.DataFrame(products).drop_duplicates(
        subset=["location", "name", "weight", "original_price"]
    )

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

    logger.info(f"CSV saved: {output_file} | Rows: {len(df)} | Deal columns: {len(deal_cols)}")

except Exception as e:
    logger.critical(f"❌ CSV save failed: {e}", exc_info=True)
    sys.exit(1)


# =========================
# SUCCESS
# =========================
logger.info(f"✅ {company} (Bernalillo) scraper completed successfully")
sys.exit(0)
