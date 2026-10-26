"""
Love Cannabis (New Mexico) scraper.

Love Cannabis's own website (lovecannabisdispensary.com) doesn't host a
menu -- it just links to their Weedmaps storefront. We hit Weedmaps'
public storefront API directly:

  GET https://api-g.weedmaps.com/discovery/v1/listings/dispensaries/
      <slug>/menu_items?page=<n>&page_size=100

Returns { meta: { total_menu_items }, data: { menu_items: [...] } }
with full product data (name, brand_endorsement, edge_category, price,
prices per weight, metrics.aggregates.thc/cbd, current_deal_title,
deal_ids, genetics_tag).

Stores (2 of the 3 Love Cannabis locations are indexed on Weedmaps;
Isleta doesn't have a Weedmaps listing):
    love-cannabis                             -> Eubank Blvd (ABQ)  ~459 items
    love-cannabis-dispensary-san-mateo        -> San Mateo (ABQ)    ~283 items

Env vars:
    WEEDMAPS_PROXY / HTTPS_PROXY  -- residential US proxy (recommended;
                                     Weedmaps geo-blocks datacenter IPs)

Requirements:
    pip install curl_cffi pytz pandas requests
"""

import os
import sys
import json
import time
import logging
from datetime import datetime
import pytz
import pandas as pd

# curl_cffi mimics Chrome's TLS handshake. Weedmaps' Cloudflare edge
# rejects plain `requests` traffic from most cloud IPs.
try:
    from curl_cffi import requests as _http
    _HTTP_BACKEND = "curl_cffi"
except ImportError:
    import requests as _http
    _HTTP_BACKEND = "requests"

# =========================
# LOGGER
# =========================
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

# =========================
# CONFIG
# =========================
company = "Love Cannabis"
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

# Weedmaps dispensary slugs. Isleta location is not indexed on Weedmaps
# so it's omitted here; add it if/when Love Cannabis lists it.
STORES = [
    {"dispensary_location": "Albuquerque - Eubank",    "slug": "love-cannabis"},
    {"dispensary_location": "Albuquerque - San Mateo", "slug": "love-cannabis-dispensary-san-mateo"},
]

# =========================
# WEEDMAPS API
# =========================
API_BASE = "https://api-g.weedmaps.com/discovery/v1/listings/dispensaries"
PAGE_SIZE = 100
REQUEST_DELAY_S = 0.5
HTTP_TIMEOUT = 45
MAX_PAGES = 60   # safety cap (60 * 100 = 6000 item ceiling)

_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": _CHROME_UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://weedmaps.com",
    "Referer": "https://weedmaps.com/",
    "sec-ch-ua": '"Chromium";v="148", "Not)A;Brand";v="8", "Google Chrome";v="148"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-site": "same-site",
    "sec-fetch-mode": "cors",
    "sec-fetch-dest": "empty",
}


def _resolve_proxy():
    url = (
        os.environ.get("WEEDMAPS_PROXY")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("HTTP_PROXY")
    )
    if not url:
        return None
    return {"http": url, "https": url}


def make_session():
    proxies = _resolve_proxy()
    if _HTTP_BACKEND == "curl_cffi":
        s = _http.Session(impersonate="chrome120")
    else:
        s = _http.Session()
    s.headers.update(HEADERS)
    if proxies:
        s.proxies.update(proxies)
    logger.info(f"backend={_HTTP_BACKEND} {'via proxy' if proxies else 'DIRECT (no proxy)'}")
    return s


def fetch_page(session, slug, page):
    url = f"{API_BASE}/{slug}/menu_items"
    params = {"page": page, "page_size": PAGE_SIZE, "sort_by": "position"}
    r = session.get(url, params=params, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


# =========================
# PARSER
# =========================
_STRAIN_MAP = {
    "sativa": "Sativa", "indica": "Indica", "hybrid": "Hybrid",
    "sativa-hybrid": "Sativa-Hybrid", "indica-hybrid": "Indica-Hybrid",
    "sativa hybrid": "Sativa-Hybrid", "indica hybrid": "Indica-Hybrid",
    "cbd": "High CBD",
}


def resolve_strain(item):
    for src in [
        (item.get("genetics_tag") or {}).get("name"),
        (item.get("category") or {}).get("name"),
    ]:
        if not src:
            continue
        k = str(src).strip().lower()
        if k in _STRAIN_MAP:
            return _STRAIN_MAP[k]
        for token in ("indica-hybrid", "sativa-hybrid", "indica", "sativa", "hybrid", "cbd"):
            if token in k:
                return _STRAIN_MAP[token]
        return str(src).strip()
    return ""


def resolve_brand(item):
    be = item.get("brand_endorsement") or {}
    name = be.get("brand_name")
    if name:
        return str(name).strip()
    # Fall back: sometimes brand is baked into the product name
    return ""


def resolve_category(item):
    ec = item.get("edge_category") or {}
    return (ec.get("name") or "").strip().lower()


def resolve_subcategory(item):
    ec = item.get("edge_category") or {}
    ancestors = ec.get("ancestors") or []
    if ancestors:
        return (ancestors[-1].get("name") or "").strip().upper().replace(" ", "-")
    slug = (ec.get("slug") or "").strip().upper().replace(" ", "-")
    return slug or "DEFAULT"


def _fmt_potency(value, unit):
    if value in (None, "", 0):
        return ""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return ""
    if v == 0:
        return ""
    u = (unit or "%").strip()
    return f"{v:g} {u}".strip()


def resolve_thc(item):
    m = ((item.get("metrics") or {}).get("aggregates") or {})
    return _fmt_potency(m.get("thc"), m.get("thc_unit"))


def resolve_cbd(item):
    m = ((item.get("metrics") or {}).get("aggregates") or {})
    return _fmt_potency(m.get("cbd"), m.get("cbd_unit"))


def _price_variants(item):
    """Return a list of (weight_label, original_price, sale_price, on_sale, deal_title)
    tuples. Weedmaps' `prices` dict groups per unit-of-measure (each / gram /
    ounce), each containing a list of {label, price, original_price, on_sale}."""
    variants = []
    prices = item.get("prices") or {}
    for unit_key, entries in prices.items():
        if not isinstance(entries, list):
            continue
        for e in entries:
            if not isinstance(e, dict):
                continue
            label = (e.get("label") or e.get("units") or unit_key or "").strip()
            orig = e.get("original_price")
            sale = e.get("price")
            variants.append((label, orig, sale, e.get("on_sale")))
    if variants:
        return variants
    # Fallback: single price object at .price
    p = item.get("price") or {}
    if p and (p.get("price") or p.get("original_price")):
        return [(
            (p.get("label") or p.get("quantity") or "").strip(),
            p.get("original_price"),
            p.get("price"),
            p.get("on_sale"),
        )]
    return []


def parse_item(item, dispensary_location):
    name = (item.get("name") or "").strip()
    if not name:
        return []

    category = resolve_category(item)
    subcategory = resolve_subcategory(item)
    brand = resolve_brand(item)
    strain = resolve_strain(item)
    thc = resolve_thc(item)
    cbd = resolve_cbd(item)

    named_deal = (item.get("current_deal_title") or "").strip()

    rows = []
    for weight, orig_price, sale_price, on_sale in _price_variants(item):
        # original_price = strikethrough MSRP. If original_price is missing
        # (Weedmaps sometimes omits it when there's no discount), fall
        # back to the displayed price.
        original_price = orig_price if orig_price not in (None, "", 0) else sale_price

        deals = []
        if named_deal:
            deals.append(named_deal)
        if (on_sale and orig_price and sale_price
                and orig_price > sale_price):
            savings = float(orig_price) - float(sale_price)
            pct = round(savings / float(orig_price) * 100)
            label = f"${savings:.2f} off ({pct}%)"
            if label not in deals:
                deals.append(label)

        row = {
            "date": today,
            "location": state,
            "company": company,
            "dispensary_location": dispensary_location,
            "name": name,
            "category": category,
            "subcategory": subcategory,
            "brand": brand,
            "strain_type": strain,
            "THC": thc,
            "CBD": cbd,
            "weight": weight,
            "original_price": original_price,
        }
        for i, d in enumerate(deals, 1):
            row[f"deal_{i}"] = d
        rows.append(row)
    return rows


# =========================
# MAIN
# =========================
all_rows = []
seen_keys = set()
raw_json_dump = []

try:
    session = make_session()
    logger.info(f"Starting {company} scrape | stores={len(STORES)}")

    for store in STORES:
        logger.info(f"--- {store['dispensary_location']} (slug={store['slug']}) ---")
        store_rows_before = len(all_rows)
        page = 1
        total = None
        collected = 0

        while page <= MAX_PAGES:
            try:
                data = fetch_page(session, store["slug"], page)
            except Exception as e:
                logger.warning(f"  page {page}: fetch failed: {e}")
                break

            if total is None:
                total = data.get("meta", {}).get("total_menu_items")
                logger.info(f"  total items: {total}")

            items = (data.get("data") or {}).get("menu_items") or []
            if not items:
                break

            raw_json_dump.append({"store": store["dispensary_location"], "page": page, "count": len(items)})

            for item in items:
                for row in parse_item(item, store["dispensary_location"]):
                    dedupe = (row["dispensary_location"], row["name"], row["weight"], row["original_price"])
                    if dedupe in seen_keys:
                        continue
                    seen_keys.add(dedupe)
                    all_rows.append(row)

            collected += len(items)
            logger.info(f"  page {page}: +{len(items)} (collected {collected}/{total or '?'})")

            if total and collected >= total:
                break
            page += 1
            time.sleep(REQUEST_DELAY_S)

        if page > MAX_PAGES:
            logger.warning(f"  hit MAX_PAGES safety cap ({MAX_PAGES})")

        rows_added = len(all_rows) - store_rows_before
        logger.info(f"=> {store['dispensary_location']}: {rows_added} rows added")

except Exception as e:
    logger.critical(f"❌ {company} fetch failed: {e}", exc_info=True)
    sys.exit(1)


# =========================
# SAVE JSON
# =========================
try:
    with open(json_file_path, "w", encoding="utf-8") as f:
        json.dump({"per_store_page_counts": raw_json_dump, "total_rows": len(all_rows)}, f, indent=2)
    logger.info(f"JSON saved: {json_file_path}")
except Exception as e:
    logger.critical(f"❌ JSON save failed: {e}", exc_info=True)
    sys.exit(1)


# =========================
# SAVE CSV
# =========================
try:
    df = pd.DataFrame(all_rows).drop_duplicates(
        subset=["dispensary_location", "name", "weight", "original_price"]
    )
    if df.empty or len(df) < 10:
        logger.critical(
            f"❌ Invalid CSV output. Rows: {len(df)}. "
            f"Likely 403 from Weedmaps geo-fence. Set HTTPS_PROXY to a "
            f"US residential proxy."
        )
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

    per_store = df.groupby(["dispensary_location"]).size().to_dict()
    cat_counts = df.groupby("category").size().to_dict()
    logger.info(f"CSV saved: {output_file} | Rows: {len(df)} | Deal cols: {len(deal_cols)}")
    logger.info(f"Rows per dispensary: {per_store}")
    logger.info(f"Rows per category: {cat_counts}")
except Exception as e:
    logger.critical(f"❌ CSV save failed: {e}", exc_info=True)
    sys.exit(1)

logger.info(f"✅ {company} scraper completed successfully")
sys.exit(0)