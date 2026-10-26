# #!/usr/bin/env python3
# """
# Trulieve PA -- FAST scraper.

# Trulieve exposes a clean JSON API at

#     https://api.trulieve.com/api/v2/menu/{store_id}/{category}/DEFAULT?page=N

# ...which is what the site itself queries under the hood. No Playwright, no
# DOM scraping, no age-gate clicking needed.

# Store IDs (the ones the API expects) were discovered by querying Trulieve's
# GraphQL storefinder and extracting the `?dutchieplus=<id>` query param from
# each store's `dutchie_embed_script_url`. Slugs stay stable but if Trulieve
# ever remaps IDs you can re-discover with the getPickupStores query.

#     18 stores * ~7-9 categories * ~5-9 pages/each = ~5 minutes total.
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

# company             = "Trulieve"
# location            = "Pennsylvania"
# PRICING_TYPE        = "rec"        # PA is medical-only today; label is rec-agnostic
# TIMEZONE            = "America/New_York"

# # All 18 PA stores with their api.trulieve.com store IDs (from
# # `dutchie_embed_script_url` -> `?dutchieplus=<id>` on each store record).
# stores = [
#     {"api_id":  71, "slug": "washington",                          "dispensary_location": "Washington"},
#     {"api_id":  74, "slug": "johnstown",                           "dispensary_location": "Johnstown"},
#     {"api_id":  76, "slug": "lancaster",                           "dispensary_location": "Lancaster"},
#     {"api_id":  79, "slug": "whitehall",                           "dispensary_location": "Whitehall"},
#     {"api_id":  80, "slug": "reading",                             "dispensary_location": "Reading (Lancaster Ave)"},
#     {"api_id":  81, "slug": "east-york",                           "dispensary_location": "York (East York)"},
#     {"api_id":  84, "slug": "reading-fifth",                       "dispensary_location": "Reading (5th Street)"},
#     {"api_id":  85, "slug": "philadelphia-washington-square-west", "dispensary_location": "Philadelphia (Washington Square)"},
#     {"api_id":  88, "slug": "camp-hill",                           "dispensary_location": "Camp Hill"},
#     {"api_id":  89, "slug": "harrisburg",                          "dispensary_location": "Harrisburg"},
#     {"api_id":  90, "slug": "pittsburgh-north-shore",              "dispensary_location": "Pittsburgh (North Shore)"},
#     {"api_id":  92, "slug": "coatesville",                         "dispensary_location": "Coatesville"},
#     {"api_id":  97, "slug": "wilkes-barre",                        "dispensary_location": "Wilkes-Barre"},
#     {"api_id": 104, "slug": "philadelphia-center-city",            "dispensary_location": "Philadelphia (Center City)"},
#     {"api_id": 106, "slug": "king-of-prussia",                     "dispensary_location": "King of Prussia (Henderson)"},
#     {"api_id": 107, "slug": "philadelphia",                        "dispensary_location": "Philadelphia"},
#     {"api_id": 108, "slug": "scranton",                            "dispensary_location": "Scranton"},
#     {"api_id": 109, "slug": "limerick",                            "dispensary_location": "Limerick"},
# ]

# CATEGORIES = [
#     "flower", "pre-rolls", "vapes", "concentrates",
#     "edibles", "tinctures", "topicals", "accessories", "cbd",
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

# HEADERS = {
#     "User-Agent": (
#         "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
#         "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
#     ),
#     "Accept": "application/json",
#     "Origin": "https://www.trulieve.com",
#     "Referer": "https://www.trulieve.com/",
# }


# # === HELPERS ================================================================


# def format_potency(value, unit):
#     if value is None or value == 0:
#         return ""
#     return f"{value}{unit}" if unit else str(value)


# def extract_deals(item):
#     seen = set()
#     out = []
#     for s in (item.get("specials") or []):
#         name = (s.get("name") or "").strip()
#         if name and name not in seen:
#             seen.add(name)
#             out.append(name)
#     return out


# def normalize_category(cat_slug):
#     m = {
#         "flower":       "Flower",
#         "pre-rolls":    "Pre-Roll",
#         "vapes":        "Vaporizers",
#         "concentrates": "Concentrate",
#         "edibles":      "Edible",
#         "tinctures":    "Tincture",
#         "topicals":     "Topical",
#         "accessories":  "Accessories",
#         "cbd":          "CBD",
#     }
#     return m.get(cat_slug, cat_slug.title())


# def normalize_strain(raw):
#     if not raw:
#         return ""
#     r = str(raw).strip().upper().replace("-", " ").replace("_", " ")
#     if r in ("HYBRID", "SATIVA", "INDICA"):
#         return r.title()
#     if "SATIVA" in r:
#         return "Sativa"
#     if "INDICA" in r:
#         return "Indica"
#     if "HYBRID" in r:
#         return "Hybrid"
#     if "CBD" in r:
#         return "High CBD"
#     return raw.title()


# # === MAIN ===================================================================

# products = []
# seen_keys = set()
# all_json = []

# logger.info(f"Starting Trulieve PA fast scraper ({len(stores)} stores)")
# t0 = time.monotonic()

# for store in stores:
#     store_id = store["api_id"]
#     disp = store["dispensary_location"]
#     t_store = time.monotonic()
#     n_store_before = len(products)
#     logger.info(f"--- {disp} (api_id={store_id}) ---")

#     for cat_slug in CATEGORIES:
#         cat_label = normalize_category(cat_slug)
#         page = 1
#         last_page = 1
#         cat_rows_before = len(products)

#         while page <= last_page:
#             url = (f"https://api.trulieve.com/api/v2/menu/{store_id}/"
#                    f"{cat_slug}/DEFAULT?page={page}")
#             try:
#                 r = httpx_requests.get(url, headers=HEADERS, timeout=30,
#                                         **_IMPERSONATE_KW)
#             except Exception as e:
#                 logger.warning(f"  {cat_label} p{page} fetch failed: {e}")
#                 break

#             if r.status_code == 404:
#                 # Store doesn't stock this category -- normal, skip
#                 break
#             if r.status_code >= 400:
#                 logger.warning(f"  {cat_label} p{page} HTTP {r.status_code}")
#                 break

#             try:
#                 data = r.json()
#             except Exception as e:
#                 logger.warning(f"  {cat_label} p{page} JSON parse failed: {e}")
#                 break

#             all_json.append({"store_id": store_id, "cat": cat_slug,
#                              "page": page, "keys": list(data.keys())})
#             last_page = int(data.get("last_page") or 1)
#             items = data.get("data") or []
#             if not items:
#                 break

#             for item in items:
#                 try:
#                     deals = extract_deals(item)
#                     variants = item.get("variants") or [None]
#                     brand = (item.get("brand") or "").strip()
#                     name = item.get("name") or ""
#                     subcat = (item.get("subcategory") or "").strip()
#                     subcat = subcat.upper().replace(" ", "-") if subcat else "DEFAULT"
#                     strain_type = normalize_strain(item.get("strain_type"))
#                     thc = format_potency(item.get("thc_content"),
#                                           item.get("thc_content_unit"))
#                     cbd = format_potency(item.get("cbd_content"),
#                                           item.get("cbd_content_unit"))
#                     pid = item.get("product_id") or item.get("id") or name

#                     for v in variants:
#                         if isinstance(v, dict):
#                             weight = (v.get("option") or "").strip()
#                             sale_p = v.get("sale_unit_price")
#                             base_p = v.get("unit_price")
#                             price = (sale_p if sale_p not in (None, 0)
#                                      else base_p) or item.get("unit_price")
#                         else:
#                             weight = ""
#                             price = item.get("unit_price")

#                         dedupe_key = (disp, pid, weight)
#                         if dedupe_key in seen_keys:
#                             continue
#                         seen_keys.add(dedupe_key)

#                         row = {
#                             "date": today,
#                             "location": location,
#                             "company": company,
#                             "dispensary_location": disp,
#                             "name": name,
#                             "category": cat_label,
#                             "subcategory": subcat,
#                             "brand": brand,
#                             "strain_type": strain_type,
#                             "THC": thc,
#                             "CBD": cbd,
#                             "weight": weight,
#                             "original_price": price,
#                         }
#                         for i, d in enumerate(deals):
#                             row[f"deal_{i+1}"] = d
#                         products.append(row)
#                 except Exception as e:
#                     logger.warning(f"  {cat_label} item parse failed: {e}")
#                     continue

#             page += 1

#         added = len(products) - cat_rows_before
#         if added:
#             logger.info(f"    {cat_label}: +{added} rows ({last_page} pages)")

#     logger.info(f"  store total: +{len(products) - n_store_before} rows in "
#                 f"{time.monotonic() - t_store:.1f}s")

# logger.info(f"Total scrape time: {time.monotonic() - t0:.1f}s")


# # === SAVE JSON ==============================================================

# try:
#     with open(json_file_path, "w", encoding="utf-8") as f:
#         json.dump({"rows": products, "raw_payloads": all_json}, f,
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
Trulieve PA -- FAST scraper.

Trulieve exposes a clean JSON API at

    https://api.trulieve.com/api/v2/menu/{store_id}/{category}/DEFAULT?page=N

...which is what the site itself queries under the hood. No Playwright, no
DOM scraping, no age-gate clicking needed.

Store IDs (the ones the API expects) were discovered by querying Trulieve's
GraphQL storefinder and extracting the `?dutchieplus=<id>` query param from
each store's `dutchie_embed_script_url`. Slugs stay stable but if Trulieve
ever remaps IDs you can re-discover with the getPickupStores query.

    18 stores * ~7-9 categories * ~5-9 pages/each = ~5 minutes total.
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

company             = "Trulieve"
location            = "Pennsylvania"
PRICING_TYPE        = "rec"        # PA is medical-only today; label is rec-agnostic
TIMEZONE            = "America/New_York"

# All 18 PA stores with their api.trulieve.com store IDs (from
# `dutchie_embed_script_url` -> `?dutchieplus=<id>` on each store record).
stores = [
    {"api_id":  71, "slug": "washington",                          "dispensary_location": "Washington"},
    {"api_id":  74, "slug": "johnstown",                           "dispensary_location": "Johnstown"},
    {"api_id":  76, "slug": "lancaster",                           "dispensary_location": "Lancaster"},
    {"api_id":  79, "slug": "whitehall",                           "dispensary_location": "Whitehall"},
    {"api_id":  80, "slug": "reading",                             "dispensary_location": "Reading (Lancaster Ave)"},
    {"api_id":  81, "slug": "east-york",                           "dispensary_location": "York (East York)"},
    {"api_id":  84, "slug": "reading-fifth",                       "dispensary_location": "Reading (5th Street)"},
    {"api_id":  85, "slug": "philadelphia-washington-square-west", "dispensary_location": "Philadelphia (Washington Square)"},
    {"api_id":  88, "slug": "camp-hill",                           "dispensary_location": "Camp Hill"},
    {"api_id":  89, "slug": "harrisburg",                          "dispensary_location": "Harrisburg"},
    {"api_id":  90, "slug": "pittsburgh-north-shore",              "dispensary_location": "Pittsburgh (North Shore)"},
    {"api_id":  92, "slug": "coatesville",                         "dispensary_location": "Coatesville"},
    {"api_id":  97, "slug": "wilkes-barre",                        "dispensary_location": "Wilkes-Barre"},
    {"api_id": 104, "slug": "philadelphia-center-city",            "dispensary_location": "Philadelphia (Center City)"},
    {"api_id": 106, "slug": "king-of-prussia",                     "dispensary_location": "King of Prussia (Henderson)"},
    {"api_id": 107, "slug": "philadelphia",                        "dispensary_location": "Philadelphia"},
    {"api_id": 108, "slug": "scranton",                            "dispensary_location": "Scranton"},
    {"api_id": 109, "slug": "limerick",                            "dispensary_location": "Limerick"},
]

CATEGORIES = [
    "flower", "pre-rolls", "vapes", "concentrates",
    "edibles", "tinctures", "topicals", "accessories", "cbd",
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

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Origin": "https://www.trulieve.com",
    "Referer": "https://www.trulieve.com/",
}


# === HELPERS ================================================================


def format_potency(value, unit):
    if value is None or value == 0:
        return ""
    return f"{value}{unit}" if unit else str(value)


def extract_deals(item):
    seen = set()
    out = []
    for s in (item.get("specials") or []):
        name = (s.get("name") or "").strip()
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def normalize_category(cat_slug):
    m = {
        "flower":       "Flower",
        "pre-rolls":    "Pre-Roll",
        "vapes":        "Vaporizers",
        "concentrates": "Concentrate",
        "edibles":      "Edible",
        "tinctures":    "Tincture",
        "topicals":     "Topical",
        "accessories":  "Accessories",
        "cbd":          "CBD",
    }
    return m.get(cat_slug, cat_slug.title())


def normalize_strain(raw):
    if not raw:
        return ""
    r = str(raw).strip().upper().replace("-", " ").replace("_", " ")
    if r in ("HYBRID", "SATIVA", "INDICA"):
        return r.title()
    if "SATIVA" in r:
        return "Sativa"
    if "INDICA" in r:
        return "Indica"
    if "HYBRID" in r:
        return "Hybrid"
    if "CBD" in r:
        return "High CBD"
    return raw.title()


# === MAIN ===================================================================

products = []
seen_keys = set()
all_json = []

logger.info(f"Starting Trulieve PA fast scraper ({len(stores)} stores)")
t0 = time.monotonic()

for store in stores:
    store_id = store["api_id"]
    disp = store["dispensary_location"]
    t_store = time.monotonic()
    n_store_before = len(products)
    logger.info(f"--- {disp} (api_id={store_id}) ---")

    for cat_slug in CATEGORIES:
        cat_label = normalize_category(cat_slug)
        page = 1
        last_page = 1
        cat_rows_before = len(products)

        while page <= last_page:
            url = (f"https://api.trulieve.com/api/v2/menu/{store_id}/"
                   f"{cat_slug}/DEFAULT?page={page}")
            try:
                r = httpx_requests.get(url, headers=HEADERS, timeout=30,
                                        **_IMPERSONATE_KW)
            except Exception as e:
                logger.warning(f"  {cat_label} p{page} fetch failed: {e}")
                break

            if r.status_code == 404:
                # Store doesn't stock this category -- normal, skip
                break
            if r.status_code >= 400:
                logger.warning(f"  {cat_label} p{page} HTTP {r.status_code}")
                break

            try:
                data = r.json()
            except Exception as e:
                logger.warning(f"  {cat_label} p{page} JSON parse failed: {e}")
                break

            all_json.append({"store_id": store_id, "cat": cat_slug,
                             "page": page, "keys": list(data.keys())})
            last_page = int(data.get("last_page") or 1)
            items = data.get("data") or []
            if not items:
                break

            for item in items:
                try:
                    deals = extract_deals(item)
                    variants = item.get("variants") or [None]
                    brand = (item.get("brand") or "").strip()
                    name = item.get("name") or ""
                    subcat = (item.get("subcategory") or "").strip()
                    subcat = subcat.upper().replace(" ", "-") if subcat else "DEFAULT"
                    strain_type = normalize_strain(item.get("strain_type"))
                    thc = format_potency(item.get("thc_content"),
                                          item.get("thc_content_unit"))
                    cbd = format_potency(item.get("cbd_content"),
                                          item.get("cbd_content_unit"))
                    pid = item.get("product_id") or item.get("id") or name

                    for v in variants:
                        if isinstance(v, dict):
                            weight = (v.get("option") or "").strip()
                            # original_price = strike-through / regular price.
                            # unit_price is the list price; sale_unit_price is
                            # the current sale price (only used as fallback).
                            base_p = v.get("unit_price")
                            sale_p = v.get("sale_unit_price")
                            price = (base_p if base_p not in (None, 0)
                                     else sale_p) or item.get("unit_price")
                        else:
                            weight = ""
                            price = item.get("unit_price")

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
                            "category": cat_label,
                            "subcategory": subcat,
                            "brand": brand,
                            "strain_type": strain_type,
                            "THC": thc,
                            "CBD": cbd,
                            "weight": weight,
                            "original_price": price,
                        }
                        for i, d in enumerate(deals):
                            row[f"deal_{i+1}"] = d
                        products.append(row)
                except Exception as e:
                    logger.warning(f"  {cat_label} item parse failed: {e}")
                    continue

            page += 1

        added = len(products) - cat_rows_before
        if added:
            logger.info(f"    {cat_label}: +{added} rows ({last_page} pages)")

    logger.info(f"  store total: +{len(products) - n_store_before} rows in "
                f"{time.monotonic() - t_store:.1f}s")

logger.info(f"Total scrape time: {time.monotonic() - t0:.1f}s")


# === SAVE JSON ==============================================================

try:
    with open(json_file_path, "w", encoding="utf-8") as f:
        json.dump({"rows": products, "raw_payloads": all_json}, f,
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