"""Read queries for the pages. SQL is assembled only from whitelisted fragments; user input is always bound."""
from sqlalchemy import text

from ..footprint import LENDER_SQL, STATE_SERVES_SQL
from ..normalise import friendly_name
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
       (SELECT hostname FROM domain d WHERE d.organisation_id = o.id ORDER BY d.is_primary DESC, d.id LIMIT 1) AS domain,
       (SELECT b.name FROM brand b LEFT JOIN domain d ON d.brand_id = b.id
         WHERE b.organisation_id = o.id ORDER BY d.is_primary IS TRUE DESC, b.id LIMIT 1) AS brand,
       EXISTS (SELECT 1 FROM review_item ri WHERE ri.status = 'open' AND ri.kind = 'possible_match'
               AND o.id = ANY(ri.entity_ids)) AS pending_match
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
        where.append(LENDER_SQL)
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
        r["name"] = friendly_name(r["display_name"], r["brand"])
    return rows, total


def top_unworked(db, kind: str, n: int = 10) -> tuple[list[dict], int]:
    """Best organisations with no relationship. Anything that may already be a partner (an open possible match
    with one of our relationships) is held back, so nobody cold-calls an existing partner as a prospect."""
    rows, _ = org_table(db, kind, {"our": "none", "sort": "score"}, limit=n + 60)
    held = [r for r in rows if r["pending_match"]]
    return [r for r in rows if not r["pending_match"]][:n], len(held)


def review_counts(db) -> dict:
    return dict(db.execute(text("SELECT kind, count(*) FROM review_item WHERE status = 'open' GROUP BY kind")).all())


def recent_activity(db, n: int = 12) -> list[dict]:
    """Latest edits and decisions, with runs of the same action by the same person (e.g. an import seeding 54
    relationships) folded into one line."""
    rows = [dict(r._mapping) for r in db.execute(text("""
        SELECT at, actor, action, table_name, row_id, after FROM audit_log
        WHERE action NOT IN ('login.success', 'login.failure')
        ORDER BY at DESC, id DESC LIMIT 400"""))]
    out = []
    for r in rows:
        if out and out[-1]["actor"] == r["actor"] and out[-1]["action"] == r["action"]:
            out[-1]["count"] += 1
            continue
        if len(out) == n:
            break
        out.append({**r, "count": 1})
    return out


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


def owner_groups(db) -> list[dict]:
    """Owners with two or more organisations under them: one contact, several brands."""
    rows = [dict(r._mapping) for r in db.execute(text("""
        SELECT g.id, g.display_name, g.kind,
               (SELECT b.name FROM brand b LEFT JOIN domain d ON d.brand_id = b.id
                 WHERE b.organisation_id = g.id ORDER BY d.is_primary IS TRUE DESC, b.id LIMIT 1) AS brand,
               count(c.id) AS members,
               array_agg(c.display_name ORDER BY c.display_name) AS member_names,
               count(ours.id) FILTER (WHERE ours.status IN ('live','contracting','in_talks','paused')) AS ours,
               count(c.id) FILTER (WHERE EXISTS (SELECT 1 FROM compliance_flag f WHERE f.entity_id = c.id)) AS flagged,
               max(greatest(coalesce(bv.value, 0), coalesce(sv.value, 0))) AS best_score,
               (SELECT string_agg(DISTINCT r.role, ',') FROM org_role r JOIN organisation c2 ON c2.id = r.organisation_id
                 WHERE c2.parent_id = g.id) AS roles,
               (SELECT ourg.status FROM our_relationship ourg WHERE ourg.organisation_id = g.id) AS group_ours
        FROM organisation g
        JOIN organisation c ON c.parent_id = g.id AND c.lifecycle IN ('active','candidate')
        LEFT JOIN our_relationship ours ON ours.organisation_id = c.id
        LEFT JOIN latest_score bv ON bv.entity_id = c.id AND bv.score_type = 'buyer_value'
        LEFT JOIN latest_score sv ON sv.entity_id = c.id AND sv.score_type = 'supply_value'
        WHERE g.lifecycle <> 'merged'
        GROUP BY g.id
        HAVING count(c.id) >= 2
        ORDER BY count(c.id) DESC, max(greatest(coalesce(bv.value, 0), coalesce(sv.value, 0))) DESC"""))]
    for r in rows:
        r["name"] = friendly_name(r["display_name"], r["brand"])
        r["role_list"] = sorted({ROLE_LABELS.get(x, x) for x in (r["roles"] or "").split(",") if x})
    return rows


def shared_feeders(db, min_buyers: int = 2) -> list[dict]:
    """Sources that, by the evidence we hold, sell leads to two or more of the buyers we already sell to."""
    rows = [dict(r._mapping) for r in db.execute(text("""
        SELECT s.id, s.display_name,
               (SELECT b.name FROM brand b LEFT JOIN domain d ON d.brand_id = b.id
                 WHERE b.organisation_id = s.id ORDER BY d.is_primary IS TRUE DESC, b.id LIMIT 1) AS brand,
               (SELECT hostname FROM domain d WHERE d.organisation_id = s.id ORDER BY d.is_primary DESC, d.id LIMIT 1) AS domain,
               count(DISTINCT t.id) AS buyers,
               array_agg(DISTINCT t.display_name) AS buyer_names,
               bool_or(e.status = 'confirmed') AS any_confirmed,
               (SELECT ours.status FROM our_relationship ours WHERE ours.organisation_id = s.id) AS our_status,
               (SELECT value FROM latest_score sv WHERE sv.entity_id = s.id AND sv.score_type = 'supply_value') AS supply_value,
               (SELECT string_agg(DISTINCT role, ',') FROM org_role r WHERE r.organisation_id = s.id) AS roles,
               (SELECT count(*) FROM relationship e2 WHERE e2.from_id = s.id AND e2.type = 'sells_leads_to') AS buyers_total
        FROM relationship e
        JOIN organisation s ON s.id = e.from_id AND s.lifecycle <> 'merged'
        JOIN organisation t ON t.id = e.to_id
        JOIN our_relationship tr ON tr.organisation_id = t.id AND tr.status IN ('live','contracting')
             AND tr.role IN ('buyer','both','network')
        WHERE e.type = 'sells_leads_to'
        GROUP BY s.id
        HAVING count(DISTINCT t.id) >= :n
        ORDER BY count(DISTINCT t.id) DESC, s.display_name"""), {"n": min_buyers})]
    for r in rows:
        r["name"] = friendly_name(r["display_name"], r["brand"])
        r["role_list"] = sorted({ROLE_LABELS.get(x, x) for x in (r["roles"] or "").split(",") if x})
    return rows
