#!/usr/bin/env python3
"""
RISE / Green Thumb Industries (Pennsylvania) -- FAST scraper.

RISE runs on iHeart Jane's white-label storefront hosted at
risecannabis.com. Every store's menu URL is

    https://risecannabis.com/dispensaries/pennsylvania/<slug>/<store_id>/medical-menu/

and that single URL returns ~2.8MB of SSR HTML with every product already
rendered as a card (usually 250-450 products per store). No pagination, no
JS execution needed -- one HTTP GET per store gives you the full catalog.

Product data is anchored on data-testid attributes:
    product-confirm-section-<pid>   -- card container (URL contains slug)
    product-name-<pid>              -- "Cranberry Z"
    product-type-<pid>              -- "Mixed Buds" (subcategory)
    product-thc-<pid>               -- "Total THC 27.08%"
    product-gram-<pid>              -- "14g"
    product-price-<pid>             -- "$ 48.00"
    product-discount-price-<pid>    -- "$ 120.00"

Brand ("RYTHM"), strain ("Indica"), and deal badge ("60% OFF") are
plain text inside the card container.

    22 PA stores * ~2s discovery + ~2s menu fetch each = ~1-2 minutes total.

WHAT ACTUALLY BLOCKS THIS (measured 2026-09-08/09)
--------------------------------------------------
RISE blocks by IP REPUTATION, not by TLS fingerprint. The evidence:

    2026-09-08, Azure, curl_cffi MISSING (stdlib TLS)
        -> 403 on all 22 landing pages. Looked like a fingerprint problem.

    2026-09-09, Azure, curl_cffi PRESENT (impersonate=chrome136)
        -> 403 on all 22 landing pages, identically.

Impersonation changed nothing, so the handshake was never what was being
rejected -- the datacenter ASN is. This is the same failure mode as Dutchie
(AYR / Liberty), and the opposite of Apothecarium/Sweed, which IS
fingerprint-gated and IS fixed by curl_cffi.

CONSEQUENCES
    * Running this in Azure REQUIRES RISE_PROXY pointed at a RESIDENTIAL
      proxy. A datacenter proxy swaps one blocked ASN for another.
    * Do not spend another rebuild on impersonation targets for this site.
    * Unverified: whether an ordinary residential IP works here the way it
      does for Dutchie. probe_rise.py answers that in ~10 seconds.

403s are therefore NOT retried (see _get) and discovery gives up after a
few consecutive ones -- retrying an IP block only deepens it. The earlier
behaviour put 66 requests through a refusing edge over 100 seconds to
learn a fact the first response already carried.
"""
import json
import logging
import os
import re
import sys
import time
from collections import Counter
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
logger = logging.getLogger(__name__)


# === TLS IMPERSONATION ======================================================
# The impersonation target and the User-Agent / sec-ch-ua headers MUST report
# the same Chrome major version. The previous build paired a chrome124 TLS
# fingerprint with a Chrome/123 UA string.

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
        "risecannabis.com fingerprints the TLS handshake and will return 403 "
        "for every store. Add `curl_cffi` to requirements.txt and rebuild."
    )
else:
    logger.info(f"HTTP backend=curl_cffi impersonate={IMPERSONATE_TARGET}")


# === CONFIG =================================================================

company             = "RISE"
location            = "Pennsylvania"
PRICING_TYPE        = "rec"        # PA is medical-only today; label rec-ready
TIMEZONE            = "America/New_York"

HTTP_TIMEOUT        = 30
MENU_TIMEOUT        = 45
RETRY_ON_FAIL       = 2
MIN_VALID_ROWS      = 5

# 22 PA RISE stores from risecannabis.com/dispensary/pennsylvania/
# Store IDs get discovered at runtime from each store's landing page (the
# numeric segment in /<slug>/<id>/medical-menu/). Slugs are stable.
STORE_SLUGS = [
    "allentown",       # marked "Coming Soon" -- discovery will skip if no menu URL
    "carlisle",
    "chambersburg",
    "cranberry",
    "duncansville",
    "erie-lake",
    "erie-peach",
    "grove-city",
    "hanover",
    "hermitage",
    "king-of-prussia",
    "latrobe",
    "lebanon",
    "meadville",
    "mechanicsburg",
    "monroeville",
    "new-castle",
    "philadelphia",
    "steelton",
    "warminster",
    "west-mifflin",
    "york",
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
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
}


def _resolve_proxy():
    """
    RISE_PROXY > HTTPS_PROXY > HTTP_PROXY.

    This scraper previously had no proxy support at all, which meant that
    if curl_cffi alone wasn't enough (i.e. the edge blocks by IP range
    rather than by TLS fingerprint) there was no lever to pull without a
    code change. Format: http://user:pass@host:port
    """
    url = (
        os.environ.get("RISE_PROXY")
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


def _get(session, url, timeout=HTTP_TIMEOUT, retries=RETRY_ON_FAIL):
    """
    GET with backoff on TRANSIENT edge statuses.

    403 is deliberately NOT retried. RISE's 403 is IP-reputation based
    (confirmed: identical from Azure with and without curl_cffi), and an IP
    block does not clear because you asked again 1.5s later. Retrying it
    turned a 5-second failure into a 100-second one across 66 requests.
    429/502/503 are genuinely transient and still get backoff.
    """
    last_err = None
    for attempt in range(retries + 1):
        try:
            r = session.get(url, headers=HEADERS, timeout=timeout)
            if r.status_code == 403:
                return r
            if r.status_code in (429, 502, 503) and attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            return r
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"fetch failed: {last_err}")


# === HELPERS ================================================================

_TAG_RE = re.compile(r"<[^>]+>")
_ENTITY_MAP = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'",
               "nbsp": " ", "mdash": "—", "ndash": "–", "hellip": "…"}
_HTML_ENTITY_RE = re.compile(r"&(#\d+|#x[0-9a-fA-F]+|[a-zA-Z]+);")


def _decode_entity(m):
    e = m.group(1)
    if e.startswith("#x"):
        try:
            return chr(int(e[2:], 16))
        except ValueError:
            return m.group(0)
    if e.startswith("#"):
        try:
            return chr(int(e[1:]))
        except ValueError:
            return m.group(0)
    return _ENTITY_MAP.get(e, m.group(0))


def html_to_text(chunk):
    chunk = _TAG_RE.sub(" ", chunk)
    chunk = _HTML_ENTITY_RE.sub(_decode_entity, chunk)
    return re.sub(r"\s+", " ", chunk).strip()


_MENU_URL_RE = re.compile(
    r'href="(/dispensaries/pennsylvania/([a-z0-9-]+)/(\d+)/(medical|recreational)-menu/?)"',
    re.IGNORECASE,
)


def discover_store_id(session, slug):
    """Fetch the store's landing page and extract its numeric menu ID."""
    url = f"https://risecannabis.com/dispensaries/pennsylvania/{slug}/"
    try:
        r = _get(session, url, timeout=HTTP_TIMEOUT)
    except Exception as e:
        return None, None, f"landing fetch failed: {e}"
    if r.status_code >= 400:
        return None, None, f"landing HTTP {r.status_code}"

    # Prefer recreational-menu URL when both exist
    rec_ids = []
    med_ids = []
    for m in _MENU_URL_RE.finditer(r.text):
        path, mslug, sid, menu_type = m.groups()
        if mslug != slug:
            continue
        (rec_ids if menu_type.lower() == "recreational" else med_ids).append((sid, path))
    picked = rec_ids[0] if rec_ids else (med_ids[0] if med_ids else None)
    if not picked:
        return None, None, "no menu URL found (store may be Coming Soon)"
    return picked[0], picked[1], None


_PRODUCT_ID_RE = re.compile(r'data-testid="product-confirm-section-(\d+)"')
_STRAIN_RE = re.compile(r"\b(Indica|Sativa|Hybrid|CBD|High CBD|1:1|2:1)\b", re.I)
_STRAIN_MAP = {"indica": "Indica", "sativa": "Sativa", "hybrid": "Hybrid",
               "cbd": "High CBD", "high cbd": "High CBD",
               "1:1": "1:1", "2:1": "2:1"}
_DEAL_RE = re.compile(r"\b(\d+%\s*OFF|SALE|NEW)\b", re.I)
_THC_RE = re.compile(r"Total\s+THC\s*([\d.]+(?:\s*-\s*[\d.]+)?)\s*%", re.I)
_CBD_RE = re.compile(r"Total\s+CBD\s*([\d.]+(?:\s*-\s*[\d.]+)?)\s*%", re.I)
_WEIGHT_RE = re.compile(r"^([\d.]+)\s*(g|mg|oz|ml|pk|ct|each)\b", re.I)
_PRICE_RE = re.compile(r"\$\s*([\d.]+)")


def _extract_testid_text(html, testid):
    """Get the text content inside the element identified by `testid` by
    walking tag nesting from the opening element."""
    m = re.search(rf'<(\w+)[^>]*data-testid="{re.escape(testid)}"[^>]*>', html)
    if not m:
        return ""
    tag = m.group(1)
    depth = 1
    i = m.end()
    open_re = re.compile(rf"<{tag}\b", re.I)
    close_re = re.compile(rf"</{tag}\s*>", re.I)
    nc = None
    while i < len(html) and depth > 0:
        no = open_re.search(html, i)
        nc = close_re.search(html, i)
        if not nc:
            break
        if no and no.start() < nc.start():
            depth += 1
            i = no.end()
        else:
            depth -= 1
            i = nc.end()
    inner = html[m.end(): (nc.start() if nc else len(html))]
    return html_to_text(inner)


def _field(block, html, testid):
    """
    Look inside the card block first, fall back to the whole document.

    The old code scanned the full ~2.8MB page for each of six fields on
    every product -- ~6 x 300 x 2.8MB of regex per store. Card-scoped
    lookup handles the overwhelming majority; the full-document fallback
    keeps correctness when a field sits outside the sliced block.
    """
    if block:
        out = _extract_testid_text(block, testid)
        if out:
            return out
    return _extract_testid_text(html, testid)


_ANY_CARD_TESTID_RE = re.compile(
    r'data-testid="product-(?:name|confirm-section)-\d+"'
)
CARD_LOOKBACK = 4000


def _extract_card_block(html, pid):
    """
    Return the HTML block that contains all data for one product.

    The previous implementation anchored on `product-confirm-section-<pid>`
    and looked back only 200 chars, then ran FORWARD to the next card's
    confirm-section. On RISE markup the brand / strain / deal-badge text is
    rendered ABOVE the product name -- i.e. before the confirm-section --
    so that window started after this card's brand block and ended after
    the NEXT card's. Result: brand, strain_type and deals were read off the
    following product. Anchor on the card's own <a> element instead.
    """
    # Anchor on the earliest of this product's own testids.
    anchor = -1
    for prefix in ("product-name-", "product-confirm-section-"):
        idx = html.find(f'data-testid="{prefix}{pid}"')
        if idx != -1:
            anchor = idx if anchor == -1 else min(anchor, idx)
    if anchor == -1:
        return ""

    # Walk back to the opening <a> of this card.
    start = html.rfind("<a ", max(0, anchor - CARD_LOOKBACK), anchor)
    if start < 0:
        start = html.rfind("<div ", max(0, anchor - CARD_LOOKBACK), anchor)
    if start < 0:
        start = max(0, anchor - CARD_LOOKBACK)

    # Guard against overshooting into the previous card: if another
    # product's testid sits between `start` and our anchor, we went too far.
    last_foreign = None
    for m in _ANY_CARD_TESTID_RE.finditer(html[start:anchor]):
        last_foreign = m
    if last_foreign is not None:
        moved = html.rfind("<a ", start + last_foreign.end(), anchor)
        start = moved if moved >= 0 else start + last_foreign.end()

    # End at this card's closing </a> (anchors don't nest), but never run
    # past the next card's first testid.
    nxt = _ANY_CARD_TESTID_RE.search(html, anchor + 1)
    while nxt and f"-{pid}\"" in nxt.group(0):
        nxt = _ANY_CARD_TESTID_RE.search(html, nxt.end())
    limit = nxt.start() if nxt else len(html)

    close = html.find("</a>", anchor)
    if close == -1 or close > limit:
        end = limit
    else:
        end = close + len("</a>")
    return html[start:end]


def parse_store_html(html, disp_location, menu_type):
    rows = []
    pids = list(dict.fromkeys(_PRODUCT_ID_RE.findall(html)))  # unique, preserve order
    for pid in pids:
        block = _extract_card_block(html, pid)
        block_text = html_to_text(block)

        name = _field(block, html, f"product-name-{pid}")
        sub = _field(block, html, f"product-type-{pid}")
        thc_text = _field(block, html, f"product-thc-{pid}")
        gram = _field(block, html, f"product-gram-{pid}")
        price_text = _field(block, html, f"product-price-{pid}")
        disc_text = _field(block, html, f"product-discount-price-{pid}")

        # THC / CBD
        thc = ""
        cbd = ""
        tmt = _THC_RE.search(thc_text) or _THC_RE.search(block_text)
        if tmt:
            thc = tmt.group(1).strip() + "%"
        cmt = _CBD_RE.search(thc_text) or _CBD_RE.search(block_text)
        if cmt:
            cbd = cmt.group(1).strip() + "%"

        # Weight
        weight = ""
        wm = _WEIGHT_RE.match(gram.strip())
        if wm:
            weight = wm.group(0).strip()
        else:
            gm = re.search(r"\b([\d.]+)\s*(g|mg|oz|ml|pk|ct|each)\b", block_text, re.I)
            if gm:
                weight = gm.group(0).strip()

        # Prices. Prefer the dedicated price elements -- block_text can
        # bleed into the neighbouring card when the 4500-char slice
        # overshoots, and max() over that would inflate original_price.
        prices = []
        for src in (price_text, disc_text):
            for pm in _PRICE_RE.findall(src or ""):
                try:
                    prices.append(float(pm))
                except (TypeError, ValueError):
                    pass
        if not prices:
            for pm in _PRICE_RE.findall(block_text or ""):
                try:
                    prices.append(float(pm))
                except (TypeError, ValueError):
                    pass
        # When both a sale price and a strike-through are shown, keep the
        # HIGHER (strike-through / pre-discount) value as original_price.
        original_price = max(prices) if prices else None

        # Strain
        strain_type = ""
        sm = _STRAIN_RE.search(block_text)
        if sm:
            key = sm.group(1).lower()
            strain_type = _STRAIN_MAP.get(key, key.title())

        # Deals
        deals = []
        seen_d = set()
        for dm in _DEAL_RE.finditer(block_text):
            d = dm.group(0).strip()
            if d.upper() not in seen_d:
                seen_d.add(d.upper())
                deals.append(d.upper())

        # Brand: on RISE cards brand is a short ALL-CAPS or Title-Case token
        # rendered ABOVE the product name.
        brand = ""
        if name:
            idx = block_text.find(name)
            pre = block_text[:idx].strip()
            pre_no_prefix = re.sub(
                r"^(?:\s*(?:Indica|Sativa|Hybrid|CBD|High CBD|1:1|2:1))?\s*"
                r"(?:\d+%\s*OFF|SALE|NEW)?\s*", "", pre, flags=re.I
            ).strip()
            if pre_no_prefix:
                tokens = pre_no_prefix.split()
                brand = " ".join(tokens[-4:]) if len(tokens) >= 4 else " ".join(tokens)
                brand = re.sub(r"^\s*(Save|Rewards|Exclusive|Featured)\s*", "", brand, flags=re.I).strip()

        # Fallback: pull from URL: /product/<pid>/<brand-name-slug>/
        if not brand:
            urlm = re.search(rf"/product/{pid}/([a-z0-9-]+)", block)
            if urlm:
                slug_parts = urlm.group(1).split("-")
                brand = slug_parts[0].title() if slug_parts else ""

        rows.append({
            "date": today,
            "location": location,
            "company": company,
            "dispensary_location": disp_location,
            "name": name,
            "category": "",   # set by caller based on subcat
            "subcategory": (sub or "DEFAULT").upper().replace(" ", "-"),
            "brand": brand,
            "strain_type": strain_type,
            "THC": thc,
            "CBD": cbd,
            "weight": weight,
            "original_price": original_price,
            "_pid": pid,
            "_deals": deals,
        })
    return rows


# Category inferred from subcategory
_SUBCAT_TO_CATEGORY = [
    (re.compile(r"flower|bud|shake|preground|small\s*buds", re.I), "Flower"),
    (re.compile(r"pre[-\s]?roll|joint|blunt", re.I),                 "Pre-Roll"),
    (re.compile(r"vape|vaporizer|cart(ridge)?|pod|all[-\s]?in[-\s]?one", re.I), "Vaporizers"),
    (re.compile(r"concentrate|resin|rosin|shatter|wax|sauce|diamond|hash|sand|badder|budder|crumble", re.I), "Concentrate"),
    (re.compile(r"edible|gummy|gummies|chocolate|mint|lozenge|dark|milk|cookie|brownie|troche|caramel", re.I), "Edible"),
    (re.compile(r"tincture|drop", re.I),                              "Tincture"),
    (re.compile(r"topical|balm|lotion|salve|patch|transdermal", re.I),"Topical"),
    (re.compile(r"capsule|pill|tablet|softgel", re.I),                "Capsule"),
    (re.compile(r"beverage|drink|seltzer", re.I),                     "Beverages"),
    (re.compile(r"accessor|gear|merch|apparel|papers|grinder|battery", re.I), "Accessories"),
]


def infer_category(subcategory):
    if not subcategory:
        return "Other"
    for pat, label in _SUBCAT_TO_CATEGORY:
        if pat.search(subcategory):
            return label
    return "Other"


# === MAIN ===================================================================

products = []
seen_keys = set()
discovery_errors = []
store_failures = []

logger.info(
    f"Starting RISE PA fast scraper ({len(STORE_SLUGS)} slugs) | "
    f"backend={_HTTP_BACKEND} proxy={'set' if _resolve_proxy() else 'unset'}"
)
t0 = time.monotonic()

session = make_session()

# Step 1: discover store IDs
# Circuit breaker: a few consecutive 403s means the egress IP is refused,
# and the remaining slugs will fail identically. Stop rather than walking
# all 22 against a blocked edge.
ABORT_AFTER_CONSECUTIVE_403 = 3
consecutive_403 = 0

resolved_stores = []
for idx, slug in enumerate(STORE_SLUGS):
    sid, path, err = discover_store_id(session, slug)
    if err:
        logger.warning(f"  {slug}: {err}")
        discovery_errors.append({"slug": slug, "error": err})
        if "HTTP 403" in err:
            consecutive_403 += 1
            if consecutive_403 >= ABORT_AFTER_CONSECUTIVE_403:
                skipped = STORE_SLUGS[idx + 1:]
                logger.error(
                    f"Aborting discovery after {consecutive_403} consecutive 403s "
                    f"-- this egress IP is refused, so the remaining {len(skipped)} "
                    "stores would fail identically. Fix the IP (RISE_PROXY on a "
                    "residential proxy) and re-run."
                )
                for rest in skipped:
                    discovery_errors.append(
                        {"slug": rest, "error": "skipped (circuit breaker)"}
                    )
                break
        else:
            consecutive_403 = 0
        continue
    consecutive_403 = 0
    resolved_stores.append({"slug": slug, "id": sid, "menu_path": path})
    logger.info(f"  {slug} -> store id {sid}")

logger.info(f"Resolved {len(resolved_stores)}/{len(STORE_SLUGS)} stores")

# Fail fast and loudly when discovery got nothing -- don't spend the rest of
# the run producing an empty CSV and then blame the row count.
if not resolved_stores:
    reasons = Counter(e["error"].split("(")[0].strip() for e in discovery_errors)
    logger.critical(f"❌ Resolved 0/{len(STORE_SLUGS)} stores. Reasons: {dict(reasons)}")
    if any("HTTP 403" in e["error"] for e in discovery_errors):
        # Confirmed 2026-09-09: RISE 403s identically from Azure with and
        # without curl_cffi, so the handshake is not what's rejected.
        logger.critical(
            "DIAGNOSIS: 403 = blocked egress IP (datacenter/VPN ASN). This is "
            "NOT a TLS/impersonation problem -- Azure got the identical 403 "
            "both with and without curl_cffi, so do not spend a rebuild on "
            "IMPERSONATE_TARGET."
        )
        if not _resolve_proxy():
            logger.critical(
                "FIX: set RISE_PROXY to a RESIDENTIAL proxy (a datacenter proxy "
                "swaps one blocked ASN for another). Testing locally: try "
                "without a VPN first -- Dutchie behaves this way and an "
                "ordinary residential IP works fine there."
            )
        else:
            logger.critical(
                "FIX: the configured RISE_PROXY exit is itself refused. Is it "
                "actually residential? If so, rotate to a different session/IP."
            )
    else:
        logger.critical(
            "DIAGNOSIS: landing pages loaded but no menu URLs matched. The "
            "URL pattern /dispensaries/pennsylvania/<slug>/<id>/<type>-menu/ "
            "has probably changed -- re-check _MENU_URL_RE against a live page."
        )
    sys.exit(1)

# Step 2: fetch each store's menu
for store in resolved_stores:
    slug = store["slug"]
    disp = slug.replace("-", " ").title()
    url = f"https://risecannabis.com{store['menu_path']}"
    if not url.endswith("/"):
        url += "/"
    t_store = time.monotonic()
    logger.info(f"--- {disp} (id={store['id']}) {url} ---")

    try:
        r = _get(session, url, timeout=MENU_TIMEOUT)
    except Exception as e:
        logger.warning(f"  fetch failed: {e}")
        store_failures.append({"store": disp, "error": str(e)})
        continue
    if r.status_code >= 400:
        logger.warning(f"  HTTP {r.status_code}")
        store_failures.append({"store": disp, "error": f"HTTP {r.status_code}"})
        continue

    rows = parse_store_html(r.text, disp, "medical")
    if not rows:
        logger.warning(
            f"  0 products parsed from {len(r.text)} bytes -- "
            "data-testid markup may have changed"
        )
        store_failures.append({
            "store": disp,
            "error": f"0 products parsed from {len(r.text)} bytes",
        })
        continue

    n_added = 0
    for row in rows:
        row["category"] = infer_category(row.get("subcategory", ""))
        pid = row.pop("_pid", "")
        deals = row.pop("_deals", [])
        dedupe_key = (disp, row["name"], row["weight"], pid)
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)
        for i, d in enumerate(deals):
            row[f"deal_{i+1}"] = d
        products.append(row)
        n_added += 1
    logger.info(f"  {n_added} rows in {time.monotonic() - t_store:.1f}s")

logger.info(f"Total scrape time: {time.monotonic() - t0:.1f}s")
if store_failures:
    logger.warning(
        f"{len(store_failures)}/{len(resolved_stores)} stores failed: "
        + ", ".join(f["store"] for f in store_failures)
    )


# === SAVE JSON ==============================================================

try:
    with open(json_file_path, "w", encoding="utf-8") as f:
        json.dump({
            "rows": products,
            "stores": resolved_stores,
            "discovery_errors": discovery_errors,
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
            f"Invalid CSV output. Rows: {len(df)} (need >= {MIN_VALID_ROWS}). "
            f"Stores resolved: {len(resolved_stores)}/{len(STORE_SLUGS)}, "
            f"store failures: {[f['store'] for f in store_failures] or 'none'}"
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
    logger.info(f"CSV saved: {output_file} | Rows: {len(df)} | "
                f"Deal cols: {len(deal_cols)}")
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

if store_failures or discovery_errors:
    logger.warning(
        f"⚠️ {company} PA fast scraper completed PARTIALLY -- "
        f"{len(resolved_stores) - len(store_failures)}/{len(STORE_SLUGS)} stores collected"
    )
else:
    logger.info(f"{company} PA fast scraper completed")
sys.exit(0)