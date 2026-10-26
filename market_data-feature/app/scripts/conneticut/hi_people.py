#!/usr/bin/env python3
# =============================================================================
# Hi! People (CONNECTICUT) — multi-store menu scraper
# -----------------------------------------------------------------------------
# PLATFORM: Blaze ecommerce (ecom-api.blaze.me). Unlike the Dutchie sites, this
# is a clean public JSON:API REST endpoint with NO Cloudflare bot challenge, so
# plain `requests` works server-side (no browser needed).
#
# =============================  DISCOVERED API MAP  ==========================
#   Base ........ https://ecom-api.blaze.me
#   Endpoint .... GET /api/v1/products/?limit=<n>&offset=<m>&delivery_type=pickup
#                 (optional: &category=<slug>, &on_sale=true)
#   Headers (REQUIRED):
#       Accept    : application/vnd.api+json
#       X-App-Mode: default
#       X-Group   : 129c3b15-fc3d-48d1-9859-25390492539d   (the Hi! People group)
#       X-Store   : <per-store UUID>                       (selects the store)
#   Pagination .. limit/offset; total in meta.total_count
#   Response .... { data:[...], meta:{limit,offset,total_count}, included:[...] }
#       included holds product_brands / global_brands / product_categories used
#       to resolve brand & category names via each product's relationships.
#   Prices are in CENTS (amount/100). Multiple price tiers live in
#       attributes.weight_prices (flower by weight) or attributes.unit_prices.
#
#   PRODUCT FIELD MAP (Blaze attributes -> output column):
#       name                         -> Product Name
#       relationships.brand    -> included brand.name      -> Brand
#       relationships.category -> included category.name   -> Category (+parent=Sub Category)
#       flower_type                  -> Strain (Indica/Sativa/Hybrid)
#       weight_prices[]|unit_prices[]-> one row per tier (Weight/Size + Price + Promo Price)
#       thc {amount,units} / potency -> THC ; cbd / potency -> CBD
#       discount {amount,type}       -> Discount ("14%"); on_sale -> Current Deals
#       pos_inventory.quantity       -> Available Qty
#
#   CONNECTICUT STORES (adult-use / Recreational):
#       Derby     90 Pershing Dr, Derby, CT 06418     X-Store bb0f32cf-4fad-4bb7-9b61-a361f7843703
#       Hartford  306 Market St, Hartford, CT 06103   X-Store 5a9689d8-5882-4457-a3f1-6159d21d1cef
#       (New Haven, 1041 State St, is "coming soon" Summer 2026 — no menu yet.)
#
# SETUP:  pip install requests openpyxl
# RUN:    python scrape_hipeople_ct.py
# =============================================================================

import time
import logging
from datetime import datetime

import requests
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Border, Side, Alignment
from openpyxl.utils import get_column_letter

SITE_NAME = "HiPeople_CT"
BASE      = "https://ecom-api.blaze.me"
GROUP_ID  = "129c3b15-fc3d-48d1-9859-25390492539d"
PAGE_SIZE = 100
DELAY_S   = 0.4
TIMEOUT   = 30
RETRIES   = 1
TODAY     = datetime.now().strftime("%Y-%m-%d")
OUTPUT_XLSX = f"{SITE_NAME}_AllStores_{TODAY}.xlsx"

CT_STORES = [
    {"name": "Derby",    "store_id": "bb0f32cf-4fad-4bb7-9b61-a361f7843703", "address": "90 Pershing Dr, Derby, CT 06418"},
    {"name": "Hartford", "store_id": "5a9689d8-5882-4457-a3f1-6159d21d1cef", "address": "306 Market St, Hartford, CT 06103"},
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)


def headers(store_id):
    return {"Accept": "application/vnd.api+json", "X-App-Mode": "default",
            "X-Group": GROUP_ID, "X-Store": store_id,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"}


def get_page(store_id, offset):
    url = f"{BASE}/api/v1/products/?limit={PAGE_SIZE}&offset={offset}&delivery_type=pickup"
    last = None
    for attempt in range(RETRIES + 1):
        try:
            r = requests.get(url, headers=headers(store_id), timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception as e:                                  # noqa: BLE001
            last = e
            if attempt < RETRIES:
                time.sleep(1.0)
    raise RuntimeError(f"products fetch failed (offset {offset}): {last}")


def money(m):
    return (m["amount"] / 100) if (isinstance(m, dict) and m.get("amount") is not None) else None


def potency(attrs, key):
    # THC/CBD live in attributes.thc/cbd ({amount,units}); some items use
    # attributes.potency (mg). Cannabis products always have one of these.
    # Accessories / Apparel / Gift Cards have NO potency in Blaze -> blank (expected).
    v = attrs.get(key)
    if isinstance(v, dict) and v.get("amount") is not None:
        return f"{v['amount']} {v.get('units', '')}".strip()
    mx = attrs.get("max_" + key)
    if isinstance(mx, dict) and mx.get("amount") is not None:
        return f"{mx['amount']} {mx.get('units', '')}".strip()
    p = attrs.get("potency")
    if isinstance(p, dict) and p.get(key):
        return f"{p[key]} {p.get('units', '')}".strip()
    return ""


def derive_subcategory(cat, name):
    # Blaze categories are FLAT (no parent/child) and products have no subcategory
    # field, so we derive a product-form sub-category from the product name.
    n = (name or "").lower()
    c = (cat or "").lower()
    has = lambda *ws: any(w in n for w in ws)
    if "vape" in c:
        if has("disposable", "all-in-one", "all in one", "aio"): return "Disposable"
        if "pod" in n: return "Pod"
        if "cart" in n: return "Cartridge"
        return "Vape"
    if "infused" in c and "pre" in c:
        return "Infused Pre-Roll"
    if ("pre" in c and "roll" in c) or c == "preroll":
        if has("infused"): return "Infused Pre-Roll"
        if has("blunt"): return "Blunt"
        if has("2pk", "3pk", "5pk", "7pk", "pack", "multi"): return "Multi-Pack"
        return "Pre-Roll"
    if "flower" in c:
        if has("pre-pack", "prepack", "pre pack", "pre-packed"): return "Pre-Pack"
        if has("smalls"): return "Smalls"
        if has("shake", "trim"): return "Shake"
        if has("popcorn"): return "Popcorn"
        return "Whole Flower"
    if "edible" in c:
        if has("gummy", "gummies", "gumm"): return "Gummies"
        if has("chocolate"): return "Chocolate"
        if has("caramel"): return "Caramels"
        if has("mint"): return "Mints"
        if has("cookie", "brownie", "baked", "bar"): return "Baked Goods"
        if has("capsule", "tablet", "pill", "softgel"): return "Capsules"
        if has("lozenge", "hard candy", "chew", "taffy"): return "Hard Candy"
        if has("honey", "syrup", "sugar", "tincture"): return "Tinctures/Sugar"
        return "Edible"
    if "drink" in c:
        if has("seltzer"): return "Seltzer"
        if has("soda"): return "Soda"
        if has("shot"): return "Shot"
        return "Beverage"
    if "concentrate" in c:
        if has("rosin"): return "Rosin"
        if has("resin"): return "Resin"
        if has("badder", "batter"): return "Badder"
        if has("budder"): return "Budder"
        if has("wax"): return "Wax"
        if has("sugar"): return "Sugar"
        if has("diamond"): return "Diamonds"
        if has("sauce"): return "Sauce"
        if has("shatter"): return "Shatter"
        if has("hash"): return "Hash"
        if has("kief"): return "Kief"
        return "Concentrate"
    return ""  # Accessories / Apparel / Gift Cards: no cannabis sub-category


def scrape():
    product_rows, deal_rows = [], []
    for store in CT_STORES:
        logger.info("=== %s ===", store["name"])
        offset, total = 0, None
        while True:
            j = get_page(store["store_id"], offset)
            total = (j.get("meta") or {}).get("total_count")
            inc = {f"{x['type']}:{x['id']}": x for x in (j.get("included") or [])}

            def cat_of(cid):
                c = inc.get(f"product_categories:{cid}")
                return (c["attributes"].get("name"), c["attributes"].get("parent_category_id")) if c else (None, None)

            for p in (j.get("data") or []):
                a = p["attributes"]
                rel = p.get("relationships") or {}
                # brand
                brand = ""
                br = (rel.get("brand") or {}).get("data")
                if br:
                    b = inc.get(f"product_brands:{br['id']}") or inc.get(f"global_brands:{br['id']}")
                    brand = b["attributes"].get("name", "") if b else ""
                # category (flat in Blaze) + derived product-form sub-category
                cat = ""
                cr = (rel.get("category") or {}).get("data")
                if cr:
                    cname, _ = cat_of(cr["id"])
                    cat = cname or ""
                sub = derive_subcategory(cat, a.get("name"))
                disc = f"{a['discount']['amount']}{a['discount'].get('type', '')}" if a.get("discount") else ""
                deal_name = (disc + " OFF" if disc else "On Sale") if a.get("on_sale") else ""
                qty = ""
                pi = a.get("pos_inventory")
                if isinstance(pi, dict) and pi.get("quantity") is not None:
                    qty = pi["quantity"]
                elif isinstance(pi, (int, float)):
                    qty = pi
                tiers = (a.get("weight_prices") or a.get("unit_prices")
                         or [{"price": a.get("unit_price"),
                              "display_name": (a.get("size") or {}).get("display_text", ""),
                              "discount_price": a.get("discount_price")}])
                for t in tiers:
                    weight = t.get("weight") or t.get("name") or t.get("display_name") or (a.get("size") or {}).get("display_text", "")
                    price = money(t.get("price") or t.get("unit_price")) if (t.get("price") or t.get("unit_price")) else money(t)
                    promo = money(t.get("discount_price"))
                    product_rows.append({
                        "Store": store["name"], "Store ID": store["store_id"], "Product Name": a.get("name"),
                        "Brand": brand, "Category": cat, "Sub Category": sub, "Weight/Size": weight,
                        "THC": potency(a, "thc"), "CBD": potency(a, "cbd"), "Strain": a.get("flower_type") or "",
                        "Price": price, "Promo Price": promo, "Discount": disc,
                        "Current Deals": deal_name, "Available Qty": qty})
                    if promo is not None or a.get("on_sale"):
                        deal_rows.append({
                            "Store": store["name"], "Store ID": store["store_id"], "Deal Name": deal_name,
                            "Deal Short": disc, "Product Name": a.get("name"), "Brand": brand, "Category": cat,
                            "Sub Category": sub, "Variant": weight, "THC": potency(a, "thc"),
                            "Original Price": price, "Promo Price": promo, "Discount": disc})
            offset += PAGE_SIZE
            logger.info("  %s: %d/%s", store["name"], min(offset, total or offset), total)
            if total is None or offset >= total:
                break
            time.sleep(DELAY_S)
    return product_rows, deal_rows


# ----------------------------------------------------------------------------- Excel
HEADER_FILL = PatternFill("solid", fgColor="2E7D32")
HEADER_FONT = Font(bold=True, color="FFFFFF")
THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
CENTER = Alignment(horizontal="center", vertical="center")
PRODUCT_COLS = ["Store", "Store ID", "Product Name", "Brand", "Category", "Sub Category",
                "Weight/Size", "THC", "CBD", "Strain", "Price", "Promo Price", "Discount",
                "Current Deals", "Available Qty"]
DEAL_COLS = ["Store", "Store ID", "Deal Name", "Deal Short", "Product Name", "Brand",
             "Category", "Sub Category", "Variant", "THC", "Original Price", "Promo Price", "Discount"]
STORE_COLS = ["Store Name", "Store ID", "Location/Address", "Sale Types",
              "Product Count", "Deal Count", "Last Scraped"]


def write_sheet(ws, cols, rows):
    ws.append(cols)
    for c in range(1, len(cols) + 1):
        cell = ws.cell(row=1, column=c)
        cell.fill, cell.font, cell.border, cell.alignment = HEADER_FILL, HEADER_FONT, BORDER, CENTER
    for r in rows:
        ws.append([r.get(c) for c in cols])
    for row in ws.iter_rows(min_row=2, max_row=ws.max_row, max_col=len(cols)):
        for cell in row:
            cell.border = BORDER
    for i, col in enumerate(cols, start=1):
        width = max([len(str(col))] + [len(str(r.get(col) or "")) for r in rows]) + 2
        ws.column_dimensions[get_column_letter(i)].width = min(40, width)
    ws.freeze_panes = "A2"


def build_excel(products, deals):
    pc, dc = {}, {}
    for r in products:
        pc[r["Store ID"]] = pc.get(r["Store ID"], 0) + 1
    for r in deals:
        dc[r["Store ID"]] = dc.get(r["Store ID"], 0) + 1
    store_rows = [{"Store Name": f"Hi! People - {s['name']}", "Store ID": s["store_id"],
                   "Location/Address": s["address"], "Sale Types": "Recreational",
                   "Product Count": pc.get(s["store_id"], 0), "Deal Count": dc.get(s["store_id"], 0),
                   "Last Scraped": TODAY} for s in CT_STORES]
    wb = Workbook()
    write_sheet(wb.active, DEAL_COLS, deals); wb.active.title = "Deals"
    write_sheet(wb.create_sheet("Products"), PRODUCT_COLS, products)
    write_sheet(wb.create_sheet("Stores"), STORE_COLS, store_rows)
    wb.save(OUTPUT_XLSX)
    logger.info("Saved %s (Products=%d, Deals=%d, Stores=%d)", OUTPUT_XLSX, len(products), len(deals), len(store_rows))


if __name__ == "__main__":
    logger.info("Starting %s scrape | stores=%d", SITE_NAME, len(CT_STORES))
    products, deals = scrape()
    if not products:
        raise SystemExit("No products scraped — check headers / store IDs.")
    build_excel(products, deals)
    logger.info("Done.")