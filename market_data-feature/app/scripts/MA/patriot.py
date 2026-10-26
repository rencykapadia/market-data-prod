import pandas as pd
import json
import os
import time
from datetime import datetime
import pytz
from playwright.sync_api import sync_playwright
import logging
import sys

# =========================
# LOGGER
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger(__name__)

# =========================
# CONFIG
# =========================
# PLATFORM: Patriot Care (Cannabist) runs the iHeartJane ("Jane") headless menu,
# served directly on patriotcare.org (NOT Cloudflare-walled). Products render as
# React components. This script opens each category page in Playwright, scrolls to
# load the full catalog, and harvests the *structured* Jane/Algolia product objects
# straight off the React fibers of the rendered cards — clean fields, no text
# parsing, no API key. One row per available weight/variant. Only the ADULT-USE
# (recreational) store is included; the medical store (731) is excluded.
# Run: pip install playwright pandas pytz && playwright install chromium
company  = "PatriotCare"
location = "Massachusetts"

us_tz = pytz.timezone("America/Phoenix")
today = datetime.now(us_tz).strftime("%Y-%m-%d")

output_folder = "app/output/pending"
csv_folder = os.path.join(output_folder, "csv")
json_folder = os.path.join(output_folder, "json")
os.makedirs(csv_folder, exist_ok=True)
os.makedirs(json_folder, exist_ok=True)

output_file = os.path.join(csv_folder, f"{today}_{company}.csv")
json_file_path = os.path.join(json_folder, f"{today}_{company}.json")

HEADLESS = False  # Jane menu needs a real (headful) window to render            # if a store returns very few products, set False (a real
                           # window scrolls/loads the Jane menu most reliably)
MAX_SCROLLS = 150
STABLE_LIMIT = 12          # stop after this many scrolls with no new products
NAV_TIMEOUT = 90_000

# MASSACHUSETTS RECREATIONAL STORES (Jane store_id). Greenfield is the only MA
# Patriot Care location; 732 = Adult-Use (731 = Medical, excluded).
stores = [
    {"store_id": 732, "dispensary_location": "Greenfield"},
]
STORE_URL = "https://www.patriotcare.org/shop/store/{store_id}/{category}"
CATEGORIES = ["flower", "vape", "pre-roll", "edible", "extract",
              "tincture", "topical", "gear", "bundles"]

# Jane weight label -> (price field suffix, affinity weight string)
WEIGHT_MAP = {
    "half gram":     ("half_gram",     "0.5g"),
    "gram":          ("gram",          "1g"),
    "two gram":      ("two_gram",      "2g"),
    "eighth ounce":  ("eighth_ounce",  "3.5g"),
    "quarter ounce": ("quarter_ounce", "7g"),
    "half ounce":    ("half_ounce",    "14g"),
    "ounce":         ("ounce",         "28g"),
    "each":          ("each",          "each"),
}

# =========================
# GLOBAL STORAGE
# =========================
products = []
all_json = []
seen_keys = set()

# JS injected each scroll step: walk the whole tree (incl. shadow DOM), pull the
# Algolia product object off each product card's React fiber, and accumulate into
# window.__acc keyed by product id (survives Jane's list virtualization). Returns
# the slimmed objects collected so far.
HARVEST_JS = r"""
() => {
  if (!window.__acc) window.__acc = {};
  function deep(root, acc){ acc = acc || [];
    (root.querySelectorAll ? root.querySelectorAll('*') : []).forEach(el => {
      acc.push(el); if (el.shadowRoot) deep(el.shadowRoot, acc); });
    return acc; }
  function fiberKey(el){ return Object.keys(el).find(k => k.startsWith('__reactFiber$')); }
  function getProd(el){
    const fk = fiberKey(el); if (!fk) return null;
    let f = el[fk], h = 0;
    while (f && h < 30){
      const mp = f.memoizedProps;
      if (mp){ for (const v of [mp.product, mp.menuProduct, mp.hit, mp]){
        if (v && typeof v === 'object' && v.store_id && (v.bucket_price !== undefined || v.kind)) return v; } }
      f = f.return; h++;
    }
    return null;
  }
  const FIELDS = ['id','store_id','name','brand','brand_subtype','kind','type','category','root_subtype',
    'amount','available_weights','bucket_price','price_each','price_half_gram','price_gram','price_two_gram',
    'price_eighth_ounce','price_quarter_ounce','price_half_ounce','price_ounce',
    'special_price_each','special_price_half_gram','special_price_gram','special_price_two_gram',
    'special_price_eighth_ounce','special_price_quarter_ounce','special_price_half_ounce','special_price_ounce',
    'primaryPercentTHC','primaryPercentCBD','percentTAC','applicable_special_ids'];
  deep(document)
    .filter(el => el.tagName === 'A' && /\/products?\/\d+/.test((el.getAttribute && el.getAttribute('href')) || ''))
    .forEach(a => {
      const p = getProd(a);
      if (p && p.id != null && !window.__acc[p.id]) {
        const o = {}; FIELDS.forEach(k => { if (k in p) o[k] = p[k]; }); window.__acc[p.id] = o;
      }
    });
  return Object.values(window.__acc);
}
"""

# Click through any age-gate / consent / "enter" prompt that blocks a fresh browser
# (shadow-DOM aware). Harmless if none is present.
DISMISS_JS = r"""
() => {
  function deep(root, acc){ acc = acc || [];
    (root.querySelectorAll ? root.querySelectorAll('*') : []).forEach(el => {
      acc.push(el); if (el.shadowRoot) deep(el.shadowRoot, acc); });
    return acc; }
  const re = /^(yes|i am 21|i'm 21|21\\+|21 or older|over 21|enter|confirm|i agree|agree|accept|continue|adult[- ]?use|recreational|i am over|yes,? i)/i;
  let clicked = false;
  for (const el of deep(document)) {
    const tag = el.tagName;
    if (tag === 'BUTTON' || tag === 'A' || (el.getAttribute && el.getAttribute('role') === 'button')) {
      const t = (el.textContent || '').trim();
      if (t && t.length < 28 && re.test(t)) { try { el.click(); clicked = true; } catch (e) {} }
    }
  }
  return clicked;
}
"""


# =========================
# HELPERS
# =========================
def _pct(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return ""
    return f"{round(v, 2)}%" if v else ""


def _special_price(val):
    """special_price_<wt> is {} when none, else a dict/number holding the deal price."""
    if isinstance(val, (int, float)) and val:
        return float(val)
    if isinstance(val, dict) and val:
        for k in ("price", "discounted_price", "amount", "value"):
            if isinstance(val.get(k), (int, float)):
                return float(val[k])
        for v in val.values():
            if isinstance(v, (int, float)):
                return float(v)
    return None


def variants_for(p):
    """Yield (weight_string, price, special_price) per available weight."""
    weights = p.get("available_weights") or []
    if not weights:
        wt = p.get("amount") or "each"
        price = p.get("price_each")
        if price is None:
            price = p.get("bucket_price")
        yield (wt, price, _special_price(p.get("special_price_each")))
        return
    for w in weights:
        suffix, label = WEIGHT_MAP.get(w, (w.replace(" ", "_"), w))
        price = p.get(f"price_{suffix}")
        if price is None:
            price = p.get("bucket_price")
        yield (label, price, _special_price(p.get(f"special_price_{suffix}")))


def handle_products(raw, store):
    for p in raw:
        name = (p.get("name") or "").strip()
        if not name:
            continue
        brand = p.get("brand") or ""
        category = p.get("kind") or ""                      # flower / vape / edible / pre-roll
        subcat = p.get("root_subtype") or p.get("brand_subtype") or ""
        strain = p.get("category") or p.get("type") or ""   # hybrid / indica / sativa
        if strain:
            strain = str(strain).title()
        thc = _pct(p.get("primaryPercentTHC"))
        cbd = _pct(p.get("primaryPercentCBD"))
        has_special_ids = bool(p.get("applicable_special_ids"))

        for weight, price, special in variants_for(p):
            key = (store["dispensary_location"], name, weight)
            if key in seen_keys:
                continue
            seen_keys.add(key)

            on_sale = (special is not None and price is not None and special < price) or has_special_ids
            deals = ["On Sale"] if on_sale else []
            deal_columns = {f"deal_{i+1}": d for i, d in enumerate(deals)}

            product = {
                "date": today,
                "location": location,
                "company": company,
                "dispensary_location": store["dispensary_location"],
                "name": name,
                "category": str(category).title(),
                "subcategory": subcat,
                "brand": brand,
                "strain_type": strain,
                "THC": thc,
                "CBD": cbd,
                "weight": weight,
                "original_price": price,
            }
            product.update(deal_columns)
            products.append(product)


def _scroll_harvest(page):
    """Scroll the current category page until no new products appear (accumulates
    into window.__acc, shared across categories so ids de-dupe automatically)."""
    count = 0
    for _ in range(20):
        try:
            count = len(page.evaluate(HARVEST_JS) or [])
        except Exception:
            count = 0
        if count > 0:
            break
        page.wait_for_timeout(1000)

    prev, stable = count, 0
    for i in range(MAX_SCROLLS):
        page.mouse.wheel(0, 2600)
        page.wait_for_timeout(650)
        try:
            count = len(page.evaluate(HARVEST_JS) or [])
        except Exception:
            continue
        if count == prev:
            stable += 1
        else:
            stable = 0
            prev = count
        if stable >= STABLE_LIMIT:
            break


def scrape_store(page, store):
    page.evaluate("() => { window.__acc = {}; }")
    for category in CATEGORIES:
        url = STORE_URL.format(store_id=store["store_id"], category=category)
        logger.info("  category: %s", category)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
        except Exception as e:                                  # noqa: BLE001
            logger.warning("    skip %s (%s)", category, e)
            continue
        page.wait_for_timeout(4000)
        try:
            if page.evaluate(DISMISS_JS):
                logger.info("    dismissed an age-gate / consent prompt")
                page.wait_for_timeout(3000)
        except Exception:
            pass
        _scroll_harvest(page)
        if not page.evaluate("() => Object.keys(window.__acc || {}).length"):
            try:
                logger.warning("    no products yet on %s — page shows: %s", category,
                               page.evaluate("() => document.body.innerText.slice(0,160)"))
            except Exception:
                pass

    raw = page.evaluate("() => Object.values(window.__acc || {})")
    logger.info("  harvested %d unique products", len(raw))
    return raw


# =========================
# SCRAPE
# =========================
try:
    logger.info(f"Starting {company} scrape | stores={len(stores)}")
    with sync_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        
        # 🔥 FIX: Pass a real-world User-Agent
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            viewport={'width': 1920, 'height': 1080}
        )
        
        page = await context.new_page()

        for store in stores:
            logger.info(f"=== {store['dispensary_location']} (store_id={store['store_id']}) ===")
            before = len(products)
            raw = scrape_store(page, store)
            all_json.append({"dispensary_location": store["dispensary_location"],
                             "store_id": store["store_id"], "count": len(raw), "products": raw})
            handle_products(raw, store)
            logger.info(f"  => {store['dispensary_location']}: {len(products) - before} product rows")

        browser.close()

except Exception as e:
    logger.critical(f"❌ Playwright failed: {e}", exc_info=True)
    sys.exit(1)


# =========================
# SAVE JSON
# =========================
try:
    if not all_json:
        logger.critical("❌ No JSON data collected")
        sys.exit(1)

    with open(json_file_path, "w", encoding="utf-8") as f:
        json.dump(products, f, indent=2)

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

    df.to_csv(output_file, index=False, na_rep="")

    logger.info(f"CSV saved: {output_file} | Rows: {len(df)}")

except Exception as e:
    logger.critical(f"❌ CSV save failed: {e}", exc_info=True)
    sys.exit(1)


# =========================
# SUCCESS
# =========================
logger.info("✅ PatriotCare scraper completed successfully")
sys.exit(0)