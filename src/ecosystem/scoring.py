"""Buyer value and supply value (brief §5). Two separate measures, never combined.

Each component scores 0-100 and records the raw input, method and claim ids behind it. A component with no
data is left out and the remaining weights are rescaled, so missing data neither helps nor hurts; the score
records which components were missing. Weights are versioned in `weights_version`; every run writes new
`score` rows (history is kept) and an audit entry.

Compliance is a gate, not a component: see `views.compliance_gate`.
"""
import math
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import select, func

from . import models as m
from .store import Store

LENDER_ROLES = ("lender_tribal", "lender_state_licensed", "lender_bank_partner", "lender_cab_cso", "servicer")
SUPPLY_ROLES = ("lead_network_pingtree", "connector_brand", "comparison_offer_wall", "content_affiliate",
                "sponsored_publisher_agency", "affiliate_cpa_network", "paid_media_platform", "fintech_decline_source")

DEFAULT_WEIGHTS = {
    "buyer_value": {
        "lead_buying": 30, "size": 25, "price_volume_fit": 15, "type_fit": 15, "state_overlap": 5, "segment": 10,
    },
    "supply_value": {
        "traffic": 30, "relevance": 25, "sheet_priority": 15, "ease": 10, "owner_leverage": 10, "overlap": 10,
    },
}

# Per-value scores inside components. Stored with the weights so they are versioned too.
DEFAULT_TABLES = {
    "type_fit": {"servicer": 100, "lender_tribal": 90, "lender_bank_partner": 75, "lender_cab_cso": 65,
                 "lender_state_licensed": 55},
    "segment": {"subprime": 100, "nonprime": 80, "all": 70, "near-prime": 45, "prime": 0},
    "relevance": {"lead_network_pingtree": 100, "connector_brand": 90, "comparison_offer_wall": 85,
                  "affiliate_cpa_network": 70, "fintech_decline_source": 70, "content_affiliate": 60,
                  "sponsored_publisher_agency": 60, "paid_media_platform": 50},
    "sheet_priority": {"A": 100, "B": 60, "C": 20},
}


def _log_scale(value: float | None, top: float) -> float | None:
    if value is None or value <= 0 or top <= 0:
        return None if value is None else 0.0
    return round(100 * math.log10(1 + value) / math.log10(1 + top), 1)


def current_weights(s, score_type: str) -> m.WeightsVersion:
    wv = s.scalar(select(m.WeightsVersion).where(m.WeightsVersion.score_type == score_type)
                  .order_by(m.WeightsVersion.id.desc()))
    if wv is None:
        wv = m.WeightsVersion(score_type=score_type, label="v1 (default)", created_by="system",
                              weights={"weights": DEFAULT_WEIGHTS[score_type], "tables": DEFAULT_TABLES},
                              note="Initial defaults from the build brief §5")
        s.add(wv)
        s.flush()
    return wv


def save_weights(s, score_type: str, weights: dict[str, float], actor: str, note: str = "") -> m.WeightsVersion:
    prev = current_weights(s, score_type)
    n = s.scalar(select(func.count()).select_from(m.WeightsVersion).where(m.WeightsVersion.score_type == score_type))
    clean = {k: float(weights.get(k, 0)) for k in DEFAULT_WEIGHTS[score_type]}
    if any(v < 0 for v in clean.values()) or sum(clean.values()) <= 0:
        raise ValueError("weights must be non-negative and not all zero")
    wv = m.WeightsVersion(score_type=score_type, label=f"v{n + 1}", created_by=actor, note=note or None,
                          weights={"weights": clean, "tables": prev.weights.get("tables", DEFAULT_TABLES)})
    s.add(wv)
    s.add(m.AuditLog(actor=actor, action="weights.save", table_name="weights_version", row_id=None,
                     before={"label": prev.label, **prev.weights["weights"]}, after={"label": wv.label, **clean}))
    s.flush()
    return wv


class _Facts:
    """Bulk-loaded inputs, so scoring 1,500 organisations is a handful of queries."""

    def __init__(self, s):
        self.roles = defaultdict(list)
        for r in s.scalars(select(m.OrgRole)):
            self.roles[r.organisation_id].append(r)
        self.metrics = {}  # (entity, metric) -> (value, claim_id), latest
        for e, metric, v, cid in s.execute(
                select(m.MetricSnapshot.entity_id, m.MetricSnapshot.metric, m.MetricSnapshot.value, m.MetricSnapshot.claim_id)
                .order_by(m.MetricSnapshot.retrieved_at, m.MetricSnapshot.id)):
            self.metrics[(e, metric)] = (float(v), cid)
        self.lbs = {}
        for e, v, comps in s.execute(select(m.Score.entity_id, m.Score.value, m.Score.components)
                                     .where(m.Score.score_type == "lbs_index").order_by(m.Score.id)):
            self.lbs[e] = (float(v), comps)
        self.current = defaultdict(dict)  # entity -> attribute -> (value, claim_id)
        wanted = ("priority", "how_to_work_with", "segment_text", "loan_max", "loan_min", "sheet_fit")
        for e, attr, v, cid in s.execute(text_current(wanted)):
            self.current[e][attr] = (v, cid)
        self.children = defaultdict(int)
        for pid, n in s.execute(select(m.Organisation.parent_id, func.count()).where(
                m.Organisation.parent_id.is_not(None), m.Organisation.lifecycle != "merged").group_by(m.Organisation.parent_id)):
            self.children[pid] = n
        self.top = defaultdict(float)
        for (e, metric), (v, _) in self.metrics.items():
            self.top[metric] = max(self.top[metric], v)


def text_current(attrs):
    from sqlalchemy import text
    return text("SELECT entity_id, attribute, value, claim_id FROM entity_current WHERE attribute = ANY(:a)").bindparams(a=list(attrs))


def _component(score, raw, method, claim_ids=()):
    return {"score": score, "raw": raw, "method": method, "claim_ids": [c for c in claim_ids if c]}


def buyer_components(org: m.Organisation, f: _Facts, tables: dict) -> dict:
    roles = [r for r in f.roles[org.id] if r.role in LENDER_ROLES]
    c = {}
    if org.id in f.lbs:
        v, comps = f.lbs[org.id]
        c["lead_buying"] = _component(round(v, 1), f"{v:g} (Lead-buying strength sheet, row {comps.get('row')})",
                                      "Lead-buying strength index (0-100): partner lists 30, organic 20, paid 15, "
                                      "disclosed 15, affiliate programme 10, conferences 5, hiring 5", [])
    sizes = []
    for metric, label in (("cfpb_complaints_12m", "CFPB complaints 12m"), ("ahrefs_organic_visits", "organic visits/mo"),
                          ("ahrefs_paid_spend_usd", "paid search $/mo")):
        if (org.id, metric) in f.metrics:
            v, cid = f.metrics[(org.id, metric)]
            sizes.append((_log_scale(v, f.top[metric]), f"{label} {v:,.0f}", cid))
    if sizes:
        best = max(sizes, key=lambda x: x[0] or 0)
        c["size"] = _component(best[0], "; ".join(x[1] for x in sizes),
                               "Estimate: largest of complaint volume, organic traffic and paid spend, each on a log "
                               "scale against the largest in the database (no disclosed originations yet)",
                               [x[2] for x in sizes])
    loan_max = next((float(r.loan_max) for r in roles if r.loan_max is not None), None)
    if loan_max is not None:
        # High-revenue buyers lend $2,500+; high-volume buyers lend small amounts. Both fit, price fits best.
        fit = 100 if loan_max >= 2500 else 80 if loan_max >= 1500 else 70 if loan_max >= 500 else 50
        c["price_volume_fit"] = _component(fit, f"max loan ${loan_max:,.0f}",
                                           "Loan size vs buyer profiles: $2,500+ = price buyer (100), $1,500+ = 80, "
                                           "$500+ = volume buyer (70), less = 50",
                                           [cid for r in roles for cid in r.claim_ids])
    if roles:
        best = max(roles, key=lambda r: tables["type_fit"].get(r.role, 0))
        c["type_fit"] = _component(tables["type_fit"].get(best.role, 0), best.role, "Best lender role", best.claim_ids)
    seg = next((r.segment for r in roles if r.segment), None)
    if seg:
        c["segment"] = _component(tables["segment"].get(seg, 50), seg, "Segment from research",
                                  [cid for r in roles if r.segment for cid in r.claim_ids])
    c["state_overlap"] = _component(None, None, "Waiting for traffic-by-state data from the internal source")
    return c


def supply_components(org: m.Organisation, f: _Facts, tables: dict) -> dict:
    roles = [r for r in f.roles[org.id] if r.role in SUPPLY_ROLES]
    c = {}
    if (org.id, "ahrefs_organic_visits") in f.metrics:
        v, cid = f.metrics[(org.id, "ahrefs_organic_visits")]
        c["traffic"] = _component(_log_scale(v, f.top["ahrefs_organic_visits"]), f"{v:,.0f} organic visits/mo",
                                  "Ahrefs estimated US organic visits, log scale (domain level; page-level table "
                                  "sizing comes in Phase 4)", [cid])
    if roles:
        best = max(roles, key=lambda r: tables["relevance"].get(r.role, 0))
        c["relevance"] = _component(tables["relevance"].get(best.role, 0), best.role,
                                    "Subprime-loan intent by supply category", best.claim_ids)
    pr = f.current[org.id].get("priority")
    if pr:
        c["sheet_priority"] = _component(tables["sheet_priority"].get(pr[0]), pr[0], "Priority A/B/C from the traffic file",
                                         [pr[1]])
    how = f.current[org.id].get("how_to_work_with")
    if how:
        t = str(how[0]).lower()
        ease = 80 if any(k in t for k in ("sign up", "self-serve", "self serve", "apply", "affiliate", "publisher")) else \
            40 if any(k in t for k in ("relationship", "negotiat", "deal", "reciprocal")) else 60
        c["ease"] = _component(ease, how[0], "Self-serve sign-up = 80, relationship deal = 40, other = 60", [how[1]])
    group = org.parent_id or (org.id if f.children[org.id] else None)
    n = f.children[group] if group else 0
    c["owner_leverage"] = _component(min(100, 20 * n) if n else 0, f"{n} organisations in the owner group",
                                     "20 points per organisation one contact unlocks, max 100", [])
    c["overlap"] = _component(None, None, "Waiting for our current traffic sources from the internal source")
    return c


MISSING_SCORE = 25  # an unknown is treated as below average, so thin records don't outrank researched ones


def combine(components: dict, weights: dict, available: set[str], missing_score: float = MISSING_SCORE
            ) -> tuple[float, list[str]]:
    """Weighted average. Components no organisation has data for yet (`available` excludes them) are left out
    for everyone; a component missing for just this organisation scores `missing_score`."""
    total_w, total = 0.0, 0.0
    missing = []
    for k, w in weights.items():
        if k not in available:
            continue
        comp = components.get(k)
        if comp is None or comp["score"] is None:
            missing.append(k)
            components[k] = _component(None, None, f"No data: scored {missing_score:g} (unknown)")
            score = missing_score
        else:
            score = comp["score"]
        total_w += w
        total += w * score
    return (round(total / total_w, 1) if total_w else 0.0), missing


def rescore(store: Store, score_types=("buyer_value", "supply_value")) -> dict:
    s = store.s
    f = _Facts(s)
    orgs = list(s.scalars(select(m.Organisation).where(m.Organisation.lifecycle.in_(("active", "candidate")))))
    out = {}
    now = datetime.now(timezone.utc)
    for st in score_types:
        wv = current_weights(s, st)
        weights, tables = wv.weights["weights"], wv.weights.get("tables", DEFAULT_TABLES)
        scored = []
        for org in orgs:
            roles = {r.role for r in f.roles[org.id]}
            if st == "buyer_value":
                if not (roles & set(LENDER_ROLES) or org.id in f.lbs):
                    continue
                scored.append((org, buyer_components(org, f, tables)))
            else:
                if not roles & set(SUPPLY_ROLES):
                    continue
                scored.append((org, supply_components(org, f, tables)))
        available = {k for _, comps in scored for k, c in comps.items() if c["score"] is not None}
        for org, comps in scored:
            value, missing = combine(comps, weights, available)
            for k, comp in comps.items():
                comp["weight"] = weights.get(k, 0) if k in available else 0
            s.add(m.Score(entity_id=org.id, score_type=st, value=value, weights_version_id=wv.id, computed_at=now,
                          components={"components": comps, "missing": missing, "weights_label": wv.label,
                                      "not_yet_available": sorted(set(weights) - available)}))
        out[st] = {"scored": len(scored), "weights": wv.label, "not_yet_available": sorted(set(weights) - available)}
    store.audit("scores.rescore", "score", None, None, {**out, "at": now.isoformat()})
    s.flush()
    return out
