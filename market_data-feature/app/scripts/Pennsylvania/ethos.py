# #!/usr/bin/env python3
# """
# Ethos Cannabis (Pennsylvania) -- FAST scraper.

# Ethos runs on Sweed's white-label storefront (same platform as Curaleaf), so
# the store URL

#     https://ethoscannabis.com/stores/pennsylvania/<slug>

# returns SSR HTML with the ENTIRE catalog embedded as JSON via multiple
# `"products":[...]` blocks (one per category grouping). Same extractor pattern
# as Curaleaf.

#     6 PA stores * 1 HTTP GET each * ~2s = ~15 seconds total.
# """
# import json
# import logging
# import os
# import sys
# import time
# from datetime import datetime

# import pandas as pd
# import pytz


# try:
#     from curl_cffi import requests as httpx_requests
#     _IMPERSONATE_KW = {"impersonate": "chrome124"}
# except ImportError:
#     import requests as httpx_requests
#     _IMPERSONATE_KW = {}


# logging.basicConfig(
#     level=logging.INFO,
#     format="%(asctime)s | %(levelname)s | %(message)s",
# )
# logger = logging.getLogger(__name__)


# # === CONFIG =================================================================

# company             = "Ethos"
# location            = "Pennsylvania"
# PRICING_TYPE        = "rec"
# TIMEZONE            = "America/New_York"

# # 6 PA stores from ethoscannabis.com/dispensaries/
# stores = [
#     {"slug": "allentown",                          "dispensary_location": "Allentown"},
#     {"slug": "northeast-philadelphia",             "dispensary_location": "Northeast Philadelphia"},
#     {"slug": "pittsburgh-north-at-harmarville",    "dispensary_location": "Pittsburgh North (Harmarville)"},
#     {"slug": "pittsburgh-south-at-pleasant-hills", "dispensary_location": "Pittsburgh South (Pleasant Hills)"},
#     {"slug": "pittsburgh-west-at-north-fayette",   "dispensary_location": "Pittsburgh West (North Fayette)"},
#     {"slug": "wilkes-barre",                       "dispensary_location": "Wilkes-Barre"},
# ]

# us_tz = pytz.timezone(TIMEZONE)
# today = datetime.now(us_tz).strftime("%Y-%m-%d")

# output_folder = "app/output/pending"
# csv_folder = os.path.join(output_folder, "csv")
# json_folder = os.path.join(output_folder, "json")
# os.makedirs(csv_folder, exist_ok=True)
# os.makedirs(json_folder, exist_ok=True)
# output_file = os.path.join(csv_folder, f"{today}_{company}_PA.csv")
# json_file_path = os.path.join(json_folder, f"{today}_{company}_PA.json")

# BASE_URL = "https://ethoscannabis.com/stores/pennsylvania"

# COOKIES = {
#     "user_state": "pennsylvania",
#     "state": "pennsylvania",
#     "age-gate": "true",
#     "age_verified": "true",
#     "ageVerified": "true",
# }

# HEADERS = {
#     "User-Agent": (
#         "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
#         "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
#     ),
#     "Accept": "text/html,application/xhtml+xml",
#     "Accept-Language": "en-US,en;q=0.9",
# }


# # === HELPERS ================================================================

# _PRODUCTS_KEY = '"products":['


# def extract_products_arrays(html):
#     """Walk each `"products":[...]` block by bracket depth, yielding parsed
#     arrays. Handles quoted strings and escapes."""
#     idx = 0
#     while True:
#         start = html.find(_PRODUCTS_KEY, idx)
#         if start == -1:
#             return
#         bracket_start = start + len(_PRODUCTS_KEY) - 1
#         depth = 0
#         in_str = False
#         escape = False
#         end = -1
#         for i in range(bracket_start, len(html)):
#             c = html[i]
#             if escape:
#                 escape = False
#                 continue
#             if c == "\\":
#                 escape = True
#                 continue
#             if c == '"':
#                 in_str = not in_str
#                 continue
#             if in_str:
#                 continue
#             if c == "[":
#                 depth += 1
#             elif c == "]":
#                 depth -= 1
#                 if depth == 0:
#                     end = i
#                     break
#         if end == -1:
#             return
#         blob = html[bracket_start : end + 1]
#         try:
#             arr = json.loads(blob)
#             if isinstance(arr, list):
#                 yield arr
#         except json.JSONDecodeError:
#             pass
#         idx = end + 1


# def format_potency(labtest):
#     if not isinstance(labtest, dict):
#         return ""
#     vals = labtest.get("value")
#     unit = labtest.get("unitAbbr") or "%"
#     if isinstance(vals, list) and vals:
#         nums = [x for x in vals if isinstance(x, (int, float)) and x > 0]
#         if nums:
#             return f"{max(nums):g}{unit}"
#     if isinstance(vals, (int, float)) and vals > 0:
#         return f"{float(vals):g}{unit}"
#     return ""


# def resolve_weight(variant):
#     if not isinstance(variant, dict):
#         return ""
#     n = variant.get("name")
#     if n:
#         return str(n).strip()
#     us = variant.get("unitSize") or {}
#     if isinstance(us, dict) and us.get("value"):
#         v, u = us.get("value"), us.get("unitAbbr") or ""
#         try:
#             return f"{float(v):g}{u.lower()}"
#         except (TypeError, ValueError):
#             return f"{v}{u.lower()}"
#     return ""


# def collect_deals(product, variant):
#     deals = []
#     seen = set()
#     for src in (product, variant):
#         if not isinstance(src, dict):
#             continue
#         for promo in (src.get("promos") or []):
#             if isinstance(promo, dict):
#                 nm = (promo.get("shortName") or promo.get("name") or "").strip()
#                 if nm and nm not in seen:
#                     seen.add(nm)
#                     deals.append(nm)
#     return deals


# # === MAIN ===================================================================

# products = []
# seen_keys = set()
# raw_payloads = []

# logger.info(f"Starting Ethos PA fast scraper ({len(stores)} stores)")
# t0 = time.monotonic()

# for store in stores:
#     slug = store["slug"]
#     disp = store["dispensary_location"]
#     url = f"{BASE_URL}/{slug}"
#     t_store = time.monotonic()
#     logger.info(f"--- {disp} ({slug}) ---")

#     try:
#         r = httpx_requests.get(url, headers=HEADERS, cookies=COOKIES,
#                                 timeout=45, allow_redirects=True,
#                                 **_IMPERSONATE_KW)
#         if r.status_code >= 400:
#             logger.warning(f"  HTTP {r.status_code}, skipping")
#             continue
#         html = r.text
#     except Exception as e:
#         logger.warning(f"  fetch failed: {e}")
#         continue

#     final_url = str(getattr(r, "url", url))
#     products_hits = html.count('"products":[')
#     logger.info(f"  HTTP {r.status_code}, {len(html)} bytes, "
#                 f"products-key hits={products_hits}")

#     n_before = len(products)
#     for arr in extract_products_arrays(html):
#         raw_payloads.append({"store": disp, "count": len(arr)})
#         for prod in arr:
#             if not isinstance(prod, dict):
#                 continue
#             try:
#                 brand_obj = prod.get("brand") or {}
#                 brand = (brand_obj.get("name") if isinstance(brand_obj, dict)
#                          else str(brand_obj or "")).strip()
#                 name = (prod.get("name") or "").strip()
#                 cat_obj = prod.get("category") or {}
#                 category = (cat_obj.get("name") if isinstance(cat_obj, dict)
#                             else str(cat_obj or "")).strip()
#                 subcat_obj = prod.get("subcategory") or {}
#                 subcategory = (subcat_obj.get("name") if isinstance(subcat_obj, dict)
#                                else str(subcat_obj or "")).strip()
#                 subcategory = subcategory.upper().replace(" ", "-") if subcategory else "DEFAULT"
#                 strain_obj = prod.get("strain") or {}
#                 prev = strain_obj.get("prevalence") if isinstance(strain_obj, dict) else None
#                 strain_type = (prev.get("name") if isinstance(prev, dict) else "") or ""
#                 pid = prod.get("id") or name

#                 variants = prod.get("variants") or [None]

#                 for v in variants:
#                     if isinstance(v, dict):
#                         weight = resolve_weight(v)
#                         lab = v.get("labTests") or {}
#                         thc = format_potency(lab.get("thc") if isinstance(lab, dict) else None)
#                         cbd = format_potency(lab.get("cbd") if isinstance(lab, dict) else None)
#                         promo = v.get("promoPrice")
#                         base = v.get("price")
#                         original_price = promo if promo is not None else base
#                         deals = collect_deals(prod, v)
#                     else:
#                         weight = ""
#                         thc = cbd = ""
#                         original_price = prod.get("price")
#                         deals = collect_deals(prod, {})

#                     dedupe_key = (disp, pid, weight)
#                     if dedupe_key in seen_keys:
#                         continue
#                     seen_keys.add(dedupe_key)

#                     row = {
#                         "date": today,
#                         "location": location,
#                         "company": company,
#                         "dispensary_location": disp,
#                         "name": name,
#                         "category": category,
#                         "subcategory": subcategory,
#                         "brand": brand,
#                         "strain_type": strain_type,
#                         "THC": thc,
#                         "CBD": cbd,
#                         "weight": weight,
#                         "original_price": original_price,
#                     }
#                     for i, d in enumerate(deals):
#                         row[f"deal_{i+1}"] = d
#                     products.append(row)
#             except Exception as e:
#                 logger.warning(f"  product parse skipped: {e}")
#                 continue

#     logger.info(f"  +{len(products) - n_before} rows in "
#                 f"{time.monotonic() - t_store:.1f}s")

# logger.info(f"Total scrape time: {time.monotonic() - t0:.1f}s")


# # === SAVE JSON ==============================================================

# try:
#     with open(json_file_path, "w", encoding="utf-8") as f:
#         json.dump({"rows": products, "raw_payloads": raw_payloads}, f,
#                   indent=2, default=str)
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
Ethos Cannabis (Pennsylvania) -- FAST scraper.

Ethos runs on Sweed's white-label storefront (same platform as Curaleaf), so
the store URL

    https://ethoscannabis.com/stores/pennsylvania/<slug>

returns SSR HTML with the ENTIRE catalog embedded as JSON via multiple
`"products":[...]` blocks (one per category grouping). Same extractor pattern
as Curaleaf.

    6 PA stores * 1 HTTP GET each * ~2s = ~15 seconds total.
"""
import json
import logging
import os
import sys
import time
from datetime import datetime

import pandas as pd
import pytz


try:
    from curl_cffi import requests as httpx_requests
    _IMPERSONATE_KW = {"impersonate": "chrome124"}
except ImportError:
    import requests as httpx_requests
    _IMPERSONATE_KW = {}


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)


# === CONFIG =================================================================

company             = "Ethos"
location            = "Pennsylvania"
PRICING_TYPE        = "rec"
TIMEZONE            = "America/New_York"

# 6 PA stores from ethoscannabis.com/dispensaries/
stores = [
    {"slug": "allentown",                          "dispensary_location": "Allentown"},
    {"slug": "northeast-philadelphia",             "dispensary_location": "Northeast Philadelphia"},
    {"slug": "pittsburgh-north-at-harmarville",    "dispensary_location": "Pittsburgh North (Harmarville)"},
    {"slug": "pittsburgh-south-at-pleasant-hills", "dispensary_location": "Pittsburgh South (Pleasant Hills)"},
    {"slug": "pittsburgh-west-at-north-fayette",   "dispensary_location": "Pittsburgh West (North Fayette)"},
    {"slug": "wilkes-barre",                       "dispensary_location": "Wilkes-Barre"},
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

BASE_URL = "https://ethoscannabis.com/stores/pennsylvania"

COOKIES = {
    "user_state": "pennsylvania",
    "state": "pennsylvania",
    "age-gate": "true",
    "age_verified": "true",
    "ageVerified": "true",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}


# === HELPERS ================================================================

_PRODUCTS_KEY = '"products":['


def extract_products_arrays(html):
    """Walk each `"products":[...]` block by bracket depth, yielding parsed
    arrays. Handles quoted strings and escapes."""
    idx = 0
    while True:
        start = html.find(_PRODUCTS_KEY, idx)
        if start == -1:
            return
        bracket_start = start + len(_PRODUCTS_KEY) - 1
        depth = 0
        in_str = False
        escape = False
        end = -1
        for i in range(bracket_start, len(html)):
            c = html[i]
            if escape:
                escape = False
                continue
            if c == "\\":
                escape = True
                continue
            if c == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end == -1:
            return
        blob = html[bracket_start : end + 1]
        try:
            arr = json.loads(blob)
            if isinstance(arr, list):
                yield arr
        except json.JSONDecodeError:
            pass
        idx = end + 1


def format_potency(labtest):
    if not isinstance(labtest, dict):
        return ""
    vals = labtest.get("value")
    unit = labtest.get("unitAbbr") or "%"
    if isinstance(vals, list) and vals:
        nums = [x for x in vals if isinstance(x, (int, float)) and x > 0]
        if nums:
            return f"{max(nums):g}{unit}"
    if isinstance(vals, (int, float)) and vals > 0:
        return f"{float(vals):g}{unit}"
    return ""


def resolve_weight(variant):
    if not isinstance(variant, dict):
        return ""
    n = variant.get("name")
    if n:
        return str(n).strip()
    us = variant.get("unitSize") or {}
    if isinstance(us, dict) and us.get("value"):
        v, u = us.get("value"), us.get("unitAbbr") or ""
        try:
            return f"{float(v):g}{u.lower()}"
        except (TypeError, ValueError):
            return f"{v}{u.lower()}"
    return ""


def collect_deals(product, variant):
    deals = []
    seen = set()
    for src in (product, variant):
        if not isinstance(src, dict):
            continue
        for promo in (src.get("promos") or []):
            if isinstance(promo, dict):
                nm = (promo.get("shortName") or promo.get("name") or "").strip()
                if nm and nm not in seen:
                    seen.add(nm)
                    deals.append(nm)
    return deals


# === MAIN ===================================================================

products = []
seen_keys = set()
raw_payloads = []

logger.info(f"Starting Ethos PA fast scraper ({len(stores)} stores)")
t0 = time.monotonic()

for store in stores:
    slug = store["slug"]
    disp = store["dispensary_location"]
    url = f"{BASE_URL}/{slug}"
    t_store = time.monotonic()
    logger.info(f"--- {disp} ({slug}) ---")

    try:
        r = httpx_requests.get(url, headers=HEADERS, cookies=COOKIES,
                                timeout=45, allow_redirects=True,
                                **_IMPERSONATE_KW)
        if r.status_code >= 400:
            logger.warning(f"  HTTP {r.status_code}, skipping")
            continue
        html = r.text
    except Exception as e:
        logger.warning(f"  fetch failed: {e}")
        continue

    final_url = str(getattr(r, "url", url))
    products_hits = html.count('"products":[')
    logger.info(f"  HTTP {r.status_code}, {len(html)} bytes, "
                f"products-key hits={products_hits}")

    n_before = len(products)
    for arr in extract_products_arrays(html):
        raw_payloads.append({"store": disp, "count": len(arr)})
        for prod in arr:
            if not isinstance(prod, dict):
                continue
            try:
                brand_obj = prod.get("brand") or {}
                brand = (brand_obj.get("name") if isinstance(brand_obj, dict)
                         else str(brand_obj or "")).strip()
                name = (prod.get("name") or "").strip()
                cat_obj = prod.get("category") or {}
                category = (cat_obj.get("name") if isinstance(cat_obj, dict)
                            else str(cat_obj or "")).strip()
                subcat_obj = prod.get("subcategory") or {}
                subcategory = (subcat_obj.get("name") if isinstance(subcat_obj, dict)
                               else str(subcat_obj or "")).strip()
                subcategory = subcategory.upper().replace(" ", "-") if subcategory else "DEFAULT"
                strain_obj = prod.get("strain") or {}
                prev = strain_obj.get("prevalence") if isinstance(strain_obj, dict) else None
                strain_type = (prev.get("name") if isinstance(prev, dict) else "") or ""
                pid = prod.get("id") or name

                variants = prod.get("variants") or [None]

                for v in variants:
                    if isinstance(v, dict):
                        weight = resolve_weight(v)
                        lab = v.get("labTests") or {}
                        thc = format_potency(lab.get("thc") if isinstance(lab, dict) else None)
                        cbd = format_potency(lab.get("cbd") if isinstance(lab, dict) else None)
                        # original_price = strike-through / regular list price.
                        base = v.get("price")
                        promo = v.get("promoPrice")
                        original_price = base if base is not None else promo
                        deals = collect_deals(prod, v)
                    else:
                        weight = ""
                        thc = cbd = ""
                        original_price = prod.get("price")
                        deals = collect_deals(prod, {})

                    dedupe_key = (disp, pid, weight)
                    if dedupe_key in seen_keys:
                        continue
                    seen_keys.add(dedupe_key)

                    row = {
                        "date": today,
                        "location": location,
                        "company": company,
                        "dispensary_location": disp,
                        "name": name,
                        "category": category,
                        "subcategory": subcategory,
                        "brand": brand,
                        "strain_type": strain_type,
                        "THC": thc,
                        "CBD": cbd,
                        "weight": weight,
                        "original_price": original_price,
                    }
                    for i, d in enumerate(deals):
                        row[f"deal_{i+1}"] = d
                    products.append(row)
            except Exception as e:
                logger.warning(f"  product parse skipped: {e}")
                continue

    logger.info(f"  +{len(products) - n_before} rows in "
                f"{time.monotonic() - t_store:.1f}s")

logger.info(f"Total scrape time: {time.monotonic() - t0:.1f}s")


# === SAVE JSON ==============================================================

try:
    with open(json_file_path, "w", encoding="utf-8") as f:
        json.dump({"rows": products, "raw_payloads": raw_payloads}, f,
                  indent=2, default=str)
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