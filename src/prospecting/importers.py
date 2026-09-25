"""Load pingtree exports into the knowledge base.

- Revenue: 'LenderTierLeadCount' xlsx (one row per buyer tier).
- Filters: 'lender_tier_filters' csv, no header: buyer, tier, filter type, filter value.
- Buyer reference: data/reference/buyers.csv, maintained by hand (aliases, merges, categories).
"""
import csv
import sqlite3
from pathlib import Path

import openpyxl

from .names import is_price_reject, looks_like_filter_value, split_buyer_tier

REVENUE_COLUMNS = {
    "Name": "raw_name", "Processed": "processed", "Sent": "sent", "Unique Sold": "unique_sold",
    "Multi Sell": "multi_sell", "Total Sold": "total_sold", "Declined": "declined", "Error": "error",
    "Redirected": "redirected", "Commission": "commission", "EPL": "epl", "Response Time": "response_secs",
}
REFERENCE_FIELDS = ["alias", "canonical_name", "category", "status", "website", "notes"]


def _new_batch(conn, source, path, period):
    cur = conn.execute("INSERT INTO import_batch (source, file_name, period) VALUES (?, ?, ?)",
                       (source, Path(path).name, period))
    return cur.lastrowid


def _num(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return v
    s = str(v).replace("secs", "").replace("%", "").replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def import_revenue(conn: sqlite3.Connection, path: Path, period: str) -> dict:
    """Replace revenue data with the given export. Returns counts for reporting."""
    ws = openpyxl.load_workbook(path, data_only=True, read_only=True).active
    rows = list(ws.iter_rows(values_only=True))
    header_idx = next(i for i, r in enumerate(rows) if r and r[0] == "Name")
    header = [str(h).strip() if h else "" for h in rows[header_idx]]
    missing = [c for c in REVENUE_COLUMNS if c not in header]
    if missing:
        raise ValueError(f"Revenue file is missing columns: {missing}")
    idx = {REVENUE_COLUMNS[h]: i for i, h in enumerate(header) if h in REVENUE_COLUMNS}

    conn.execute("DELETE FROM tier_revenue")
    batch = _new_batch(conn, "revenue", path, period)
    seen, dupes, loaded = set(), 0, 0
    for r in rows[header_idx + 1:]:
        if not r or not r[0]:
            continue
        if r in seen:              # the export occasionally repeats a row verbatim
            dupes += 1
            continue
        seen.add(r)
        rec = {k: r[i] for k, i in idx.items()}
        alias, tier = split_buyer_tier(str(rec["raw_name"]))
        conn.execute(
            """INSERT INTO tier_revenue (batch_id, raw_name, alias, tier_name, is_price_reject,
                   processed, sent, unique_sold, multi_sell, total_sold, declined, error, redirected,
                   commission, epl, response_secs)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (batch, rec["raw_name"], alias, tier, int(is_price_reject(tier)),
             *(_num(rec[k]) for k in ("processed", "sent", "unique_sold", "multi_sell", "total_sold",
                                      "declined", "error", "redirected", "commission", "epl",
                                      "response_secs"))))
        loaded += 1
    conn.commit()
    return {"rows": loaded, "duplicates_skipped": dupes}


def import_filters(conn: sqlite3.Connection, path: Path) -> dict:
    conn.execute("DELETE FROM tier_filter")
    batch = _new_batch(conn, "filters", path, None)
    loaded, unnamed = 0, 0
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.reader(f):
            if len(row) < 4 or not row[0].strip():
                continue
            alias, raw_tier, ftype, value = (x.strip() for x in row[:4])
            tier = None if looks_like_filter_value(raw_tier) else raw_tier
            unnamed += tier is None
            conn.execute(
                """INSERT INTO tier_filter (batch_id, alias, tier_name, raw_tier, filter_type, filter_value)
                   VALUES (?,?,?,?,?,?)""", (batch, alias, tier, raw_tier, ftype, value))
            loaded += 1
    conn.commit()
    return {"rows": loaded, "unnamed_tier_rows": unnamed}


def load_reference(conn: sqlite3.Connection, path: Path) -> dict:
    """Rebuild buyer + alias tables from the hand-maintained reference CSV."""
    conn.execute("DELETE FROM buyer_alias")
    conn.execute("DELETE FROM buyer")
    n = 0
    with open(path, newline="", encoding="utf-8-sig") as f:
        for rec in csv.DictReader(f):
            alias = rec["alias"].strip()
            canonical = (rec.get("canonical_name") or "").strip() or alias
            conn.execute("INSERT OR IGNORE INTO buyer (canonical_name) VALUES (?)", (canonical,))
            # Attributes may be given on any alias row of the buyer; first non-empty wins.
            for field in ("category", "status", "website", "notes"):
                val = (rec.get(field) or "").strip()
                if val:
                    conn.execute(f"UPDATE buyer SET {field} = COALESCE({field}, ?) WHERE canonical_name = ?",
                                 (val, canonical))
            conn.execute("INSERT OR REPLACE INTO buyer_alias (alias, buyer_id) "
                         "SELECT ?, id FROM buyer WHERE canonical_name = ?", (alias, canonical))
            n += 1
    conn.commit()
    return {"aliases": n}


def unknown_aliases(conn: sqlite3.Connection) -> list[str]:
    """Aliases in the imports that the reference file doesn't cover yet."""
    return [r[0] for r in conn.execute(
        """SELECT alias FROM tier_revenue UNION SELECT alias FROM tier_filter
           EXCEPT SELECT alias FROM buyer_alias ORDER BY 1""")]


def append_reference_template(conn: sqlite3.Connection, path: Path) -> int:
    """Add a blank row to the reference CSV for every unknown alias."""
    missing = unknown_aliases(conn)
    new_file = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=REFERENCE_FIELDS)
        if new_file:
            w.writeheader()
        for alias in missing:
            w.writerow({"alias": alias, "canonical_name": alias})
    return len(missing)
