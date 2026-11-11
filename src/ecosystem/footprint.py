"""Which states a lender serves, and the state map summary (brief §8.5).

A lender serves a state when any of these holds, in order of strength:
- it is listed for the state (`entity_state` basis lends / licensed)
- it says it lends nationwide (claim `nationwide`)
- it publishes an exclusion list (`entity_state` basis excluded) that doesn't include the state
Lenders with none of these have an unknown footprint and are counted separately, never guessed.
"""
from collections import defaultdict

from sqlalchemy import text

from .parsing import US_STATES

STATE_SERVES_SQL = """(
  EXISTS (SELECT 1 FROM entity_state es WHERE es.entity_id = o.id AND es.state = :state AND es.basis IN ('lends','licensed'))
  OR EXISTS (SELECT 1 FROM entity_current ec WHERE ec.entity_id = o.id AND ec.attribute = 'nationwide' AND ec.value = 'true'::jsonb)
  OR (EXISTS (SELECT 1 FROM entity_state es WHERE es.entity_id = o.id AND es.basis = 'excluded')
      AND NOT EXISTS (SELECT 1 FROM entity_state es WHERE es.entity_id = o.id AND es.basis = 'excluded' AND es.state = :state))
)"""

LENDER_TYPES = [("lender_tribal", "Tribal"), ("lender_bank_partner", "Bank-partner"), ("lender_cab_cso", "CAB/CSO"),
                ("lender_state_licensed", "State-licensed")]
TYPE_LABELS = dict(LENDER_TYPES) | {"unknown": "Type not researched"}
OUR_BUYER_STATUSES = ("live", "contracting")

# Tile-grid US map: (row, column) per state.
TILES = {
    "AK": (0, 0), "ME": (0, 11), "WI": (1, 6), "VT": (1, 10), "NH": (1, 11),
    "WA": (2, 0), "ID": (2, 1), "MT": (2, 2), "ND": (2, 3), "MN": (2, 4), "IL": (2, 5), "MI": (2, 7), "NY": (2, 9),
    "MA": (2, 10), "OR": (3, 0), "NV": (3, 1), "WY": (3, 2), "SD": (3, 3), "IA": (3, 4), "IN": (3, 5), "OH": (3, 6),
    "PA": (3, 7), "NJ": (3, 8), "CT": (3, 9), "RI": (3, 10), "CA": (4, 0), "UT": (4, 1), "CO": (4, 2), "NE": (4, 3),
    "MO": (4, 4), "KY": (4, 5), "WV": (4, 6), "VA": (4, 7), "MD": (4, 8), "DE": (4, 9), "AZ": (5, 1), "NM": (5, 2),
    "KS": (5, 3), "AR": (5, 4), "TN": (5, 5), "NC": (5, 6), "SC": (5, 7), "DC": (5, 8), "OK": (6, 3), "LA": (6, 4),
    "MS": (6, 5), "AL": (6, 6), "GA": (6, 7), "HI": (7, 0), "TX": (7, 3), "FL": (7, 8),
}


def lender_footprints(db) -> dict[int, dict]:
    """org id -> {'type', 'explicit', 'excluded', 'nationwide', 'ours'} for every lender that isn't blocked."""
    rows = db.execute(text("""
        SELECT o.id,
               (SELECT array_agg(DISTINCT role) FROM org_role r WHERE r.organisation_id = o.id) AS roles,
               coalesce((SELECT array_agg(state) FROM entity_state es WHERE es.entity_id = o.id AND es.basis IN ('lends','licensed')), '{}') AS explicit,
               coalesce((SELECT array_agg(state) FROM entity_state es WHERE es.entity_id = o.id AND es.basis = 'excluded'), '{}') AS excluded,
               EXISTS (SELECT 1 FROM entity_current ec WHERE ec.entity_id = o.id AND ec.attribute = 'nationwide'
                       AND ec.value = 'true'::jsonb) AS nationwide,
               ours.status AS our_status, ours.role AS our_role
        FROM organisation o
        LEFT JOIN our_relationship ours ON ours.organisation_id = o.id
        LEFT JOIN compliance_status cs ON cs.entity_id = o.id
        WHERE o.lifecycle IN ('active', 'candidate') AND cs.value IS DISTINCT FROM 'blocked'
          AND (EXISTS (SELECT 1 FROM org_role r WHERE r.organisation_id = o.id
                       AND r.role IN ('lender_tribal','lender_state_licensed','lender_bank_partner','lender_cab_cso'))
               OR (ours.status IN ('live','contracting') AND ours.role IN ('buyer','both')))
    """))
    out = {}
    for r in rows:
        roles = set(r.roles or [])
        ltype = next((k for k, _ in LENDER_TYPES if k in roles), "unknown")
        out[r.id] = {"type": ltype, "explicit": set(r.explicit), "excluded": set(r.excluded), "nationwide": r.nationwide,
                     "ours": r.our_status in OUR_BUYER_STATUSES and r.our_role in ("buyer", "both", "network")}
    return out


def serves(fp: dict, state: str) -> str | None:
    """How a lender serves a state: 'listed', 'nationwide', 'not excluded', or None."""
    if state in fp["explicit"]:
        return "listed"
    if fp["nationwide"]:
        return "nationwide"
    if fp["excluded"] and state not in fp["excluded"]:
        return "not excluded"
    return None


def state_summary(db) -> dict:
    fps = lender_footprints(db)
    known = {i: fp for i, fp in fps.items() if fp["explicit"] or fp["nationwide"] or fp["excluded"]}
    per = {}
    for st in US_STATES:
        by_type = defaultdict(int)
        by_basis = defaultdict(int)
        ours = 0
        for fp in known.values():
            how = serves(fp, st)
            if how:
                by_type[fp["type"]] += 1
                by_basis[how] += 1
                ours += fp["ours"]
        per[st] = {"total": sum(by_type.values()), "by_type": dict(by_type), "by_basis": dict(by_basis), "ours": ours,
                   "tile": TILES.get(st)}
    return {
        "states": per,
        "lenders": len(fps),
        "known_footprint": len(known),
        "unknown_footprint": len(fps) - len(known),
        "our_buyers": sum(fp["ours"] for fp in fps.values()),
        "our_buyers_unknown": sum(fp["ours"] for i, fp in fps.items() if i not in known),
        "max_total": max([v["total"] for v in per.values()] + [1]),
    }
