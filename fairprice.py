#!/usr/bin/env python3
"""
FairPrice -> SQLite -> ready for Cloudflare D1.  One-shot, no scheduling.

Setup:  pip install -r requirements.txt
Run:    python3 fairprice.py
Output: fairprice.db    (local SQLite - inspect it / test the bot against it)
        fairprice.sql   (import into D1, see command printed at the end)
        fairprice_raw_<category>_page1.json  (one raw page per category, to verify fields)

Field mappings below were verified against the live API response. The product
object uses `final_price`, `brand` (a dict), `barcodes` (a list), `has_stock`
and `storeSpecificData` - NOT the keys a naive guess would use.
"""

import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

CATEGORIES = [
    "rice-noodles-cooking-ingredients",
    "dairy-chilled-eggs",
    "jams,-spreads--honey",
    "frozen",
    "fruits-vegetables",
    "snacks--confectionery-1",
    "dried-fruits--nuts",
]
STORE_ID = "165"  # FairPrice fulfilment store; drives price + stock figures
API = "https://website-api.omni.fairprice.com.sg/api/product/v2"
HOME = "https://www.fairprice.com.sg"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "application/json",
}
DB_FILE, SQL_FILE = "fairprice.db", "fairprice.sql"
PAGE_PAUSE = 0.4  # seconds between page requests, be polite to the API

# best-effort quantity from a "500g" / "2L" / "6 x 14 G" style string.
# Prefer the unit-bearing token (handles "6 x 14 G" -> 14 g, not 6 x).
QTY_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(kg|g|ml|l|pcs?|pack|s)\b", re.I)


def parse_qty(text):
    """Return (value, unit) for the *last* unit-bearing number in the string.

    For "6 x 14 G" the meaningful quantity is 14 g (a 6-pack of 14 g), so we
    take the last match rather than the first.
    """
    if not text:
        return (None, "")
    matches = QTY_RE.findall(text)
    if not matches:
        return (None, "")
    value, unit = matches[-1]
    return (float(value), unit.lower())


def coerce_price(p):
    if isinstance(p, dict):
        p = p.get("value") or p.get("price")
    try:
        return float(p)
    except (TypeError, ValueError):
        return None


# --- Nutrition ---------------------------------------------------------------
# FairPrice puts a per-serving nutrition label in metaData["Nutritional Data"]
# as an HTML table, e.g.:
#   <tr><th>Attributes</th><th>Per Serving (100g)</th></tr>
#   <tr><td>Energy</td><td>628kcal</td></tr> ...
# Values are stored AS REPORTED (not normalised to per-100g); serving_basis
# records the basis so the numbers stay interpretable. Macros only - the source
# carries essentially no micronutrient data. Missing fields stay NULL.
NUTRITION_KEYS = (
    "energy_kcal", "protein_g", "fat_g", "saturated_fat_g", "trans_fat_g",
    "cholesterol_mg", "carb_g", "sugar_g", "fibre_g", "sodium_mg",
    "serving_basis",
)
# label (lowercased, stripped of leading "- ") -> column
_NUTRI_LABELS = {
    "energy": "energy_kcal", "calories": "energy_kcal",
    "protein": "protein_g",
    "total fat": "fat_g", "fat": "fat_g",
    "saturated fat": "saturated_fat_g",
    "trans fat": "trans_fat_g",
    "cholesterol": "cholesterol_mg",
    "carbohydrate": "carb_g", "carbohydrates": "carb_g",
    "sugars": "sugar_g", "sugar": "sugar_g",
    "total sugar": "sugar_g", "total sugars": "sugar_g",
    "dietary fibre": "fibre_g", "dietary fiber": "fibre_g", "fibre": "fibre_g",
    "sodium": "sodium_mg", "salt": "sodium_mg",
}
_ROW_RE = re.compile(r"<tr>\s*<td>(.*?)</td>\s*<td>(.*?)</td>\s*</tr>", re.I | re.S)
_HDR_RE = re.compile(r"<th>.*?</th>\s*<th>(.*?)</th>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _num(text):
    m = _NUM_RE.search(text.replace(",", ""))
    return float(m.group()) if m else None


def parse_nutrition(html):
    """Parse the metaData Nutritional Data HTML table -> dict of NUTRITION_KEYS."""
    out = {k: None for k in NUTRITION_KEYS}
    if not html or "<tr" not in html.lower():
        return out
    hdr = _HDR_RE.search(html)
    if hdr:
        basis = _TAG_RE.sub("", hdr.group(1)).strip()
        out["serving_basis"] = basis or None
    for raw_label, raw_value in _ROW_RE.findall(html):
        label = _TAG_RE.sub("", raw_label).strip().lstrip("- ").lower()
        if label in ("attributes", ""):
            continue
        col = _NUTRI_LABELS.get(label)
        if not col or out.get(col) is not None:
            continue  # unknown row, or already filled (keep first occurrence)
        value = _TAG_RE.sub("", raw_value).strip()
        n = _num(value)
        if n is None:
            continue
        # kJ -> kcal so the energy column has a single unit
        if col == "energy_kcal" and "kj" in value.lower() and "kcal" not in value.lower():
            n = round(n / 4.184, 1)
        out[col] = n
    return out


def get_session():
    s = requests.Session()
    s.headers.update(HEADERS)
    retry = Retry(
        total=4,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.get(HOME, timeout=30)  # warms the connect.sid cookie the API wants
    return s


def store_data(product):
    """The per-store block matching STORE_ID, else the first one, else {}."""
    blocks = product.get("storeSpecificData") or []
    if not isinstance(blocks, list):
        return {}
    for b in blocks:
        if str(b.get("storeId")) == STORE_ID:
            return b
    return blocks[0] if blocks else {}


def first_barcode(product):
    codes = product.get("barcodes")
    if isinstance(codes, list) and codes:
        return str(codes[0])
    return str(codes) if codes else ""


def parse_product(p, slug, scraped_at):
    name = p.get("name", "") or ""
    meta = p.get("metaData") or {}
    sd = store_data(p)

    pack = meta.get("DisplayUnit") or p.get("displayUnit") or ""
    qv, qu = parse_qty(pack)
    if qv is None:
        qv, qu = parse_qty(name)

    brand = p.get("brand") or {}
    brand_name = brand.get("name", "") if isinstance(brand, dict) else str(brand)

    primary = p.get("primaryCategory") or {}
    primary_name = primary.get("name", "") if isinstance(primary, dict) else ""

    price = coerce_price(p.get("final_price"))
    if price is None:
        price = coerce_price(sd.get("mrp"))

    has_stock = p.get("has_stock")
    in_stock = 1 if has_stock else 0
    stock_count = sd.get("stock")
    try:
        stock_count = int(stock_count) if stock_count is not None else None
    except (TypeError, ValueError):
        stock_count = None

    product_id = str(p.get("id") or p.get("productId") or "")
    product_slug = p.get("slug") or ""
    url = f"{HOME}/product/{product_slug}" if product_slug else f"{HOME}/product/{product_id}"

    row = {
        "product_id": product_id,
        "sku": str(p.get("clientItemId", "") or ""),
        "category": slug,
        "primary_category": primary_name,
        "name": name,
        "brand": brand_name,
        "pack_raw": pack,
        "qty_value": qv,
        "qty_unit": qu,
        "price": price,
        "in_stock": in_stock,
        "stock_count": stock_count,
        "barcode": first_barcode(p),
        "country": meta.get("Country of Origin", "") or "",
        "nutrigrade": meta.get("Nutrigrade", "") or "",
        "url": url,
        "scraped_at": scraped_at,
    }
    row.update(parse_nutrition(meta.get("Nutritional Data") or ""))
    return row


def fetch_category(s, slug, scraped_at):
    rows, page, total, expected = [], 1, 1, None
    while page <= total:
        r = s.get(API, timeout=30,
                  params={"category": slug, "url": slug, "pageType": "category",
                          "storeId": STORE_ID, "page": page},
                  headers={"Referer": f"{HOME}/category/{slug}"})
        r.raise_for_status()
        data = r.json()
        block = data.get("data", data)

        if page == 1:
            with open(f"fairprice_raw_{slug}_page1.json", "w") as f:
                json.dump(data, f, indent=2)  # inspect if columns come out empty
            pagination = block.get("pagination") or {}
            total = pagination.get("total_pages", 1) or 1
            expected = block.get("count")

        items = block.get("product") or block.get("products") or []
        for p in items:
            rows.append(parse_product(p, slug, scraped_at))

        print(f"  {slug}: page {page}/{total} -> {len(items)} items")
        page += 1
        time.sleep(PAGE_PAUSE)

    if expected is not None and len(rows) != expected:
        print(f"  !! {slug}: expected {expected} items, got {len(rows)} "
              f"(possible short read)", file=sys.stderr)
    return rows


COLUMNS = (
    "product_id", "sku", "category", "primary_category", "name", "brand",
    "pack_raw", "qty_value", "qty_unit", "price", "in_stock", "stock_count",
    "barcode", "country", "nutrigrade",
    # nutrition (macros only; NULL where the product carries no label data) --
    "energy_kcal", "protein_g", "fat_g", "saturated_fat_g", "trans_fat_g",
    "cholesterol_mg", "carb_g", "sugar_g", "fibre_g", "sodium_mg", "serving_basis",
    "url", "scraped_at",
)


def write_outputs(rows):
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("DROP TABLE IF EXISTS products")
    cur.execute("""CREATE TABLE products (
        product_id       TEXT PRIMARY KEY,
        sku              TEXT,
        category         TEXT,
        primary_category TEXT,
        name             TEXT,
        brand            TEXT,
        pack_raw         TEXT,
        qty_value        REAL,
        qty_unit         TEXT,
        price            REAL,
        in_stock         INTEGER,
        stock_count      INTEGER,
        barcode          TEXT,
        country          TEXT,
        nutrigrade       TEXT,
        energy_kcal      REAL,
        protein_g        REAL,
        fat_g            REAL,
        saturated_fat_g  REAL,
        trans_fat_g      REAL,
        cholesterol_mg   REAL,
        carb_g           REAL,
        sugar_g          REAL,
        fibre_g          REAL,
        sodium_mg        REAL,
        serving_basis    TEXT,
        url              TEXT,
        scraped_at       TEXT)""")
    cur.execute("CREATE INDEX idx_products_category ON products(category)")
    cur.execute("CREATE INDEX idx_products_name ON products(name)")
    placeholders = ",".join(["?"] * len(COLUMNS))
    # rows are dicts -> order values by COLUMNS for the positional insert.
    values = [tuple(r.get(c) for c in COLUMNS) for r in rows]
    # INSERT OR REPLACE so a product appearing in two categories upserts cleanly.
    cur.executemany(
        f"INSERT OR REPLACE INTO products ({','.join(COLUMNS)}) VALUES ({placeholders})",
        values,
    )
    con.commit()
    # D1-friendly dump: drop the transaction wrappers, keep it re-runnable
    with open(SQL_FILE, "w") as f:
        f.write("DROP TABLE IF EXISTS products;\n")
        for line in con.iterdump():
            if line.startswith(("BEGIN TRANSACTION", "COMMIT", "PRAGMA")):
                continue
            f.write(line + "\n")
    inserted = cur.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    con.close()
    return inserted


def main():
    scraped_at = datetime.now(timezone.utc).isoformat()
    s = get_session()
    rows = []
    for slug in CATEGORIES:
        print(f"Fetching {slug} ...")
        try:
            rows += fetch_category(s, slug, scraped_at)
        except Exception as e:
            print(f"  !! {slug} failed: {e}", file=sys.stderr)
    if not rows:
        print("No rows parsed. Open a fairprice_raw_*_page1.json, check the real "
              "field names, then adjust the keys above.", file=sys.stderr)
        return
    unique = write_outputs(rows)
    print(f"\nDone: {len(rows)} rows fetched, {unique} unique products "
          f"-> {DB_FILE} and {SQL_FILE}")
    print(f"Load into D1:  wrangler d1 execute <your-d1-name> --remote --file={SQL_FILE}")


if __name__ == "__main__":
    main()
