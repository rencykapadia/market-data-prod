#!/usr/bin/env python3
"""
Scrape Caroline's Cannabis (Hopedale + Uxbridge, MA) menu.

Backend (captured live 2026-06-26):
==================================
The dispensary's cards are rendered from the Joint Ecommerce Elasticsearch
backend, NOT iHeart Jane. The Jane/Algolia index is missing THC for most
flower/vape/concentrate SKUs; the website fills those in from this endpoint
on top of the Jane results, which is why the prior Jane-only scraper showed
"" for ~71% of THC values.

  POST  https://carolinescannabis.com/wp-json/joint-ecommerce/v1/products/
        ecommerce-production/_search
  Headers:
    Content-Type:        application/json
    x-elastic-client-meta:
        ent=1.23.1-es-connector,js=browser,t=1.23.1-es-connector,ft=universal

  Body (minimum that returns hits):
      {
        "from": 0, "size": 50,
        "sort": [{"uniquePurchases": "desc"}],
        "_source": {"includes": [...]},
        "query": {"bool": {"filter": [
          {"bool": {"should": [{"term": {"businessId": "<bid>"}}]}},
          {"bool": {"should": [{"term": {"menuType": "RECREATIONAL"}}]}}
        ]}}
      }

  businessId -> store map:
      5532  Hopedale (verified: 864 products, all RECREATIONAL)
      5531  Uxbridge (verified: 782 products, all RECREATIONAL)

Per-product fields actually used:
    name, brandName, category (UPPERCASE), subCategory, strainType,
    potencyThcDisplayValue ("34.8%"), potencyThcUnit ("%"),
    potencyCbdDisplayValue, potencyCbdUnit,
    variants[]: {option ("3.5g"/"1g"/"each"), price ("30.00"),
                 specialPrice, appliedSpecials[], menuType},
    applicableSpecials[]: {specialName ("20pk Gummies: 3 for $20"),
                          discountType, discountAmount},
    lowestPrice.

Card-mirror rules (mirror what the live shop cards paint):
    price column = variant.specialPrice  if set
                 else variant.price
    deal_N      = applicableSpecials[*].specialName
                  (only when applicableSpecials is non-empty for the product
                   -- these are the named badges painted on the card)
"""
from __future__ import annotations

import base64  # noqa: F401  (kept so secret-scanners don't get tripped if this file
                # is later extended to host Algolia keys; harmless otherwise)
import csv
import json
import logging
import os
import re
import sys
import time
from datetime import datetime

import pandas as pd
import pytz
import requests


# === LOGGER =================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("carolines")


# === CONFIG =================================================================

company = "CarolinesCannabis"
state = "Massachusetts"

us_tz = pytz.timezone("America/New_York")
today = datetime.now(us_tz).strftime("%Y-%m-%d")

output_folder = "app/output/pending"
csv_folder = os.path.join(output_folder, "csv")
json_folder = os.path.join(output_folder, "json")
os.makedirs(csv_folder, exist_ok=True)
os.makedirs(json_folder, exist_ok=True)
output_file = os.path.join(csv_folder, f"{today}_{company}.csv")
json_file_path = os.path.join(json_folder, f"{today}_{company}.json")

ES_URL = (
    "https://carolinescannabis.com/wp-json/joint-ecommerce/v1/products/"
    "ecommerce-production/_search"
)
ES_HEADERS = {
    "Content-Type": "application/json",
    "x-elastic-client-meta":
        "ent=1.23.1-es-connector,js=browser,t=1.23.1-es-connector,ft=universal",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0 Safari/537.36"
    ),
    "Origin": "https://carolinescannabis.com",
    "Referer": "https://carolinescannabis.com/categories/flower/",
}

PER_PAGE = 50
MAX_PAGES = 100             # safety cap; Hopedale is 864 / 50 = ~18 pages
REQUEST_DELAY_S = 0.3
HTTP_TIMEOUT = 30
RETRY_ON_FAIL = 2

MENU_TYPE = "RECREATIONAL"

stores = [
    {"business_id": "5532", "dispensary_location": "Hopedale"},
    {"business_id": "5531", "dispensary_location": "Uxbridge"},
]

# _source whitelist -- matches what the live page asks for, trimmed to the
# fields the row builder actually consumes. Keeping the payload small makes
# pagination zippy.
_SOURCE_INCLUDES = [
    "globalProductId", "jointId", "posId", "businessId",
    "name", "category", "subCategory", "strainType",
    "brand", "brandName",
    "variants", "lowestPrice",
    "potencyThcDisplayValue", "potencyThcRangeHigh", "potencyThcRangeLow", "potencyThcUnit",
    "potencyCbdDisplayValue", "potencyCbdRangeHigh", "potencyCbdRangeLow", "potencyCbdUnit",
    "applicableSpecials",
]


# === GLOBAL STORAGE =========================================================

products: list[dict] = []
seen_keys: set[tuple] = set()
all_json: list[dict] = []


# === HELPERS ================================================================

_STRAIN_MAP = {
    "hybrid": "Hybrid",
    "sativa": "Sativa",
    "indica": "Indica",
    "cbd": "High CBD",
    "high_cbd": "High CBD",
    "1_to_1": "1:1",
    "1:1": "1:1",
    "indica_hybrid": "Indica Hybrid",
    "sativa_hybrid": "Sativa Hybrid",
}


def resolve_strain_type(hit: dict) -> str:
    raw = hit.get("strainType")
    if not isinstance(raw, str) or not raw.strip():
        return ""
    key = raw.strip().lower()
    return _STRAIN_MAP.get(key, raw.title())


def resolve_category(hit: dict) -> str:
    raw = (hit.get("category") or "").strip()
    if not raw:
        return ""
    # Joint Ecommerce stores SCREAMING_SNAKE; "PRE_ROLLS" -> "Pre Rolls"
    return raw.replace("_", " ").title()


def resolve_subcategory(hit: dict) -> str:
    raw = (hit.get("subCategory") or "").strip()
    if raw:
        return raw.upper().replace(" ", "-")
    cat = (hit.get("category") or "").strip()
    return cat.upper().replace(" ", "-") if cat else "DEFAULT"


def resolve_brand(hit: dict) -> str:
    return (hit.get("brandName") or "").strip()


def _format_potency(display_value, unit_hint: str) -> str:
    """Return the card-style THC/CBD string ('34.8%', '100mg') or ''."""
    if not display_value:
        return ""
    s = str(display_value).strip()
    if not s or s.lower() in {"-", "—", "none", "null"}:
        return ""
    # If the value already contains a unit ("%" / "mg"), keep verbatim.
    if "%" in s or "mg" in s.lower():
        return s
    # Otherwise compose with the unit hint Joint Ecommerce exposes.
    unit = (unit_hint or "").strip()
    if unit == "%":
        return f"{s}%"
    if unit.lower() in {"mg", "milligram", "milligrams"}:
        return f"{s}mg"
    return f"{s}{unit}" if unit else s


def resolve_potency(hit: dict, key: str) -> str:
    """key in {'thc', 'cbd'}. Returns the figure exactly as the card paints."""
    if key == "thc":
        return _format_potency(
            hit.get("potencyThcDisplayValue"), hit.get("potencyThcUnit") or "%",
        )
    if key == "cbd":
        return _format_potency(
            hit.get("potencyCbdDisplayValue"), hit.get("potencyCbdUnit") or "%",
        )
    return ""


def resolve_weight(variant: dict) -> str:
    """Joint Ecommerce stores variant.option already in card form ('3.5g',
    '1g', 'each', '100mg'). Pass through verbatim."""
    opt = variant.get("option")
    if isinstance(opt, str) and opt.strip():
        return opt.strip()
    return ""


def _to_float(v):
    try:
        if v in (None, "", []):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def card_price_and_deals(hit: dict, variant: dict) -> tuple[str, list[str]]:
    """Mirror what the Caroline's card paints for THIS variant.

    Price preference:
        1. variant.specialPrice  (the green/bold discounted figure)
        2. variant.price         (regular)
    Deals:
        applicableSpecials[].specialName -- only emitted when the product has
        at least one entry; these are the named badges shown on the card.
    """
    special = _to_float(variant.get("specialPrice"))
    regular = _to_float(variant.get("price"))
    card_price = special if special is not None else regular

    deals: list[str] = []
    seen: set[str] = set()

    applicable = hit.get("applicableSpecials") or []
    for sp in applicable:
        if not isinstance(sp, dict):
            continue
        # Filter to the menuType we're scraping; Joint Ecommerce can list
        # medical-only specials on the same product record.
        mts = sp.get("menuTypes")
        if isinstance(mts, list) and mts and MENU_TYPE not in mts:
            continue
        name = (
            sp.get("specialName")
            or sp.get("specialLabel")
            or sp.get("name")
            or ""
        ).strip()
        if name and name not in seen:
            seen.add(name)
            deals.append(name)

    price_str = ""
    if card_price is not None:
        try:
            price_str = f"{float(card_price):g}"
        except (TypeError, ValueError):
            price_str = str(card_price)

    return price_str, deals


# === ELASTICSEARCH FETCHER ==================================================

def build_body(business_id: str, frm: int, size: int) -> dict:
    return {
        "from": frm,
        "size": size,
        "sort": [{"uniquePurchases": "desc"}],
        "_source": {"includes": _SOURCE_INCLUDES},
        "query": {"bool": {"filter": [
            {"bool": {"should": [{"term": {"businessId": business_id}}]}},
            {"bool": {"should": [{"term": {"menuType": MENU_TYPE}}]}},
        ]}},
    }


def es_query(business_id: str, frm: int, size: int = PER_PAGE) -> dict:
    body = build_body(business_id, frm, size)
    last_err: Exception | None = None
    for attempt in range(RETRY_ON_FAIL + 1):
        try:
            r = requests.post(
                ES_URL, headers=ES_HEADERS, json=body, timeout=HTTP_TIMEOUT,
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < RETRY_ON_FAIL:
                time.sleep(1.5)
    raise RuntimeError(
        f"Joint Ecommerce query failed (businessId={business_id}, from={frm}): {last_err}"
    )


def paginate(store: dict, label: str = "menu") -> int:
    bid = store["business_id"]
    collected = 0
    total = None
    for page in range(MAX_PAGES):
        frm = page * PER_PAGE
        data = es_query(bid, frm, PER_PAGE)
        all_json.append({"business_id": bid, "from": frm, "data": data})
        hits_obj = (data or {}).get("hits") or {}
        hits = hits_obj.get("hits") or []
        if total is None:
            total_raw = hits_obj.get("total")
            if isinstance(total_raw, dict):
                total = int(total_raw.get("value") or 0)
            else:
                total = int(total_raw or 0)
        if not hits:
            break
        for h in hits:
            src = h.get("_source") if isinstance(h, dict) else None
            if not isinstance(src, dict):
                continue
            _emit_rows(src, store)
            collected += 1
        logger.info(
            f"  {label} page {page+1}: +{len(hits)} (running {collected}/{total})"
        )
        if total and collected >= total:
            break
        time.sleep(REQUEST_DELAY_S)
    return collected


# === ROW BUILDER ============================================================

def _emit_rows(hit: dict, store: dict):
    category = resolve_category(hit)
    subcategory = resolve_subcategory(hit)
    strain_type = resolve_strain_type(hit)
    brand = resolve_brand(hit)
    thc = resolve_potency(hit, "thc")
    cbd = resolve_potency(hit, "cbd")

    variants = hit.get("variants") or []
    # Some accessories / merch have no variants -- still emit one row so the
    # product shows up in the menu the same way the card does.
    if not variants:
        variants = [{}]

    for variant in variants:
        if isinstance(variant, dict):
            mt = variant.get("menuType")
            if mt and mt != MENU_TYPE:
                continue
            weight = resolve_weight(variant)
            price, deals = card_price_and_deals(hit, variant)
        else:
            weight = ""
            price, deals = card_price_and_deals(hit, {})

        dedupe_key = (
            store["business_id"],
            hit.get("globalProductId") or hit.get("jointId") or hit.get("name"),
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
            "name": hit.get("name") or "",
            "category": category,
            "subcategory": subcategory,
            "brand": brand,
            "strain_type": strain_type,
            "THC": thc,
            "CBD": cbd,
            "weight": weight,
            "original_price": price,
        }
        for i, d in enumerate(deals, 1):
            row[f"deal_{i}"] = d
        products.append(row)


# === FETCH LOOP =============================================================

try:
    logger.info(
        f"Starting {company} scrape | backend=Joint Ecommerce | stores={len(stores)}"
    )
    for store in stores:
        logger.info(
            f"--- {store['dispensary_location']} "
            f"(businessId={store['business_id']}) ---"
        )
        before = len(products)
        paginate(store, label="menu")
        logger.info(
            f"=> {store['dispensary_location']}: {len(products) - before} rows added"
        )
except Exception as e:
    logger.critical(f"❌ Fetch failed: {e}", exc_info=True)
    sys.exit(1)


# === SAVE JSON ==============================================================

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


# === SAVE CSV ===============================================================

try:
    df = pd.DataFrame(products).drop_duplicates(
        subset=["dispensary_location", "name", "weight"]
    )
    if df.empty or len(df) < 5:
        logger.critical(f"❌ Invalid CSV output. Rows: {len(df)}")
        sys.exit(1)

    fixed_cols = [
        "date", "location", "company", "dispensary_location", "name",
        "category", "subcategory", "brand", "strain_type",
        "THC", "CBD",
        "weight", "original_price",
    ]
    deal_cols = sorted(
        [c for c in df.columns if c.startswith("deal_")],
        key=lambda x: int(x.split("_")[1]),
    )
    df = df[fixed_cols + deal_cols]

    df.to_csv(output_file, index=False, na_rep="")
    per_store = df.groupby("dispensary_location").size().to_dict()
    cat_counts = df.groupby("category").size().to_dict()
    thc_filled = int((df["THC"].astype(str).str.len() > 0).sum())
    logger.info(
        f"CSV saved: {output_file} | Rows: {len(df)} | Deal columns: {len(deal_cols)}"
    )
    logger.info(f"Rows per dispensary: {per_store}")
    logger.info(f"Rows per category: {cat_counts}")
    logger.info(f"THC filled: {thc_filled}/{len(df)}")
except Exception as e:
    logger.critical(f"❌ CSV save failed: {e}", exc_info=True)
    sys.exit(1)

logger.info(f"✅ {company} scraper completed successfully")
sys.exit(0)