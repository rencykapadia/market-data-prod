#!/usr/bin/env python3
"""
Liberty Cannabis / Holistic Industries (Pennsylvania) -- FAST scraper.

Liberty embeds Dutchie Classic. Store slugs follow libertycannabis.com
/shop/<city> -> dutchie.com/dispensary/liberty-<city>.

Strategy:
  1. GET  https://dutchie.com/embedded-menu/liberty-<city>       (HTML)
     -> extract dispensaryId from __NEXT_DATA__.
  2. Probe the GraphQL endpoint + pricingType combination that actually
     returns products for that store, then page through it.

WHAT ACTUALLY BLOCKS THIS (measured 2026-09-09, probe_dutchie.py)
-----------------------------------------------------------------
Dutchie blocks by IP REPUTATION -- not geo, not TLS fingerprint. Measured
against AYR's Gibsonia store from one machine minutes apart:

    ordinary residential IP, no VPN, stdlib requests
        -> dutchie.com 200, storefront 200, GraphQL 200 with 50 products
           of 695. Works on api-0 AND api-3, on 'med' AND 'rec'.

    same machine via a Proton US VPN exit, curl_cffi chrome136/131/124
        -> 403 on the dutchie.com HOMEPAGE, before any slug or API call.

    Azure container (US datacenter), stdlib requests -> 403 at warm-up.

So commercial VPN and cloud-datacenter ranges are refused while ordinary
residential ranges are fine, and impersonation is irrelevant either way.
Running this in Azure REQUIRES LIBERTY_PROXY pointed at a RESIDENTIAL
proxy; a datacenter proxy only swaps one blocked ASN for another. Testing
locally, run WITHOUT a VPN -- the opposite of Apothecarium.py, which is
geo-gated and needs a US exit.

WHY IT WAS RETURNING 0 ROWS
---------------------------
Three silent-zero paths were stacked here, each logging at WARNING and
carrying on:

  1. The 403s above, swallowed per-category by `logger.warning`.

  2. `PRICING_TYPE = "rec"` hard-coded, on the theory that PA is a
     medical-only market. NOTE: this turned out NOT to be a proven cause --
     the 2026-09-09 probe found AYR's PA stores return an identical 695
     products under BOTH 'med' and 'rec'. Liberty may or may not behave the
     same, so this version stops guessing and probes for the pricingType
     that actually returns products, per store.

  3. `types: [prod_type]` looped over a hand-written PRODUCT_TYPES list.
     Any string not matching Dutchie's type enum exactly returns zero for
     that whole category, with no error. AYR.py passes `types: []` and
     sweeps everything in one pass -- faster and immune to enum drift, so
     this version does the same.

Confirmed healthy as of 2026-09-09, so don't start there: the persisted-
query hash, and both the api-0 and api-3 endpoints.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime

import pandas as pd
import pytz


# === HTTP BACKEND ===========================================================

try:
    from curl_cffi import requests as _http
    _HTTP_BACKEND = "curl_cffi"
except ImportError:
    import requests as _http
    _HTTP_BACKEND = "requests"


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("liberty")


# === TLS IMPERSONATION ======================================================
# The impersonation target and the User-Agent / sec-ch-ua headers MUST report
# the same Chrome major version. The previous build sent a chrome124 TLS
# fingerprint with Chrome/126 UA strings.

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
    """Return (impersonate_target_or_None, chrome_major_string)."""
    override = os.environ.get("IMPERSONATE_TARGET")
    if _HTTP_BACKEND != "curl_cffi":
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


# === CONFIG =================================================================

company      = "Liberty"
location     = "Pennsylvania"
TIMEZONE     = "America/New_York"

# Dutchie has moved this path before. Probe both rather than guessing.
GRAPHQL_ENDPOINTS = (
    "https://dutchie.com/api-3/graphql",   # what AYR.py currently uses
    "https://dutchie.com/api-0/graphql",   # what this scraper used to use
)
OPERATION_NAME   = "FilteredProducts"
PERSISTED_QUERY_SHA = "0fae0c65b4f7910948ee6bb3ff5dc31e5a437839609b037f7d88a5215c82768d"

# PA is a MEDICAL market. "med" is tried first; "rec" stays in the list so
# the scraper self-heals if/when adult-use goes live. Override the order
# with LIBERTY_PRICING_TYPE=rec if you ever need to force one.
PRICING_TYPES = ("med", "rec")

PER_PAGE         = 50
MAX_PAGES        = 100
REQUEST_DELAY_S  = 0.35
HTTP_TIMEOUT     = 45
RETRY_ON_FAIL    = 2
MIN_VALID_ROWS   = 5

# 7 PA stores from libertycannabis.com/locations/pennsylvania.
# Slugs verified via dutchie.com/dispensary/<slug> pages (2026-08).
stores = [
    {"slug": "liberty-aliquippa",          "dispensary_location": "Aliquippa"},
    {"slug": "liberty-bensalem",           "dispensary_location": "Bensalem"},
    {"slug": "liberty-cranberry-township", "dispensary_location": "Cranberry Township"},
    {"slug": "liberty-norristown",         "dispensary_location": "Norristown"},
    {"slug": "liberty-philadelphia",       "dispensary_location": "Philadelphia"},
    {"slug": "liberty-pittsburgh",         "dispensary_location": "Pittsburgh"},
    {"slug": "liberty-willow-grove",       "dispensary_location": "Willow Grove"},
]

us_tz = pytz.timezone(TIMEZONE)
today = datetime.now(us_tz).strftime("%Y-%m-%d")

output_folder = "app/output/pending"
csv_folder = os.path.join(output_folder, "csv")
json_folder = os.path.join(output_folder, "json")
os.makedirs(csv_folder, exist_ok=True)
os.makedirs(json_folder, exist_ok=True)
output_file = os.path.join(csv_folder, f"{today}_{company}.csv")
json_file_path = os.path.join(json_folder, f"{today}_{company}.json")


# === SESSION ================================================================

_CHROME_UA = (
    f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    f"(KHTML, like Gecko) Chrome/{_CHROME_MAJOR}.0.0.0 Safari/537.36"
)
_SEC_CH_UA = (
    f'"Chromium";v="{_CHROME_MAJOR}", "Not)A;Brand";v="8", '
    f'"Google Chrome";v="{_CHROME_MAJOR}"'
)

_BROWSERISH_HEADERS = {
    "User-Agent": _CHROME_UA,
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,image/apng,*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "sec-ch-ua": _SEC_CH_UA,
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}


def _resolve_proxy():
    """LIBERTY_PROXY > HTTPS_PROXY > HTTP_PROXY. Format http://user:pass@host:port"""
    url = (
        os.environ.get("LIBERTY_PROXY")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("HTTP_PROXY")
    )
    if not url:
        return None
    return {"http": url, "https": url}


def make_session():
    proxies = _resolve_proxy()
    if _HTTP_BACKEND == "curl_cffi":
        s = _http.Session(impersonate=IMPERSONATE_TARGET)
    else:
        s = _http.Session()
    if proxies:
        s.proxies.update(proxies)
    return s


# Reusable session -- Cloudflare/Dutchie serves 403 to fresh connections
# with no cookies. Keeping the session lets the first response's cookies
# (cf_clearance etc.) attach to subsequent calls.
_SESSION = make_session()


def _http_get(url: str, headers: dict | None = None):
    hdr = dict(_BROWSERISH_HEADERS)
    if headers:
        hdr.update(headers)
    try:
        return _SESSION.get(url, headers=hdr, timeout=HTTP_TIMEOUT,
                            allow_redirects=True)
    except Exception as e:
        logger.warning(f"GET failed ({url[:80]}...): {e}")
        return None


def _cf_warmup():
    """One-off request to the Dutchie homepage so the shared session picks
    up Cloudflare's clearance cookie before we start hitting per-store URLs."""
    try:
        r = _http_get("https://dutchie.com/")
        if r is not None:
            logger.info(
                f"CF warm-up: HTTP {r.status_code}, "
                f"cookies={len(getattr(_SESSION, 'cookies', []))}, "
                f"backend={_HTTP_BACKEND}, "
                f"impersonate={IMPERSONATE_TARGET or 'n/a'}, "
                f"proxy={'set' if _resolve_proxy() else 'unset'}"
            )
            if r.status_code == 403:
                logger.error(
                    "Dutchie homepage returned 403 -- this egress IP is blocked "
                    "before any store or API call. It is IP reputation, NOT geo "
                    "and NOT TLS (stdlib requests succeeds from a residential "
                    "IP; curl_cffi fails from a VPN/datacenter one). "
                    + ("The configured LIBERTY_PROXY exit is itself blocked -- it "
                       "must be RESIDENTIAL." if _resolve_proxy() else
                       "Set LIBERTY_PROXY to a RESIDENTIAL proxy. Locally: turn "
                       "the VPN OFF.")
                )
    except Exception as e:
        logger.warning(f"CF warm-up failed: {e}")


# === ID RESOLUTION ==========================================================

def resolve_dispensary_id(slug: str):
    """Fetch Dutchie's embedded menu page and pull the dispensaryId.
    Returns (id, tried_list). Tries multiple URL patterns."""
    urls = [
        f"https://dutchie.com/embedded-menu/{slug}",
        f"https://dutchie.com/embedded-menu/{slug}/products/flower",
        f"https://dutchie.com/dispensary/{slug}",
        f"https://dutchie.com/dispensary/{slug}/info",
        f"https://dutchie.com/stores/{slug}",
    ]
    tried = []
    for u in urls:
        r = _http_get(u, {"Accept": "text/html,application/xhtml+xml"})
        if r is None:
            tried.append(f"{u.rsplit('/', 2)[-1]}=noresp")
            continue
        if r.status_code >= 400:
            tried.append(f"{u.rsplit('/', 2)[-1]}=HTTP{r.status_code}")
            continue
        html = r.text or ""
        # Dutchie has two id formats:
        #   - classic 24-char hex MongoDB ObjectId  (older stores)
        #   - 16-32 char nanoid alnum id            (newer stores)
        _ID = r"[A-Za-z0-9_-]{16,32}"
        patterns = [
            r'"dispensaryId"\s*:\s*"(' + _ID + r')"',
            r'"dispensary_id"\s*:\s*"(' + _ID + r')"',
            r'"cName"\s*:\s*"' + re.escape(slug) + r'"[^}]{0,600}"_id"\s*:\s*"(' + _ID + r')"',
            r'"_id"\s*:\s*"(' + _ID + r')"[^}]{0,600}"cName"\s*:\s*"' + re.escape(slug) + r'"',
            r'dispensary\?[^"]*id=(' + _ID + r')',
        ]
        for pat in patterns:
            m = re.search(pat, html)
            if m:
                return m.group(1), tried
        tried.append(f"{u.rsplit('/', 2)[-1]}=noID(len={len(html)})")
    return None, tried


# === GRAPHQL ================================================================

def build_headers(slug: str) -> dict:
    return {
        "Accept": "*/*",
        "Content-Type": "application/json",
        "User-Agent": _CHROME_UA,
        "Origin": "https://dutchie.com",
        "Referer": f"https://dutchie.com/embedded-menu/{slug}",
        "x-apollo-operation-name": OPERATION_NAME,
        "apollo-require-preflight": "true",
        "sec-ch-ua": _SEC_CH_UA,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }


def _build_url(endpoint: str, store: dict, pricing: str, page: int) -> str:
    variables = {
        "includeEnterpriseSpecials": False,
        "productsFilter": {
            "dispensaryId": store["id"],
            "pricingType": pricing,
            "strainTypes": [],
            "subcategories": [],
            "Status": "Active",
            # Sweep every category in one pass. Filtering by `types` against
            # a hand-maintained list is how whole categories used to vanish
            # without an error when Dutchie's enum drifted.
            "types": [],
            "useCache": False,
            "isDefaultSort": True,
            "sortBy": "popularSortIdx",
            "sortDirection": 1,
            "bypassOnlineThresholds": False,
            "isKioskMenu": False,
            "removeProductsBelowOptionThresholds": True,
            "platformType": "ONLINE_MENU",
            "preOrderType": None,
        },
        "page": page,
        "perPage": PER_PAGE,
    }
    extensions = {
        "persistedQuery": {"version": 1, "sha256Hash": PERSISTED_QUERY_SHA},
    }
    params = {
        "operationName": OPERATION_NAME,
        "variables": json.dumps(variables, separators=(",", ":")),
        "extensions": json.dumps(extensions, separators=(",", ":")),
    }
    return f"{endpoint}?{urllib.parse.urlencode(params)}"


def graphql_get(endpoint: str, store: dict, pricing: str, page: int) -> dict:
    url = _build_url(endpoint, store, pricing, page)
    last_err: Exception | None = None
    for attempt in range(RETRY_ON_FAIL + 1):
        r = _http_get(url, build_headers(store["slug"]))
        if r is None:
            last_err = RuntimeError("no response")
            time.sleep(1.0)
            continue
        try:
            r.raise_for_status()
            data = r.json()
            if "errors" in data and data["errors"]:
                raise RuntimeError(f"GraphQL errors: {data['errors']}")
            return data
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < RETRY_ON_FAIL:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(
        f"Dutchie query failed (store={store['slug']}, pricing={pricing}, "
        f"page={page}, endpoint={endpoint}): {last_err}"
    )


def _filtered(data: dict) -> dict:
    return (data.get("data") or {}).get("filteredProducts") or {}


def _total_of(fp: dict) -> int:
    """Dutchie reports the count as either `total` or `queryInfo.totalCount`
    depending on schema version. The old code only read `total`, so it fell
    back to paging-until-empty silently."""
    for v in (fp.get("total"), (fp.get("queryInfo") or {}).get("totalCount")):
        try:
            if v is not None:
                return int(v)
        except (TypeError, ValueError):
            continue
    return 0


def probe_store(store: dict):
    """
    Find the (endpoint, pricingType) pair that actually returns products for
    this store. Returns (endpoint, pricing, first_page_data, attempts).

    Trying rather than assuming is the whole point: a hard-coded
    pricingType of "rec" against a medical-only PA dispensary returns an
    empty list with a 200 and no GraphQL error.
    """
    forced = os.environ.get("LIBERTY_PRICING_TYPE")
    pricings = (forced,) if forced else PRICING_TYPES
    attempts = []
    for endpoint in GRAPHQL_ENDPOINTS:
        for pricing in pricings:
            try:
                data = graphql_get(endpoint, store, pricing, 0)
            except Exception as e:
                attempts.append({
                    "endpoint": endpoint, "pricing": pricing,
                    "result": "error", "detail": str(e)[:300],
                })
                continue
            fp = _filtered(data)
            n = len(fp.get("products") or [])
            attempts.append({
                "endpoint": endpoint, "pricing": pricing,
                "result": "ok", "products": n, "total": _total_of(fp),
            })
            if n:
                logger.info(
                    f"  probe OK: pricing={pricing} "
                    f"endpoint={endpoint.rsplit('/', 2)[-2]} "
                    f"page0={n} total={_total_of(fp)}"
                )
                return endpoint, pricing, data, attempts
            logger.info(
                f"  probe empty: pricing={pricing} "
                f"endpoint={endpoint.rsplit('/', 2)[-2]} -> 0 products"
            )
    return None, None, None, attempts


# === PARSING ================================================================

_STRAIN_MAP = {
    "hybrid": "Hybrid", "sativa": "Sativa", "indica": "Indica",
    "indicadominant": "Indica Hybrid", "sativadominant": "Sativa Hybrid",
    "cbd": "High CBD", "highcbd": "High CBD",
    "1:1": "1:1", "1to1": "1:1",
}


def _format_content(content) -> str:
    if not isinstance(content, dict):
        return ""
    rng = content.get("range")
    unit = (content.get("unit") or "").upper()
    if not isinstance(rng, list) or not rng:
        return ""
    vals = [v for v in rng if v not in (None, 0, 0.0)]
    if not vals:
        return ""
    suffix = "%" if unit == "PERCENTAGE" else ("mg" if unit == "MILLIGRAMS" else "")
    if len(set(vals)) == 1:
        return f"{vals[0]:g}{suffix}"
    return f"{vals[0]:g}-{vals[-1]:g}{suffix}"


def resolve_potency(product: dict, key: str) -> str:
    key = key.lower()
    if key == "thc":
        out = _format_content(product.get("THCContent"))
        if out:
            return out
        v = product.get("THC")
        if isinstance(v, (int, float)) and v:
            return f"{v:g}%"
    elif key == "cbd":
        out = _format_content(product.get("CBDContent"))
        if out:
            return out
        v = product.get("CBD")
        if isinstance(v, (int, float)) and v:
            return f"{v:g}%"
    return ""


def resolve_strain_type(product: dict) -> str:
    raw = product.get("strainType") or ""
    if not isinstance(raw, str):
        return ""
    key = raw.replace(" ", "").lower()
    if not key or key in {"none", "n/a", "notapplicable"}:
        return ""
    return _STRAIN_MAP.get(key, raw)


def resolve_brand(product: dict) -> str:
    brand = product.get("brand")
    if isinstance(brand, dict):
        return (brand.get("name") or "").strip()
    bn = product.get("brandName")
    if isinstance(bn, str):
        return bn.strip()
    if isinstance(brand, str):
        return brand.strip()
    return ""


def _pick_num(arr, idx):
    if not isinstance(arr, list) or idx >= len(arr):
        return None
    v = arr[idx]
    try:
        n = float(v)
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def card_price_and_deals(product: dict, idx: int):
    """Return (original_price, deals).

    Mirrors the Earth's Healing filter logic (Dutchie API):
      - original_price = strike-through / regular list price. Only falls
        back to the sale price when the regular fields are empty.
      - deals: only promos that ACTUALLY applied to THIS variant:
          (1) bogoSpecials -- kept when specialId is listed in the option's
              specialIdsPerOption.rewardIds/conditionIds/specialIds.
          (2) saleSpecials -- must list this option in eligibleProductOptions
              AND compute back to the current sale price (using percent /
              dollar / target-price discount type).
        Display label uses menuDisplayName > displayName > title >
        specialName.
    """
    rec = _pick_num(product.get("recPrices"), idx)
    base = _pick_num(product.get("Prices"), idx)
    regular = rec if rec is not None else base

    rec_special = _pick_num(product.get("recSpecialPrices"), idx)
    special = _pick_num(product.get("specialPrices"), idx)
    discounted = next((p for p in (rec_special, special) if p is not None), None)

    price = regular if regular is not None else discounted

    options = product.get("Options") or []
    this_option = (options[idx] if idx < len(options) else "") or ""

    applicable_ids = set()
    for so in (product.get("specialIdsPerOption") or []):
        if not isinstance(so, dict):
            continue
        if so.get("option") == this_option or so.get("optionIdx") == idx:
            for k in ("rewardIds", "conditionIds", "specialIds",
                      "saleIds", "bogoIds"):
                for sid in (so.get(k) or []):
                    if sid:
                        applicable_ids.add(str(sid))

    def _display_name(entry: dict) -> str:
        return (
            entry.get("menuDisplayName")
            or entry.get("displayName")
            or entry.get("title")
            or entry.get("specialName")
            or ""
        ).strip()

    def _entry_sid(entry: dict) -> str:
        return str(entry.get("specialId") or entry.get("_id") or entry.get("id") or "")

    deals: list = []
    seen: set = set()
    sd = product.get("specialData") or {}
    if isinstance(sd, dict) and discounted is not None:
        for entry in (sd.get("bogoSpecials") or []):
            if not isinstance(entry, dict):
                continue
            sid = _entry_sid(entry)
            if applicable_ids and sid and sid not in applicable_ids:
                continue
            name = _display_name(entry)
            if name and name not in seen:
                seen.add(name)
                deals.append(name)

        for entry in (sd.get("saleSpecials") or []):
            if not isinstance(entry, dict):
                continue
            eligible = entry.get("eligibleProductOptions")
            if isinstance(eligible, list) and eligible:
                if this_option and this_option not in eligible:
                    continue
            disc = entry.get("discount")
            if isinstance(disc, (int, float)) and disc > 0 and regular is not None:
                if entry.get("percentDiscount"):
                    expected = float(regular) * (1.0 - float(disc) / 100.0)
                elif entry.get("dollarDiscount"):
                    expected = float(regular) - float(disc)
                else:
                    expected = float(disc)
                if abs(expected - float(discounted)) > 0.05:
                    continue
            name = _display_name(entry)
            if name and name not in seen:
                seen.add(name)
                deals.append(name)

    return (f"{price:g}" if price is not None else ""), deals


# === STATE =================================================================

products: list[dict] = []
seen_keys: set = set()
all_json: list[dict] = []
store_failures: list[dict] = []
probe_log: list[dict] = []


def handle_products(fp, store):
    queue = (fp or {}).get("products") or []
    for product in queue:
        if not isinstance(product, dict):
            continue
        options = product.get("Options") or []
        if not options:
            options = [""]
        category = (product.get("type") or "").strip()
        sub = (product.get("subcategory") or "").strip() if product.get("subcategory") else ""
        subcategory = sub.upper().replace(" ", "-") if sub else (
            (product.get("type") or "DEFAULT").upper().replace(" ", "-")
        )
        strain = resolve_strain_type(product)
        brand = resolve_brand(product)
        thc = resolve_potency(product, "thc")
        cbd = resolve_potency(product, "cbd")
        name = (product.get("Name") or product.get("name") or "").strip()
        pid = product.get("_id") or product.get("id") or product.get("enterpriseProductId") or name

        for idx, opt in enumerate(options):
            weight = (opt or "").strip()
            price, deals = card_price_and_deals(product, idx)
            key = (store["id"], pid, weight)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            row = {
                "date": today,
                "location": location,
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
            for i, d in enumerate(deals, 1):
                row[f"deal_{i}"] = d
            products.append(row)


def paginate(store, endpoint, pricing, first_page_data):
    """Page from 0 (reusing the probe's page-0 response) until exhausted."""
    total = None
    collected = 0
    for page in range(MAX_PAGES):
        data = first_page_data if page == 0 else graphql_get(endpoint, store, pricing, page)
        fp = _filtered(data)
        if total is None:
            total = _total_of(fp)
        q = fp.get("products") or []
        all_json.append({
            "store": store["dispensary_location"],
            "pricing": pricing,
            "endpoint": endpoint,
            "page": page,
            "data": {"filteredProducts": {"total": total, "products": q}},
        })
        handle_products(fp, store)
        collected += len(q)
        if not q or (total and collected >= total):
            break
        time.sleep(REQUEST_DELAY_S)
    return collected


# === MAIN ===================================================================

logger.info(
    f"Starting Liberty PA scraper ({len(stores)} stores) | "
    f"backend={_HTTP_BACKEND} | pricing order={PRICING_TYPES}"
)
if _HTTP_BACKEND != "curl_cffi":
    logger.warning(
        "curl_cffi is NOT installed -- Dutchie will almost certainly return "
        "HTTP 403 for every request. Add `curl_cffi` to requirements.txt and "
        "rebuild the image."
    )
else:
    logger.info(f"impersonate={IMPERSONATE_TARGET}")

t0 = time.monotonic()

# Prime the shared HTTP session with Cloudflare cookies.
_cf_warmup()

for store in stores:
    slug = store["slug"]
    disp = store["dispensary_location"]
    logger.info(f"--- {disp} (slug={slug}) ---")
    # Small human-ish pause between stores to avoid tripping Cloudflare.
    time.sleep(0.8)

    disp_id, tried = resolve_dispensary_id(slug)
    if not disp_id:
        logger.error(f"  no dispensaryId found. Tried: {tried}")
        store_failures.append({
            "store": disp, "stage": "id_resolution", "detail": tried,
        })
        continue
    store["id"] = disp_id
    logger.info(f"  dispensaryId={disp_id}")

    endpoint, pricing, first_page, attempts = probe_store(store)
    probe_log.append({"store": disp, "attempts": attempts})
    if not endpoint:
        logger.error(
            f"  no (endpoint, pricingType) combination returned products. "
            f"Attempts: {attempts}"
        )
        store_failures.append({
            "store": disp, "stage": "probe", "detail": attempts,
        })
        continue

    before = len(products)
    try:
        paginate(store, endpoint, pricing, first_page)
    except Exception as e:
        logger.error(f"  pagination FAILED: {e}")
        store_failures.append({"store": disp, "stage": "paginate", "detail": str(e)})
    logger.info(f"=> {disp}: +{len(products) - before} rows (pricing={pricing})")

logger.info(f"Total scrape time: {time.monotonic() - t0:.1f}s")
if store_failures:
    logger.warning(
        f"{len(store_failures)}/{len(stores)} stores failed: "
        + ", ".join(f["store"] for f in store_failures)
    )


# === SAVE JSON ==============================================================
# Written before validation so a failed run still leaves the probe log
# behind -- `probe_log` tells you at a glance whether every combination
# came back empty (wrong pricingType / dead endpoint) or errored (403).

try:
    with open(json_file_path, "w", encoding="utf-8") as f:
        json.dump({
            "responses": all_json,
            "probe_log": probe_log,
            "store_failures": store_failures,
            "backend": _HTTP_BACKEND,
            "impersonate": IMPERSONATE_TARGET,
            "proxy": bool(_resolve_proxy()),
        }, f, indent=2, default=str)
    logger.info(f"JSON saved: {json_file_path}")
except Exception as e:
    logger.critical(f"JSON save failed: {e}", exc_info=True)
    sys.exit(1)


# === SAVE CSV ===============================================================

try:
    df = pd.DataFrame(products).drop_duplicates(
        subset=["dispensary_location", "name", "weight"]
    )
    if df.empty or len(df) < MIN_VALID_ROWS:
        logger.critical(
            f"Invalid CSV output. Rows: {len(df)} (need >= {MIN_VALID_ROWS})."
        )
        # Turn the probe log into an actual diagnosis rather than a row count.
        all_attempts = [a for entry in probe_log for a in entry["attempts"]]
        errored = [a for a in all_attempts if a["result"] == "error"]
        empty = [a for a in all_attempts if a["result"] == "ok"]
        if not probe_log:
            logger.critical(
                "DIAGNOSIS: never reached the probe stage -- dispensaryId "
                "resolution failed for every store. Check store_failures in "
                f"{json_file_path}; if those are HTTP 403, it's the TLS/IP block."
            )
        elif errored and not empty:
            if not _resolve_proxy():
                logger.critical(
                    "DIAGNOSIS: every GraphQL probe errored. If those are 403s, "
                    "this egress IP is blocked -- Dutchie refuses VPN and "
                    "datacenter ranges. This is NOT a curl_cffi problem: stdlib "
                    "requests succeeds from a residential IP. FIX: set "
                    "LIBERTY_PROXY to a RESIDENTIAL proxy, or run locally with "
                    "the VPN OFF."
                )
            else:
                logger.critical(
                    "DIAGNOSIS: probes errored even WITH a proxy -- that proxy's "
                    "exit is itself blocked (is it residential, or datacenter?), "
                    "or the persisted-query hash has rotated. Re-capture "
                    "PERSISTED_QUERY_SHA from a live browser network log."
                )
        elif empty:
            logger.critical(
                "DIAGNOSIS: the API answered 200 but returned 0 products for "
                f"every pricingType tried ({PRICING_TYPES}) on every endpoint. "
                "That means the dispensaryIds resolve but the menu filter "
                "matches nothing -- check whether the resolved ids are stale, "
                f"or whether Dutchie changed the filter shape. See probe_log in "
                f"{json_file_path}."
            )
        sys.exit(1)

    fixed_cols = [
        "date", "location", "company", "dispensary_location", "name",
        "category", "subcategory", "brand",
        "strain_type", "THC", "CBD", "weight", "original_price",
    ]
    deal_cols = sorted(
        [c for c in df.columns if c.startswith("deal_")],
        key=lambda x: int(x.split("_")[1]),
    )
    df = df.reindex(columns=fixed_cols + deal_cols)

    df.to_csv(output_file, index=False, na_rep="")
    per_store = df.groupby("dispensary_location").size().to_dict()
    cat_counts = df.groupby("category").size().to_dict()
    logger.info(f"CSV saved: {output_file} | Rows: {len(df)}")
    logger.info(f"Rows per dispensary: {per_store}")
    logger.info(f"Rows per category: {cat_counts}")

    top_brands = df["brand"].value_counts().head(20).to_dict()
    logger.info("Top brands:")
    for b, n in top_brands.items():
        logger.info(f"    {n:4d}  {b}")
except SystemExit:
    raise
except Exception as e:
    logger.critical(f"CSV save failed: {e}", exc_info=True)
    sys.exit(1)

if store_failures:
    logger.warning(
        f"⚠️ {company} PA scraper completed PARTIALLY -- "
        f"{len(stores) - len(store_failures)}/{len(stores)} stores collected"
    )
else:
    logger.info(f"{company} PA fast scraper completed")
sys.exit(0)