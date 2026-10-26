"""
Hii Experience (Sunland Park, NM) scraper.

Their own site (hiiexperience.com) uses Blaze, but Weedmaps also indexes
their menu and has a cleaner API. Using Weedmaps for consistency.

  GET https://api-g.weedmaps.com/discovery/v1/listings/dispensaries/
      <slug>/menu_items?page=<n>&page_size=100

Store:
    hii-experience  -> Sunland Park  ~410 items

Env vars:
    HII_PROXY / WEEDMAPS_PROXY / HTTPS_PROXY  -- residential US proxy

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

try:
    from curl_cffi import requests as _http
    _HTTP_BACKEND = "curl_cffi"
except ImportError:
    import requests as _http
    _HTTP_BACKEND = "requests"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

company = "Hii Experience"
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

STORES = [
    {"dispensary_location": "Sunland Park", "slug": "hii-experience"},
]

API_BASE = "https://api-g.weedmaps.com/discovery/v1/listings/dispensaries"
PAGE_SIZE = 100
REQUEST_DELAY_S = 0.5
HTTP_TIMEOUT = 45
MAX_PAGES = 60

_CHROME_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36")

HEADERS = {"User-Agent": _CHROME_UA, "Accept": "application/json, text/plain, */*",
           "Accept-Language": "en-US,en;q=0.9", "Origin": "https://weedmaps.com",
           "Referer": "https://weedmaps.com/",
           "sec-ch-ua": '"Chromium";v="148", "Not)A;Brand";v="8", "Google Chrome";v="148"',
           "sec-ch-ua-mobile": "?0", "sec-ch-ua-platform": '"Windows"',
           "sec-fetch-site": "same-site", "sec-fetch-mode": "cors", "sec-fetch-dest": "empty"}


def _resolve_proxy():
    url = (os.environ.get("HII_PROXY") or os.environ.get("WEEDMAPS_PROXY")
           or os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY"))
    return {"http": url, "https": url} if url else None


def make_session():
    proxies = _resolve_proxy()
    s = _http.Session(impersonate="chrome120") if _HTTP_BACKEND == "curl_cffi" else _http.Session()
    s.headers.update(HEADERS)
    if proxies:
        s.proxies.update(proxies)
    logger.info(f"backend={_HTTP_BACKEND} {'via proxy' if proxies else 'DIRECT (no proxy)'}")
    return s


def fetch_page(session, slug, page):
    r = session.get(f"{API_BASE}/{slug}/menu_items",
                    params={"page": page, "page_size": PAGE_SIZE, "sort_by": "position"},
                    timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


_STRAIN_MAP = {"sativa": "Sativa", "indica": "Indica", "hybrid": "Hybrid",
               "sativa-hybrid": "Sativa-Hybrid", "indica-hybrid": "Indica-Hybrid",
               "sativa hybrid": "Sativa-Hybrid", "indica hybrid": "Indica-Hybrid", "cbd": "High CBD"}


def resolve_strain(item):
    """
    Weedmaps' top-level `category.name` is a trap:
      - For non-flower products (Edibles, Concentrates, Gear, Topicals,
        Preroll, Tincture, Drink...) it mirrors the product type, not the
        strain -- so using it fills `strain_type` with values like "Edible".
      - For flower products with no real strain tag it defaults to "Indica",
        so every untagged Sativa/Hybrid comes out as Indica.
    So we only trust `genetics_tag.name` (human-assigned, may be null) and,
    for flower products specifically, fall back to parsing the strain from
    the product name's (S)/(I)/(H) or `| Hybrid` tokens. Anything else gets
    a blank strain -- which is correct, not a bug.
    """
    gt = (item.get("genetics_tag") or {}).get("name")
    if gt:
        k = str(gt).strip().lower()
        if k in _STRAIN_MAP:
            return _STRAIN_MAP[k]
        for token in ("indica-hybrid", "sativa-hybrid", "indica", "sativa", "hybrid", "cbd"):
            if token in k:
                return _STRAIN_MAP[token]
        return str(gt).strip()
    edge = ((item.get("edge_category") or {}).get("name") or "").strip().lower()
    if not any(t in edge for t in ("flower", "pre-roll", "preroll", "bud", "smalls")):
        return ""
    lower = (item.get("name") or "").strip().lower()
    if not lower:
        return ""
    for tok, label in (
        ("(sativa hybrid)", "Sativa-Hybrid"), ("(indica hybrid)", "Indica-Hybrid"),
        ("(sh)", "Sativa-Hybrid"), ("(hs)", "Sativa-Hybrid"),
        ("(ih)", "Indica-Hybrid"), ("(hi)", "Indica-Hybrid"),
        ("(hybrid)", "Hybrid"), ("(sativa)", "Sativa"), ("(indica)", "Indica"),
        ("(cbd)", "High CBD"),
        ("(s)", "Sativa"), ("(i)", "Indica"), ("(h)", "Hybrid"),
        ("| sativa hybrid", "Sativa-Hybrid"), ("| indica hybrid", "Indica-Hybrid"),
        ("| sativa", "Sativa"), ("| indica", "Indica"), ("| hybrid", "Hybrid"),
        ("sativa hybrid", "Sativa-Hybrid"), ("indica hybrid", "Indica-Hybrid"),
        ("sativa", "Sativa"), ("indica", "Indica"), ("hybrid", "Hybrid"),
        ("high cbd", "High CBD"),
    ):
        if tok in lower:
            return label
    return ""


def resolve_brand(item):
    return str((item.get("brand_endorsement") or {}).get("brand_name") or "").strip()


def resolve_category(item):
    return ((item.get("edge_category") or {}).get("name") or "").strip().lower()


def resolve_subcategory(item):
    ec = item.get("edge_category") or {}
    ancestors = ec.get("ancestors") or []
    if ancestors:
        return (ancestors[-1].get("name") or "").strip().upper().replace(" ", "-")
    return (ec.get("slug") or "").strip().upper().replace(" ", "-") or "DEFAULT"


def _fmt_potency(value, unit):
    if value in (None, "", 0):
        return ""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return ""
    if v == 0:
        return ""
    return f"{v:g} {(unit or '%').strip()}".strip()


def resolve_thc(item):
    m = ((item.get("metrics") or {}).get("aggregates") or {})
    return _fmt_potency(m.get("thc"), m.get("thc_unit"))


def resolve_cbd(item):
    m = ((item.get("metrics") or {}).get("aggregates") or {})
    return _fmt_potency(m.get("cbd"), m.get("cbd_unit"))


def _price_variants(item):
    variants = []
    for unit_key, entries in (item.get("prices") or {}).items():
        if not isinstance(entries, list):
            continue
        for e in entries:
            if not isinstance(e, dict):
                continue
            label = (e.get("label") or e.get("units") or unit_key or "").strip()
            variants.append((label, e.get("original_price"), e.get("price"), e.get("on_sale")))
    if variants:
        return variants
    p = item.get("price") or {}
    if p and (p.get("price") or p.get("original_price")):
        return [((p.get("label") or p.get("quantity") or "").strip(),
                 p.get("original_price"), p.get("price"), p.get("on_sale"))]
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
        original_price = orig_price if orig_price not in (None, "", 0) else sale_price
        deals = []
        if named_deal:
            deals.append(named_deal)
        if on_sale and orig_price and sale_price and orig_price > sale_price:
            savings = float(orig_price) - float(sale_price)
            pct = round(savings / float(orig_price) * 100)
            label = f"${savings:.2f} off ({pct}%)"
            if label not in deals:
                deals.append(label)
        row = {"date": today, "location": state, "company": company,
               "dispensary_location": dispensary_location, "name": name,
               "category": category, "subcategory": subcategory, "brand": brand,
               "strain_type": strain, "THC": thc, "CBD": cbd,
               "weight": weight, "original_price": original_price}
        for i, d in enumerate(deals, 1):
            row[f"deal_{i}"] = d
        rows.append(row)
    return rows


all_rows = []
seen_keys = set()
raw_json_dump = []

session = make_session()
logger.info(f"Starting {company} scrape | stores={len(STORES)}")

for store in STORES:
    try:
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
        logger.info(f"=> {store['dispensary_location']}: {len(all_rows) - store_rows_before} rows added")
    except Exception as e:
        logger.warning(f"⚠️ {store['dispensary_location']} failed: {e}. Continuing.")


try:
    with open(json_file_path, "w", encoding="utf-8") as f:
        json.dump({"per_store_page_counts": raw_json_dump, "total_rows": len(all_rows)}, f, indent=2)
    logger.info(f"JSON saved: {json_file_path}")
except Exception as e:
    logger.critical(f"❌ JSON save failed: {e}", exc_info=True)
    sys.exit(1)

try:
    df = pd.DataFrame(all_rows).drop_duplicates(subset=["dispensary_location", "name", "weight", "original_price"])
    if df.empty or len(df) < 5:
        logger.critical(f"❌ Invalid CSV output. Rows: {len(df)}. Likely 403 from Weedmaps geo-fence.")
        sys.exit(1)
    fixed_cols = ["date", "location", "company", "dispensary_location", "name",
                  "category", "subcategory", "brand", "strain_type",
                  "THC", "CBD", "weight", "original_price"]
    deal_cols = sorted([c for c in df.columns if c.startswith("deal_")], key=lambda x: int(x.split("_")[1]))
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