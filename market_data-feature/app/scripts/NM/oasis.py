import pandas as pd
import json
import os
import re
from datetime import datetime
import pytz
from playwright.sync_api import sync_playwright
import logging
import sys

# =========================
# LOGGER
# =========================
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

# =========================
# CONFIG
# =========================
# PLATFORM: Oasis Cannabis relaunched on its own SERVER-RENDERED menu (custom Next.js). Each store's
# menu lives at  https://www.oasiscannabisnm.com/menu/<location>  and filters by category with
# ?family=<slug>. The product cards are in the initial HTML (no client product API). The whole card
# IS the <a href="/menu/<loc>/p/<slug>"> element; read fields off its spans by class fragment:
#   brand chip = span with 'uppercase'+'tracking'  (e.g. "AWA 3.5g Pre-Pack" — use textContent,
#                the chip is CSS-uppercased so innerText would shout it)
#   name       = span with 'min-h-['              (e.g. "AWA Flower - Bavarian Creme 3.5g Pre-Pack")
#   potency    = span with 'font-mono'+'tabular'  ("THC 22.9% · CBD 0%" — both on the card now)
#   strain     = span with 'sm:inline'            ("Hybrid")
#   price      = span with 'font-figure'          ("$24.00")
#   deals      = promo badges ("2+ · 10% off")
# CBD is now on the card; a specific sub-category is not, so detail pages fill subcategory (and any
# blank THC/CBD). TAC is not published by Oasis anywhere, so it stays "". Requested URL:
# /menu/midtown?utm_source=portal...  Run: pip install playwright pandas pytz && playwright install chromium
company = "Oasis"
location = "New Mexico"

BASE = "https://www.oasiscannabisnm.com"
HEADLESS = True   # SSR menu; flip to False only if a bot-check/age-gate appears

# The menu cards don't carry CBD or a specific sub-category; open each product's detail page to fill
# them (and edible mg-THC). Adds ~1 fetch per product — set False to skip and run cards-only.
ENRICH_FROM_DETAIL = True
DETAIL_DELAY_MS = 150   # pause between detail fetches to stay polite

# Oasis stores by their /menu/<slug> location. Add more once you have their menu slug.
STORES = [
    {"loc": "midtown", "dispensary_location": "Albuquerque - Midtown"},
    # {"loc": "coors", "dispensary_location": "Albuquerque - Coors"},
]

# ?family=<slug> -> affinity category label
FAMILIES = {
    "flower": "Flower",
    "pre-rolls": "Pre-rolls",
    "vapes": "Vaporizers",
    "concentrates": "Concentrates",
    "edibles": "Edibles",
    "topicals": "Topicals",
    "accessories": "Accessories",
}
STRAIN_MAP = {
    "hybrid": "Hybrid", "sativa": "Sativa", "indica": "Indica",
    "sativa-hybrid": "Sativa", "indica-hybrid": "Indica", "s-h": "Sativa", "i-h": "Indica",
    "cbd": "High CBD", "high cbd": "High CBD",
}

us_tz = pytz.timezone("America/Denver")
today = datetime.now(us_tz).strftime("%Y-%m-%d")

output_folder = "app/output/pending"
csv_folder = os.path.join(output_folder, "csv")
json_folder = os.path.join(output_folder, "json")
os.makedirs(csv_folder, exist_ok=True)
os.makedirs(json_folder, exist_ok=True)
output_file = os.path.join(csv_folder, f"{today}_{company}.csv")
json_file_path = os.path.join(json_folder, f"{today}_{company}.json")

products = []
all_json = []
seen = set()

# Pull the structured fields straight off the SSR product cards.
_EXTRACT = r"""() => {
  const out = [], seen = new Set();
  // The whole product card IS the <a href="/menu/<loc>/p/<slug>"> element. Read fields off its
  // spans by class fragment. Use textContent (NOT innerText) — the brand chip is CSS-uppercased,
  // so innerText would return "AWA 3.5G PRE-PACK" while textContent keeps proper case.
  for (const a of document.querySelectorAll('a[href*="/menu/"][href*="/p/"]')) {
    const href = a.getAttribute('href');
    if (!href || seen.has(href)) continue;
    seen.add(href);
    const spans = [...a.querySelectorAll('span')];
    const find = (pred) => { const el = spans.find(pred); return el ? (el.textContent || '').trim() : ''; };
    const hasCls = (frag) => (s) => ((s.className || '') + '').includes(frag);
    const brand  = find((s) => { const c = (s.className || '') + ''; return c.includes('uppercase') && c.includes('tracking'); });
    const name   = find(hasCls('min-h-[')) || (a.textContent || '').trim();
    const pot    = find((s) => { const c = (s.className || '') + ''; return c.includes('font-mono') && c.includes('tabular'); });
    const strain = find(hasCls('sm:inline'));
    const price  = find(hasCls('font-figure'));
    // Deal badges only — keep short promo spans ("2+ · 10% off", "BOGO"), dropping the price ($..),
    // the strain word, and the whole-card text wrapper (which is long / contains "$").
    const deals = spans
      .map((s) => (s.textContent || '').replace(/\s+/g, ' ').trim())
      .filter((t) => t.length < 40 && !t.includes('$') && /(\d+%\s*off|\bbogo\b|\bdeal\b|\bsave\b)/i.test(t));
    out.push({ href, brand, name, pot, strain, price, deals: [...new Set(deals)] });
  }
  return out;
}"""

# Fetch a product's detail page (same-origin, SSR) and pull the fields that are blank on the menu
# cards: CBD, a specific sub-category ("Category" field, e.g. "Flower Pre-Pack", "Edibles", "Carts"),
# and — for edibles — the mg THC the card omits. TAC is not published by Oasis, so it stays "".
_DETAIL_JS = r"""async (url) => {
  try {
    const r = await fetch(url, {headers: {"Accept": "text/html"}});
    if (r.status !== 200) return {};
    const txt = (await r.text()).replace(/<[^>]+>/g, " ").replace(/&amp;/g, "&").replace(/\s+/g, " ");
    const fmt = (m) => m ? (m[2] ? m[1] + m[2] : m[1] + "mg") : "";
    const thc = fmt(txt.match(/THC\s*([\d.]+)\s*(%|mg)/i) || txt.match(/([\d.]+)\s*mg\s*THC/i));
    const cbd = fmt(txt.match(/CBD\s*([\d.]+)\s*(%|mg)/i) || txt.match(/([\d.]+)\s*mg\s*CBD/i));
    const cm = txt.match(/Category\s+(.+?)\s+(?:What|POTENCY|POS|Description|Effects|Listed|Similar|Add|Related|About|Reviews|Menu|Home|Shop)\b/i);
    let subcat = cm ? cm[1].trim() : "";
    subcat = subcat.replace(/\s*\([^)]*\)\s*$/, "").trim();
    // Full deal ladder, e.g. "Buy 2 · 10% off · $13.50 each". Skip the "Buy 1" base tier (no
    // discount). Format each discounted tier as a clean "Buy N · X% off · $Y each" string.
    const deals = [...txt.matchAll(/Buy\s+(\d+)\s*·\s*(?:(\d+)%\s*off\s*·\s*)?\$([\d.]+)\s*each/gi)]
      .filter((m) => m[2])
      .map((m) => `Buy ${m[1]} · ${m[2]}% off · $${(+m[3]).toFixed(2)} each`);
    return {thc, cbd, subcat, deals: [...new Set(deals)]};
  } catch (e) { return {}; }
}"""


def parse_weight(name):
    m = re.search(r"\((\d+(?:\.\d+)?)\s*Gram\)", name, re.IGNORECASE)
    if m:
        return f"{float(m.group(1)):g}g"
    m = re.search(r"(?<![A-Za-z0-9])(\d+\s*/\s*\d+\s*oz|\d+(?:\.\d+)?\s*(?:g|mg|oz))\b", name, re.IGNORECASE)
    return m.group(1).replace(" ", "") if m else ""


def parse_strain(strain, name):
    s = (strain or "").strip().lower()
    if s in STRAIN_MAP:
        return STRAIN_MAP[s]
    m = re.search(r"\((Indica-Hybrid|Sativa-Hybrid|Indica|Sativa|Hybrid|S-H|I-H|CBD)\)", name, re.IGNORECASE)
    if m:
        return STRAIN_MAP.get(m.group(1).lower(), m.group(1).title())
    return strain.title() if strain else ""


def _dollar(s):
    m = re.search(r"\$([\d.]+)", s or "")
    return float(m.group(1)) if m else None


def _potency(pot, cann):
    # Card potency line reads "THC 22.9% · CBD 0%" (edibles show mg). Pull THC or CBD with its unit.
    m = re.search(rf"{cann}\s*([\d.]+)\s*(%|mg)", pot or "", re.IGNORECASE)
    if m:
        return f"{m.group(1)}{m.group(2)}"
    return ""


def handle(cards, store, category):
    for c in cards:
        name = (c.get("name") or "").strip()
        if not name:
            continue
        # brand = the brand chip ("AWA 3.5g Pre-Pack"), proper case straight off the card — NOT
        # derived from splitting the name. Fall back to the name's prefix only if the chip is blank.
        brand = (c.get("brand") or "").strip()
        if not brand and " - " in name:
            brand = name.split(" - ")[0].strip()
        weight = parse_weight(name)
        strain = parse_strain(c.get("strain"), name)
        thc = _potency(c.get("pot"), "THC")
        cbd = _potency(c.get("pot"), "CBD")
        price = _dollar(c.get("price"))

        key = (store["dispensary_location"], c.get("href"), weight)
        if key in seen:
            continue
        seen.add(key)

        row = {
            "date": today, "location": location, "company": company,
            "dispensary_location": store["dispensary_location"], "name": name,
            "category": category, "subcategory": "", "brand": brand,
            "strain_type": strain, "THC": thc, "CBD": cbd, "TAC": "",
            "weight": weight, "original_price": price,
            "_href": c.get("href"),
        }
        for i, d in enumerate([d for d in (c.get("deals") or []) if d][:5]):
            row[f"deal_{i+1}"] = d
        products.append(row)


# =========================
# PLAYWRIGHT
# =========================
try:
    logger.info(f"Starting {company} scrape | stores={len(STORES)} (oasiscannabisnm.com /menu SSR)")
    with sync_playwright() as p:
        launch_args = {"headless": HEADLESS, "args": ["--disable-blink-features=AutomationControlled"]}
        try:
            browser = p.chromium.launch(channel="chrome", **launch_args)
        except Exception:
            browser = p.chromium.launch(**launch_args)
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            viewport={"width": 1440, "height": 2200},
        )
        ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        page = ctx.new_page()

        for store in STORES:
            before_store = len(products)
            logger.info(f"=== {store['dispensary_location']} (/menu/{store['loc']}) ===")
            for fam, cat in FAMILIES.items():
                before = len(products)
                url = f"{BASE}/menu/{store['loc']}?family={fam}"
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(3500)
                except Exception as e:
                    logger.warning(f"  {fam}: nav failed ({str(e).splitlines()[0]})")
                    continue
                try:
                    cards = page.evaluate(_EXTRACT)
                except Exception as e:
                    logger.warning(f"  {fam}: extract failed ({e})")
                    cards = []
                all_json.append({"store": store["dispensary_location"], "family": fam, "cards": len(cards)})
                handle(cards, store, cat)
                logger.info(f"  {cat}: {len(cards)} cards -> +{len(products) - before} rows (total {len(products)})")

            # Fill the blank menu-card fields (CBD, subcategory, edible mg-THC) from detail pages.
            if ENRICH_FROM_DETAIL:
                store_rows = [r for r in products if r.get("_href")
                              and r["dispensary_location"] == store["dispensary_location"]]
                hrefs = list(dict.fromkeys(r["_href"] for r in store_rows))
                logger.info(f"  enriching {len(hrefs)} product detail pages…")
                cache = {}
                for i, href in enumerate(hrefs, 1):
                    try:
                        cache[href] = page.evaluate(_DETAIL_JS, BASE + href) or {}
                    except Exception:
                        cache[href] = {}
                    page.wait_for_timeout(DETAIL_DELAY_MS)
                    if i % 25 == 0:
                        logger.info(f"    details {i}/{len(hrefs)}")
                def _clean_subcat(s):
                    # Guard against the Category regex occasionally grabbing JSON/description noise.
                    s = (s or "").strip()
                    if not s or len(s) > 30 or re.search(r'[{}"\.]', s) or " " in s.strip() and len(s.split()) > 4:
                        return ""
                    return s

                filled = 0
                for r in store_rows:
                    d = cache.get(r["_href"]) or {}
                    # THC and CBD are already on the card; only backfill blanks. subcategory is
                    # never on the card, so always take it from the detail page (when it looks clean).
                    subcat = _clean_subcat(d.get("subcat"))
                    if subcat:
                        r["subcategory"] = subcat
                    if not r.get("CBD") and d.get("cbd"):
                        r["CBD"] = d["cbd"]
                    if not r.get("THC") and d.get("thc"):
                        r["THC"] = d["thc"]
                    # The card shows only the first deal tier ("2+ · 10% off"); the detail page has
                    # the full ladder. Replace the card deal with every discounted tier.
                    ladder = d.get("deals") or []
                    if ladder:
                        for k in [c for c in list(r) if c.startswith("deal_")]:
                            r.pop(k, None)
                        for i, dv in enumerate(ladder[:8]):
                            r[f"deal_{i+1}"] = dv
                    if subcat or ladder:
                        filled += 1
                logger.info(f"  enriched {filled}/{len(store_rows)} rows from detail pages")

            logger.info(f"=> {store['dispensary_location']}: {len(products) - before_store} rows")
        browser.close()
except Exception as e:
    logger.critical(f"❌ Scraper failed: {e}", exc_info=True)
    sys.exit(1)


# =========================
# SAVE JSON + CSV
# =========================
try:
    if not products:
        logger.critical("❌ No products collected — the /menu page may have changed (or a gate appeared; try HEADLESS=False).")
        sys.exit(1)
    for r in products:
        r.pop("_href", None)   # drop the internal detail-page key before saving
    with open(json_file_path, "w", encoding="utf-8") as f:
        json.dump(products, f, indent=2)
    logger.info(f"JSON saved: {json_file_path}")

    df = pd.DataFrame(products).drop_duplicates(subset=["dispensary_location", "name", "weight"])
    if df.empty or len(df) < 10:
        logger.critical(f"❌ Invalid CSV output. Rows: {len(df)}")
        sys.exit(1)
    fixed_cols = ["date", "location", "company", "dispensary_location", "name",
                  "category", "subcategory", "brand", "strain_type",
                  "THC", "CBD", "TAC", "weight", "original_price"]
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
