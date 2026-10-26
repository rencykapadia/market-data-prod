# #!/usr/bin/env python3
# """
# Zen Leaf PA -- FAST scraper.

# The Playwright version scrolled every category page and waited for JS to
# hydrate. Turns out Zen Leaf server-side-renders every product on every
# category page: a single HTTP GET returns the full HTML with all
# `data-testid="product-card"` blocks already populated.

# Flow:
#     1. GET https://data.zenleafdispensaries.com/menu-config/<slug>
#        -> get store metadata + confirm PA is medical-only
#     2. GET https://zenleafdispensaries.com/locations/<slug>
#        -> discover category URLs (each has its own numeric ID per store)
#     3. GET each /locations/<slug>/medical-menu/menu/<cat>-<id>
#        -> parse card text out of the SSR HTML

#     18 stores * ~10 categories * ~2s per request ~= 6 minutes total.
# """
# import json
# import logging
# import os
# import re
# import sys
# import time
# from datetime import datetime

# import pandas as pd
# import pytz


# try:
#     from curl_cffi import requests as httpx_requests
#     _IMPERSONATE_KW = {"impersonate": "chrome124"}
#     _USE_CURL_CFFI = True
# except ImportError:
#     import requests as httpx_requests
#     _IMPERSONATE_KW = {}
#     _USE_CURL_CFFI = False


# logging.basicConfig(
#     level=logging.INFO,
#     format="%(asctime)s | %(levelname)s | %(message)s",
# )
# logger = logging.getLogger(__name__)
# if not _USE_CURL_CFFI:
#     logger.warning("curl_cffi not installed -- falling back to stdlib requests. "
#                    "If you get 403s, run: pip install curl_cffi")


# # === CONFIG =================================================================

# company             = "ZenLeaf"
# location            = "Pennsylvania"
# PRICING_TYPE        = "rec"
# TIMEZONE            = "America/New_York"

# stores = [
#     {"slug": "abington",                  "dispensary_location": "Abington"},
#     {"slug": "altoona",                   "dispensary_location": "Altoona"},
#     {"slug": "clifton-heights",           "dispensary_location": "Clifton Heights"},
#     {"slug": "cranberry",                 "dispensary_location": "Cranberry"},
#     {"slug": "fairless-hills",            "dispensary_location": "Fairless Hills"},
#     {"slug": "harrisburg",                "dispensary_location": "Harrisburg"},
#     {"slug": "malvern",                   "dispensary_location": "Malvern"},
#     {"slug": "monroeville",               "dispensary_location": "Monroeville"},
#     {"slug": "new-kensington",            "dispensary_location": "New Kensington (Pittsburgh)"},
#     {"slug": "norristown",                "dispensary_location": "Norristown"},
#     {"slug": "philadelphia",              "dispensary_location": "Philadelphia"},
#     {"slug": "pittsburgh-mcknight",       "dispensary_location": "Pittsburgh (McKnight)"},
#     {"slug": "pittsburgh-robinson",       "dispensary_location": "Pittsburgh (Robinson)"},
#     {"slug": "sellersville",              "dispensary_location": "Sellersville"},
#     {"slug": "washington",                "dispensary_location": "Washington"},
#     {"slug": "west-chester",              "dispensary_location": "West Chester"},
#     {"slug": "wynnewood",                 "dispensary_location": "Wynnewood"},
#     {"slug": "york",                      "dispensary_location": "York"},
# ]

# CATEGORY_LABELS = {
#     "flower":            "Flower",
#     "pre-rolls":         "Pre-Roll",
#     "infused-pre-rolls": "Pre-Roll",
#     "vapes":             "Vaporizers",
#     "concentrates":      "Concentrate",
#     "edibles":           "Edible",
#     "tinctures":         "Tincture",
#     "topicals":          "Topical",
#     "capsules":          "Capsule",
#     "troches":           "Troches",
#     "accessories":       "Accessories",
#     "hemp-beverages":    "Beverages",
#     "beverages":         "Beverages",
# }

# us_tz = pytz.timezone(TIMEZONE)
# today = datetime.now(us_tz).strftime("%Y-%m-%d")

# output_folder = "app/output/pending"
# csv_folder = os.path.join(output_folder, "csv")
# json_folder = os.path.join(output_folder, "json")
# os.makedirs(csv_folder, exist_ok=True)
# os.makedirs(json_folder, exist_ok=True)
# output_file = os.path.join(csv_folder, f"{today}_{company}_PA.csv")
# json_file_path = os.path.join(json_folder, f"{today}_{company}_PA.json")

# HEADERS = {
#     "User-Agent": (
#         "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
#         "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
#     ),
#     "Accept": "text/html,application/xhtml+xml",
#     "Accept-Language": "en-US,en;q=0.9",
# }


# # === HELPERS ================================================================


# # Strip all HTML tags to text
# _TAG_RE = re.compile(r"<[^>]+>")
# _HTML_ENTITY_RE = re.compile(r"&(#\d+|#x[0-9a-fA-F]+|[a-zA-Z]+);")
# _ENTITY_MAP = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'",
#                "nbsp": " ", "mdash": "—", "ndash": "–", "hellip": "…",
#                "rsquo": "'", "lsquo": "'", "rdquo": "\"", "ldquo": "\""}


# def _decode_entity(m):
#     e = m.group(1)
#     if e.startswith("#x"):
#         try:
#             return chr(int(e[2:], 16))
#         except ValueError:
#             return m.group(0)
#     if e.startswith("#"):
#         try:
#             return chr(int(e[1:]))
#         except ValueError:
#             return m.group(0)
#     return _ENTITY_MAP.get(e, m.group(0))


# def html_to_text(chunk):
#     # Convert tags to spaces (not newlines) -- each React <span> around a
#     # word would otherwise split "Flower by Savvy" across three lines.
#     chunk = _TAG_RE.sub(" ", chunk)
#     chunk = _HTML_ENTITY_RE.sub(_decode_entity, chunk)
#     # Collapse runs of whitespace
#     chunk = re.sub(r"\s+", " ", chunk).strip()
#     return chunk


# _CARD_RE = re.compile(
#     r'<div[^>]*data-testid="product-card"[^>]*>',
#     re.IGNORECASE,
# )


# def iter_card_blocks(html):
#     """Yield each product-card HTML block by finding start markers and
#     walking the <div> nesting to the matching closing tag."""
#     for m in _CARD_RE.finditer(html):
#         start = m.start()
#         depth = 1
#         i = m.end()
#         # Walk forward counting <div ...> and </div>
#         while i < len(html):
#             next_open = html.find("<div", i)
#             next_close = html.find("</div>", i)
#             if next_close == -1:
#                 break
#             if next_open != -1 and next_open < next_close:
#                 depth += 1
#                 i = next_open + 4
#             else:
#                 depth -= 1
#                 i = next_close + len("</div>")
#                 if depth == 0:
#                     yield html[start:i]
#                     break


# _STRAIN_TOKENS = {"indica", "sativa", "hybrid", "cbd", "high cbd", "1:1", "2:1"}
# _STRAIN_MAP = {"indica": "Indica", "sativa": "Sativa", "hybrid": "Hybrid",
#                "cbd": "High CBD", "high cbd": "High CBD",
#                "1:1": "1:1", "2:1": "2:1"}
# # Zen Leaf renders the brand line as "<Category-in-any-form> by <Brand>".
# # Cover singular + plural forms of every category we've seen live.
# _BRAND_RE = re.compile(
#     r"^(?:"
#     r"Flowers?|Vapes?|Vaporizers?|Concentrates?|Extracts?|Edibles?|"
#     r"Tinctures?|Topicals?|Capsules?|Beverages?|Pre-Rolls?|"
#     r"Infused Pre-Rolls?|Troches?|Accessor(?:y|ies)|Merch|Gear"
#     r")\s+by\s+(.+)$",
#     re.IGNORECASE,
# )
# _WEIGHT_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(g|mg|oz|ml|pk|ct|pack|count)\b",
#                         re.IGNORECASE)
# _PRICE_RE = re.compile(r"\$(\d+(?:\.\d{1,2})?)(?!\d)")


# _STRAIN_INLINE_RE = re.compile(
#     r"\b(Indica|Sativa|Hybrid|CBD|High CBD|1:1|2:1)\b", re.I
# )
# _CATEGORY_ALT = (
#     r"Flowers?|Vapes?|Vaporizers?|Concentrates?|Extracts?|Edibles?|"
#     r"Tinctures?|Topicals?|Capsules?|Beverages?|Pre-Rolls?|"
#     r"Infused Pre-Rolls?|Troches?|Accessor(?:y|ies)|Merch|Gear"
# )
# # "<Category> by <Brand>" with brand captured up to the product name.
# # The name always starts with a capital letter; brand ends before that.
# _BRAND_FLAT_RE = re.compile(
#     rf"\b(?:{_CATEGORY_ALT})\s+by\s+([A-Z0-9&][\w &.'’!-]*?)\s+(?=[A-Z0-9]\w)",
#     re.IGNORECASE,
# )
# _THC_FLAT_RE = re.compile(
#     r"THC\s*[:\s]+([\d.]+(?:\s*-\s*[\d.]+)?)\s*%", re.I
# )
# _CBD_FLAT_RE = re.compile(
#     r"CBD\s*[:\s]+([\d.]+(?:\s*-\s*[\d.]+)?)\s*%", re.I
# )
# _WEIGHT_FLAT_RE = re.compile(
#     r"\b(\d+(?:\.\d+)?)\s*(g|mg|oz|ml|pk|ct|pack|count)\b", re.I
# )
# _DEAL_RE = re.compile(
#     r"(\d+%\s*off|only\s+\d+\s+left|as low as[^$]*|\bnew\b|\bsale\b)",
#     re.I,
# )


# def parse_card(card_html):
#     text = html_to_text(card_html)
#     brand = name = strain_type = thc = cbd = weight = ""
#     prices = []
#     deals = []

#     # Strain type
#     m = _STRAIN_INLINE_RE.search(text)
#     if m:
#         s = m.group(1).lower()
#         strain_type = _STRAIN_MAP.get(s, s.title())

#     # Brand + name -- brand line format "<Category> by <Brand> <Name>..."
#     m = _BRAND_FLAT_RE.search(text)
#     if m:
#         brand = m.group(1).strip().rstrip(",")
#         # Name is everything AFTER brand up to THC/TERP/weight/price
#         after = text[m.end():]
#         # Cut at first THC/TERP/CBD/weight/price marker
#         cuts = []
#         for pat in (r"\bTHC\b", r"\bTERP\b", r"\bCBD\b",
#                     r"\d+(?:\.\d+)?\s*(?:g|mg|oz|ml)\b",
#                     r"\$\d", r"Only \d+ left", r"\d+%\s*off"):
#             mm = re.search(pat, after, re.I)
#             if mm:
#                 cuts.append(mm.start())
#         end = min(cuts) if cuts else len(after)
#         name = after[:end].strip().rstrip(",;")

#     # THC / CBD
#     m = _THC_FLAT_RE.search(text)
#     if m:
#         thc = m.group(1).replace(" ", "") + "%"
#     m = _CBD_FLAT_RE.search(text)
#     if m:
#         cbd = m.group(1).replace(" ", "") + "%"

#     # Weight -- prefer one that appears with the category label like "3.5g Flower"
#     weight_matches = list(_WEIGHT_FLAT_RE.finditer(text))
#     if weight_matches:
#         # Take the first weight after the brand line
#         cutoff = _BRAND_FLAT_RE.search(text).end() if _BRAND_FLAT_RE.search(text) else 0
#         for wm in weight_matches:
#             if wm.start() >= cutoff:
#                 weight = wm.group(0).strip()
#                 break
#         if not weight:
#             weight = weight_matches[0].group(0).strip()

#     # Prices
#     for pm in _PRICE_RE.findall(text):
#         try:
#             prices.append(float(pm))
#         except (TypeError, ValueError):
#             pass

#     # Deals
#     for dm in _DEAL_RE.finditer(text):
#         d = dm.group(0).strip()
#         # Skip empty "SALE" that stands alone -- it's a badge
#         if d.lower() == "sale" and len(deals) == 0:
#             deals.append("Sale")
#         elif d not in deals:
#             deals.append(d)

#     original_price = min(prices) if prices else None
#     return {
#         "brand": brand, "name": name, "strain_type": strain_type,
#         "THC": thc, "CBD": cbd, "weight": weight,
#         "original_price": original_price, "deals": deals,
#     }


# _MENU_LINK_RE = re.compile(
#     r'href="(/locations/([a-z0-9-]+)/(medical|recreational)-menu/menu/([a-z0-9-]+)-(\d+))"',
#     re.IGNORECASE,
# )


# def discover_categories(html, expected_slug):
#     """Extract category menu URLs + labels from a store's landing HTML.

#     Prefer recreational-menu URLs when available; fall back to medical-menu
#     for stores that are medical-only.
#     """
#     # (label, url) grouped by (category_label, menu_type)
#     rec = {}
#     med = {}
#     for m in _MENU_LINK_RE.finditer(html):
#         path, slug, menu_type, cat_slug, cat_id = m.groups()
#         if slug != expected_slug:
#             continue
#         label = CATEGORY_LABELS.get(cat_slug)
#         if not label:
#             for k, v in CATEGORY_LABELS.items():
#                 if cat_slug.startswith(k):
#                     label = v
#                     break
#         if not label:
#             continue
#         full = f"https://zenleafdispensaries.com{path}"
#         bucket = rec if menu_type.lower() == "recreational" else med
#         # Keep first occurrence per category label
#         bucket.setdefault(label, full)
#     # Prefer rec; add any med categories not covered by rec
#     combined = dict(rec)
#     for label, url in med.items():
#         combined.setdefault(label, url)
#     return list(combined.items())


# # === MAIN ===================================================================

# products = []
# seen_keys = set()

# logger.info(f"Starting Zen Leaf PA fast scraper ({len(stores)} stores)")
# t0 = time.monotonic()

# for store in stores:
#     slug = store["slug"]
#     disp = store["dispensary_location"]
#     t_store = time.monotonic()
#     logger.info(f"--- {disp} ({slug}) ---")

#     # Step 1: get store landing HTML to discover category URLs
#     try:
#         r = httpx_requests.get(
#             f"https://zenleafdispensaries.com/locations/{slug}",
#             headers=HEADERS, timeout=45, **_IMPERSONATE_KW,
#         )
#         if r.status_code >= 400:
#             logger.warning(f"  landing HTTP {r.status_code}, skipping")
#             continue
#         landing_html = r.text
#         cats = discover_categories(landing_html, slug)
#     except Exception as e:
#         logger.warning(f"  landing fetch failed: {e}")
#         continue

#     if not cats:
#         logger.warning(f"  no categories discovered for {slug} "
#                        f"(landing {len(landing_html)} bytes)")
#         continue

#     logger.info(f"  {len(cats)} categories discovered")

#     # Step 2: fetch each category page and parse cards
#     # cats is dict.items() -> (label, url) pairs
#     for label, url in cats:
#         try:
#             r = httpx_requests.get(url, headers=HEADERS, timeout=45,
#                                     **_IMPERSONATE_KW)
#             if r.status_code >= 400:
#                 logger.warning(f"  {label} HTTP {r.status_code}")
#                 continue
#             html = r.text
#         except Exception as e:
#             logger.warning(f"  {label} fetch failed: {e}")
#             continue

#         card_hits = html.count('data-testid="product-card"')
#         n_before = len(products)
#         parsed_ok = 0
#         for card_html in iter_card_blocks(html):
#             parsed = parse_card(card_html)
#             if not parsed["brand"] or not parsed["name"]:
#                 continue
#             parsed_ok += 1
#             deals = parsed.pop("deals")
#             dedupe_key = (disp, parsed["name"], parsed["weight"])
#             if dedupe_key in seen_keys:
#                 continue
#             seen_keys.add(dedupe_key)
#             row = {
#                 "date": today,
#                 "location": location,
#                 "company": company,
#                 "dispensary_location": disp,
#                 "name": parsed["name"],
#                 "category": label,
#                 "subcategory": "DEFAULT",
#                 "brand": parsed["brand"],
#                 "strain_type": parsed["strain_type"],
#                 "THC": parsed["THC"],
#                 "CBD": parsed["CBD"],
#                 "weight": parsed["weight"],
#                 "original_price": parsed["original_price"],
#             }
#             for i, d in enumerate(deals):
#                 row[f"deal_{i+1}"] = d
#             products.append(row)
#         added = len(products) - n_before
#         note = ""
#         if card_hits > 0 and parsed_ok == 0:
#             note = " (cards found but brand/name parse failed -- check regex)"
#         logger.info(f"    {label}: {card_hits} cards, +{added} rows{note}")

#     logger.info(f"  store done in {time.monotonic() - t_store:.1f}s")

# logger.info(f"Total scrape time: {time.monotonic() - t0:.1f}s")


# # === SAVE JSON ==============================================================

# try:
#     with open(json_file_path, "w", encoding="utf-8") as f:
#         json.dump({"rows": products}, f, indent=2, default=str)
#     logger.info(f"JSON saved: {json_file_path}")
# except Exception as e:
#     logger.critical(f"JSON save failed: {e}", exc_info=True)
#     sys.exit(1)


# # === SAVE CSV ===============================================================

# try:
#     df = pd.DataFrame(products).drop_duplicates(
#         subset=["dispensary_location", "name", "weight"]
#     )
#     if df.empty or len(df) < 5:
#         logger.critical(f"Invalid CSV output. Rows: {len(df)}")
#         sys.exit(1)

#     fixed_cols = [
#         "date", "location", "company", "dispensary_location", "name",
#         "category", "subcategory", "brand",
#         "strain_type", "THC", "CBD", "weight", "original_price",
#     ]
#     deal_cols = sorted(
#         [c for c in df.columns if c.startswith("deal_")],
#         key=lambda x: int(x.split("_")[1]),
#     )
#     df = df.reindex(columns=fixed_cols + deal_cols)

#     df.to_csv(output_file, index=False, na_rep="")
#     per_store = df.groupby("dispensary_location").size().to_dict()
#     cat_counts = df.groupby("category").size().to_dict()
#     logger.info(f"CSV saved: {output_file} | Rows: {len(df)} | "
#                 f"Deal cols: {len(deal_cols)}")
#     logger.info(f"Rows per dispensary: {per_store}")
#     logger.info(f"Rows per category: {cat_counts}")

#     top_brands = df["brand"].value_counts().head(20).to_dict()
#     logger.info("Top brands:")
#     for b, n in top_brands.items():
#         logger.info(f"    {n:4d}  {b}")
# except Exception as e:
#     logger.critical(f"CSV save failed: {e}", exc_info=True)
#     sys.exit(1)

# logger.info(f"{company} PA fast scraper completed")
# sys.exit(0)


#!/usr/bin/env python3
"""
Zen Leaf PA -- FAST scraper.

The Playwright version scrolled every category page and waited for JS to
hydrate. Turns out Zen Leaf server-side-renders every product on every
category page: a single HTTP GET returns the full HTML with all
`data-testid="product-card"` blocks already populated.

Flow:
    1. GET https://data.zenleafdispensaries.com/menu-config/<slug>
       -> get store metadata + confirm PA is medical-only
    2. GET https://zenleafdispensaries.com/locations/<slug>
       -> discover category URLs (each has its own numeric ID per store)
    3. GET each /locations/<slug>/medical-menu/menu/<cat>-<id>
       -> parse card text out of the SSR HTML

    18 stores * ~10 categories * ~2s per request ~= 6 minutes total.
"""
import json
import logging
import os
import re
import sys
import time
from datetime import datetime

import pandas as pd
import pytz


try:
    from curl_cffi import requests as httpx_requests
    _IMPERSONATE_KW = {"impersonate": "chrome124"}
    _USE_CURL_CFFI = True
except ImportError:
    import requests as httpx_requests
    _IMPERSONATE_KW = {}
    _USE_CURL_CFFI = False


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)
if not _USE_CURL_CFFI:
    logger.warning("curl_cffi not installed -- falling back to stdlib requests. "
                   "If you get 403s, run: pip install curl_cffi")


# === CONFIG =================================================================

company             = "ZenLeaf"
location            = "Pennsylvania"
PRICING_TYPE        = "rec"
TIMEZONE            = "America/New_York"

stores = [
    {"slug": "abington",                  "dispensary_location": "Abington"},
    {"slug": "altoona",                   "dispensary_location": "Altoona"},
    {"slug": "clifton-heights",           "dispensary_location": "Clifton Heights"},
    {"slug": "cranberry",                 "dispensary_location": "Cranberry"},
    {"slug": "fairless-hills",            "dispensary_location": "Fairless Hills"},
    {"slug": "harrisburg",                "dispensary_location": "Harrisburg"},
    {"slug": "malvern",                   "dispensary_location": "Malvern"},
    {"slug": "monroeville",               "dispensary_location": "Monroeville"},
    {"slug": "new-kensington",            "dispensary_location": "New Kensington (Pittsburgh)"},
    {"slug": "norristown",                "dispensary_location": "Norristown"},
    {"slug": "philadelphia",              "dispensary_location": "Philadelphia"},
    {"slug": "pittsburgh-mcknight",       "dispensary_location": "Pittsburgh (McKnight)"},
    {"slug": "pittsburgh-robinson",       "dispensary_location": "Pittsburgh (Robinson)"},
    {"slug": "sellersville",              "dispensary_location": "Sellersville"},
    {"slug": "washington",                "dispensary_location": "Washington"},
    {"slug": "west-chester",              "dispensary_location": "West Chester"},
    {"slug": "wynnewood",                 "dispensary_location": "Wynnewood"},
    {"slug": "york",                      "dispensary_location": "York"},
]

CATEGORY_LABELS = {
    "flower":            "Flower",
    "pre-rolls":         "Pre-Roll",
    "infused-pre-rolls": "Pre-Roll",
    "vapes":             "Vaporizers",
    "concentrates":      "Concentrate",
    "edibles":           "Edible",
    "tinctures":         "Tincture",
    "topicals":          "Topical",
    "capsules":          "Capsule",
    "troches":           "Troches",
    "accessories":       "Accessories",
    "hemp-beverages":    "Beverages",
    "beverages":         "Beverages",
}

us_tz = pytz.timezone(TIMEZONE)
today = datetime.now(us_tz).strftime("%Y-%m-%d")

output_folder = "app/output/pending"
csv_folder = os.path.join(output_folder, "csv")
json_folder = os.path.join(output_folder, "json")
os.makedirs(csv_folder, exist_ok=True)
os.makedirs(json_folder, exist_ok=True)
output_file = os.path.join(csv_folder, f"{today}_{company}.csv")
json_file_path = os.path.join(json_folder, f"{today}_{company}.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}


# === HELPERS ================================================================


# Strip all HTML tags to text
_TAG_RE = re.compile(r"<[^>]+>")
_HTML_ENTITY_RE = re.compile(r"&(#\d+|#x[0-9a-fA-F]+|[a-zA-Z]+);")
_ENTITY_MAP = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'",
               "nbsp": " ", "mdash": "—", "ndash": "–", "hellip": "…",
               "rsquo": "'", "lsquo": "'", "rdquo": "\"", "ldquo": "\""}


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
    # Convert tags to spaces (not newlines) -- each React <span> around a
    # word would otherwise split "Flower by Savvy" across three lines.
    chunk = _TAG_RE.sub(" ", chunk)
    chunk = _HTML_ENTITY_RE.sub(_decode_entity, chunk)
    # Collapse runs of whitespace
    chunk = re.sub(r"\s+", " ", chunk).strip()
    return chunk


_CARD_RE = re.compile(
    r'<div[^>]*data-testid="product-card"[^>]*>',
    re.IGNORECASE,
)


def iter_card_blocks(html):
    """Yield each product-card HTML block by finding start markers and
    walking the <div> nesting to the matching closing tag."""
    for m in _CARD_RE.finditer(html):
        start = m.start()
        depth = 1
        i = m.end()
        # Walk forward counting <div ...> and </div>
        while i < len(html):
            next_open = html.find("<div", i)
            next_close = html.find("</div>", i)
            if next_close == -1:
                break
            if next_open != -1 and next_open < next_close:
                depth += 1
                i = next_open + 4
            else:
                depth -= 1
                i = next_close + len("</div>")
                if depth == 0:
                    yield html[start:i]
                    break


_STRAIN_TOKENS = {"indica", "sativa", "hybrid", "cbd", "high cbd", "1:1", "2:1"}
_STRAIN_MAP = {"indica": "Indica", "sativa": "Sativa", "hybrid": "Hybrid",
               "cbd": "High CBD", "high cbd": "High CBD",
               "1:1": "1:1", "2:1": "2:1"}
# Zen Leaf renders the brand line as "<Category-in-any-form> by <Brand>".
# Cover singular + plural forms of every category we've seen live.
_BRAND_RE = re.compile(
    r"^(?:"
    r"Flowers?|Vapes?|Vaporizers?|Concentrates?|Extracts?|Edibles?|"
    r"Tinctures?|Topicals?|Capsules?|Beverages?|Pre-Rolls?|"
    r"Infused Pre-Rolls?|Troches?|Accessor(?:y|ies)|Merch|Gear"
    r")\s+by\s+(.+)$",
    re.IGNORECASE,
)
_WEIGHT_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(g|mg|oz|ml|pk|ct|pack|count)\b",
                        re.IGNORECASE)
_PRICE_RE = re.compile(r"\$(\d+(?:\.\d{1,2})?)(?!\d)")


_STRAIN_INLINE_RE = re.compile(
    r"\b(Indica|Sativa|Hybrid|CBD|High CBD|1:1|2:1)\b", re.I
)
_CATEGORY_ALT = (
    r"Flowers?|Vapes?|Vaporizers?|Concentrates?|Extracts?|Edibles?|"
    r"Tinctures?|Topicals?|Capsules?|Beverages?|Pre-Rolls?|"
    r"Infused Pre-Rolls?|Troches?|Accessor(?:y|ies)|Merch|Gear"
)
# "<Category> by <Brand>" with brand captured up to the product name.
# The name always starts with a capital letter; brand ends before that.
_BRAND_FLAT_RE = re.compile(
    rf"\b(?:{_CATEGORY_ALT})\s+by\s+([A-Z0-9&][\w &.'’!-]*?)\s+(?=[A-Z0-9]\w)",
    re.IGNORECASE,
)
_THC_FLAT_RE = re.compile(
    r"THC\s*[:\s]+([\d.]+(?:\s*-\s*[\d.]+)?)\s*%", re.I
)
_CBD_FLAT_RE = re.compile(
    r"CBD\s*[:\s]+([\d.]+(?:\s*-\s*[\d.]+)?)\s*%", re.I
)
_WEIGHT_FLAT_RE = re.compile(
    r"\b(\d+(?:\.\d+)?)\s*(g|mg|oz|ml|pk|ct|pack|count)\b", re.I
)
_DEAL_RE = re.compile(
    r"(\d+%\s*off|only\s+\d+\s+left|as low as[^$]*|\bnew\b|\bsale\b)",
    re.I,
)


def parse_card(card_html):
    text = html_to_text(card_html)
    brand = name = strain_type = thc = cbd = weight = ""
    prices = []
    deals = []

    # Strain type
    m = _STRAIN_INLINE_RE.search(text)
    if m:
        s = m.group(1).lower()
        strain_type = _STRAIN_MAP.get(s, s.title())

    # Brand + name -- brand line format "<Category> by <Brand> <Name>..."
    m = _BRAND_FLAT_RE.search(text)
    if m:
        brand = m.group(1).strip().rstrip(",")
        # Name is everything AFTER brand up to THC/TERP/weight/price
        after = text[m.end():]
        # Cut at first THC/TERP/CBD/weight/price marker
        cuts = []
        for pat in (r"\bTHC\b", r"\bTERP\b", r"\bCBD\b",
                    r"\d+(?:\.\d+)?\s*(?:g|mg|oz|ml)\b",
                    r"\$\d", r"Only \d+ left", r"\d+%\s*off"):
            mm = re.search(pat, after, re.I)
            if mm:
                cuts.append(mm.start())
        end = min(cuts) if cuts else len(after)
        name = after[:end].strip().rstrip(",;")

    # THC / CBD
    m = _THC_FLAT_RE.search(text)
    if m:
        thc = m.group(1).replace(" ", "") + "%"
    m = _CBD_FLAT_RE.search(text)
    if m:
        cbd = m.group(1).replace(" ", "") + "%"

    # Weight -- prefer one that appears with the category label like "3.5g Flower"
    weight_matches = list(_WEIGHT_FLAT_RE.finditer(text))
    if weight_matches:
        # Take the first weight after the brand line
        cutoff = _BRAND_FLAT_RE.search(text).end() if _BRAND_FLAT_RE.search(text) else 0
        for wm in weight_matches:
            if wm.start() >= cutoff:
                weight = wm.group(0).strip()
                break
        if not weight:
            weight = weight_matches[0].group(0).strip()

    # Prices
    for pm in _PRICE_RE.findall(text):
        try:
            prices.append(float(pm))
        except (TypeError, ValueError):
            pass

    # Deals
    for dm in _DEAL_RE.finditer(text):
        d = dm.group(0).strip()
        # Skip empty "SALE" that stands alone -- it's a badge
        if d.lower() == "sale" and len(deals) == 0:
            deals.append("Sale")
        elif d not in deals:
            deals.append(d)

    # Two $ values on a sale card = sale + strike-through; keep the higher
    # (strike-through / pre-discount) as original_price.
    original_price = max(prices) if prices else None
    return {
        "brand": brand, "name": name, "strain_type": strain_type,
        "THC": thc, "CBD": cbd, "weight": weight,
        "original_price": original_price, "deals": deals,
    }


_MENU_LINK_RE = re.compile(
    r'href="(/locations/([a-z0-9-]+)/(medical|recreational)-menu/menu/([a-z0-9-]+)-(\d+))"',
    re.IGNORECASE,
)


def discover_categories(html, expected_slug):
    """Extract category menu URLs + labels from a store's landing HTML.

    Prefer recreational-menu URLs when available; fall back to medical-menu
    for stores that are medical-only.
    """
    # (label, url) grouped by (category_label, menu_type)
    rec = {}
    med = {}
    for m in _MENU_LINK_RE.finditer(html):
        path, slug, menu_type, cat_slug, cat_id = m.groups()
        if slug != expected_slug:
            continue
        label = CATEGORY_LABELS.get(cat_slug)
        if not label:
            for k, v in CATEGORY_LABELS.items():
                if cat_slug.startswith(k):
                    label = v
                    break
        if not label:
            continue
        full = f"https://zenleafdispensaries.com{path}"
        bucket = rec if menu_type.lower() == "recreational" else med
        # Keep first occurrence per category label
        bucket.setdefault(label, full)
    # Prefer rec; add any med categories not covered by rec
    combined = dict(rec)
    for label, url in med.items():
        combined.setdefault(label, url)
    return list(combined.items())


# === MAIN ===================================================================

products = []
seen_keys = set()

logger.info(f"Starting Zen Leaf PA fast scraper ({len(stores)} stores)")
t0 = time.monotonic()

for store in stores:
    slug = store["slug"]
    disp = store["dispensary_location"]
    t_store = time.monotonic()
    logger.info(f"--- {disp} ({slug}) ---")

    # Step 1: get store landing HTML to discover category URLs
    try:
        r = httpx_requests.get(
            f"https://zenleafdispensaries.com/locations/{slug}",
            headers=HEADERS, timeout=45, **_IMPERSONATE_KW,
        )
        if r.status_code >= 400:
            logger.warning(f"  landing HTTP {r.status_code}, skipping")
            continue
        landing_html = r.text
        cats = discover_categories(landing_html, slug)
    except Exception as e:
        logger.warning(f"  landing fetch failed: {e}")
        continue

    if not cats:
        logger.warning(f"  no categories discovered for {slug} "
                       f"(landing {len(landing_html)} bytes)")
        continue

    logger.info(f"  {len(cats)} categories discovered")

    # Step 2: fetch each category page and parse cards
    # cats is dict.items() -> (label, url) pairs
    for label, url in cats:
        try:
            r = httpx_requests.get(url, headers=HEADERS, timeout=45,
                                    **_IMPERSONATE_KW)
            if r.status_code >= 400:
                logger.warning(f"  {label} HTTP {r.status_code}")
                continue
            html = r.text
        except Exception as e:
            logger.warning(f"  {label} fetch failed: {e}")
            continue

        card_hits = html.count('data-testid="product-card"')
        n_before = len(products)
        parsed_ok = 0
        for card_html in iter_card_blocks(html):
            parsed = parse_card(card_html)
            if not parsed["brand"] or not parsed["name"]:
                continue
            parsed_ok += 1
            deals = parsed.pop("deals")
            dedupe_key = (disp, parsed["name"], parsed["weight"])
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)
            row = {
                "date": today,
                "location": location,
                "company": company,
                "dispensary_location": disp,
                "name": parsed["name"],
                "category": label,
                "subcategory": "DEFAULT",
                "brand": parsed["brand"],
                "strain_type": parsed["strain_type"],
                "THC": parsed["THC"],
                "CBD": parsed["CBD"],
                "weight": parsed["weight"],
                "original_price": parsed["original_price"],
            }
            for i, d in enumerate(deals):
                row[f"deal_{i+1}"] = d
            products.append(row)
        added = len(products) - n_before
        note = ""
        if card_hits > 0 and parsed_ok == 0:
            note = " (cards found but brand/name parse failed -- check regex)"
        logger.info(f"    {label}: {card_hits} cards, +{added} rows{note}")

    logger.info(f"  store done in {time.monotonic() - t_store:.1f}s")

logger.info(f"Total scrape time: {time.monotonic() - t0:.1f}s")


# === SAVE JSON ==============================================================

try:
    with open(json_file_path, "w", encoding="utf-8") as f:
        json.dump({"rows": products}, f, indent=2, default=str)
    logger.info(f"JSON saved: {json_file_path}")
except Exception as e:
    logger.critical(f"JSON save failed: {e}", exc_info=True)
    sys.exit(1)


# === SAVE CSV ===============================================================

try:
    df = pd.DataFrame(products).drop_duplicates(
        subset=["dispensary_location", "name", "weight"]
    )
    if df.empty or len(df) < 5:
        logger.critical(f"Invalid CSV output. Rows: {len(df)}")
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
except Exception as e:
    logger.critical(f"CSV save failed: {e}", exc_info=True)
    sys.exit(1)

logger.info(f"{company} PA fast scraper completed")
sys.exit(0)