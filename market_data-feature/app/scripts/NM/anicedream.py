import pandas as pd
import json
import os
import re
from datetime import datetime
import pytz
import logging
import sys
from playwright.sync_api import sync_playwright

# =========================
# LOGGER
# =========================
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

# =========================
# CONFIG
# =========================
# PLATFORM: A Nice Dream (2506 San Mateo Pl NE, Albuquerque) runs a WEEDMAPS white-label storefront
# at  https://anicedreamdispensary.wm.store/  (iframed from anicedream.org/shop/). Its menu is served
# by  https://api-g.weedmaps.com/discovery/v1/listings/dispensaries/anicedream/menu_items  (the API
# listing slug is "anicedream", NOT the wm.store subdomain). We warm up the storefront in a real
# browser (Cloudflare clearance) and fetch the paginated menu_items from that session.
# NOTE on this store's data: the API carries NO brand field and NO per-weight variants (every price
# label is "each"), so we derive both from the product NAME —
#   • brand  = matched against the store's brand list (see BRANDS below); blank if none present.
#   • weight = the g / oz value written in the name (e.g. "3.5g", "1/2 OZ" -> "0.5oz"); blank if the
#              name gives no gram/ounce value.
# Source URL the user provided: https://www.anicedream.org/shop/?store_path=%2F&store_query=page%3D1
# Run: pip install playwright pandas pytz && playwright install chromium
company = "ANiceDream"
location = "New Mexico"
dispensary_location = "Albuquerque"

SLUG = "anicedream"   # Weedmaps API listing slug (the wm.store subdomain is anicedreamdispensary)
STORE_URL = "https://anicedreamdispensary.wm.store/?&page=1"   # storefront warm-up page
API = f"https://api-g.weedmaps.com/discovery/v1/listings/dispensaries/{SLUG}/menu_items"
PAGE_SIZE = 100
MAX_PAGES = 40
# Headed on local Windows (helps clear bot checks); headless where there's no display — e.g. the
# scheduled Linux container, which has no X server and can't launch a headed browser.
HEADLESS = os.name != "nt" and not os.environ.get("DISPLAY")

# Brand list from the store's own brand filter. Each entry: (canonical name to write, regex that
# detects it in a product name). Order matters — first match wins.
BRANDS = [
    ("Airo Brands", r"\bairo\b"),
    ("BLOOM", r"\bbloom\b"),
    ("Caviar Gold", r"\bcaviar\b"),
    ("Cheeba Chews", r"\bcheeba\b"),
    ("Cure Injoy", r"\b(?:cure\s*injoy|injoy)\b"),
    ("Dime Industries", r"\bdime\b"),
    ("Dixie Brands", r"\bdixie\b"),
    ("Don Verde", r"\bdon\s*verde\b"),
    ("Elevated Labs", r"\belevated\s*labs?\b"),
    ("ghost.", r"\bghost\b(?!\s*train)"),
    ("HeadSpace Alchemy", r"\bhead\s*space\b"),
    ("Major", r"\bmajor\b"),
    ("Muha Meds", r"\bmuha\b"),
    ("PACKS LOS ANGELES", r"\bpacks\b"),
    ("Ric Flair Drip", r"\bric\s*flair\b"),
    ("TYSON 2.0", r"\btyson\b"),
    ("Urb NM", r"\burb\b"),
    ("Wana Edibles", r"\bwana\b"),
    ("WYLD", r"\bwyld\b"),
]
BRANDS = [(name, re.compile(pat, re.IGNORECASE)) for name, pat in BRANDS]


def match_brand(name):
    for canonical, rx in BRANDS:
        if rx.search(name or ""):
            return canonical
    return ""


def parse_weight(name):
    """Weight as written in the product name, in grams or ounces (blank if neither is present)."""
    n = name or ""
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:g|gram|grams)\b", n, re.IGNORECASE)
    if m:
        return f"{float(m.group(1)):g}g"
    m = re.search(r"(\d+\s*/\s*\d+|\d+(?:\.\d+)?)\s*(?:oz|ounce|ounces)\b", n, re.IGNORECASE)
    if m:
        v = m.group(1).replace(" ", "")
        if "/" in v:
            a, b = v.split("/")
            try:
                v = float(a) / float(b)
            except Exception:
                return ""
        return f"{float(v):g}oz"
    if re.search(r"\b(?:8th|eighth)\b", n, re.IGNORECASE):
        return "3.5g"
    return ""

us_tz = pytz.timezone("America/Denver")
today = datetime.now(us_tz).strftime("%Y-%m-%d")

output_folder = "app/output/pending"
csv_folder = os.path.join(output_folder, "csv")
json_folder = os.path.join(output_folder, "json")
os.makedirs(csv_folder, exist_ok=True)
os.makedirs(json_folder, exist_ok=True)
output_file = os.path.join(csv_folder, f"{today}_{company}.csv")
json_file_path = os.path.join(json_folder, f"{today}_{company}.json")

CAT_RULES = [
    ("pre-roll", "Pre-rolls"), ("preroll", "Pre-rolls"),
    ("flower", "Flower"),
    ("vaporizer", "Vaporizers"), ("vape", "Vaporizers"), ("cartridge", "Vaporizers"),
    ("disposable", "Vaporizers"),
    ("concentrate", "Concentrates"), ("extract", "Concentrates"), ("wax", "Concentrates"),
    ("rosin", "Concentrates"), ("resin", "Concentrates"), ("dab", "Concentrates"),
    ("hash", "Concentrates"), ("diamond", "Concentrates"),
    ("edible", "Edibles"), ("gummies", "Edibles"), ("gummy", "Edibles"),
    ("chocolate", "Edibles"), ("drink", "Edibles"), ("beverage", "Edibles"),
    ("tincture", "Tinctures"), ("topical", "Topicals"), ("cbd", "CBD"),
    ("accessor", "Accessories"), ("gear", "Accessories"),
]

products = []
all_json = []
seen = set()
wm_hits = [0]


def affinity_category(edge):
    edge = edge or {}
    names = [edge.get("name", "")] + [a.get("name", "") for a in (edge.get("ancestors") or [])]
    hay = " ".join(n for n in names if n).lower()
    for kw, cat in CAT_RULES:
        if kw in hay:
            return cat
    return (names[-1] if names and names[-1] else "Other")


def potency(agg, cann):
    if not agg:
        return ""
    val = agg.get(cann)
    unit = agg.get(f"{cann}_unit") or ""
    if val in (None, 0, 0.0):
        return ""
    return f"{val}{unit}"


def variants(prices):
    out = []
    for k, v in (prices or {}).items():
        if k == "grams_per_eighth":
            continue
        if isinstance(v, list):
            out.extend(v)
        elif isinstance(v, dict):
            out.append(v)
    return out


def handle(items):
    for it in items:
        if not isinstance(it, dict):
            continue
        if (it.get("license_type") or "").lower() == "medical":
            continue
        name = (it.get("name") or "").strip()
        if not name:
            continue
        category = affinity_category(it.get("edge_category"))
        subcat = (it.get("edge_category") or {}).get("name", "")
        strain = (it.get("genetics_tag") or {}).get("name", "") or ""
        agg = (it.get("metrics") or {}).get("aggregates") or {}
        thc = potency(agg, "thc")
        cbd = potency(agg, "cbd")

        # This store has no brand field and no per-weight variants — derive both from the name.
        brand = match_brand(name)
        weight = parse_weight(name)

        vs = variants(it.get("prices"))
        if not vs and it.get("price"):
            vs = [it["price"]]
        for e in vs:
            label = (e.get("label") or "").strip()
            price = e.get("price")
            orig = e.get("original_price")
            on_sale = bool(e.get("on_sale"))
            key = (dispensary_location, it.get("id") or name, weight or label)
            if key in seen:
                continue
            seen.add(key)
            row = {
                "date": today, "location": location, "company": company,
                "dispensary_location": dispensary_location, "name": name,
                "category": category, "subcategory": subcat, "brand": brand,
                "strain_type": strain, "THC": thc, "CBD": cbd,
                "weight": weight, "original_price": price,
            }
            if on_sale and orig and price is not None and orig > price:
                row["deal_1"] = f"Sale (reg ${orig})"
            products.append(row)


_FETCH_JS = """async (url) => {
  try {
    const r = await fetch(url, {headers: {"Accept": "application/json"}});
    return {status: r.status, body: await r.text()};
  } catch (e) { return {status: -1, body: String(e)}; }
}"""


# =========================
# PLAYWRIGHT (fetch the Weedmaps API from inside a real browser session)
# =========================
try:
    logger.info(f"Starting {company} scrape | {dispensary_location} (Weedmaps via browser fetch)")
    with sync_playwright() as p:
        launch_args = {"headless": HEADLESS, "args": ["--disable-blink-features=AutomationControlled"]}
        try:
            browser = p.chromium.launch(channel="chrome", **launch_args)
        except Exception:
            browser = p.chromium.launch(**launch_args)
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            viewport={"width": 1440, "height": 2200}, locale="en-US",
        )
        ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        page = ctx.new_page()

        # Warm up: load the store page so the browser gets Cloudflare clearance + cookies.
        page.goto(STORE_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(6000)

        total = None
        for pg in range(1, MAX_PAGES + 1):
            url = f"{API}?page={pg}&page_size={PAGE_SIZE}"
            res = page.evaluate(_FETCH_JS, url)
            if res.get("status") != 200:
                logger.warning(f"  page {pg}: status {res.get('status')} — {str(res.get('body'))[:200]}")
                break
            try:
                data = json.loads(res["body"])
            except Exception:
                break
            meta = data.get("meta") or {}
            total = meta.get("total_menu_items", total)
            items = ((data.get("data") or {}).get("menu_items")) or []
            if not items:
                break
            before = len(products)
            handle(items)
            all_json.append({"page": pg, "items": len(items)})
            logger.info(f"  page {pg}: {len(items)} items (+{len(products) - before}, rows {len(products)}, total {total})")
            if len(items) < PAGE_SIZE:
                break
            page.wait_for_timeout(400)
        browser.close()
except Exception as e:
    logger.critical(f"❌ Scraper failed: {e}", exc_info=True)
    sys.exit(1)


# =========================
# SAVE JSON + CSV
# =========================
try:
    if not products:
        logger.critical("❌ No products collected (try HEADLESS=False).")
        sys.exit(1)
    with open(json_file_path, "w", encoding="utf-8") as f:
        json.dump(products, f, indent=2)
    logger.info(f"JSON saved: {json_file_path}")

    df = pd.DataFrame(products).drop_duplicates(subset=["dispensary_location", "name", "weight"])
    if df.empty or len(df) < 5:
        logger.critical(f"❌ Invalid CSV output. Rows: {len(df)}")
        sys.exit(1)
    fixed_cols = ["date", "location", "company", "dispensary_location", "name",
                  "category", "subcategory", "brand", "strain_type",
                  "THC", "CBD", "weight", "original_price"]
    deal_cols = sorted([c for c in df.columns if c.startswith("deal_")],
                       key=lambda x: int(x.split("_")[1]))
    df = df[fixed_cols + deal_cols]
    df.to_csv(output_file, index=False, na_rep="")
    logger.info(f"CSV saved: {output_file} | Rows: {len(df)}")
except SystemExit:
    raise
except Exception as e:
    logger.critical(f"❌ Save failed: {e}", exc_info=True)
    sys.exit(1)

logger.info(f"✅ {company} scraper completed successfully")
sys.exit(0)
