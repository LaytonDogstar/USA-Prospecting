"""Phase 4 enrichment jobs: Ahrefs traffic, keyword SERP snapshots, and the research pass (see research.py).

Measurements from a licensed source (Ahrefs) are written as accepted claims with their source and date, because
they are data points, not judgements. Anything that would change what we believe about an organisation, or a
new organisation found in search results, goes to the review inbox.
"""
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from sqlalchemy import select, text

from . import models as m
from .jobs import job
from .normalise import norm_domain
from .sources.ahrefs import AhrefsClient
from .store import Store

METHOD_TRAFFIC = "ahrefs:metrics:v1"
METHOD_SERP = "ahrefs:serp:v1"
MATERIAL_TRAFFIC_CHANGE = 0.3
DEFAULT_KEYWORDS = [
    "bad credit loans", "payday loans online", "installment loans for bad credit", "personal loans for bad credit",
    "cash advance online", "loans for bad credit guaranteed approval", "online loans no credit check",
    "same day loans", "emergency loans", "tribal loans",
]


def seed_keywords(s, actor: str) -> int:
    if s.scalar(select(m.TrackedKeyword.id).limit(1)):
        return 0
    for k in DEFAULT_KEYWORDS:
        s.add(m.TrackedKeyword(keyword=k, state=None, active=True))
    s.add(m.AuditLog(actor=actor, action="keywords.seed", after={"keywords": DEFAULT_KEYWORDS}))
    s.flush()
    return len(DEFAULT_KEYWORDS)


def traffic_targets(s, limit: int, max_age_days: int = 30) -> list[tuple[int, str, str]]:
    """Organisations with a website but no fresh Ahrefs figure: our relationships first, then A/B priority,
    then by score."""
    return [tuple(r) for r in s.execute(text("""
        SELECT o.id, o.display_name, d.hostname
        FROM organisation o
        JOIN LATERAL (SELECT hostname FROM domain WHERE organisation_id = o.id ORDER BY is_primary DESC, id LIMIT 1) d ON true
        LEFT JOIN our_relationship ours ON ours.organisation_id = o.id
        LEFT JOIN latest_score bv ON bv.entity_id = o.id AND bv.score_type = 'buyer_value'
        LEFT JOIN latest_score sv ON sv.entity_id = o.id AND sv.score_type = 'supply_value'
        LEFT JOIN compliance_status cs ON cs.entity_id = o.id
        WHERE o.lifecycle IN ('active','candidate') AND cs.value IS DISTINCT FROM 'blocked'
          AND NOT EXISTS (SELECT 1 FROM metric_snapshot ms JOIN source src ON src.id = ms.source_id
                          WHERE ms.entity_id = o.id AND ms.metric = 'ahrefs_organic_visits'
                            AND src.source_type = 'ahrefs' AND ms.retrieved_at > now() - make_interval(days => :age))
        ORDER BY (ours.id IS NULL), (o.tier IS NULL), o.tier,
                 greatest(coalesce(bv.value, 0), coalesce(sv.value, 0)) DESC, o.id
        LIMIT :lim"""), {"lim": limit, "age": max_age_days})]


def _ahrefs_source(store: Store, url: str, title: str) -> m.Source:
    src = m.Source(url=url, source_type="ahrefs", title=title, retrieved_at=datetime.now(timezone.utc))
    store.s.add(src)
    store.s.flush()
    return src


def fill_traffic(store: Store, limit: int = 50, client: AhrefsClient | None = None, force: bool = False) -> dict:
    s = store.s
    out = {"checked": 0, "cached": 0, "material_changes": 0}
    with job(s, "ahrefs_fill_traffic", {"limit": limit, "force": force}, store.actor) as ctx:
        client = client or AhrefsClient(s, job_run_id=ctx.run.id)
        client.job_run_id = ctx.run.id
        before = client.subscription().get("units_usage_api_key")
        period = datetime.now(timezone.utc).strftime("%Y-%m")
        for org_id, name, host in traffic_targets(s, limit):
            mt = client.domain_metrics(host, force=force)
            out["checked"] += 1
            out["cached"] += bool(mt.get("_cached"))
            src = _ahrefs_source(store, f"https://app.ahrefs.com/site-explorer/overview/v2/subdomains/live?target={quote(host)}",
                                 f"Ahrefs Site Explorer, {host}, US, {period}")
            prev = s.scalar(select(m.MetricSnapshot.value).where(
                m.MetricSnapshot.entity_id == org_id, m.MetricSnapshot.metric == "ahrefs_organic_visits")
                .order_by(m.MetricSnapshot.retrieved_at.desc()).limit(1))
            for attr, val in (("ahrefs_organic_visits", mt.get("org_traffic")),
                              ("ahrefs_paid_spend_usd", (mt["paid_cost"] / 100) if mt.get("paid_cost") is not None else None),
                              ("ahrefs_organic_keywords", mt.get("org_keywords")),
                              ("ahrefs_paid_traffic", mt.get("paid_traffic"))):
                if val is None:
                    continue
                c = store.claim(org_id, attr, val, src, confidence="medium", method=METHOD_TRAFFIC)
                store.metric(org_id, attr, val, period, src, c)
                ctx.wrote()
            new = mt.get("org_traffic")
            if prev and new is not None and abs(float(new) - float(prev)) / max(float(prev), 1) > MATERIAL_TRAFFIC_CHANGE:
                store.review("material_change", f"traffic:{org_id}:{period}",
                             f"Organic traffic moved {((float(new) - float(prev)) / float(prev)):+.0%}: {name}",
                             [org_id], {"reasons": [f"US organic visits {float(prev):,.0f} → {float(new):,.0f} ({host})"]})
                out["material_changes"] += 1
        after = client.subscription().get("units_usage_api_key")
        if before is not None and after is not None:
            actual = after - before
            estimated = sum(float(u.units) for u in s.scalars(select(m.ApiUsage).where(m.ApiUsage.job_run_id == ctx.run.id)))
            if actual != estimated:  # correct the ledger to what Ahrefs actually charged
                s.add(m.ApiUsage(provider="ahrefs", units=actual - estimated, cost_usd=0, job_run_id=ctx.run.id))
            out["units_charged"] = actual
        ctx.run.args = {**ctx.run.args, "result": out}
    return out


def serp_snapshot(store: Store, keywords: list[str] | None = None, top: int = 20, client: AhrefsClient | None = None,
                  force: bool = False) -> dict:
    """Who ranks for each tracked keyword, with page-level traffic (the basis for comparison-table sizing)."""
    s = store.s
    out = {"keywords": 0, "pages": 0, "new_domains": 0}
    with job(s, "ahrefs_serp_snapshot", {"keywords": keywords, "top": top}, store.actor) as ctx:
        seed_keywords(s, store.actor)
        kws = keywords or [k.keyword for k in s.scalars(select(m.TrackedKeyword).where(m.TrackedKeyword.active))]
        client = client or AhrefsClient(s, job_run_id=ctx.run.id)
        client.job_run_id = ctx.run.id
        period = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        for kw in kws:
            res = client.serp(kw, top=top, force=force)
            out["keywords"] += 1
            src = _ahrefs_source(store, f"https://app.ahrefs.com/keywords-explorer/google/us/overview?keyword={quote(kw)}",
                                 f"Ahrefs SERP overview, '{kw}', US, {period}")
            for pos in res["positions"]:
                host = norm_domain(pos.get("url"))
                if not host or not pos.get("url"):
                    continue
                owner = store.domain_owner(host)
                if owner is None:
                    org, _ = store.resolve(host, [host], lifecycle="candidate")
                    store.add_domain(org.id, host, primary=True)
                    owner = org.id
                    store.review("new_entrant", f"new_entrant:serp:{host}", f"New in search results: {host}",
                                 [owner], {"reasons": [f"ranks #{pos.get('position')} for '{kw}'"], "url": pos["url"]})
                    out["new_domains"] += 1
                d = s.scalar(select(m.Domain).where(m.Domain.hostname == host))
                page = s.scalar(select(m.Page).where(m.Page.url == pos["url"]))
                if page is None:
                    page = m.Page(id=store._entity("page"), domain_id=d.id if d else None, url=pos["url"],
                                  page_type="serp_result")
                    s.add(page)
                    s.flush()
                c = store.claim(page.id, "serp_rank", {"keyword": kw, "position": pos.get("position"),
                                                       "type": pos.get("type"), "title": pos.get("title"),
                                                       "organisation_id": owner}, src,
                                confidence="medium", method=METHOD_SERP)
                store.metric(page.id, f"serp_position:{kw}", pos.get("position"), period, src, c)
                if pos.get("traffic") is not None:
                    store.metric(page.id, "page_organic_traffic", pos["traffic"], period, src, c)
                ctx.wrote()
                out["pages"] += 1
        ctx.run.args = {**ctx.run.args, "result": out}
    return out
