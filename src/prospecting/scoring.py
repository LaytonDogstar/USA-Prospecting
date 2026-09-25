"""Score prospect companies against the client profile.

The client research showed ~80% of direct revenue comes from operators/servicers running lending for
tribal or bank-partner brands, and that high-price buyers lend larger installment loans (~$5,000 max)
while high-volume buyers are multi-state small-dollar lenders. Scores follow that:

  size (0-30)        relative complaint volume in the CFPB database (log scale)
  type fit (0-30)    operator/servicer or multi-brand group > tribal / bank-partner > state-licensed > branch
  online (0-10)      online lending (lead buyers are overwhelmingly online)
  lead signals (0-15) evidence the company buys leads / runs affiliate programs
  segment (-20..+5)  subprime +5, near-prime 0, prime -20

Each target also gets a fit label: 'price' (installment loans up to $2,500+), 'volume' (small-dollar),
or both. Weights are deliberately simple; tune them in WEIGHTS once outreach results come in.
Existing clients and screened-out companies are never scored.
"""
import math
import re
import sqlite3

from .discover import SOURCE as UNIVERSE_SOURCE
from .research import PROSPECT_PREFIX

WEIGHTS = {"size": 30, "type": 30, "online": 10, "lead_signals": 15}
TYPE_SCORES = [
    (r"servicer|platform|operator", 1.0),
    (r"tribal", 0.8),
    (r"bank-partner|bank partner", 0.7),
    (r"state-licensed|state licensed|cab|cso", 0.5),
    (r"fintech", 0.4),
]
MONEY = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)\s*(k)?", re.IGNORECASE)

ROLE_PRIORITY = {
    "operator": "Head of Customer Acquisition / Marketing Director; COO; Head of Partnerships",
    "tribal": "Servicer's acquisition or marketing lead (if identifiable); tribal lending enterprise CEO/GM",
    "bank-partner": "VP Growth / Head of Performance Marketing; Director of Affiliate Partnerships",
    "state-licensed": "Director of Marketing / Lead Acquisition Manager; CEO at smaller shops",
    "branch": "VP Digital Acquisition / Head of Direct Marketing",
    "other": "Head of Marketing / Growth; CEO at smaller shops",
}


def _rows(conn: sqlite3.Connection) -> list[dict]:
    rows: dict[int, dict] = {}
    for r in conn.execute(
            """SELECT c.id, c.name, c.website, b.canonical_name AS buyer, f.field, f.value, f.source_url
               FROM company c LEFT JOIN buyer b ON b.id = c.buyer_id
               JOIN company_fact f ON f.company_id = c.id
               WHERE c.source = ?""", (UNIVERSE_SOURCE,)):
        d = rows.setdefault(r["id"], {"company": r["name"], "website": r["website"], "buyer": r["buyer"],
                                      "sources": set()})
        field = r["field"]
        if field.startswith(PROSPECT_PREFIX):
            field = "r_" + field[len(PROSPECT_PREFIX):]
            if r["source_url"] and r["source_url"] != "n/a":
                d["sources"].add(r["source_url"])
            d[field] = r["value"] if field not in d else f"{d[field]}; {r['value']}"
        else:
            d[field] = r["value"]
    for d in rows.values():
        d["complaints_total"] = int(d.get("complaints_total", 0))
        d["researched"] = "r_research_confidence" in d
    return list(rows.values())


def _max_loan(text: str | None) -> float | None:
    if not text:
        return None
    vals = [float(m.group(1).replace(",", "")) * (1000 if m.group(2) else 1) for m in MONEY.finditer(text)]
    return max(vals) if vals else None


def _type_key(company_type: str, brands: str | None) -> tuple[float, str]:
    t = (company_type or "").lower()
    n_brands = len([b for b in (brands or "").split(";") if b.strip()])
    for pattern, score in TYPE_SCORES:
        if re.search(pattern, t):
            key = ("operator" if score == 1.0 else "tribal" if "tribal" in t else
                   "bank-partner" if "bank" in t else "state-licensed" if score == 0.5 else "other")
            if key == "tribal" and n_brands >= 2:      # a tribal entity running several brands acts as an operator
                return 0.95, "operator"
            return score, key
    return 0.2, "other"


def score(conn: sqlite3.Connection) -> list[dict]:
    rows = [r for r in _rows(conn) if not r["buyer"] and "screened_out" not in r]
    max_log = max((math.log1p(r["complaints_total"]) for r in rows), default=1) or 1
    for r in rows:
        text = " ".join(str(r.get(k, "")) for k in ("r_company_type", "r_storefront_or_online", "r_notes")).lower()
        type_score, type_key = _type_key(r.get("r_company_type", ""), r.get("r_consumer_brands"))
        if "branch" in (r.get("r_storefront_or_online") or "").lower() and "online" not in (
                r.get("r_storefront_or_online") or "").lower():
            type_key = "branch"
        online = 1.0 if "online" in text else 0.5 if "both" in text else 0.0
        signals = 1.0 if r.get("r_lead_buying_signals") else 0.0
        seg = (r.get("r_customer_segment") or "").lower()
        segment = -20 if re.search(r"\bprime\b", seg) and "sub" not in seg and "near" not in seg else (
            5 if "subprime" in seg else 0)
        parts = {
            "size": WEIGHTS["size"] * math.log1p(r["complaints_total"]) / max_log,
            "type": WEIGHTS["type"] * type_score if r["researched"] else 0,
            "online": WEIGHTS["online"] * online,
            "lead_signals": WEIGHTS["lead_signals"] * signals,
            "segment": segment,
        }
        max_loan = _max_loan(r.get("r_loan_amount_range"))
        products = (r.get("r_products") or "").lower()
        fit = []
        if (max_loan and max_loan >= 2500) or "installment" in products:
            fit.append("price")
        if (max_loan and max_loan <= 1500) or "payday" in products or "line of credit" in products:
            fit.append("volume")
        r.update({f"score_{k}": round(v, 1) for k, v in parts.items()})
        r["score"] = round(sum(parts.values()), 1)
        r["type_key"] = type_key
        r["max_loan"] = max_loan
        r["fit"] = " + ".join(fit) or None
        r["roles"] = ROLE_PRIORITY.get(type_key, ROLE_PRIORITY["other"])
        r["why"] = _why(r)
        r["n_sources"] = len(r["sources"])
    rows.sort(key=lambda r: (-r["researched"], -r["score"]))
    return rows


def _why(r: dict) -> str:
    bits = []
    if r.get("r_company_type"):
        bits.append(r["r_company_type"].split(";")[0].strip())
    if r.get("r_tribe"):
        bits.append(f"tribe: {r['r_tribe'].split(';')[0].strip()}")
    if r.get("r_consumer_brands"):
        brands = [b.strip() for b in r["r_consumer_brands"].split(";") if b.strip()]
        bits.append(f"{len(brands)} brand(s): " + ", ".join(brands[:4]) + ("…" if len(brands) > 4 else ""))
    if r.get("max_loan"):
        bits.append(f"loans up to ${r['max_loan']:,.0f}")
    bits.append(f"{r['complaints_total']} CFPB loan complaints/12m")
    if r.get("r_lead_buying_signals"):
        bits.append("buys leads: " + r["r_lead_buying_signals"].split(";")[0].strip()[:120])
    return "; ".join(bits)
