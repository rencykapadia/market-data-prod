import pandas as pd
import json
import os
import re
import time
from datetime import datetime
import pytz
import logging
import sys

# =========================
# HTTP BACKEND
# =========================
# Dutchie's edge (Cloudflare) returns 403 to plain `requests` traffic from
# cloud-provider IPs (we see this from Azure). curl_cffi mimics a real
# Chrome TLS handshake so the request passes the JA3/JA4 fingerprint check.
# Fall back to stdlib requests when the package isn't installed (e.g.,
# local dev on a residential IP, where plain requests is sufficient).
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
# TLS IMPERSONATION
# =========================
# The impersonation target and the User-Agent / sec-ch-ua headers MUST report
# the same Chrome major version. A Chrome-120 TLS handshake carrying a
# Chrome-148 UA string is itself a detection signal -- that mismatch was in
# the build that failed on 2026-09-08.
#
# Newest-first. We only pick a target this curl_cffi build actually knows
# about; if we can't enumerate them we fall back to a long-standing one.
_IMPERSONATE_CANDIDATES = (
    "chrome136", "chrome133a", "chrome131", "chrome124", "chrome120",
)
_CONSERVATIVE_TARGET = "chrome124"


def _supported_impersonate_targets():
    """Enumerate targets this curl_cffi build supports; None when unknowable."""
    try:
        from curl_cffi.requests import BrowserType
        return {b.value for b in BrowserType}
    except Exception:
        pass
    try:
        import typing
        from curl_cffi.requests.impersonate import BrowserTypeLiteral
        return set(typing.get_args(BrowserTypeLiteral))
    except Exception:
        return None


def _resolve_impersonate():
    """
    Return (impersonate_target_or_None, chrome_major_string).

    Override the auto-pick with IMPERSONATE_TARGET=<target> on the container
    env if Dutchie starts flagging whatever we default to.
    """
    override = os.environ.get("IMPERSONATE_TARGET")
    if _HTTP_BACKEND != "curl_cffi":
        # No TLS impersonation available. Still report a coherent UA.
        return None, _CONSERVATIVE_TARGET.replace("chrome", "")

    supported = _supported_impersonate_targets()
    candidates = ((override,) if override else ()) + _IMPERSONATE_CANDIDATES

    if supported is None:
        target = override or _CONSERVATIVE_TARGET
    else:
        target = next((c for c in candidates if c in supported), _CONSERVATIVE_TARGET)

    m = re.search(r"(\d+)", target)
    return target, (m.group(1) if m else "124")


IMPERSONATE_TARGET, _CHROME_MAJOR = _resolve_impersonate()

if _HTTP_BACKEND != "curl_cffi":
    logger.warning(
        "curl_cffi is NOT installed -- falling back to stdlib requests. "
        "Dutchie's edge 403s stdlib TLS from cloud IPs, so this run will "
        "almost certainly fail. Add `curl_cffi` to requirements.txt and "
        "rebuild the image."
    )
else:
    logger.info(f"HTTP backend=curl_cffi impersonate={IMPERSONATE_TARGET}")

# =========================
# CONFIG
# =========================
company = "AYR"
state = "Pennsylvania"

us_tz = pytz.timezone("America/Phoenix")
today = datetime.now(us_tz).strftime("%Y-%m-%d")

output_folder = "app/output/pending"
csv_folder = os.path.join(output_folder, "csv")
json_folder = os.path.join(output_folder, "json")
os.makedirs(csv_folder, exist_ok=True)
os.makedirs(json_folder, exist_ok=True)
output_file = os.path.join(csv_folder, f"{today}_{company}.csv")
json_file_path = os.path.join(json_folder, f"{today}_{company}.json")

# AYR Wellness PA uses Dutchie for menu, but their WordPress storefront
# does NOT proxy `/api-3/graphql` on ayrdispensaries.com. Hit Dutchie's
# canonical host directly, then warm the session with a load of the
# per-store Dutchie storefront page.
API_URL = "https://dutchie.com/api-3/graphql"
OPERATION_NAME = "FilteredProducts"
# Persisted-query hash that the live storefront uses for FilteredProducts.
# If Dutchie rotates the hash the script will fail with a GraphQL
# "PersistedQueryNotFound" error -- refetch it from a live network log at
# https://dutchie.com/dispensary/ayr-pa-gibsonia and update.
PERSISTED_HASH = "0fae0c65b4f7910948ee6bb3ff5dc31e5a437839609b037f7d88a5215c82768d"

# AYR PA is a MEDICAL market -- use 'med' pricing. Change to 'rec' if
# the store also carries recreational SKUs.
PRICING_TYPE = "med"
PAGE_SIZE = 50
REQUEST_DELAY_S = 0.5
RETRY_ON_FAIL = 1
HTTP_TIMEOUT = 30
MAX_PAGES = 100                # safety cap

# Minimum rows before we call the run valid. A partial run (some stores
# blocked) still writes a CSV -- we only hard-fail when we got essentially
# nothing, so one bad store can't zero out five good ones.
MIN_VALID_ROWS = 10

stores = [
    {"id": "5ff8ee358174a300e11a15cb", "dispensary_location": "Gibsonia",         "cName": "ayr-pa-gibsonia"},
    {"id": "613a9411f117b000bee2ce2a", "dispensary_location": "Bryn Mawr",        "cName": "ayr-pa-bryn-mawr"},
    {"id": "628fbf469d871500ac3100d6", "dispensary_location": "Indiana",          "cName": "ayr-pa-indiana"},
    {"id": "613a93ed31607800d9de4f18", "dispensary_location": "Montgomeryville",  "cName": "ayr-pa-montgomeryville"},
    {"id": "5f8dac128397a700d26ff5bd", "dispensary_location": "New Castle",       "cName": "ayr-pa-new-castle"},
    {"id": "5ff8ee158707e300e97eb9ee", "dispensary_location": "Plymouth Meeting", "cName": "ayr-pa-plymouth-meeting"},
]


def _storefront_url_for(store):
    return f"https://dutchie.com/dispensary/{store.get('cName', 'ayr-pa-gibsonia')}"


# Browser-grade headers. Cloud-provider IPs get flagged by Dutchie's edge
# unless the request looks indistinguishable from Chrome -- including the
# Sec-Fetch-* and Sec-CH-UA-* hints that browsers send automatically.
# Both are derived from _CHROME_MAJOR so they always agree with the TLS
# fingerprint selected above.
_CHROME_UA = (
    f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    f"(KHTML, like Gecko) Chrome/{_CHROME_MAJOR}.0.0.0 Safari/537.36"
)
_SEC_CH_UA = (
    f'"Chromium";v="{_CHROME_MAJOR}", "Not)A;Brand";v="8", '
    f'"Google Chrome";v="{_CHROME_MAJOR}"'
)


def build_headers(store):
    """GraphQL request headers. Referer is per-store so it matches the
    dispensaryId in the query -- previously every store sent Gibsonia's."""
    return {
        "User-Agent": _CHROME_UA,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Origin": "https://dutchie.com",
        "Referer": _storefront_url_for(store),
        "apollo-require-preflight": "true",
        "x-apollo-operation-name": OPERATION_NAME,
        "sec-ch-ua": _SEC_CH_UA,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
    }


def build_warmup_headers():
    """First-load headers for the storefront HTML page (sets session cookies)."""
    return {
        "User-Agent": _CHROME_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "sec-ch-ua": _SEC_CH_UA,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-site": "none",
        "sec-fetch-mode": "navigate",
        "sec-fetch-dest": "document",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
    }


def _resolve_proxy():
    """
    Read proxy config from env. Priority:
      AYR_PROXY      -- explicit override for this scraper only
      HTTPS_PROXY    -- standard env var (picked up by most HTTP clients)
      HTTP_PROXY     -- same, http variant
    Returns a {scheme: url} dict that curl_cffi/requests accepts, or None
    when no proxy is configured.

    Recommended residential-proxy services that work for Dutchie:
        - Bright Data (residential or ISP plan)
        - Smartproxy / Decodo (residential)
        - Oxylabs (residential)
        - IPRoyal (residential)
    Proxy URL format:
        http://user:pass@host:port
        socks5://user:pass@host:port  (curl_cffi only)
    """
    url = (
        os.environ.get("AYR_PROXY")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("HTTP_PROXY")
    )
    if not url:
        return None
    return {"http": url, "https": url}


def make_session():
    """
    Build a session with curl_cffi if available (impersonates Chrome's TLS
    handshake) -- otherwise stdlib requests.

    If AYR_PROXY / HTTPS_PROXY is set, all traffic is routed through that
    proxy. This is required when running from cloud-provider IPs (Azure,
    AWS, GCP) because Dutchie's edge blacklists them.
    """
    proxies = _resolve_proxy()
    if _HTTP_BACKEND == "curl_cffi":
        s = _http.Session(impersonate=IMPERSONATE_TARGET)
    else:
        s = _http.Session()
    if proxies:
        s.proxies.update(proxies)
    return s


def warm_up(session, store):
    """
    GET this store's Dutchie storefront so the edge sets its cookies before
    any GraphQL call for that dispensaryId goes out. Called once per store
    -- the previous version warmed only stores[0] and reused those cookies
    (and that Referer) for all six.
    """
    proxies = _resolve_proxy()
    proxy_note = "via proxy" if proxies else "DIRECT (no proxy)"
    url = _storefront_url_for(store)
    try:
        r = session.get(url, headers=build_warmup_headers(), timeout=HTTP_TIMEOUT)
        logger.info(
            f"warm-up {store['dispensary_location']}: status={r.status_code} "
            f"cookies={len(session.cookies)} backend={_HTTP_BACKEND} "
            f"impersonate={IMPERSONATE_TARGET or 'n/a'} {proxy_note}"
        )
        if r.status_code == 403 and not proxies:
            logger.warning(
                "warm-up returned 403 with no proxy configured. "
                "Dutchie likely blocks this server's IP range. "
                "Set AYR_PROXY env var to a residential proxy URL "
                "(e.g. http://user:pass@host:port)."
            )
        return r.status_code
    except Exception as e:
        logger.warning(f"warm-up {store['dispensary_location']} failed (continuing): {e}")
        return None


# =========================
# GLOBAL STORAGE
# =========================
products = []
seen_keys = set()
all_json = []
store_failures = []


# =========================
# HELPERS
# =========================
_STRAIN_MAP = {
    "hybrid": "Hybrid",
    "sativa": "Sativa",
    "indica": "Indica",
    "sativa-hybrid": "Sativa-Hybrid",
    "indica-hybrid": "Indica-Hybrid",
    "cbd": "High CBD",
    "high cbd": "High CBD",
    "high_cbd": "High CBD",
}


def resolve_strain_type(product):
    """
    Dutchie exposes the strain type at product.strainType. Observed values
    include 'Sativa', 'Indica', 'Hybrid', 'Sativa-Hybrid'. Normalize through
    the same mapping the ZenLeaf scraper uses so the output CSV is
    consistent across companies.
    """
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
    "PERCENTAGE": "%",
    "PERCENT": "%",
    "%": "%",
    "MILLIGRAMS": "mg",
    "MILLIGRAM": "mg",
    "MG": "mg",
    "GRAMS": "g",
    "GRAM": "g",
    "G": "g",
}


def _format_potency(content_obj):
    """
    Dutchie potency shape:
        {"unit": "PERCENTAGE" | "MILLIGRAMS", "range": [value] | [low, high] | null}
    Returns 'value unit', or 'low - high unit', or '' when no data.
    """
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
    # Dutchie newer schema: THCContent. Legacy: THC (a number).
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
    cat = product.get("type")
    return (cat or "").strip().lower()


def resolve_subcategory(product):
    raw = product.get("subcategory") or ""
    raw = raw.strip()
    return raw.upper().replace(" ", "-") if raw else "DEFAULT"


def resolve_brand(product):
    return (product.get("brandName") or "").strip()


def _deal_label(d):
    """Pick the best display string for a sale/bogo special."""
    if not isinstance(d, dict):
        return ""
    for k in ("specialName", "menuDisplayName", "name", "title"):
        v = d.get(k)
        if v and isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def extract_deals(product, option):
    """
    Per-option deal labels. saleSpecials carry an `eligibleProductOptions`
    list -- only emit a named sale when it covers the option we're on (an
    empty/missing list means "all options"). BOGOs are treated as
    product-level (they can have multi-product conditions, so option-level
    filtering would be misleading).
    """
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
    """
    Build the menu-style '$X.XX off (N%)' label from per-option price
    arrays. Returns '' when there is no usable discount for this option.
    Dutchie's `recSpecialPrices` is index-aligned with `Options` /
    `recPrices`, and is often shorter than recPrices when only some
    options are on sale.
    """
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
# Single session reused for every query so cookies, TLS state, and
# connection-pooling stick across pages and stores.
_session = None


def _get_session():
    global _session
    if _session is None:
        _session = make_session()
    return _session


def dutchie_query(store, page):
    """
    Use the persisted-query GET endpoint that the storefront uses.
    Falls back gracefully if Dutchie rotates the hash (caller logs).
    """
    variables = {
        "includeEnterpriseSpecials": False,
        "productsFilter": {
            "dispensaryId": store["id"],
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
    extensions = {
        "persistedQuery": {"version": 1, "sha256Hash": PERSISTED_HASH}
    }
    params = {
        "operationName": OPERATION_NAME,
        "variables": json.dumps(variables, separators=(",", ":")),
        "extensions": json.dumps(extensions, separators=(",", ":")),
    }
    headers = build_headers(store)
    session = _get_session()

    last_err = None
    for attempt in range(RETRY_ON_FAIL + 1):
        try:
            r = session.get(API_URL, params=params, headers=headers, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            data = r.json()
            if "errors" in data and data["errors"]:
                # surface GraphQL-level errors (e.g. PersistedQueryNotFound)
                raise RuntimeError(f"GraphQL errors: {data['errors']}")
            return data
        except Exception as e:
            last_err = e
            # If we got an HTTP 403, the edge has flagged us -- refreshing
            # cookies via another storefront GET sometimes unblocks the
            # next attempt, especially right after deploy. Re-warm against
            # THIS store, not stores[0].
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status == 403 and attempt < RETRY_ON_FAIL:
                logger.warning("403 received; re-warming session before retry")
                try:
                    warm_up(session, store)
                except Exception:
                    pass
            if attempt < RETRY_ON_FAIL:
                time.sleep(1.5 * (attempt + 1))

    # Final failure -- include a deployment hint when the cause is an IP block.
    hint = ""
    final_status = getattr(getattr(last_err, "response", None), "status_code", None)
    if final_status == 403 or "403" in str(last_err):
        if _HTTP_BACKEND != "curl_cffi":
            hint = (
                " | HINT: 403 while running on stdlib requests -- install "
                "curl_cffi (add it to requirements.txt and rebuild) before "
                "reaching for a proxy. Dutchie fingerprints the TLS handshake."
            )
        elif not _resolve_proxy():
            hint = (
                " | HINT: 403 with curl_cffi and no proxy = Dutchie has "
                "blacklisted this server's IP. Set AYR_PROXY="
                "<residential-proxy-url> on the container env and redeploy."
            )
        else:
            hint = (
                " | HINT: 403 even WITH proxy -- the proxy IP itself may be "
                "burned. Rotate to a different residential session/IP."
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
                price = rec_prices[idx] if idx < len(rec_prices) else None

                # Per-option deals: named specials filtered by eligibility,
                # plus a price-savings label ('$X.XX off (N%)') derived
                # from rec_prices[idx] vs sale_prices[idx]. This is the
                # red badge users see on the menu cards.
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
    """
    Page through FilteredProducts until queryInfo.totalCount is reached
    (or an empty page comes back). We deliberately do NOT short-circuit
    on a smaller-than-PAGE_SIZE page -- Dutchie occasionally returns
    short pages mid-way through a category sweep.
    """
    page_idx = 0
    collected = 0
    total = None
    while page_idx < MAX_PAGES:
        data = dutchie_query(store, page_idx)
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
# One store failing no longer aborts the run. We collect what we can and
# validate the total at the end, so a single blocked dispensary can't zero
# out five good ones.
logger.info(
    f"Starting AYR scrape | pricing={PRICING_TYPE} | stores={len(stores)} "
    f"| backend={_HTTP_BACKEND}"
)

session = _get_session()

for store in stores:
    logger.info(f"--- {store['dispensary_location']} (id={store['id']}) ---")
    rows_before = len(products)
    try:
        warm_up(session, store)
        paginate(store)
    except Exception as e:
        logger.error(f"  {store['dispensary_location']} FAILED: {e}")
        store_failures.append({"store": store["dispensary_location"], "error": str(e)})
        continue
    rows_added = len(products) - rows_before
    logger.info(f"=> {store['dispensary_location']}: {rows_added} rows added")

if store_failures:
    logger.warning(
        f"{len(store_failures)}/{len(stores)} stores failed: "
        + ", ".join(f["store"] for f in store_failures)
    )


# =========================
# SAVE JSON
# =========================
# Written before validation so a failed run still leaves something to
# inspect offline.
try:
    with open(json_file_path, "w", encoding="utf-8") as f:
        json.dump(
            {"responses": all_json, "store_failures": store_failures},
            f, indent=2, default=str,
        )
    logger.info(f"JSON saved: {json_file_path}")
except Exception as e:
    logger.critical(f"❌ JSON save failed: {e}", exc_info=True)
    sys.exit(1)

if not all_json:
    logger.critical(
        "❌ No API responses captured for any store. "
        f"backend={_HTTP_BACKEND} proxy={'set' if _resolve_proxy() else 'unset'}"
    )
    sys.exit(1)


# =========================
# SAVE CSV
# =========================
try:
    df = pd.DataFrame(products).drop_duplicates(subset=["location", "name", "weight"])
    if df.empty or len(df) < MIN_VALID_ROWS:
        logger.critical(
            f"❌ Invalid CSV output. Rows: {len(df)} (need >= {MIN_VALID_ROWS}). "
            f"Failed stores: {[f['store'] for f in store_failures] or 'none'}"
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
    df = df.reindex(columns=fixed_cols + deal_cols)

    df.to_csv(output_file, index=False, na_rep="")
    per_store = df.groupby("dispensary_location").size().to_dict()
    cat_counts = df.groupby("category").size().to_dict()
    logger.info(f"CSV saved: {output_file} | Rows: {len(df)} | Deal columns: {len(deal_cols)}")
    logger.info(f"Rows per dispensary: {per_store}")
    logger.info(f"Rows per category: {cat_counts}")
except SystemExit:
    raise
except Exception as e:
    logger.critical(f"❌ CSV save failed: {e}", exc_info=True)
    sys.exit(1)

# A partial run is a warning, not a silent success.
if store_failures:
    logger.warning(
        f"⚠️ AYR scraper completed PARTIALLY -- "
        f"{len(stores) - len(store_failures)}/{len(stores)} stores collected"
    )
else:
    logger.info("✅ AYR scraper completed successfully")
sys.exit(0)