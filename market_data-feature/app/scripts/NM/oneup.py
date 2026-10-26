import pandas as pd
import json
import os
import time
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
# PLATFORM: 1UP (1upabq.com) runs on the CARROT platform. Its JSON API
# (https://api.nevada.getcarrot.io/api/v1/store/...) is TOKEN-GATED — plain `requests` get a
# 403 no matter the headers. So instead of calling the API directly, we load the real menu in
# a browser (Playwright) and INTERCEPT the API responses the site's own JS makes: the page
# authenticates itself, we just read the JSON. The Carrot widget mounts on the menu page below and
# routes by URL hash; we drive each category route and scroll to trigger Carrot's pagination.
#   menu page:      https://1upabq.com/menu-/-order-online   (locId=1 == Albuquerque store)
#   category route: https://1upabq.com/#menu/albuquerque-nm/category/<slug>
# The per-category endpoint (/store/category/slug/<slug>/product) returns a BARE ARRAY of
# {product, cashOptions}; each product carries name, masterCategoryName, subcategoryName, brand,
# strain, labResults[] (THC/CBD) and cashOptions[] (one row per weight/variant w/ price).
# Requested URL: https://1upabq.com/#menu/albuquerque-nm/category/flower
# Run: pip install playwright pandas pytz  &&  playwright install chromium
company = "1Up"
location = "New Mexico"
dispensary_location = "Albuquerque"

MENU_URL = "https://1upabq.com/menu-/-order-online"   # page where the Carrot menu widget mounts
STORE_SLUG = "albuquerque-nm"                          # hash-route store segment (#menu/<slug>/...)
# Headed on local Windows (helps clear bot checks); headless where there's no display — e.g. the
# scheduled Linux container, which has no X server and can't launch a headed browser.
HEADLESS = os.name != "nt" and not os.environ.get("DISPLAY")
MAX_SCROLLS = 60
# Category labels to click inside the Carrot widget so every category's full list loads.
CATEGORY_LABELS = ["Flower", "Pre-Rolls", "Prerolls", "Pre-Roll", "Carts", "Cartridges",
                   "Vaporizers", "Vapes", "Concentrates", "Concentrate", "Edibles", "Edible",
                   "Drinks", "Beverages", "Topicals", "Topical", "Tinctures", "CBD",
                   "Accessories", "Accessory", "Merch"]

us_tz = pytz.timezone("America/Denver")
today = datetime.now(us_tz).strftime("%Y-%m-%d")

output_folder = "app/output/pending"
csv_folder = os.path.join(output_folder, "csv")
json_folder = os.path.join(output_folder, "json")
os.makedirs(csv_folder, exist_ok=True)
os.makedirs(json_folder, exist_ok=True)
output_file = os.path.join(csv_folder, f"{today}_{company}.csv")
json_file_path = os.path.join(json_folder, f"{today}_{company}.json")

# Carrot master category -> affinity category
CAT_MAP = {
    "Flower": "Flower", "Preroll": "Pre-rolls", "Pre-Roll": "Pre-rolls", "Prerolls": "Pre-rolls",
    "Carts": "Vaporizers", "Cartridge": "Vaporizers", "Cartridges": "Vaporizers", "Vape": "Vaporizers",
    "Concentrate": "Concentrates", "Concentrates": "Concentrates",
    "Edible": "Edibles", "Edibles": "Edibles",
    "Topical": "Topicals", "Topicals": "Topicals",
    "CBD": "CBD", "Tincture": "Tinctures", "Tinctures": "Tinctures",
    "Accessories": "Accessories", "Accessory": "Accessories", "Merch": "Other", "Other": "Other",
}
STRAIN_MAP = {
    "50/50": "Hybrid", "hybrid": "Hybrid",
    "indica": "Indica", "indica-dom": "Indica", "indica dominant": "Indica",
    "sativa": "Sativa", "sativa-dom": "Sativa", "sativa dominant": "Sativa",
}

products = []
seen = set()
deal_prices = {}    # "n:<pid>:<weightname>" / "u:<pid>:<unitQty>" -> discounted (offered) price
carrot_hits = [0]   # count of getcarrot.io 200 JSON responses seen


def norm_strain(s):
    s = (s or "").strip()
    return STRAIN_MAP.get(s.lower(), s)


def sub_of(subname):
    if not subname:
        return ""
    parts = subname.split(" - ", 1)
    return parts[1].strip() if len(parts) == 2 else ""


def potency(lab_results, cannabinoid):
    for lr in (lab_results or []):
        test = lr.get("labTest") or {}
        if cannabinoid in test:
            val = lr.get("value")
            if val in (None, 0) and cannabinoid == "CBD":
                continue
            unit = lr.get("labResultUnit") or {}
            if "Percentage" in unit:
                return f"{val}%"
            if "Milligrams" in unit:
                return f"{val}mg"
            return str(val)
    return ""


def unwrap(entry):
    """Return (detail_dict, cashOptions list) handling both API shapes."""
    if not isinstance(entry, dict):
        return None, []
    p = entry.get("product")
    if isinstance(p, dict) and isinstance(p.get("product"), dict):        # promotion/products
        return p.get("product"), (p.get("cashOptions") or [])
    if isinstance(p, dict):                                               # frontpage best-sellers
        return p, (entry.get("cashOptions") or p.get("cashOptions") or [])
    return None, []


def add_product(detail, cash_options):
    if not detail:
        return
    name = (detail.get("name") or "").strip()
    if not name:
        return
    master = detail.get("masterCategoryName") or detail.get("master") or ""
    category = CAT_MAP.get(master, master or "Other")
    subcat = sub_of(detail.get("subcategoryName") or detail.get("carrotSubcategory"))
    brand = (detail.get("brand") or "").strip()
    strain = norm_strain(detail.get("strain"))
    lab = detail.get("labResults")
    thc = potency(lab, "THC")
    cbd = potency(lab, "CBD")

    for opt in (cash_options or []):
        unit = opt.get("optionUnit") or ""
        disp = (opt.get("displayName") or "").strip()
        weight = "" if unit == "Units" else disp
        try:
            price = float(opt.get("price")) if opt.get("price") is not None else None
        except (TypeError, ValueError):
            price = None

        pid = opt.get("productId") or detail.get("productId") or detail.get("id")
        key = (dispensary_location, pid, weight or disp)
        if key in seen:
            continue
        seen.add(key)

        products.append({
            "date": today, "location": location, "company": company,
            "dispensary_location": dispensary_location, "name": name,
            "category": category, "subcategory": subcat, "brand": brand,
            "strain_type": strain, "THC": thc, "CBD": cbd,
            "weight": weight, "original_price": price,
            "_pid": pid, "_uq": opt.get("unitQty"), "_disp": disp,
        })


def process_payload(data):
    """Handle every Carrot response shape.

    The per-category endpoint (/store/category/slug/<slug>/product) returns a BARE ARRAY of
    {product, cashOptions} entries; the catalog/search endpoints wrap them under 'products', and
    the frontpage under 'nodes'. Handle all three.
    """
    if isinstance(data, list):
        for entry in data:
            detail, opts = unwrap(entry)
            add_product(detail, opts)
        return
    if not isinstance(data, dict):
        return
    if isinstance(data.get("products"), list):
        for entry in data["products"]:
            detail, opts = unwrap(entry)
            add_product(detail, opts)
    if isinstance(data.get("nodes"), list):
        for node in data["nodes"]:
            block = (node.get("element") or {}).get("BestSellingInCategory") or {}
            for entry in (block.get("products") or []):
                detail, opts = unwrap(entry)
                add_product(detail, opts)


def record_promotions(data):
    """Carrot returns deal prices in each promotion's `modifiedOptions` map:
       modifiedOptions[productId] = [{unitQty, name, price}, ...]  (name == cashOption displayName).
    Record the best (lowest) discounted price per (productId, weight-name) and per (productId, unitQty)
    so we can override original_price with the offered/deal price later. (The category/product feeds
    only carry the regular price; the storefront computes the "Deal!" price from these promos.)"""
    promos = data if isinstance(data, list) else (data.get("promotions") if isinstance(data, dict) else None)
    if not isinstance(promos, list):
        return
    for pr in promos:
        mo = (pr or {}).get("modifiedOptions") or {}
        if not isinstance(mo, dict):
            continue
        for pid, opts in mo.items():
            for opt in (opts or []):
                price = opt.get("price")
                if price is None:
                    continue
                price = round(float(price), 2)
                nm = (opt.get("name") or "").strip().lower()
                uq = opt.get("unitQty")
                keys = []
                if nm:
                    keys.append(f"n:{pid}:{nm}")
                if uq is not None:
                    keys.append(f"u:{pid}:{uq}")
                for k in keys:
                    if k not in deal_prices or price < deal_prices[k]:
                        deal_prices[k] = price


def on_response(resp):
    try:
        if "getcarrot.io" not in resp.url or resp.status != 200:
            return
        try:
            data = resp.json()
        except Exception:
            return
        carrot_hits[0] += 1
        # /store/promotion carries the deal prices; everything else is product data.
        if "/store/promotion" in resp.url and "/promotion/products" not in resp.url:
            record_promotions(data)
        else:
            process_payload(data)
    except Exception:
        pass


def dismiss_age_gate(page):
    for lab in ("Yes", "I am 21", "I'm 21", "Enter", "Confirm", "21+", "Accept"):
        try:
            page.get_by_role("button", name=lab, exact=False).first.click(timeout=1000)
            page.wait_for_timeout(500)
            return
        except Exception:
            pass


def carrot_frame(page):
    for f in page.frames:
        u = (f.url or "").lower()
        if "getcarrot" in u or "carrot" in u:
            return f
    return None


_SCROLL_INNER = """() => {
  const els = [...document.querySelectorAll('*')];
  let best = null, mh = 0;
  for (const e of els) {
    if (e.scrollHeight > e.clientHeight + 200 && e.clientHeight > 200 && e.scrollHeight > mh) {
      mh = e.scrollHeight; best = e;
    }
  }
  if (best) { best.scrollTop = best.scrollHeight; }
  window.scrollTo(0, document.body.scrollHeight);
}"""


def scroll_all(page, tag=""):
    stable = 0
    last = len(products)
    for i in range(MAX_SCROLLS):
        frame = carrot_frame(page)
        if frame:
            try:
                frame.evaluate(_SCROLL_INNER)
            except Exception:
                pass
        try:
            page.mouse.wheel(0, 6000)
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        except Exception:
            pass
        page.wait_for_timeout(1300)
        if len(products) == last:
            stable += 1
            if stable >= 5:
                break
        else:
            stable = 0
            last = len(products)
    logger.info(f"  [{tag or 'menu'}] rows {len(products)} | api hits {carrot_hits[0]}")


def click_categories(page):
    """Click each category inside the Carrot widget and scroll it fully."""
    for label in CATEGORY_LABELS:
        clicked = False
        frame = carrot_frame(page)
        targets = [frame] if frame else []
        targets.append(page)                      # some widgets render inline, not in an iframe
        for scope in targets:
            for role in ("tab", "button", "link"):
                try:
                    scope.get_by_role(role, name=label, exact=False).first.click(timeout=1200)
                    clicked = True
                    break
                except Exception:
                    pass
            if not clicked:
                try:
                    scope.get_by_text(label, exact=False).first.click(timeout=1200)
                    clicked = True
                except Exception:
                    pass
            if clicked:
                break
        if clicked:
            page.wait_for_timeout(2500)
            scroll_all(page, label)


# =========================
# MAIN
# =========================
try:
    logger.info(f"Starting {company} scrape | {dispensary_location} (Carrot via browser)")
    with sync_playwright() as p:
        launch_args = {
            "headless": HEADLESS,
            "args": ["--disable-blink-features=AutomationControlled",
                     "--disable-features=IsolateOrigins,site-per-process"],
        }
        # Prefer the user's real Chrome (beats Carrot's bot check); fall back to bundled Chromium.
        try:
            browser = p.chromium.launch(channel="chrome", **launch_args)
        except Exception:
            browser = p.chromium.launch(**launch_args)
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            viewport={"width": 1440, "height": 2200},
        )
        # Hide the automation fingerprint that Carrot/Cloudflare block on.
        ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = ctx.new_page()
        page.on("response", on_response)

        page.goto(MENU_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(6000)
        dismiss_age_gate(page)
        page.wait_for_timeout(3000)
        scroll_all(page)              # default/all view
        click_categories(page)        # walk each category tab (backup)
        # Primary: drive the widget's hash routes for each category slug (needs the store segment).
        # Each route fires GET /store/category/slug/<slug>/product -> a bare array of products.
        for slug in ["flower", "preroll", "vaporizer", "cartridge", "concentrate", "edible",
                     "drink", "beverage", "topical", "tincture", "cbd", "accessory", "merch"]:
            try:
                page.evaluate(
                    "s => { window.location.hash = '#menu/' + %r + '/category/' + s;"
                    " window.dispatchEvent(new HashChangeEvent('hashchange')); }" % STORE_SLUG,
                    slug,
                )
                page.wait_for_timeout(2500)
                scroll_all(page, slug)
            except Exception:
                pass

        browser.close()
    logger.info(f"Carrot API responses captured: {carrot_hits[0]}")
except Exception as e:
    logger.critical(f"❌ Scraper failed: {e}", exc_info=True)
    sys.exit(1)


# =========================
# SAVE JSON + CSV
# =========================
try:
    if not products:
        logger.critical("❌ No products collected — the menu may not have loaded (try HEADLESS=False).")
        sys.exit(1)

    # Apply promotion deal prices: set original_price to the OFFERED (discounted) price when a promo
    # covers this product/weight, and record the regular price as a deal note. Then drop helper keys.
    dealt = 0
    for r in products:
        pid, uq, disp = r.pop("_pid", None), r.pop("_uq", None), r.pop("_disp", None)
        wname = (r.get("weight") or disp or "").strip().lower()
        candidates = []
        for k in (f"n:{pid}:{wname}" if wname else None, f"u:{pid}:{uq}" if uq is not None else None):
            if k and k in deal_prices:
                candidates.append(deal_prices[k])
        if not candidates:
            continue
        deal = min(candidates)
        reg = r.get("original_price")
        if reg is None or deal < reg:
            r["original_price"] = deal
            if reg is not None and reg > deal:
                r["deal_1"] = f"Deal (reg ${reg:g})"
            dealt += 1
    logger.info(f"Applied deal prices to {dealt} rows (promos captured: {len(deal_prices)} option-keys)")

    with open(json_file_path, "w", encoding="utf-8") as f:
        json.dump(products, f, indent=2)
    logger.info(f"JSON saved: {json_file_path}")

    df = pd.DataFrame(products).drop_duplicates(subset=["dispensary_location", "name", "weight"])
    if df.empty or len(df) < 10:
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

logger.info(f"✅ {company} (Albuquerque) scraper completed successfully")
sys.exit(0)
