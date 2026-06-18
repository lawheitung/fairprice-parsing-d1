#!/usr/bin/env python3
"""
Filter the 'frozen' category down to raw/whole seafood + frozen vegetables/fruits.
Removes all red meat, poultry, and processed food (incl. processed seafood such as
fish balls, otah, breaded/smoked/marinated fish, dumplings, nuggets, ready meals,
ice cream, pizza, fries, etc.).

Usage:
    python3 filter_frozen.py            # dry run - prints what would be kept/removed
    python3 filter_frozen.py --apply    # actually delete from fairprice.db + rebuild .sql
"""
import sqlite3
import sys
import shutil

DB_FILE, SQL_FILE = "fairprice.db", "fairprice.sql"

# FairPrice sub-categories that hold raw seafood.
SEAFOOD_BUCKETS = {"Frozen Fish", "Frozen Shellfish & Others"}

# Markers of processing -> excluded even when the item is seafood (user wants raw/whole).
PROCESSED = (
    "marinade", "marinated", "teriyaki", "seasoned", "smoked", "smoke ",
    "kabayaki", "curd", "breaded", "crumbed", "tempura", "fried", "fish ball",
    "fishball", "fish cake", "otah", "finger", "surimi", "paste", "nugget",
)

# Genuine frozen vegetable/fruit items (no dedicated bucket exists in 'frozen').
VEG_FRUIT_KEEP = ("cauliflower rice",)


def is_processed(low):
    hits = [p for p in PROCESSED if p in low]
    # Avoid false positives: "unseasoned" != seasoned, "pasteurized" != fish paste.
    if "seasoned" in hits and "unseasoned" in low:
        hits.remove("seasoned")
    if "paste" in hits and "pasteur" in low:
        hits.remove("paste")
    return bool(hits)


def is_keep(name, primary_category):
    low = name.lower()
    if any(v in low for v in VEG_FRUIT_KEEP):
        return True
    if primary_category in SEAFOOD_BUCKETS:
        return not is_processed(low)  # raw seafood kept, processed dropped
    return False


def main():
    apply = "--apply" in sys.argv
    con = sqlite3.connect(DB_FILE)
    rows = con.execute(
        "SELECT product_id, name, primary_category FROM products WHERE category='frozen'"
    ).fetchall()

    keep_ids, keep, remove = [], [], []
    for pid, name, pc in rows:
        if is_keep(name, pc):
            keep_ids.append(pid)
            keep.append((name, pc))
        else:
            remove.append((name, pc))

    print(f"frozen total: {len(rows)}  ->  KEEP {len(keep)}  /  REMOVE {len(remove)}")
    print(f"\n--- KEEP ({len(keep)}) ---")
    for name, pc in sorted(keep):
        print(f"  [{pc}] {name}")

    if not apply:
        print("\n(dry run - nothing deleted. Re-run with --apply to delete.)")
        con.close()
        return

    shutil.copy(DB_FILE, DB_FILE + ".bak")
    placeholders = ",".join("?" * len(keep_ids))
    con.execute(
        f"DELETE FROM products WHERE category='frozen' AND product_id NOT IN ({placeholders})",
        keep_ids,
    )
    con.commit()
    remaining = con.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    frozen_left = con.execute(
        "SELECT COUNT(*) FROM products WHERE category='frozen'"
    ).fetchone()[0]

    with open(SQL_FILE, "w") as f:
        f.write("DROP TABLE IF EXISTS products;\n")
        for line in con.iterdump():
            if line.startswith(("BEGIN TRANSACTION", "COMMIT", "PRAGMA")):
                continue
            f.write(line + "\n")
    con.close()
    print(f"\nApplied. Backup at {DB_FILE}.bak")
    print(f"frozen now: {frozen_left} products   total DB: {remaining}")
    print(f"Rebuilt {SQL_FILE}")


if __name__ == "__main__":
    main()
