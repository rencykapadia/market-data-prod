"""
Apothecarium (TerrAscend) Pennsylvania scraper.

Apothecarium's storefront runs on Sweed (the same POS platform as
ZenLeaf), but instead of Sweed's REST product API each store's menu is
server-side rendered as HTML at:

    https://shop.apothecarium.com/<store-slug>/menu?page=<N>

The catalog is fully in the HTML (product name, brand, strain, THC/THCA,
terpenes, weight, sale/original price, discount badge), so we just fetch
each page and parse product cards until we run out.

Same output schema as the ZenLeaf / Naature scrapers:
    date, location, company, dispensary_location, name, category,
    subcategory, brand, strain_type, THC, CBD, weight, original_price,
    deal_1 ... deal_N

Pass extra Apothecarium PA stores by appending to STORES with the store's
url_slug (the piece between `shop.apothecarium.com/` and `/menu`).

NOTE on the 2026-09-08 failure mode: every store warmed up with HTTP 200
but page 1 parsed 0 cards, and `paginate` treated that as "end of
catalog" and stopped silently. That is NOT an IP block -- a 200 with zero
matching anchors means either the markup changed or we're being served an
age-gate / JS-shell page. This version captures a diagnostic snapshot for
exactly that case instead of failing mute at the CSV guard.
"""

import pandas as pd
import json
import os
import re
import time
from datetime import datetime
import pytz
import logging
import sys

from bs4 import BeautifulSoup

# =========================
# HTTP BACKEND
# =========================
# curl_cffi mimics Chrome's TLS handshake; Sweed's edge rate-limits and
# 403s plain `requests` from cloud IPs. Falls back to stdlib requests if
# not installed.
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
# The impersonation target and the User-Agent / sec-ch-ua headers MUST
# report the same Chrome major version. A Chrome-120 TLS handshake with a
# Chrome-148 UA is itself a detection signal.
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

if _HTTP_BACKEND != "curl_cffi":
    logger.warning(
        "curl_cffi is NOT installed -- falling back to stdlib requests. "
        "Add `curl_cffi` to requirements.txt and rebuild the image."
    )
else:
    logger.info(f"HTTP backend=curl_cffi impersonate={IMPERSONATE_TARGET}")

# =========================
# CONFIG
# =========================
company = "Apothecarium"
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

BASE_URL = "https://shop.apothecarium.com"
REQUEST_DELAY_S = 0.6
RETRY_ON_FAIL = 2
HTTP_TIMEOUT = 30
MAX_PAGES = 60          # safety cap; Thorndale sits at ~34 pages today
DEFAULT_PAGE_SIZE = 24  # Apothecarium pages 24 cards each
MIN_VALID_ROWS = 5

# How much of an unparseable page to keep in the JSON for offline triage.
DIAG_HTML_CHARS = 4000

# One entry per PA store. `url_slug` is the piece between
# `shop.apothecarium.com/` and `/menu` in the store's URL.
STORES = [
    {"id": "thorndale",   "url_slug": "thorndale",   "dispensary_location": "Thorndale"},
    {"id": "lancaster",   "url_slug": "lancaster",   "dispensary_location": "Lancaster"},
    {"id": "plymouth-mtg","url_slug": "pm",          "dispensary_location": "Plymouth Meeting"},
    {"id": "stroudsburg", "url_slug": "stroudsburg", "dispensary_location": "Stroudsburg"},
    {"id": "allentown",   "url_slug": "allentown",   "dispensary_location": "Allentown"},
    {"id": "bethlehem",   "url_slug": "bethlehem",   "dispensary_location": "Bethlehem"},
]


# =========================
# HTTP SESSION
# =========================
_CHROME_UA = (
    f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    f"(KHTML, like Gecko) Chrome/{_CHROME_MAJOR}.0.0.0 Safari/537.36"
)
_SEC_CH_UA = (
    f'"Chromium";v="{_CHROME_MAJOR}", "Not)A;Brand";v="8", '
    f'"Google Chrome";v="{_CHROME_MAJOR}"'
)

HEADERS = {
    "User-Agent": _CHROME_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "sec-ch-ua": _SEC_CH_UA,
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-site": "none",
    "sec-fetch-mode": "navigate",
    "sec-fetch-dest": "document",
    "upgrade-insecure-requests": "1",
}

WARMUP_HEADERS = {
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
    """APOTHECARIUM_PROXY > HTTPS_PROXY > HTTP_PROXY."""
    url = (
        os.environ.get("APOTHECARIUM_PROXY")
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


def warmup(session, store):
    """
    Visit the store's landing page BEFORE hitting the menu, so Sweed's
    edge sets its bot-check cookies. Without this we tend to get 403 on
    the first /menu request from a fresh session.

    On 2026-09-08 this returned 200 with cookies=0 for every store, which
    means the warm-up is not achieving its purpose -- log enough to tell.
    """
    proxies = _resolve_proxy()
    proxy_note = "via proxy" if proxies else "DIRECT (no proxy)"
    try:
        r = session.get(
            f"{BASE_URL}/{store['url_slug']}",
            headers=WARMUP_HEADERS, timeout=HTTP_TIMEOUT,
        )
        set_cookie = r.headers.get("set-cookie") or r.headers.get("Set-Cookie") or ""
        logger.info(
            f"warm-up {store['dispensary_location']}: status={r.status_code} "
            f"cookies={len(session.cookies)} set-cookie={'yes' if set_cookie else 'no'} "
            f"bytes={len(r.text or '')} backend={_HTTP_BACKEND} "
            f"impersonate={IMPERSONATE_TARGET or 'n/a'} {proxy_note}"
        )
        if r.status_code == 403 and not proxies:
            logger.warning(
                "warm-up returned 403 with no proxy configured. "
                "Apothecarium's Sweed edge is blocking this server's IP. "
                "Set APOTHECARIUM_PROXY (or HTTPS_PROXY) env var to a "
                "US residential proxy URL, e.g. http://user:pass@host:port"
            )
        if r.status_code == 200 and not set_cookie:
            logger.warning(
                f"warm-up {store['dispensary_location']}: HTTP 200 but the edge "
                "set NO cookies -- the age-gate/bot-check handshake did not "
                "happen. Menu pages may render empty."
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
all_html_summaries = []
empty_page_diagnostics = []
store_failures = []


# =========================
# HELPERS
# =========================
_STRAIN_MAP = {
    "hybrid": "Hybrid",
    "sativa": "Sativa",
    "indica": "Indica",
    "sativa dominant": "Sativa-Hybrid",
    "indica dominant": "Indica-Hybrid",
    "hybrid dominant": "Hybrid",
    "cbd": "High CBD",
    "high cbd": "High CBD",
}


def resolve_strain_type(raw):
    if not raw:
        return ""
    key = raw.strip().lower()
    return _STRAIN_MAP.get(key, raw.strip().title())


def _num(x):
    try:
        return f"{float(x):g}"
    except (TypeError, ValueError):
        return str(x)


_THC_RE = re.compile(r"THC\s*:?\s*([\d.]+(?:\s*-\s*[\d.]+)?)\s*(%|mg)?", re.IGNORECASE)
_THCA_RE = re.compile(r"THCA\s*:?\s*([\d.]+(?:\s*-\s*[\d.]+)?)\s*(%|mg)?", re.IGNORECASE)
_CBD_RE = re.compile(r"CBD\s*:?\s*([\d.]+(?:\s*-\s*[\d.]+)?)\s*(%|mg)?", re.IGNORECASE)
_PRICE_RE = re.compile(r"\$([\d.,]+)")
_PCT_OFF_RE = re.compile(r"(\d+)%\s*OFF", re.IGNORECASE)


def _clean(s):
    return re.sub(r"\s+", " ", (s or "").strip())


def _format_potency(match):
    if not match:
        return ""
    val = match.group(1).replace(" ", "")
    unit_raw = (match.group(2) or "").strip().lower()
    unit = "mg" if unit_raw == "mg" else "%"
    return f"{val} {unit}".strip()


def parse_card(card):
    """
    Extract a row from a single Apothecarium product-card <a>.

    The card renders roughly as:
        <a href="/thorndale/menu/<cat>-<catId>/<strain>-<slug>-<pack>-<pid>?...">
          <text>NameBrandSaleStrainCategory by BrandNameTHCA:xxTERP:xxWeightDeal$sale$origAdd to Cart</text>
        </a>

    Category slug (e.g. "cartridges-255") is in the href; strain and brand
    are the first three text lines, weight/prices/discount come next.
    """
    href = card.get("href") or ""
    if "/menu/" not in href:
        return None

    # Parse URL parts for stable metadata
    m_url = re.search(r"/([^/?]+)/menu/([^/?]+)/([^/?]+?)(?:\?|$)", href)
    if not m_url:
        return None
    store_from_url = m_url.group(1)
    category_slug = m_url.group(2)         # e.g. "flower-257" or "cartridges-255"
    product_slug = m_url.group(3)          # e.g. "hybrid-ic-banger-3.5g-398649"

    # Category comes from the slug prefix ("flower-257" -> "flower")
    category = category_slug.rsplit("-", 1)[0]

    # Numeric SKU/product id is the trailing chunk of the product slug
    pid_match = re.search(r"-(\d+)$", product_slug)
    product_id = pid_match.group(1) if pid_match else product_slug

    text = _clean(card.get_text(" ", strip=True))
    if not text:
        return None

    # Deal badge (top-of-card). Apothecarium prints these before the name
    # ("SALE Hybrid" or "$25", "45% OFF", "Deals from $X").
    deal_names = set()
    if re.search(r"\bsale\b", text, re.IGNORECASE):
        deal_names.add("SALE")

    # Pull the strain prevalence out of the visible text by walking the
    # card's small elements -- Apothecarium places the strain label in a
    # dedicated element.
    strain_raw = ""
    for tag in card.find_all(["div", "span", "p"]):
        t = _clean(tag.get_text(" ", strip=True))
        if t in ("Hybrid", "Indica", "Sativa", "Indica Dominant", "Sativa Dominant",
                 "Indica-Hybrid", "Sativa-Hybrid", "Hybrid Dominant", "Hybrid-Dominant"):
            strain_raw = t
            break
    strain_type = resolve_strain_type(strain_raw)

    # Extract cannabinoid values
    thc_match = _THCA_RE.search(text) or _THC_RE.search(text)
    cbd_match = _CBD_RE.search(text)
    thc = _format_potency(thc_match)
    cbd = _format_potency(cbd_match)

    # Weight extraction. Try richest patterns first so we don't misread
    # a cannabinoid value ("THC: 500MG") as the pack weight.
    #   1) Pack form: "50mg/10pk", "0.5g/7pk"
    #   2) Standalone total: "3.5g", "28g", "1g", "0.5g"
    #   3) mg total that is NOT immediately preceded by "THC" / "CBD"
    weight = ""
    m = re.search(r"\b(\d+(?:\.\d+)?(?:mg|g|oz|kg|ml)/\d+pk)\b", text, re.IGNORECASE)
    if not m:
        m = re.search(r"\b(\d+(?:\.\d+)?(?:g|oz|kg|ml))\b", text, re.IGNORECASE)
    if not m:
        for m2 in re.finditer(r"\b(\d+(?:\.\d+)?mg)\b", text, re.IGNORECASE):
            preceding = text[max(0, m2.start() - 12):m2.start()].upper()
            if "THC" in preceding or "CBD" in preceding:
                continue
            m = m2
            break
    if m:
        weight = m.group(1)

    # Prices: first $ is the sale/current price, second is the strikethrough
    prices = [float(p.replace(",", "")) for p in _PRICE_RE.findall(text)]
    sale_price = prices[0] if prices else None
    original_price = prices[1] if len(prices) > 1 else sale_price

    pct_match = _PCT_OFF_RE.search(text)
    if pct_match:
        deal_names.add(f"{pct_match.group(1)}% off")

    named_deal_match = re.search(r"\b(\$\d+(?:\.\d+)?)\s+(?:Deals|Cart|Add)", text)
    if named_deal_match:
        deal_names.add(named_deal_match.group(1))

    if sale_price and original_price and original_price > sale_price:
        savings = original_price - sale_price
        pct = round(savings / original_price * 100)
        deal_names.add(f"${savings:.2f} off ({pct}%)")

    # Product NAME + BRAND: the rendered layout mashes them together, so
    # pull them from the "<Category> by <Brand>" fragment plus the tag with
    # the largest text weight; fall back to the URL slug.
    by_match = re.search(r"([A-Za-z0-9 &'\-().]+?)\s+by\s+([A-Za-z0-9 &'\-().]+?)(?:\s+[A-Z]{2,}|\s*\d)", text)
    brand = _clean(by_match.group(2)) if by_match else ""
    name_candidates = []
    for tag in card.find_all(["h2", "h3", "h4", "h5", "h6", "p", "strong"]):
        t = _clean(tag.get_text(" ", strip=True))
        if not t or len(t) > 120:
            continue
        if re.match(r"^(THC|CBD|TERP|Sale|SALE|Hybrid|Indica|Sativa|\$|\d)", t):
            continue
        if brand and t.lower() == brand.lower():
            continue
        if "by " in t.lower():
            continue
        name_candidates.append(t)
    name = name_candidates[0] if name_candidates else product_slug

    return {
        "store_from_url": store_from_url,
        "category_slug": category_slug,
        "product_id": product_id,
        "name": name,
        "brand": brand,
        "strain_type": strain_type,
        "thc": thc,
        "cbd": cbd,
        "weight": weight,
        "sale_price": sale_price,
        "original_price": original_price,
        "category": category,
        "deals": sorted(deal_names, key=lambda x: (x.startswith("$"), x)),
    }


def resolve_subcategory(category):
    return (category or "").strip().upper().replace(" ", "-") or "DEFAULT"


# =========================
# FETCHER
# =========================
def fetch_menu_page(session, store, page):
    url = f"{BASE_URL}/{store['url_slug']}/menu"
    params = {"page": page} if page > 1 else {}
    # Referer must look intra-site or Sweed treats us as a bot.
    headers = dict(HEADERS)
    headers["Referer"] = f"{BASE_URL}/{store['url_slug']}"
    headers["sec-fetch-site"] = "same-origin"
    last_err = None
    for attempt in range(RETRY_ON_FAIL + 1):
        try:
            r = session.get(url, params=params, headers=headers, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            return r
        except Exception as e:
            last_err = e
            status = getattr(getattr(e, "response", None), "status_code", None)
            if (status == 403 or "403" in str(e)) and attempt < RETRY_ON_FAIL:
                logger.warning(f"  page {page}: got 403, re-warming session")
                try:
                    warmup(session, store)
                except Exception:
                    pass
            if attempt < RETRY_ON_FAIL:
                time.sleep(1.5 * (attempt + 1))

    hint = ""
    final_status = getattr(getattr(last_err, "response", None), "status_code", None)
    if final_status == 403 or "403" in str(last_err):
        if _HTTP_BACKEND != "curl_cffi":
            hint = (
                " | HINT: 403 on stdlib requests -- install curl_cffi before "
                "reaching for a proxy."
            )
        elif not _resolve_proxy():
            hint = (
                " | HINT: 403 with curl_cffi and no proxy = Sweed has "
                "blacklisted this server's IP. Set APOTHECARIUM_PROXY="
                "<residential-us-proxy-url> on the container env and redeploy."
            )
        else:
            hint = (
                " | HINT: 403 even WITH proxy -- the proxy IP itself may be "
                "burned. Rotate to a different residential session/IP."
            )
    raise RuntimeError(f"Fetch failed after retries: {last_err}{hint}")


def parse_menu_page(html, store):
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    # Substring match rather than a ^= prefix selector: Sweed serves the
    # product hrefs relative ("/thorndale/menu/...") today, but the same
    # markup rendered with absolute URLs ("https://shop.apothecarium.com
    # /thorndale/menu/...") would silently match nothing under ^=.
    # parse_card's own URL regex is host-agnostic, so both forms parse.
    needle = f'/{store["url_slug"]}/menu/'
    for a in soup.find_all("a", href=True):
        if needle not in a["href"]:
            continue
        parsed = parse_card(a)
        if not parsed:
            continue

        dedupe_key = (
            store["id"], parsed["product_id"], parsed["weight"], parsed["sale_price"],
        )
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)

        deal_columns = {f"deal_{i+1}": d for i, d in enumerate(parsed["deals"])}
        row = {
            "date": today,
            "location": state,
            "company": company,
            "dispensary_location": store["dispensary_location"],
            "name": parsed["name"],
            "category": parsed["category"],
            "subcategory": resolve_subcategory(parsed["category"]),
            "brand": parsed["brand"],
            "strain_type": parsed["strain_type"],
            "THC": parsed["thc"],
            "CBD": parsed["cbd"],
            "weight": parsed["weight"],
            "original_price": parsed["original_price"],
        }
        row.update(deal_columns)
        rows.append(row)
    return rows


_AGE_GATE_MARKERS = (
    "are you 21", "21 or older", "verify your age", "age verification",
    "date of birth", "must be 21", "age gate", "medical card",
)
_JS_SHELL_MARKERS = (
    "__next_data__", "window.__nuxt", "data-reactroot", "<noscript",
    "please enable javascript",
)


def diagnose_empty_page(html, store, page, resp=None):
    """
    A 200 response that yields zero cards is ambiguous. Record enough to
    tell the cases apart without re-running the scraper:

      * html_len tiny            -> interstitial / redirect shell
      * age_gate_markers present -> cookie/age-gate handshake needed
      * substantial HTML, ZERO   -> SOFT BOT-BLOCK. Sweed's edge answers 200
        "/menu/" hrefs anywhere,     with a normal-looking, normally-sized
        and no Set-Cookie            page that simply has the catalog
                                     stripped out. Verified 2026-09-09: the
                                     same URL fetched with an ordinary
                                     browser client returns 39 pages of
                                     products with hrefs in exactly the
                                     shape parse_card expects. So this is
                                     NOT a markup change -- it is the same
                                     TLS-fingerprint gate that gives AYR and
                                     RISE a 403, just failing quietly.
      * js_shell_markers present -> menu is client-rendered; needs a real
                                    browser or the Sweed JSON API
      * menu hrefs present but    -> genuine selector/markup drift
        none match our slug
    """
    html = html or ""
    lowered = html.lower()
    anchor_needle = f'/{store["url_slug"]}/menu/'
    set_cookie = ""
    if resp is not None:
        try:
            set_cookie = resp.headers.get("set-cookie") or resp.headers.get("Set-Cookie") or ""
        except Exception:
            set_cookie = ""
    title_m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)

    diag = {
        "store": store["dispensary_location"],
        "url_slug": store["url_slug"],
        "page": page,
        "html_len": len(html),
        "title": _clean(title_m.group(1))[:200] if title_m else "",
        "exact_anchor_hits": html.count(anchor_needle),
        "any_menu_href_hits": lowered.count("/menu/"),
        "set_cookie": bool(set_cookie),
        "backend": _HTTP_BACKEND,
        "age_gate_markers": [k for k in _AGE_GATE_MARKERS if k in lowered],
        "js_shell_markers": [k for k in _JS_SHELL_MARKERS if k in lowered],
        "html_head": html[:DIAG_HTML_CHARS],
    }

    if diag["html_len"] < 2000:
        verdict = "tiny response -- interstitial or redirect shell"
    elif diag["age_gate_markers"]:
        verdict = "age-gate detected -- needs cookie/age handshake"
    elif diag["any_menu_href_hits"] == 0 and diag["html_len"] > 20000:
        # A full-size page with not a single menu link anywhere.
        verdict = (
            "SOFT BOT-BLOCK -- edge served a full-size page with the catalog "
            "stripped out"
        )
        if _HTTP_BACKEND != "curl_cffi":
            verdict += " (running on stdlib TLS; install curl_cffi first)"
        elif not _resolve_proxy():
            verdict += " (curl_cffi active but no proxy; this IP may be flagged)"
        else:
            verdict += " (curl_cffi + proxy active; rotate the proxy IP)"
    elif diag["exact_anchor_hits"] == 0 and diag["any_menu_href_hits"] > 0:
        verdict = "menu links present but slug prefix changed -- fix the selector"
    elif diag["js_shell_markers"]:
        verdict = "client-rendered shell -- menu is no longer server-side rendered"
    else:
        verdict = "markup changed -- product anchors no longer match the selector"
    diag["verdict"] = verdict

    logger.error(
        f"  page {page}: 0 cards from HTTP 200. "
        f"html_len={diag['html_len']} title={diag['title']!r} "
        f"anchor_hits={diag['exact_anchor_hits']} "
        f"any_menu_href_hits={diag['any_menu_href_hits']} "
        f"set_cookie={diag['set_cookie']} "
        f"age_gate={diag['age_gate_markers'] or 'none'} "
        f"js_shell={diag['js_shell_markers'] or 'none'} -> {verdict}"
    )
    return diag


def paginate(session, store):
    """
    Page through the store's menu.

    Page 1 returning zero cards is a FAILURE, not the end of the catalog --
    that conflation is what let the 2026-09-08 run die silently. Later
    pages returning zero is a normal stop.
    """
    collected = 0
    page = 0
    for page in range(1, MAX_PAGES + 1):
        resp = fetch_menu_page(session, store, page)
        html = resp.text or ""

        rows = parse_menu_page(html, store)
        all_html_summaries.append({
            "store": store["dispensary_location"], "page": page,
            "html_len": len(html), "rows": len(rows),
        })

        if not rows:
            if page == 1:
                diag = diagnose_empty_page(html, store, page, resp)
                empty_page_diagnostics.append(diag)
                raise RuntimeError(
                    f"page 1 returned 0 product cards ({diag['verdict']})"
                )
            logger.info(f"  page {page}: 0 cards (end of catalog)")
            break

        products.extend(rows)
        collected += len(rows)
        logger.info(f"  page {page}: +{len(rows)} (store total {collected})")

        # A short page = last page
        if len(rows) < DEFAULT_PAGE_SIZE:
            break

        time.sleep(REQUEST_DELAY_S)

    if page >= MAX_PAGES:
        logger.warning(f"  hit MAX_PAGES safety cap ({MAX_PAGES})")
    return collected


# =========================
# MAIN LOOP
# =========================
logger.info(
    f"Starting Apothecarium scrape | stores={len(STORES)} | backend={_HTTP_BACKEND}"
)
session = make_session()

for store in STORES:
    logger.info(f"--- {store['dispensary_location']} ---")
    rows_before = len(products)
    try:
        warmup(session, store)
        paginate(session, store)
    except Exception as e:
        logger.error(f"  {store['dispensary_location']} FAILED: {e}")
        store_failures.append({"store": store["dispensary_location"], "error": str(e)})
        continue
    logger.info(f"=> {store['dispensary_location']}: {len(products) - rows_before} rows")

if store_failures:
    logger.warning(
        f"{len(store_failures)}/{len(STORES)} stores failed: "
        + ", ".join(f["store"] for f in store_failures)
    )


# =========================
# SAVE JSON
# =========================
# Always written, before validation, so a failed run still leaves the
# diagnostics behind. Inspect `empty_page_diagnostics[].verdict` and
# `html_head` when the CSV guard trips.
try:
    with open(json_file_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "summaries": all_html_summaries,
                "total_rows": len(products),
                "store_failures": store_failures,
                "empty_page_diagnostics": empty_page_diagnostics,
                "backend": _HTTP_BACKEND,
                "impersonate": IMPERSONATE_TARGET,
                "proxy": bool(_resolve_proxy()),
            },
            f, indent=2, default=str,
        )
    logger.info(f"JSON saved: {json_file_path}")
except Exception as e:
    logger.critical(f"❌ JSON save failed: {e}", exc_info=True)
    sys.exit(1)


# =========================
# SAVE CSV
# =========================
try:
    df = pd.DataFrame(products).drop_duplicates(
        subset=["dispensary_location", "name", "weight", "original_price"]
    )
    if df.empty or len(df) < MIN_VALID_ROWS:
        verdicts = sorted({d["verdict"] for d in empty_page_diagnostics})
        logger.critical(
            f"❌ Invalid CSV output. Rows: {len(df)} (need >= {MIN_VALID_ROWS}). "
            f"Diagnosis: {verdicts or 'no page-level diagnostics captured'}. "
            f"Full detail (including the first {DIAG_HTML_CHARS} chars of each "
            f"unparseable page) is in {json_file_path}"
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

if store_failures:
    logger.warning(
        f"⚠️ Apothecarium scraper completed PARTIALLY -- "
        f"{len(STORES) - len(store_failures)}/{len(STORES)} stores collected"
    )
else:
    logger.info("✅ Apothecarium scraper completed successfully")
sys.exit(0)