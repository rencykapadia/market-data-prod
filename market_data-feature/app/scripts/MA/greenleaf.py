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
# PLATFORM: Dutchie white-label (Next.js + Apollo/GraphQL) on menu.greenerleaf.com.
# The page exposes window.__APOLLO_CLIENT__; a real Chromium (Playwright) clears
# Cloudflare via normal browsing, then we reuse the client to paginate the menu.
# Run: pip install playwright openpyxl pandas pytz && playwright install chromium
#
# Greener Leaf is a SINGLE Massachusetts location (Fall River). Recreational menu
# (slug ...-rec). If a medical menu exists later, add it to `stores` below.
company  = "GreenerLeaf"
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

HEADLESS    = False
NAV_TIMEOUT = 90_000
PRODUCTS_URL = "https://menu.greenerleaf.com/stores/{cName}/products"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36")

# MASSACHUSETTS STORES (all Greener Leaf locations).
stores = [
    {"cName": "greener-leaf-fall-river-rec", "dispensary_location": "Fall River"},
]

# =========================
# GLOBAL STORAGE
# =========================
products = []
all_json = []

# =========================
# BROWSER PREDICATE + EXTRACTOR (run in the page via Playwright)
# =========================
# True once the Apollo client + a FilteredProducts query exist.
READY_PREDICATE = """
() => {
  try {
    const c = window.__APOLLO_CLIENT__;
    if (!c) return false;
    let ok = false;
    c.queryManager.queries.forEach(q => {
      try {
        const d = q.document || (q.observableQuery && q.observableQuery.query);
        const nm = d && d.definitions[0].name && d.definitions[0].name.value;
        if (nm === 'FilteredProducts' && q.observableQuery && q.observableQuery.variables &&
            q.observableQuery.variables.productsFilter &&
            q.observableQuery.variables.productsFilter.dispensaryId) ok = true;
      } catch (e) {}
    });
    return ok;
  } catch (e) { return false; }
}
"""

# Paginate FilteredProducts via the page's Apollo client; one record per option.
# Returns { dispensaryId, total, products:[{name, category, subcategory, brand,
#           strain_type, THC, CBD, weight, original_price, deals[]}] }.
EXTRACTOR_JS = r"""
async () => {
  const c = window.__APOLLO_CLIENT__;
  let fpDoc = null, baseVars = null;
  c.queryManager.queries.forEach(q => {
    try {
      const d = q.document || (q.observableQuery && q.observableQuery.query);
      const nm = d && d.definitions[0].name && d.definitions[0].name.value;
      if (nm === 'FilteredProducts') { fpDoc = d; if (q.observableQuery) baseVars = JSON.parse(JSON.stringify(q.observableQuery.variables)); }
    } catch (e) {}
  });
  if (!fpDoc || !baseVars) return { error: 'no FilteredProducts doc/vars' };
  const SID = baseVars.productsFilter.dispensaryId;
  baseVars.perPage = 100;
  const cache = c.cache.extract();
  const smap = {};
  Object.keys(cache).filter(k => k.startsWith('Specials:')).forEach(k => {
    const s = cache[k]; smap[s._id || s.id || k.split(':')[1]] = s.menuDisplayName || s.name || '';
  });
  const pot = pc => {
    if (!pc || !pc.range) return '';
    const r = pc.range.filter(x => x != null);
    if (!r.length || (r.length === 1 && r[0] === 0)) return '';
    const u = pc.unit === 'PERCENTAGE' ? '%' : (pc.unit === 'MILLIGRAMS' ? 'mg' : '');
    const v = (r.length > 1 && r[1] != null && r[1] !== r[0]) ? (r[0] + '-' + r[1]) : r[0];
    return (v + ' ' + u).trim();
  };
  const dealsFor = (p, opt) => {
    const sip = (p.specialIdsPerOption || []).find(s => s.option === opt);
    if (!sip) return [];
    const ids = [].concat(sip.rewardIds || [], sip.conditionIds || []);
    return [...new Set(ids.map(id => smap[id]).filter(Boolean))];
  };
  const products = [];
  let page = 0, total = null, collected = 0, pages = 0;
  while (true) {
    const vars = JSON.parse(JSON.stringify(baseVars)); vars.page = page;
    const res = await c.query({ query: fpDoc, variables: vars, fetchPolicy: 'no-cache' });
    const fp = res.data && res.data.filteredProducts;
    const list = (fp && fp.products) || [];
    total = fp && fp.queryInfo && fp.queryInfo.totalCount;
    list.forEach(p => (p.Options || []).forEach((opt, i) => {
      const price = (p.recPrices && p.recPrices[i] != null) ? p.recPrices[i]
                  : ((p.Prices && p.Prices[i] != null) ? p.Prices[i] : null);
      const promo = (p.recSpecialPrices && p.recSpecialPrices[i] != null) ? p.recSpecialPrices[i] : null;
      let dnames = dealsFor(p, opt);
      if (!dnames.length && promo != null) dnames = ['On Sale'];
      products.push({
        name: p.Name, category: p.type || '', subcategory: p.subcategory || '',
        brand: p.brandName || '', strain_type: p.strainType || '',
        THC: pot(p.THCContent), CBD: pot(p.CBDContent),
        weight: opt, original_price: price, deals: dnames,
      });
    }));
    collected += list.length; pages++;
    if (!list.length || (total != null && collected >= total) || pages > 40) break;
    page++; await new Promise(r => setTimeout(r, 500));
  }
  return { dispensaryId: SID, total, products };
}
"""

# =========================
# PLAYWRIGHT SCRAPER
# =========================
try:
    logger.info(f"Starting {company} scrape | stores={len(stores)}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        
        # 🔥 FIX: Pass a real-world User-Agent
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            viewport={'width': 1920, 'height': 1080}
        )
        
        page = context.new_page()

        for store in stores:
            url = PRODUCTS_URL.format(cName=store["cName"])
            logger.info(f"=== {store['dispensary_location']} ({store['cName']}) ===")

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
                page.wait_for_function(READY_PREDICATE, timeout=NAV_TIMEOUT)
                result = page.evaluate(EXTRACTOR_JS)
            except Exception as e:
                logger.error(f"❌ {store['dispensary_location']}: menu load/extract failed: {e}")
                continue

            if not isinstance(result, dict) or result.get("error"):
                logger.error(f"❌ {store['dispensary_location']}: {result.get('error') if isinstance(result, dict) else result}")
                continue

            all_json.append({"dispensary_location": store["dispensary_location"], "data": result})

            for p in result.get("products", []):
                deals = p.get("deals") or []
                deal_columns = {f"deal_{i+1}": d for i, d in enumerate(deals)}
                product = {
                    "date": today,
                    "location": location,
                    "company": company,
                    "dispensary_location": store["dispensary_location"],
                    "name": p.get("name"),
                    "category": p.get("category"),
                    "subcategory": p.get("subcategory"),
                    "brand": p.get("brand"),
                    "strain_type": p.get("strain_type"),
                    "THC": p.get("THC"),
                    "CBD": p.get("CBD"),
                    "weight": p.get("weight"),
                    "original_price": p.get("original_price"),
                }
                product.update(deal_columns)
                products.append(product)

            logger.info(f"  => {store['dispensary_location']}: {len(result.get('products', []))} rows (total {result.get('total')})")

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
        json.dump(all_json, f, indent=2)

    logger.info(f"JSON saved: {json_file_path}")

except Exception as e:
    logger.critical(f"❌ JSON save failed: {e}", exc_info=True)
    sys.exit(1)


# =========================
# SAVE CSV
# =========================
try:
    # multi-store safe: dedupe within a store so a product carried at two stores isn't collapsed
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
logger.info("✅ GreenerLeaf scraper completed successfully")
sys.exit(0)