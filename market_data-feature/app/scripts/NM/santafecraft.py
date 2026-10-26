"""
Santa Fe Craft Cannabis (New Mexico) scraper.

Santa Fe Craft embeds a DUTCHIE menu on santafecraftcannabis.com/products
(dutchie.com/api/v2/embedded-menu/<dispensaryId>.js). The old Playwright
interception approach kept crashing in the scheduled Linux container
(EPIPE / browser process death), so this fetches Dutchie's canonical GraphQL
endpoint directly — the same persisted FilteredProducts query as Dark Matter /
BOGO / Dreamz. No browser.

  API: GET https://dutchie.com/api-3/graphql
       ?operationName=FilteredProducts
       &extensions=<persistedQuery{sha256Hash}>
       &variables=<productsFilter with dispensaryId>

Dispensary ID captured from the embedded-menu loader URL on the store page:
    6493c85cc7d7b60009b1e480 -> Santa Fe (607 Cerrillos Road, Suite A)

PRICE NOTE: original_price is the OFFERED (sale) price when a product is on
special; the savings is recorded as a deal column.

Env vars:
    SANTAFE_PROXY / HTTPS_PROXY  -- residential US proxy (Dutchie 403s cloud IPs on Azure)

Requirements:
    pip install curl_cffi pytz pandas requests
"""

import pandas as pd
import json
import os
import time
from datetime import datetime
import pytz
import logging
import sys

try:
    from curl_cffi import requests as _http  # pip install curl_cffi
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
company = "SantaFeCraft"
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

# =========================
# DUTCHIE API
# =========================
API_URL = "https://dutchie.com/api-3/graphql"
OPERATION_NAME = "FilteredProducts"
# Persisted-query hash for FilteredProducts (matches Dark Matter / BOGO / Dreamz). If Dutchie rotates
# the hash the script fails with a GraphQL "PersistedQueryNotFound" error — refetch it from any live
# Dutchie storefront network log and update.
PERSISTED_HASH = "0fae0c65b4f7910948ee6bb3ff5dc31e5a437839609b037f7d88a5215c82768d"

PRICING_TYPE = "rec"
PAGE_SIZE = 50
REQUEST_DELAY_S = 0.5
RETRY_ON_FAIL = 1
HTTP_TIMEOUT = 30
MAX_PAGES = 100

# Santa Fe Craft store(s). `storefront` is the Dutchie-hosted embedded-menu page we hit once to seed
# Dutchie/Cloudflare cookies.
stores = [
    {"id": "6493c85cc7d7b60009b1e480", "dispensary_location": "Santa Fe",
     "storefront": "https://dutchie.com/embedded-menu/6493c85cc7d7b60009b1e480"},
]

STOREFRONT_URL = stores[0]["storefront"]

_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)


def build_headers():
    return {
        "User-Agent": _CHROME_UA,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Origin": "https://dutchie.com",
        "Referer": STOREFRONT_URL,
        "apollo-require-preflight": "true",
        "x-apollo-operation-name": OPERATION_NAME,
        "sec-ch-ua": '"Chromium";v="148", "Not)A;Brand";v="8", "Google Chrome";v="148"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
    }


def build_warmup_headers():
    return {
        "User-Agent": _CHROME_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "sec-ch-ua": '"Chromium";v="148", "Not)A;Brand";v="8", "Google Chrome";v="148"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-site": "none",
        "sec-fetch-mode": "navigate",
        "sec-fetch-dest": "document",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
    }


def _resolve_proxy():
    url = (
        os.environ.get("SANTAFE_PROXY")
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
    if proxies:
        s.proxies.update(proxies)

    proxy_note = "via proxy" if proxies else "DIRECT (no proxy)"
    try:
        r = s.get(STOREFRONT_URL, headers=build_warmup_headers(), timeout=HTTP_TIMEOUT)
        logger.info(
            f"warm-up: status={r.status_code} cookies={len(s.cookies)} "
            f"backend={_HTTP_BACKEND} {proxy_note}"
        )
        if r.status_code == 403 and not proxies:
            logger.warning(
                "warm-up returned 403 with no proxy configured. "
                "Set SANTAFE_PROXY to a residential US proxy URL."
            )
    except Exception as e:
        logger.warning(f"warm-up GET failed (continuing anyway): {e}")
    return s


# =========================
# GLOBAL STORAGE
# =========================
products = []
seen_keys = set()
all_json = []


# =========================
# HELPERS
# =========================
_STRAIN_MAP = {
    "hybrid": "Hybrid", "sativa": "Sativa", "indica": "Indica",
    "sativa-hybrid": "Sativa-Hybrid", "indica-hybrid": "Indica-Hybrid",
    "cbd": "High CBD", "high cbd": "High CBD", "high_cbd": "High CBD",
}


def resolve_strain_type(product):
    raw = product.get("strainType")
    if not raw or not isinstance(raw, str):
        return ""
    key = raw.strip().lower()
    if not key or key in {"not_applicable", "n/a", "none", "no strain"}:
        return ""
    if key in _STRAIN_MAP:
        return _STRAIN_MAP[key]
    for token in ("indica-hybrid", "sativa-hybrid", "indica", "sativa", "hybrid", "high cbd", "cbd"):
        if token in key:
            return _STRAIN_MAP[token]
    return raw.strip()


_UNIT_ABBR = {
    "PERCENTAGE": "%", "PERCENT": "%", "%": "%",
    "MILLIGRAMS": "mg", "MILLIGRAM": "mg", "MG": "mg",
    "GRAMS": "g", "GRAM": "g", "G": "g",
}


def _format_potency(content_obj):
    if not isinstance(content_obj, dict):
        return ""
    rng = content_obj.get("range")
    unit_raw = (content_obj.get("unit") or "").strip().upper()
    unit = _UNIT_ABBR.get(unit_raw, unit_raw.lower() if unit_raw else "")
    if not rng or not isinstance(rng, list):
        return ""

    def _num(x):
        try:
            return f"{float(x):g}"
        except (TypeError, ValueError):
            return str(x)

    nums = [r for r in rng if r not in (None, "")]
    if not nums:
        return ""
    if len(nums) == 1:
        val_str = _num(nums[0])
    else:
        val_str = f"{_num(nums[0])} - {_num(nums[-1])}"
    return f"{val_str} {unit}".strip()


def resolve_thc(product):
    out = _format_potency(product.get("THCContent"))
    if out:
        return out
    legacy = product.get("THC")
    if legacy in (None, "", 0):
        return ""
    try:
        return f"{float(legacy):g} %"
    except (TypeError, ValueError):
        return str(legacy)


def resolve_cbd(product):
    out = _format_potency(product.get("CBDContent"))
    if out:
        return out
    legacy = product.get("CBD")
    if legacy in (None, "", 0):
        return ""
    try:
        return f"{float(legacy):g} %"
    except (TypeError, ValueError):
        return str(legacy)


def resolve_category(product):
    return (product.get("type") or "").strip().lower()


def resolve_subcategory(product):
    raw = (product.get("subcategory") or "").strip()
    return raw.upper().replace(" ", "-") if raw else "DEFAULT"


def resolve_brand(product):
    return (product.get("brandName") or "").strip()


def _deal_label(d):
    if not isinstance(d, dict):
        return ""
    for k in ("specialName", "menuDisplayName", "name", "title"):
        v = d.get(k)
        if v and isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def extract_deals(product, option):
    deals = []
    seen = set()
    sd = product.get("specialData") or {}

    for d in sd.get("saleSpecials") or []:
        eligible = d.get("eligibleProductOptions") or []
        if eligible and option not in eligible:
            continue
        label = _deal_label(d)
        if label and label not in seen:
            seen.add(label)
            deals.append(label)

    for d in sd.get("bogoSpecials") or []:
        label = _deal_label(d)
        if label and label not in seen:
            seen.add(label)
            deals.append(label)

    return deals


def compute_savings_label(rec_prices, sale_prices, idx):
    if idx >= len(rec_prices) or idx >= len(sale_prices):
        return ""
    try:
        orig = float(rec_prices[idx])
        spec = float(sale_prices[idx])
    except (TypeError, ValueError):
        return ""
    if orig <= 0 or spec <= 0 or spec >= orig:
        return ""
    savings = orig - spec
    pct = round(savings / orig * 100)
    return f"${savings:.2f} off ({pct}%)"


# =========================
# DUTCHIE FETCHER
# =========================
_session = None


def _get_session():
    global _session
    if _session is None:
        _session = make_session()
    return _session


def _rewarm(store):
    global STOREFRONT_URL
    STOREFRONT_URL = store["storefront"]
    try:
        _get_session().get(STOREFRONT_URL, headers=build_warmup_headers(), timeout=HTTP_TIMEOUT)
    except Exception as e:
        logger.warning(f"per-store warm-up failed: {e}")


def dutchie_query(store_id, page):
    variables = {
        "includeEnterpriseSpecials": False,
        "productsFilter": {
            "dispensaryId": store_id,
            "pricingType": PRICING_TYPE,
            "strainTypes": [],
            "subcategories": [],
            "Status": "Active",
            "types": [],
            "useCache": False,
            "isDefaultSort": True,
            "sortBy": "popular",
            "sortDirection": 1,
            "bypassOnlineThresholds": False,
            "isKioskMenu": False,
            "removeProductsBelowOptionThresholds": True,
            "platformType": "ONLINE_MENU",
            "preOrderType": None,
        },
        "page": page,
        "perPage": PAGE_SIZE,
    }
    extensions = {"persistedQuery": {"version": 1, "sha256Hash": PERSISTED_HASH}}
    params = {
        "operationName": OPERATION_NAME,
        "variables": json.dumps(variables, separators=(",", ":")),
        "extensions": json.dumps(extensions, separators=(",", ":")),
    }
    headers = build_headers()
    session = _get_session()

    last_err = None
    for attempt in range(RETRY_ON_FAIL + 1):
        try:
            r = session.get(API_URL, params=params, headers=headers, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            data = r.json()
            if "errors" in data and data["errors"]:
                raise RuntimeError(f"GraphQL errors: {data['errors']}")
            return data
        except Exception as e:
            last_err = e
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status == 403 and attempt < RETRY_ON_FAIL:
                logger.warning("403 received; re-warming session before retry")
                try:
                    session.get(STOREFRONT_URL, headers=build_warmup_headers(), timeout=HTTP_TIMEOUT)
                except Exception:
                    pass
            if attempt < RETRY_ON_FAIL:
                time.sleep(1.5 * (attempt + 1))

    hint = ""
    final_status = getattr(getattr(last_err, "response", None), "status_code", None)
    if final_status == 403 or "403" in str(last_err):
        if not _resolve_proxy():
            hint = (
                " | HINT: 403 with no proxy = Dutchie has blacklisted this "
                "server's IP. Set SANTAFE_PROXY=<residential-proxy-url>."
            )
        else:
            hint = (
                " | HINT: 403 even WITH proxy -- rotate to a different "
                "residential session/IP."
            )
    raise RuntimeError(f"Dutchie query failed after retries: {last_err}{hint}")


def handle_response(data, store):
    try:
        all_json.append({"store_id": store["id"], "page": data.get("__page"), "data": data})
        fp = (data.get("data") or {}).get("filteredProducts") or {}
        items = fp.get("products") or []
    except Exception as e:
        logger.critical(f"❌ Response parse failed: {e}", exc_info=True)
        sys.exit(1)

    for product in items:
        options = product.get("Options") or []
        rec_prices = product.get("recPrices") or product.get("Prices") or []
        sale_prices = product.get("recSpecialPrices") or []
        if not options:
            logger.warning(f"⚠️ No options for: {product.get('Name')}")
            continue

        category = resolve_category(product)
        subcategory = resolve_subcategory(product)
        brand = resolve_brand(product)
        strain = resolve_strain_type(product)
        thc = resolve_thc(product)
        cbd = resolve_cbd(product)
        name = product.get("Name")

        for idx, option in enumerate(options):
            try:
                weight = (option or "").strip()
                # original_price = the OFFERED (sale) price when on special, else the regular price.
                price = None
                if idx < len(sale_prices) and sale_prices[idx] not in (None, 0):
                    price = sale_prices[idx]
                elif idx < len(rec_prices):
                    price = rec_prices[idx]

                deals = extract_deals(product, weight)
                savings = compute_savings_label(rec_prices, sale_prices, idx)
                if savings:
                    deals.append(savings)
                deal_columns = {f"deal_{i+1}": d for i, d in enumerate(deals)}

                dedupe_key = (
                    store["id"],
                    product.get("id") or product.get("_id") or name,
                    weight,
                )
                if dedupe_key in seen_keys:
                    continue
                seen_keys.add(dedupe_key)

                row = {
                    "date": today,
                    "location": state,
                    "company": company,
                    "dispensary_location": store["dispensary_location"],
                    "name": name,
                    "category": category,
                    "subcategory": subcategory,
                    "brand": brand,
                    "strain_type": strain,
                    "THC": thc,
                    "CBD": cbd,
                    "weight": weight,
                    "original_price": price,
                }
                row.update(deal_columns)
                products.append(row)
            except Exception as e:
                logger.critical(f"❌ Variant processing failed: {e}", exc_info=True)
                sys.exit(1)


def paginate(store):
    page_idx = 0
    collected = 0
    total = None
    while page_idx < MAX_PAGES:
        data = dutchie_query(store["id"], page_idx)
        data["__page"] = page_idx
        fp = (data.get("data") or {}).get("filteredProducts") or {}
        items = fp.get("products") or []
        qi = fp.get("queryInfo") or {}
        total = qi.get("totalCount", total or 0) or 0
        handle_response(data, store)
        collected += len(items)
        logger.info(f"  page {page_idx}: +{len(items)} (collected {collected}/{total})")

        if not items or (total and collected >= total):
            break
        page_idx += 1
        time.sleep(REQUEST_DELAY_S)

    if page_idx >= MAX_PAGES:
        logger.warning(f"  hit MAX_PAGES safety cap ({MAX_PAGES})")
    return collected


# =========================
# FETCH LOOP
# =========================
try:
    logger.info(f"Starting {company} scrape | pricing={PRICING_TYPE} | stores={len(stores)} (Dutchie)")

    for store in stores:
        logger.info(f"--- {store['dispensary_location']} (id={store['id']}) ---")
        _rewarm(store)
        rows_before = len(products)
        paginate(store)
        rows_added = len(products) - rows_before
        logger.info(f"=> {store['dispensary_location']}: {rows_added} rows added")

except Exception as e:
    # Graceful skip: a 403 with no proxy configured = Dutchie is IP-blocking this host. Exit 0 (don't
    # fail the pipeline) and warn; set SANTAFE_PROXY/HTTPS_PROXY to a US residential proxy to enable it.
    # A 403 WITH a proxy is a real problem (bad/blocked proxy), so still fail then.
    if "403" in str(e) and not _resolve_proxy():
        logger.warning(
            f"⚠ {company}: SKIPPED — Dutchie is IP-blocking this host (403) and no proxy is set. "
            f"Set SANTAFE_PROXY or HTTPS_PROXY to a US residential proxy to enable it. No data this run."
        )
        sys.exit(0)
    logger.critical(f"❌ {company}/Dutchie fetch failed: {e}", exc_info=True)
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

    try:
        df.to_csv(output_file, index=False, na_rep="")
    except PermissionError:
        alt = output_file.replace(".csv", f"_{datetime.now(us_tz).strftime('%H%M%S')}.csv")
        df.to_csv(alt, index=False, na_rep="")
        logger.warning(f"⚠ {output_file} is locked (open in Excel?). Saved to {alt} instead")
        output_file = alt
    per_store = df.groupby("dispensary_location").size().to_dict()
    logger.info(f"CSV saved: {output_file} | Rows: {len(df)} | Deal columns: {len(deal_cols)}")
    logger.info(f"Rows per dispensary: {per_store}")
except Exception as e:
    logger.critical(f"❌ CSV save failed: {e}", exc_info=True)
    sys.exit(1)

logger.info(f"✅ {company} scraper completed successfully")
sys.exit(0)
