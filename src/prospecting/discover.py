"""Build the prospect universe from the CFPB consumer complaint database.

Every company with complaints about installment loans, payday loans or personal lines of credit in the
look-back window becomes a candidate. Complaint volume is used as a rough size signal (more customers,
more complaints) and the complaint states as a rough footprint. Candidates are then:
  - matched to existing buyers (via data/reference/company_matches.csv, maintained by hand), and
  - screened out when they are clearly not a subprime lead buyer (banks, BNPL, prime lenders, ...).
Screening only labels rows; nothing is deleted, so the rules can be reviewed.
"""
import csv
import json
import re
import sqlite3
import time
import urllib.parse
from datetime import date, timedelta
from pathlib import Path

from .fetch import _get

API = "https://www.consumerfinance.gov/data-research/consumer-complaints/search/api/v1/"
PRODUCT = "Payday loan, title loan, personal loan, or advance loan"
SUB_PRODUCTS = ("Installment loan", "Payday loan", "Personal line of credit")
SOURCE = "CFPB consumer complaint database"

# Name patterns for companies that are not subprime lead buyers. Checked case-insensitively.
SCREEN_RULES = [
    ("bank or credit union", r"\b(bank|bancorp|banc|n\.a\.|national association|credit union|federal savings|"
                             r"truist|wells fargo|citibank|santander|fifth third|"
                             r"jpmorgan|capital one|us bancorp|pnc|td bank|regions|huntington|keycorp|"
                             r"citizens financial|synchrony|american express|barclays|ally financial)\b"),
    ("BNPL / payments", r"\b(affirm|klarna|afterpay|sezzle|zip co|block, inc|paypal|bread financial|"
                        r"splitit|katapult|acima|progressive leasing|snap rto|uown|cherry technologies)\b"),
    ("prime / near-prime personal lender", r"\b(sofi|upgrade|upstart|prosper|marlette|lendingclub|lending club|"
                                           r"best egg|avant|laurel road|lightstream|payoff|happy money|discover)\b"),
    ("cash-advance / EWA app", r"\b(dave operating|chime|brigit|earnin|activehours|cleo|floatme|"
                               r"empower finance|klover|kikoff|self financial|possible financial)\b"),
    ("credit bureau / servicer of other products", r"\b(experian|transunion|equifax|alorica|"
                                                    r"navient|nelnet|mohela)\b"),
    ("auto / home / solar finance", r"\b(westlake|solar|hanwha|qcells|sunstrong|mosaic|greensky|aqua finance|service finance|"
                                    r"sunlight|goodleap|vehicle|auto)\b"),
    ("debt collector / debt relief", r"\b(collection|recovery|receivables|portfolio recovery|midland|"
                                      r"encore capital|lvnv|jefferson capital|debt|resurgent|credit adjusters|"
                                      r"freedom financial|accredited debt)\b"),
    ("healthcare / retail point-of-sale finance", r"\b(healthcare|health care|patient\w*|dental|medical|sunbit|"
                                                  r"claritypay|american first finance|monterey financial|"
                                                  r"duvera|snap us|westcreek|prog holdings|momnt|koalafi|"
                                                  r"flexshopper|lease|leasing|rent-to-own)\b"),
    ("insurer / membership bank", r"\b(usaa|united services automobile)\b"),
]


def _query(params: dict) -> dict:
    url = API + "?" + urllib.parse.urlencode(params)
    _status, _final, body = _get(url, timeout=60)
    return json.loads(body)


def _companies(sub_product: str, since: str) -> list[dict]:
    d = _query({"product": f"{PRODUCT}•{sub_product}", "date_received_min": since, "size": 0})
    return d["aggregations"]["company"]["company"]["buckets"]


def company_states(company: str, since: str) -> dict[str, int]:
    """Complaint count by consumer state for one company (footprint signal)."""
    d = _query({"company": company, "product": PRODUCT, "date_received_min": since, "size": 0})
    return {b["key"]: b["doc_count"] for b in d["aggregations"]["state"]["state"]["buckets"]}


def screen(name: str) -> str | None:
    for label, pattern in SCREEN_RULES:
        if re.search(pattern, name, re.IGNORECASE):
            return label
    return None


def load_matches(path: Path) -> dict[str, dict]:
    """company_matches.csv: cfpb_name, buyer (canonical buyer name, blank if not a client), operator_group, note,
    exclude_reason (set to screen a company out by hand, e.g. after research shows it isn't a lender)."""
    if not path.exists():
        return {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        return {r["cfpb_name"].strip(): r for r in csv.DictReader(f)}


def build_universe(conn: sqlite3.Connection, matches_path: Path, months: int = 12) -> dict:
    since = (date.today() - timedelta(days=round(months * 30.4))).isoformat()
    counts: dict[str, dict[str, int]] = {}
    for sp in SUB_PRODUCTS:
        for b in _companies(sp, since):
            name = " ".join(b["key"].replace("\u00a0", " ").split())   # the API uses non-breaking spaces
            counts.setdefault(name, {})[sp] = b["doc_count"]
        time.sleep(1)

    matches = load_matches(matches_path)
    buyer_ids = {r["canonical_name"]: r["id"] for r in conn.execute("SELECT id, canonical_name FROM buyer")}
    conn.execute("DELETE FROM company_fact WHERE company_id IN (SELECT id FROM company WHERE source = ?)", (SOURCE,))
    conn.execute("DELETE FROM company WHERE source = ?", (SOURCE,))
    source_url = API + "?" + urllib.parse.urlencode({"product": PRODUCT, "date_received_min": since})

    stats = {"companies": 0, "screened_out": 0, "matched_to_buyers": 0}
    for name, by_sp in counts.items():
        m = matches.get(name, {})
        buyer = (m.get("buyer") or "").strip()
        cur = conn.execute(
            "INSERT INTO company (name, category, buyer_id, source, source_url) VALUES (?,?,?,?,?)",
            (name, None, buyer_ids.get(buyer), SOURCE, source_url))
        cid = cur.lastrowid
        facts = {f"complaints_{sp.lower().replace(' ', '_')}": n for sp, n in by_sp.items()}
        facts["complaints_total"] = sum(by_sp.values())
        facts["complaints_since"] = since
        label = (m.get("exclude_reason") or "").strip() or screen(name)
        if label:
            facts["screened_out"] = label
            stats["screened_out"] += 1
        if m.get("operator_group"):
            facts["operator_group"] = m["operator_group"]
        for field, value in facts.items():
            conn.execute("INSERT INTO company_fact (company_id, field, value, source_url) VALUES (?,?,?,?)",
                         (cid, field, str(value), source_url))
        stats["companies"] += 1
        stats["matched_to_buyers"] += bool(buyer)
    conn.commit()
    return stats


def universe_rows(conn: sqlite3.Connection) -> list[dict]:
    rows = {}
    for r in conn.execute(
            """SELECT c.id, c.name, b.canonical_name AS buyer, f.field, f.value
               FROM company c LEFT JOIN buyer b ON b.id = c.buyer_id
               JOIN company_fact f ON f.company_id = c.id
               WHERE c.source = ?""", (SOURCE,)):
        d = rows.setdefault(r["id"], {"company": r["name"], "buyer": r["buyer"]})
        d[r["field"]] = r["value"]
    out = list(rows.values())
    for d in out:
        d["complaints_total"] = int(d.get("complaints_total", 0))
    out.sort(key=lambda d: -d["complaints_total"])
    return out
