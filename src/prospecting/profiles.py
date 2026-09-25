"""Buyer-level profiles: commercial metrics, filter summary and value tiers.

Value model (agreed): score on both accepted leads (volume) and revenue (price).
For each measure, buyers are ordered largest first and tiered by cumulative share including
themselves: A while the running total is within 80% of the total, B within 95%, C the rest.
A buyer is a 'volume' reference if A on accepted leads, 'price' if A on revenue, 'both' if both.
Only direct lenders (D) and service providers (S) are tiered; networks and offers are excluded.
"""
import sqlite3
from collections import defaultdict
from statistics import median

from .names import parse_range, split_list

US_STATES = (
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ "
    "NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY").split()
TIERED_CATEGORIES = ("D", "S")
TIER_A_SHARE, TIER_B_SHARE = 0.80, 0.95


def _commercials(conn):
    rows = conn.execute(
        """SELECT COALESCE(b.canonical_name, r.alias) AS buyer,
                  COUNT(*) AS tiers,
                  SUM(CASE WHEN r.total_sold > 0 THEN 1 ELSE 0 END) AS active_tiers,
                  SUM(r.total_sold) AS accepted,
                  SUM(r.commission) AS revenue,
                  SUM(CASE WHEN r.is_price_reject = 1 THEN r.commission ELSE 0 END) AS price_reject_revenue,
                  SUM(r.redirected) AS redirected,
                  MAX(r.sent) AS max_sent
           FROM tier_revenue r
           LEFT JOIN buyer_alias a ON a.alias = r.alias
           LEFT JOIN buyer b ON b.id = a.buyer_id
           GROUP BY 1""")
    return {r["buyer"]: dict(r) for r in rows}


def _filters(conn):
    """Summarise filters per buyer across all its tiers, taking the loosest value where tiers differ."""
    acc = defaultdict(lambda: {"states": set(), "loan_min": [], "loan_max": [], "income_min": [],
                               "age_min": [], "income_types": set(), "pay_freq": set(),
                               "dd_only": [], "tiers": set()})
    rows = conn.execute(
        """SELECT COALESCE(b.canonical_name, f.alias) AS buyer,
                  COALESCE(f.tier_name, '(unnamed)') AS tier, f.filter_type, f.filter_value
           FROM tier_filter f
           LEFT JOIN buyer_alias a ON a.alias = f.alias
           LEFT JOIN buyer b ON b.id = a.buyer_id""")
    per_tier_blacklist = {}
    for r in rows:
        s = acc[r["buyer"]]
        s["tiers"].add(r["tier"])
        t, v = r["filter_type"], r["filter_value"]
        if t == "State Blacklist":
            key = (r["buyer"], r["tier"])
            # Conflicting blacklists within one tier: keep the shorter (looser) one.
            bl = set(split_list(v))
            if key not in per_tier_blacklist or len(bl) < len(per_tier_blacklist[key]):
                per_tier_blacklist[key] = bl
        elif t == "Loan Amount":
            lo, hi = parse_range(v)
            s["loan_min"].append(lo)
            s["loan_max"].append(hi)
        elif t == "Monthly Income":
            s["income_min"].append(parse_range(v)[0])
        elif t == "Age":
            s["age_min"].append(parse_range(v)[0])
        elif t == "Income Type":
            s["income_types"].update(split_list(v))
        elif t == "Payment Frequency":
            s["pay_freq"].update(split_list(v))
        elif t == "Direct Deposit Only":
            s["dd_only"].append(v.strip().lower() == "true")
    for buyer, s in acc.items():
        for tier in s["tiers"]:
            # A tier with no blacklist accepts every state.
            bl = per_tier_blacklist.get((buyer, tier), set())
            s["states"].update(st for st in US_STATES if st not in bl)

    out = {}
    for buyer, s in acc.items():
        maxes = s["loan_max"]
        out[buyer] = {
            "filter_tiers": len(s["tiers"]),
            "states_accepted": sorted(s["states"]) if s["states"] else None,
            "loan_min": min((x for x in s["loan_min"] if x is not None), default=None),
            # Any tier without an upper limit means no upper limit for the buyer.
            "loan_max": None if not maxes or None in maxes else max(maxes),
            "loan_max_unbounded": bool(maxes) and None in maxes,
            "income_min": min((x for x in s["income_min"] if x is not None), default=None),
            "age_min": min((x for x in s["age_min"] if x is not None), default=None),
            "income_types": ", ".join(sorted(s["income_types"])),
            "pay_freq": ", ".join(sorted(s["pay_freq"])),
            "direct_deposit_only": all(s["dd_only"]) if s["dd_only"] else None,
        }
    return out


def _tier(values: dict[str, float]) -> dict[str, str]:
    total = sum(v for v in values.values() if v > 0)
    tiers, running = {}, 0.0
    for name, v in sorted(values.items(), key=lambda kv: -kv[1]):
        if v <= 0 or total == 0:
            tiers[name] = "-"
            continue
        first = running == 0
        running += v
        share = running / total
        # The largest buyer is always A, even when it alone exceeds the A share.
        tiers[name] = "A" if first or share <= TIER_A_SHARE else "B" if share <= TIER_B_SHARE else "C"
    return tiers


def build_profiles(conn: sqlite3.Connection) -> list[dict]:
    buyers = {r["canonical_name"]: dict(r) for r in conn.execute("SELECT * FROM buyer")}
    com, fil = _commercials(conn), _filters(conn)
    names = set(buyers) | set(com) | set(fil)

    profiles = []
    for name in names:
        b = buyers.get(name, {})
        c = com.get(name, {})
        accepted = c.get("accepted") or 0
        revenue = c.get("revenue") or 0.0
        profiles.append({
            "buyer": name,
            "category": b.get("category"),
            "status": b.get("status") or ("active" if accepted else "no sales this period"),
            "website": b.get("website"),
            "notes": b.get("notes"),
            "in_reference": name in buyers,
            "tiers": c.get("tiers", 0),
            "active_tiers": c.get("active_tiers", 0),
            "accepted": accepted,
            "revenue": revenue,
            "epl": revenue / accepted if accepted else None,
            "price_reject_share": (c.get("price_reject_revenue") or 0) / revenue if revenue else None,
            "redirect_rate": (c.get("redirected") or 0) / accepted if accepted else None,
            "max_sent": c.get("max_sent") or 0,
            "has_filters": name in fil,
            **fil.get(name, {}),
        })

    tiered = [p for p in profiles if p["category"] in TIERED_CATEGORIES]
    vol = _tier({p["buyer"]: p["accepted"] for p in tiered})
    rev = _tier({p["buyer"]: p["revenue"] for p in tiered})
    for p in profiles:
        p["volume_tier"] = vol.get(p["buyer"])
        p["price_tier"] = rev.get(p["buyer"])
        p["reference_segment"] = (
            "both" if p["volume_tier"] == "A" and p["price_tier"] == "A"
            else "volume" if p["volume_tier"] == "A"
            else "price" if p["price_tier"] == "A" else None)
    profiles.sort(key=lambda p: -p["revenue"])
    return profiles


def segment_summary(profiles: list[dict]) -> list[dict]:
    """What the reference buyers in each segment look like: the basis for lookalike rules."""
    def stats(group):
        def med(key):
            vals = [p[key] for p in group if p.get(key) is not None]
            return median(vals) if vals else None
        state_counts = defaultdict(int)
        for p in group:
            for st in p.get("states_accepted") or []:
                state_counts[st] += 1
        with_states = sum(1 for p in group if p.get("states_accepted"))
        common = sorted(st for st, n in state_counts.items() if with_states and n / with_states >= 0.5)
        return {
            "buyers": len(group),
            "names": ", ".join(sorted(p["buyer"] for p in group)),
            "accepted": sum(p["accepted"] for p in group),
            "revenue": sum(p["revenue"] for p in group),
            "median_epl": med("epl"),
            "median_loan_min": med("loan_min"),
            "median_loan_max": med("loan_max"),
            "median_income_min": med("income_min"),
            "median_states": median([len(p["states_accepted"]) for p in group if p.get("states_accepted")])
            if with_states else None,
            "states_accepted_by_half_or_more": ", ".join(common),
            "with_filters": with_states,
        }

    groups = {
        "Volume reference (A on accepted leads)": [p for p in profiles if p["volume_tier"] == "A"],
        "Price reference (A on revenue)": [p for p in profiles if p["price_tier"] == "A"],
        "All other direct buyers with sales": [
            p for p in profiles if p["category"] in TIERED_CATEGORIES and p["accepted"]
            and p["volume_tier"] != "A" and p["price_tier"] != "A"],
    }
    return [{"segment": k, **stats(v)} for k, v in groups.items()]
