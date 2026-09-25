"""Load company research (JSON) into company / company_fact.

Research file format: a list of
  {"buyer": str, "website": str|null, "confidence": "high|medium|low",
   "facts": [{"field": str, "value": any, "source_url": str, "quote": str}], "notes": str}
Every fact must carry a source_url; facts without one are skipped and counted.
"""
import json
import sqlite3
from pathlib import Path

SOURCE = "web research (client profiling)"
SUMMARY_FIELDS = ("company_type", "tribe", "products", "parent_or_servicer", "related_brands", "hq_location",
                  "size_signals", "litigation_or_regulatory")


def _text(value) -> str:
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value)
    return str(value)


def load_research(conn: sqlite3.Connection, paths: list[Path]) -> dict:
    """Replace client-research companies with the contents of the given files."""
    conn.execute("DELETE FROM company_fact WHERE company_id IN (SELECT id FROM company WHERE source = ?)", (SOURCE,))
    conn.execute("DELETE FROM company WHERE source = ?", (SOURCE,))
    companies = facts = skipped = unmatched = 0
    for path in paths:
        for rec in json.loads(Path(path).read_text()):
            row = conn.execute(
                "SELECT b.id FROM buyer_alias a JOIN buyer b ON b.id = a.buyer_id WHERE a.alias = ? "
                "UNION SELECT id FROM buyer WHERE canonical_name = ?", (rec["buyer"], rec["buyer"])).fetchone()
            buyer_id = row[0] if row else None
            unmatched += buyer_id is None
            cur = conn.execute(
                "INSERT INTO company (name, website, category, buyer_id, source, source_url) VALUES (?,?,?,?,?,?)",
                (rec["buyer"], rec.get("website"), None, buyer_id, SOURCE, None))
            cid = cur.lastrowid
            companies += 1
            conn.execute("INSERT INTO company_fact (company_id, field, value, source_url, quote) VALUES (?,?,?,?,?)",
                         (cid, "research_confidence", rec.get("confidence") or "low", "n/a", rec.get("notes")))
            for f in rec.get("facts") or []:
                if f.get("value") in (None, "", []) :
                    continue
                if not f.get("source_url"):
                    skipped += 1
                    continue
                conn.execute(
                    "INSERT INTO company_fact (company_id, field, value, source_url, quote) VALUES (?,?,?,?,?)",
                    (cid, f["field"], _text(f["value"]), f["source_url"], f.get("quote")))
                facts += 1
    conn.commit()
    return {"companies": companies, "facts": facts, "skipped_no_source": skipped, "unmatched": unmatched}


def research_by_buyer(conn: sqlite3.Connection) -> dict[str, dict]:
    """Per canonical buyer: website, confidence, notes and one summary value per field."""
    out: dict[str, dict] = {}
    rows = conn.execute(
        """SELECT b.canonical_name AS buyer, c.website, f.field, f.value, f.quote
           FROM company c JOIN buyer b ON b.id = c.buyer_id
           JOIN company_fact f ON f.company_id = c.id
           WHERE c.source = ? ORDER BY f.id""", (SOURCE,))
    for r in rows:
        d = out.setdefault(r["buyer"], {"research_website": r["website"]})
        if r["field"] == "research_confidence":
            d["research_confidence"] = r["value"]
            d["research_notes"] = r["quote"]
        elif r["field"] in SUMMARY_FIELDS:
            key = f"r_{r['field']}"
            d[key] = r["value"] if key not in d else f"{d[key]}; {r['value']}"
    return out


def fact_rows(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        """SELECT COALESCE(b.canonical_name, c.name) AS buyer, c.website, f.field, f.value,
                  f.source_url, f.quote, f.verified_by
           FROM company c LEFT JOIN buyer b ON b.id = c.buyer_id
           JOIN company_fact f ON f.company_id = c.id
           WHERE c.source = ? AND f.field != 'research_confidence'
           ORDER BY 1, f.id""", (SOURCE,))]


PROSPECT_PREFIX = "research."


def load_prospect_research(conn: sqlite3.Connection, paths: list[Path], company_source: str) -> dict:
    """Attach prospect research facts to universe companies (matched on exact company name).

    Facts are stored with a 'research.' field prefix so universe rebuilds and research reloads
    don't clobber each other.
    """
    ids = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM company WHERE source = ?", (company_source,))}
    conn.execute(f"DELETE FROM company_fact WHERE field LIKE '{PROSPECT_PREFIX}%' "
                 "AND company_id IN (SELECT id FROM company WHERE source = ?)", (company_source,))
    stats = {"companies": 0, "facts": 0, "skipped_no_source": 0, "unmatched": []}
    for path in paths:
        for rec in json.loads(Path(path).read_text()):
            cid = ids.get(" ".join(rec["buyer"].split()))
            if cid is None:
                stats["unmatched"].append(rec["buyer"])
                continue
            stats["companies"] += 1
            if rec.get("website"):
                conn.execute("UPDATE company SET website = ? WHERE id = ?", (rec["website"], cid))
            conn.execute("INSERT INTO company_fact (company_id, field, value, source_url, quote) VALUES (?,?,?,?,?)",
                         (cid, PROSPECT_PREFIX + "research_confidence", rec.get("confidence") or "low", "n/a",
                          rec.get("notes")))
            for f in rec.get("facts") or []:
                if f.get("value") in (None, "", []):
                    continue
                if not f.get("source_url"):
                    stats["skipped_no_source"] += 1
                    continue
                conn.execute("INSERT INTO company_fact (company_id, field, value, source_url, quote) VALUES (?,?,?,?,?)",
                             (cid, PROSPECT_PREFIX + f["field"], _text(f["value"]), f["source_url"], f.get("quote")))
                stats["facts"] += 1
    conn.commit()
    return stats
