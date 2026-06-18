# fairprice-parsing-d1

Scrapes selected FairPrice categories into a SQLite file (`fairprice.db`) and a
D1-importable dump (`fairprice.sql`).

## Usage — manual, run once or twice a year

Everything in one command (scrape -> filter frozen -> load into D1):

```bash
./update.sh
```

`update.sh` bootstraps the Python venv if missing, so it works on a fresh
machine. It loads into the Cloudflare D1 database named **`fairprice`**
(`wrangler login` must be done first; override with `D1_NAME=other ./update.sh`).

Or run the steps by hand:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python fairprice.py             # scrape -> fairprice.db + fairprice.sql
.venv/bin/python filter_frozen.py         # dry-run: preview the frozen filter
.venv/bin/python filter_frozen.py --apply # filter 'frozen' to raw seafood + frozen veg
npx wrangler d1 execute fairprice --remote --file=fairprice.sql -y
```

Notes:
- `filter_frozen.py` must be re-run after every scrape (a fresh scrape restores
  the full frozen category). It writes a `fairprice.db.bak` backup before deleting.
- The generated SQL drops & recreates the table, so loading is idempotent — each
  run leaves D1 matching the latest scrape.
- D1 database: `fairprice` (id `ea340366-9799-45ab-9fe8-bccef0f81870`, region APAC).

## Table: `products`

| column | type | notes |
|---|---|---|
| product_id | TEXT PK | FairPrice product id |
| sku | TEXT | clientItemId |
| category | TEXT | the scraped category slug |
| primary_category | TEXT | FairPrice's own sub-category |
| name | TEXT | |
| brand | TEXT | |
| pack_raw | TEXT | display unit, e.g. `6 x 14 G` |
| qty_value / qty_unit | REAL / TEXT | best-effort parse of pack_raw |
| price | REAL | SGD, store 165 |
| in_stock | INTEGER | 0/1 |
| stock_count | INTEGER | units in stock at store 165 |
| barcode | TEXT | |
| country | TEXT | country of origin |
| nutrigrade | TEXT | SG Nutri-Grade (A–D), where labelled |
| **energy_kcal** | REAL | see Nutrition below |
| **protein_g, fat_g, saturated_fat_g, trans_fat_g** | REAL | |
| **cholesterol_mg, carb_g, sugar_g, fibre_g, sodium_mg** | REAL | |
| **serving_basis** | TEXT | the basis the nutrition values are reported on |
| url | TEXT | product page |
| scraped_at | TEXT | ISO-8601 UTC of the scrape |

## Nutrition data — read this before using it

The nutrition columns are **extracted from the product's nutrition label** as
published by FairPrice (`metaData["Nutritional Data"]`). They are NOT estimated.

- **Coverage is partial (~21% of rows).** It is high for packaged foods
  (rice/noodles/sauces ~43%) and near-zero for fresh produce and seafood. A
  `NULL` means *"no label data published"* — it does **not** mean zero.
- **Macronutrients only.** The source carries essentially no micronutrient
  (vitamin/mineral) data, so none is stored. Do not infer micros from this table.
- **Values are AS REPORTED, not normalised.** `serving_basis` records the basis
  for each row and **varies** — e.g. `Per 100g`, `Per Serving (48g)`, `Per 100 mL`.
  Two rows are only directly comparable when their `serving_basis` matches.
  Always read `serving_basis` alongside the numbers; to compare per-100g you must
  rescale using the grams in `serving_basis`.
- `energy_kcal` is in kcal (kJ-only labels are converted at 1 kcal = 4.184 kJ).
