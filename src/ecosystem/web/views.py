"""Read queries for the pages. SQL is assembled only from whitelisted fragments; user input is always bound."""
from sqlalchemy import text

from ..footprint import STATE_SERVES_SQL
from ..scoring import LENDER_ROLES, SUPPLY_ROLES

ROLE_LABELS = {
    "lender_tribal": "Tribal", "lender_state_licensed": "State-licensed", "lender_bank_partner": "Bank-partner",
    "lender_cab_cso": "CAB/CSO", "servicer": "Servicer", "lead_network_pingtree": "Lead network / pingtree",
    "connector_brand": "Connector brand", "comparison_offer_wall": "Comparison / offer wall",
    "content_affiliate": "Content affiliate", "sponsored_publisher_agency": "Sponsored publisher / agency",
    "affiliate_cpa_network": "Affiliate / CPA network", "paid_media_platform": "Paid media",
    "fintech_decline_source": "Fintech / decline source",
}

BASE = """
SELECT o.id, o.display_name, o.tier, o.lifecycle, o.kind, p.id AS parent_id, p.display_name AS parent_name,
       sc.value AS score, lbs.value AS lbs, ref.value AS sheet_score,
       (SELECT string_agg(DISTINCT r.role, ',') FROM org_role r WHERE r.organisation_id = o.id) AS roles,
       (SELECT max(r.loan_max) FROM org_role r WHERE r.organisation_id = o.id) AS loan_max,
       (SELECT count(*) FROM entity_state es WHERE es.entity_id = o.id AND es.basis IN ('lends','licensed')) AS states_n,
       cmp.value AS complaints, org_v.value AS organic, paid.value AS paid,
       ours.status AS our_status, ours.role AS our_role, ours.needs_confirmation,
       cs.value AS compliance, (SELECT count(*) FROM compliance_flag f WHERE f.entity_id = o.id) AS flags,
       (SELECT hostname FROM domain d WHERE d.organisation_id = o.id ORDER BY d.is_primary DESC, d.id LIMIT 1) AS domain
FROM organisation o
LEFT JOIN organisation p ON p.id = o.parent_id
LEFT JOIN latest_score sc ON sc.entity_id = o.id AND sc.score_type = :score_type
LEFT JOIN latest_score lbs ON lbs.entity_id = o.id AND lbs.score_type = 'lbs_index'
LEFT JOIN latest_score ref ON ref.entity_id = o.id AND ref.score_type = 'sheet_reference'
LEFT JOIN LATERAL (SELECT value FROM metric_snapshot ms WHERE ms.entity_id = o.id AND ms.metric = 'cfpb_complaints_12m'
                   ORDER BY retrieved_at DESC, id DESC LIMIT 1) cmp ON true
LEFT JOIN LATERAL (SELECT value FROM metric_snapshot ms WHERE ms.entity_id = o.id AND ms.metric = 'ahrefs_organic_visits'
                   ORDER BY retrieved_at DESC, id DESC LIMIT 1) org_v ON true
LEFT JOIN LATERAL (SELECT value FROM metric_snapshot ms WHERE ms.entity_id = o.id AND ms.metric = 'ahrefs_paid_spend_usd'
                   ORDER BY retrieved_at DESC, id DESC LIMIT 1) paid ON true
LEFT JOIN our_relationship ours ON ours.organisation_id = o.id
LEFT JOIN compliance_status cs ON cs.entity_id = o.id
"""

SORTS = {
    "score": "sc.value DESC NULLS LAST", "name": "o.display_name", "lbs": "lbs.value DESC NULLS LAST",
    "complaints": "cmp.value DESC NULLS LAST", "organic": "org_v.value DESC NULLS LAST",
    "sheet": "ref.value DESC NULLS LAST", "tier": "o.tier NULLS LAST, sc.value DESC NULLS LAST",
    "loan": "loan_max DESC NULLS LAST",
}

OUR_STATUSES = ("live", "paused", "contracting", "in_talks", "former", "prospect", "parked", "dropped")


def org_table(db, kind: str, f: dict, limit: int | None = 100, offset: int = 0) -> tuple[list[dict], int]:
    """kind = 'lenders' or 'supply'. f = filter dict from the query string."""
    roles = LENDER_ROLES if kind == "lenders" else SUPPLY_ROLES
    params = {"score_type": "buyer_value" if kind == "lenders" else "supply_value", "roles": list(roles)}
    where = ["o.lifecycle <> 'merged'"]
    if kind == "lenders":
        where.append("(EXISTS (SELECT 1 FROM org_role r WHERE r.organisation_id = o.id AND r.role = ANY(:roles))"
                     " OR lbs.id IS NOT NULL)")
    else:
        where.append("EXISTS (SELECT 1 FROM org_role r WHERE r.organisation_id = o.id AND r.role = ANY(:roles))")
    if not f.get("screened"):
        where.append("o.lifecycle <> 'screened_out'")
    if f.get("q"):
        params["q"] = f"%{f['q'].strip()}%"
        where.append("(o.display_name ILIKE :q OR EXISTS (SELECT 1 FROM name_alias a WHERE a.entity_id = o.id AND a.alias ILIKE :q)"
                     " OR EXISTS (SELECT 1 FROM domain d WHERE d.organisation_id = o.id AND d.hostname ILIKE :q))")
    if f.get("role") in roles:
        params["role"] = f["role"]
        where.append("EXISTS (SELECT 1 FROM org_role r WHERE r.organisation_id = o.id AND r.role = :role)")
    our = f.get("our")
    if our == "none":
        where.append("ours.id IS NULL")
    elif our == "any":
        where.append("ours.id IS NOT NULL")
    elif our in OUR_STATUSES:
        params["our"] = our
        where.append("ours.status = :our")
    comp = f.get("compliance") or "hide_blocked"
    if comp == "hide_blocked":
        where.append("cs.value IS DISTINCT FROM 'blocked'")
    elif comp == "unreviewed":
        where.append("cs.value IS NULL")
    elif comp in ("clear", "review", "blocked"):
        params["comp"] = comp
        where.append("cs.value = :comp")
    elif comp == "flagged":
        where.append("EXISTS (SELECT 1 FROM compliance_flag cf WHERE cf.entity_id = o.id)")
    if f.get("state") and len(f["state"]) == 2:
        params["state"] = f["state"].upper()
        where.append(STATE_SERVES_SQL)
    if f.get("tier") in ("A", "B", "C"):
        params["tier"] = f["tier"]
        where.append("o.tier = :tier")
    if f.get("min_score"):
        try:
            params["min_score"] = float(f["min_score"])
            where.append("sc.value >= :min_score")
        except ValueError:
            pass
    if f.get("tribe"):
        params["tribe"] = int(f["tribe"]) if str(f["tribe"]).isdigit() else -1
        where.append("EXISTS (SELECT 1 FROM org_role r WHERE r.organisation_id = o.id AND r.tribe_id = :tribe)")
    if f.get("group"):
        params["group"] = int(f["group"]) if str(f["group"]).isdigit() else -1
        where.append("(o.parent_id = :group OR o.id = :group)")
    order = SORTS.get(f.get("sort") or "score", SORTS["score"])
    sql = f"{BASE} WHERE {' AND '.join(where)} ORDER BY {order}, o.display_name"
    total = db.execute(text(f"SELECT count(*) FROM ({sql}) t"), params).scalar()
    if limit:
        sql += " LIMIT :limit OFFSET :offset"
        params.update(limit=limit, offset=offset)
    rows = [dict(r._mapping) for r in db.execute(text(sql), params)]
    for r in rows:
        mine = [x for x in (r["roles"] or "").split(",") if x in roles]
        r["role_list"] = [ROLE_LABELS.get(x, x) for x in mine]
        r["other_roles"] = len([x for x in (r["roles"] or "").split(",") if x and x not in roles])
    return rows, total


def top_unworked(db, kind: str, n: int = 10) -> list[dict]:
    rows, _ = org_table(db, kind, {"our": "none", "sort": "score"}, limit=n)
    return rows


def review_counts(db) -> dict:
    return dict(db.execute(text("SELECT kind, count(*) FROM review_item WHERE status = 'open' GROUP BY kind")).all())


def recent_activity(db, n: int = 12) -> list[dict]:
    return [dict(r._mapping) for r in db.execute(text("""
        SELECT at, actor, action, table_name, row_id, after FROM audit_log
        WHERE NOT (action = 'our_relationship.set' AND left(actor, 4) = 'cli:')
        ORDER BY at DESC, id DESC LIMIT :n"""), {"n": n})]


def headline_counts(db) -> dict:
    return dict(db.execute(text("""
        SELECT
          (SELECT count(*) FROM organisation WHERE lifecycle IN ('active','candidate')) AS orgs,
          (SELECT count(*) FROM our_relationship WHERE status IN ('live','contracting','in_talks')) AS working,
          (SELECT count(*) FROM our_relationship WHERE needs_confirmation) AS unconfirmed,
          (SELECT count(*) FROM review_item WHERE status = 'open') AS reviews,
          (SELECT count(*) FROM compliance_status WHERE value = 'blocked') AS blocked,
          (SELECT max(imported_at) FROM import_file) AS last_import
    """)).one()._mapping)


def people_table(db, f: dict, limit: int | None = 200) -> list[dict]:
    params, where = {}, ["true"]
    if f.get("q"):
        params["q"] = f"%{f['q']}%"
        where.append("(p.name ILIKE :q OR p.title ILIKE :q OR o.display_name ILIKE :q)")
    if f.get("role_category"):
        params["rc"] = f["role_category"]
        where.append("p.role_category = :rc")
    sql = f"""SELECT p.id, p.name, p.title, p.role_category, p.email, p.linkedin_url, p.source, p.last_verified_at,
                     o.id AS org_id, o.display_name AS org_name
              FROM person p LEFT JOIN organisation o ON o.id = p.organisation_id
              WHERE {' AND '.join(where)} ORDER BY o.display_name, p.name"""
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [dict(r._mapping) for r in db.execute(text(sql), params)]


def tribes(db) -> list[tuple[int, str]]:
    return db.execute(text("SELECT id, display_name FROM organisation WHERE kind = 'tribe' AND lifecycle <> 'merged'"
                           " ORDER BY display_name")).all()
