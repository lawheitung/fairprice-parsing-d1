#!/usr/bin/env bash
#
# Annual refresh: scrape FairPrice -> filter frozen -> load into Cloudflare D1.
#
# Run once a year:
#     ./update.sh
#
# The D1 table is fully rebuilt each run (the generated SQL drops & recreates it),
# so this is safe to re-run and always leaves D1 matching the latest scrape.
#
# Override the D1 database name if yours differs:
#     D1_NAME=my-db ./update.sh
#
set -euo pipefail
cd "$(dirname "$0")"

D1_NAME="${D1_NAME:-fairprice}"
PY=".venv/bin/python"

# 0/3  Bootstrap the Python env if it's missing (e.g. fresh machine next year).
if [ ! -x "$PY" ]; then
  echo "==> Setting up Python environment ..."
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi

echo "==> 1/3 Scraping FairPrice (all categories) ..."
"$PY" fairprice.py

echo "==> 2/3 Filtering 'frozen' to raw seafood + frozen veg ..."
"$PY" filter_frozen.py --apply

echo "==> 3/3 Loading into Cloudflare D1 ('$D1_NAME') ..."
npx wrangler d1 execute "$D1_NAME" --remote --file=fairprice.sql -y

echo "==> Done. D1 database '$D1_NAME' now matches the latest scrape."
