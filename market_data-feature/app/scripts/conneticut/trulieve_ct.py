import pandas as pd
import json
import os
import gc
import copy
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
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
store_slug = "trulieve-bristol"

company = "TrulieveCT"
location = "Connecticut"
dispensary_location = "Bristol"

us_tz = pytz.timezone("America/Phoenix")

today = datetime.now(us_tz).strftime("%Y-%m-%d")

output_folder = "app/output/pending"
csv_folder = os.path.join(output_folder, "csv")

os.makedirs(csv_folder, exist_ok=True)

output_file = os.path.join(
    csv_folder,
    f"{today}_{company}.csv"
)

base_url = (
    f"https://dutchie.com/embedded-menu/"
    f"{store_slug}/products?category="
)

PAGE_SIZE = 100          # Dutchie returns up to 100 products per page
MAX_PAGES = 50           # hard safety cap per category

# =========================
# CATEGORIES
# =========================
categories = {
    "Flower": f"{base_url}flower",
    "Pre-rolls": f"{base_url}pre-rolls",
    "Vaporizers": f"{base_url}vaporizers",
    "Concentrates": f"{base_url}concentrates",
    "Edibles": f"{base_url}edibles",
    "Tinctures": f"{base_url}tinctures",
    "Topicals": f"{base_url}topicals",
    "CBD": f"{base_url}cbd",
    "Accessories": f"{base_url}accessories"
}

# =========================
# GLOBAL STORAGE
# =========================
products = []
seen_ids = set()

current_page = {"page": None}

# Captures the first FilteredProducts GET request seen for the current
# category, so we can replay it page by page. Reset each category.
captured_request = {"url": None}

# =========================
# HELPERS
# =========================
def format_potency(content):

    try:

        if not content:
            return None

        value = (content.get("range") or [None])[0]

        if value in [None, 0]:
            return None

        return value

    except Exception:
        return None


def extract_deals(item):

    deals = []

    try:

        special = item.get("specialData")

        if not special:
            return deals

        keys = [
            "saleSpecials",
            "bundleSpecials",
            "rewardSpecials",
            "conditionSpecials",
            "bogoSpecials"
        ]

        for key in keys:

            for s in special.get(key, []) or []:

                if s.get("specialName"):
                    deals.append(s["specialName"])

    except Exception:
        pass

    return list(set(deals))


def parse_items(items, inferred_category):
    """
    Parse a list of product items into the `products` list.
    Returns the number of NEW (non-duplicate) products added.
    Shared by both the passive handler and the active page replay.
    """
    added = 0

    for item in items or []:

        pid = item.get("id")

        if not pid:
            continue

        if pid in seen_ids:
            continue

        seen_ids.add(pid)
        added += 1

        deals = extract_deals(item)

        deal_columns = {
            f"deal_{i+1}": d
            for i, d in enumerate(deals)
        }

        variants = (
            item.get("POSMetaData", {})
            .get("children")
        ) or []

        if not variants:
            logger.warning(
                f"No variants for: {item.get('Name')}"
            )

        for variant in variants:

            try:

                product = {
                    "date": today,
                    "location": location,
                    "company": company,
                    "dispensary_location": dispensary_location,
                    "name": item.get("Name"),
                    "category": (
                        inferred_category
                        if inferred_category != "Default"
                        else item.get("categoryName")
                    ),
                    "subcategory": (
                        item.get("POSMetaData", {})
                        .get("canonicalCategory")
                    ),
                    "brand": item.get("brandName"),
                    "strain_type": item.get("strainType"),
                    "THC": format_potency(
                        item.get("THCContent")
                    ),
                    "CBD": format_potency(
                        item.get("CBDContent")
                    ),
                    "weight": variant.get("option"),
                    "original_price": variant.get("price"),
                }

                product.update(deal_columns)

                products.append(product)

            except Exception as variant_err:
                logger.warning(
                    f"Variant failed: {variant_err}"
                )

    return added


def category_from_variables(variables):
    """Pull a human category label out of a variables dict."""
    try:
        filter_cat = (
            variables
            .get("filter", {})
            .get("category", "")
        )
        if filter_cat:
            return filter_cat.capitalize()
    except Exception:
        pass
    return "Default"


# =========================
# RESPONSE HANDLER (passive: page 1 + request capture)
# =========================
def handle_response(response):

    try:

        pg = current_page.get("page")
        if pg is not None:
            try:
                if pg.is_closed():
                    return
            except Exception:
                return

        if "FilteredProducts" not in response.url:
            return

        if response.request.method == "OPTIONS":
            return

        # Capture the first FilteredProducts GET URL for this category
        # so the replay loop can paginate from it.
        if (
            captured_request["url"] is None
            and response.request.method == "GET"
        ):
            captured_request["url"] = response.url

        if response.status != 200:
            return

        try:
            data = response.json()
        except Exception as json_err:
            logger.warning(f"Skipping invalid JSON: {json_err}")
            return

        try:
            items = (
                data["data"]
                ["filteredProducts"]
                ["products"]
            )
        except Exception:
            return

        # Determine category from the request variables (query string).
        inferred_category = "Default"
        try:
            qs = parse_qs(urlparse(response.url).query)
            if "variables" in qs:
                variables = json.loads(qs["variables"][0])
                inferred_category = category_from_variables(variables)
        except Exception:
            pass

        parse_items(items, inferred_category)

    except Exception as handler_err:
        logger.warning(f"Response handler failed: {handler_err}")


# =========================
# ACTIVE PAGINATION (replay the captured query)
# =========================
def paginate_via_replay(page, category_name):
    """
    Dutchie's FilteredProducts query returns up to PAGE_SIZE (100) items
    per request, paged server-side via variables.page. Scrolling does NOT
    load more, so we replay the captured GET request with an incrementing
    page number until a page returns fewer than PAGE_SIZE items.

    Uses page.request so the call carries the browser's cookies/headers.
    """
    cap = captured_request["url"]

    if not cap:
        logger.warning(
            f"{category_name} | no FilteredProducts request captured; "
            f"cannot paginate (page 1 may still be parsed)"
        )
        return

    parsed = urlparse(cap)
    qs = parse_qs(parsed.query)

    # variables is a JSON blob in the query string
    try:
        base_variables = json.loads(qs.get("variables", ["{}"])[0])
    except Exception:
        logger.warning(
            f"{category_name} | could not parse variables; skipping replay"
        )
        return

    inferred_category = category_from_variables(base_variables)

    # Figure out which page we already have (page 1 came in passively).
    start_page = 0
    try:
        start_page = int(base_variables.get("page", 0))
    except Exception:
        start_page = 0

    # Page 1 already parsed by the handler. Continue from the next page.
    next_page = start_page + 1

    while next_page < MAX_PAGES:

        variables = copy.deepcopy(base_variables)
        variables["page"] = next_page
        # Some Dutchie schemas use perPage; set it if present.
        if "perPage" in variables:
            variables["perPage"] = PAGE_SIZE

        new_qs = dict(qs)
        new_qs["variables"] = [json.dumps(
            variables, separators=(",", ":")
        )]

        # rebuild query string (parse_qs gives lists; flatten)
        flat_qs = {k: v[0] for k, v in new_qs.items()}
        new_query = urlencode(flat_qs)

        replay_url = urlunparse((
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            new_query,
            parsed.fragment
        ))

        try:
            resp = page.request.get(replay_url, timeout=30000)
        except Exception as e:
            logger.warning(
                f"{category_name} | page {next_page} request failed: {e}"
            )
            break

        if resp.status != 200:
            logger.warning(
                f"{category_name} | page {next_page} "
                f"status {resp.status}; stopping"
            )
            break

        try:
            data = resp.json()
            items = (
                data["data"]
                ["filteredProducts"]
                ["products"]
            )
        except Exception:
            logger.warning(
                f"{category_name} | page {next_page} "
                f"unparseable; stopping"
            )
            break

        page_len = len(items or [])
        added = parse_items(items, inferred_category)

        logger.info(
            f"{category_name} | replay page {next_page} "
            f"| items={page_len} new={added} total={len(products)}"
        )

        # Last page: a short page means no more results.
        if page_len < PAGE_SIZE:
            break

        # Defensive: if a full page added zero new items, we're looping.
        if added == 0:
            logger.info(
                f"{category_name} | no new items on full page; stopping"
            )
            break

        next_page += 1


def wait_for_first_page(page):
    """Wait for the initial FilteredProducts response to be captured."""
    for _ in range(8):
        try:
            page.wait_for_load_state("networkidle", timeout=4000)
        except Exception:
            page.wait_for_timeout(1000)
        if captured_request["url"] is not None and len(products) > 0:
            return
    # one last grace wait
    page.wait_for_timeout(2000)


# =========================
# PLAYWRIGHT
# =========================
try:

    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=True,
            chromium_sandbox=False,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding",
                "--disable-sync",
                "--mute-audio"
            ]
        )

        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/123.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            java_script_enabled=True,
            ignore_https_errors=True
        )

        page = context.new_page()
        current_page["page"] = page

        page.set_default_timeout(30000)
        page.set_default_navigation_timeout(60000)

        page.on("response", handle_response)

        for current_category, url in categories.items():

            logger.info(f"Opening category: {current_category}")

            count_before = len(products)

            # reset per-category capture
            captured_request["url"] = None

            try:

                page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=60000
                )

                # Wait for page 1 to arrive (captured passively).
                wait_for_first_page(page)

                # Replay the query for pages 2..N.
                paginate_via_replay(page, current_category)

                gained = len(products) - count_before

                logger.info(
                    f"{current_category} | done "
                    f"| +{gained} this category "
                    f"| total {len(products)}"
                )

                if gained == 0:
                    logger.warning(
                        f"{current_category} returned ZERO products "
                        f"- check menu/category"
                    )

            except Exception as cat_err:
                logger.error(
                    f"Category failed [{current_category}] | {cat_err}"
                )

            finally:
                gc.collect()

        try:
            page.remove_listener("response", handle_response)
        except Exception:
            pass

        try:
            page.wait_for_timeout(500)
        except Exception:
            pass

        current_page["page"] = None

        page.close()
        context.close()
        browser.close()

except Exception as e:

    logger.critical(f"Playwright failed: {e}", exc_info=True)
    sys.exit(1)

# =========================
# SAVE CSV
# =========================
try:

    df = pd.DataFrame(products)

    if not df.empty:
        df.drop_duplicates(
            subset=["name", "weight"],
            inplace=True
        )

    fixed_cols = [
        "date",
        "location",
        "company",
        "dispensary_location",
        "name",
        "category",
        "subcategory",
        "brand",
        "strain_type",
        "THC",
        "CBD",
        "weight",
        "original_price"
    ]

    deal_cols = sorted(
        [c for c in df.columns if c.startswith("deal_")],
        key=lambda x: int(x.split("_")[1])
    ) if not df.empty else []

    for col in fixed_cols:
        if col not in df.columns:
            df[col] = ""

    if df.empty:
        logger.warning(
            "No records parsed - writing empty CSV with headers only"
        )
        df = pd.DataFrame(columns=fixed_cols)
    elif len(df) < 10:
        logger.warning(f"Very low product count: {len(df)}")

    df = df[fixed_cols + deal_cols]

    df.to_csv(output_file, index=False, na_rep="")

    logger.info(
        f"CSV saved successfully: {output_file} | Rows: {len(df)}"
    )

except Exception as e:
    logger.critical(f"CSV writing failure: {e}", exc_info=True)
    sys.exit(1)

logger.info("Trulieve scraper completed successfully")

sys.exit(0)