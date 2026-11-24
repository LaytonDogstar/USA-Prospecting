"""Contacts: decision-makers' work emails for the best prospects (Hunter), with the researchers as the fallback.

For each qualifying organisation (score 60+, not held back as a possible partner, compliance not blocked, not
screened out, has a website, nobody on record yet, not marked do-not-contact, not looked up in the last 90 days):

1. Hunter domain search (personal addresses only). Keep the best 3 by job title (partnerships / affiliate / lead
   acquisition / marketing / growth / leadership). Generic inboxes (info@, support@) and anyone without a relevant
   title are left out. Hunter's verification status is used; an address without one is verified (0.5 credit).
   Invalid addresses are never saved.
2. If nobody relevant is found, the company goes to the researchers ("people" mode) for named decision-makers.
   When those names are approved on the research card they become people without an email, and the next run
   asks Hunter's email finder for each of them.

Every person keeps its source (the Hunter lookup and the public pages Hunter cites) and a lawful-basis note.
Contacts are never sent anything by this app.
"""
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text

from . import models as m
from .budget import BudgetExceeded
from .jobs import job
from .store import Store

MIN_SCORE = 60
MIN_SCORE_PARTNER_LIST = 50  # a lender named on a lead source's partner list qualifies from 50
PER_ORG = 3
RECHECK_DAYS = 90
METHOD = "hunter:v1"

# title keyword -> (rank, role category). The highest-ranking match wins.
TITLE_RULES = [
    (r"affiliate|partnership|partner (?:manager|development|marketing)|business development|bizdev|\bbd\b", 100,
     "affiliate_partnerships"),
    (r"lead (?:gen|generation|acquisition|buying|partnerships|purchasing)|\bleads\b|acquisition|performance|paid (?:media|search)|media buy|digital marketing|demand gen", 90,
     "acquisition_performance"),
    (r"\bcmo\b|chief marketing|growth|marketing", 80, "growth_cmo"),
    (r"\bceo\b|chief executive|founder|(?<!vice )(?<!vice-)\bpresident\b|\bowner\b|managing director", 70, "ceo_gm"),
    (r"^(?:(?!vice).)*\b(?:general manager|\bgm\b)", 40, "ceo_gm"),  # often a store manager at a large chain
    (r"\bcoo\b|chief operating|chief revenue|\bcro\b|head of (?:sales|revenue)|vp,? sales|sales director", 60,
     "ceo_gm"),
    (r"servicing|origination", 40, "servicer_acquisition"),
]
SENIOR = {"executive": 30, "senior": 15}
# titles that are never the right first contact, and roles outside the US
EXCLUDE_TITLE = re.compile(r"technolog|engineer|developer|software|\bit\b|information|data|security|project|product "
                           r"manag|analyst|recruit|talent|human resources|\bhr\b|people|finance|accounting|"
                           r"controller|treasur|legal|counsel|attorney|risk|collections|customer service|support|store|branch|district|"
                           r"region(?:al)? manager|operations manager|^(?:senior |executive |assistant )?vice president$|"
                           r"^(?:senior |executive )?vp$|"
                           r"\b(?:uk|emea|europe|apac|canada|australia|brazil|mexico|india|latam)\b", re.I)
GENERIC_LOCAL = re.compile(r"^(info|support|help|hello|contact|admin|sales|service|customerservice|cs|team|office|"
                           r"marketing|privacy|legal|compliance|jobs|careers|hr|billing|noreply|no-reply)\b", re.I)

# who may be looked up: same hold-back rule as the tables and map (a possible partner is never contacted)
CANDIDATES_SQL = """
SELECT o.id, o.display_name,
       (SELECT hostname FROM domain d WHERE d.organisation_id = o.id ORDER BY d.is_primary DESC, d.id LIMIT 1) AS domain,
       GREATEST(coalesce(b.value, 0), coalesce(sv.value, 0)) AS best
FROM organisation o
LEFT JOIN latest_score b ON b.entity_id = o.id AND b.score_type = 'buyer_value'
LEFT JOIN latest_score sv ON sv.entity_id = o.id AND sv.score_type = 'supply_value'
LEFT JOIN compliance_status cs ON cs.entity_id = o.id
WHERE o.lifecycle NOT IN ('merged', 'screened_out')
  AND o.market = 'US'  -- Australian contacts wait for their own lawful-basis note (Spam Act / Privacy Act)
  AND (GREATEST(coalesce(b.value, 0), coalesce(sv.value, 0)) >= :min_score
       OR (GREATEST(coalesce(b.value, 0), coalesce(sv.value, 0)) >= :min_partner
           AND EXISTS (SELECT 1 FROM entity_current ec WHERE ec.entity_id = o.id
                       AND ec.attribute = 'appears_on_partner_lists')))
  AND (cs.value IS NULL OR cs.value <> 'blocked')
  AND NOT EXISTS (SELECT 1 FROM our_relationship r WHERE r.organisation_id = o.id)
  AND EXISTS (SELECT 1 FROM domain d WHERE d.organisation_id = o.id)
  AND NOT EXISTS (SELECT 1 FROM person p WHERE p.organisation_id = o.id)
  AND NOT EXISTS (SELECT 1 FROM entity_current ec WHERE ec.entity_id = o.id AND ec.attribute = 'do_not_contact'
                  AND ec.value = 'true'::jsonb)
  AND NOT EXISTS (SELECT 1 FROM claim c WHERE c.entity_id = o.id AND c.attribute = 'contacts_checked'
                  AND c.created_at > :since)
  AND NOT EXISTS (SELECT 1 FROM review_item ri WHERE ri.status = 'open' AND ri.kind = 'possible_match'
                  AND o.id = ANY(ri.entity_ids))
  AND NOT EXISTS (SELECT 1 FROM entity_current ec WHERE ec.entity_id = o.id AND ec.attribute = 'partner_related')
  AND NOT EXISTS (SELECT 1 FROM organisation ch JOIN our_relationship oc ON oc.organisation_id = ch.id
                  WHERE ch.parent_id = o.id)
  AND NOT EXISTS (SELECT 1 FROM our_relationship op WHERE op.organisation_id = o.parent_id)
ORDER BY best DESC, o.id
LIMIT :limit
"""


def rank_title(title: str | None, seniority: str | None = None) -> tuple[int, str]:
    t = (title or "").lower()
    if not t or EXCLUDE_TITLE.search(t):
        return 0, "other"
    for pattern, rank, cat in TITLE_RULES:
        if re.search(pattern, t):
            return rank + SENIOR.get(seniority or "", 0), cat
    return 0, "other"  # seniority alone is not enough: the title has to say what they do


def pick(emails: list[dict], n: int = PER_ORG) -> list[dict]:
    """The best n personal addresses with a relevant title. Generic inboxes and invalid addresses are dropped."""
    out = []
    for e in emails or []:
        addr = (e.get("value") or "").strip().lower()
        if not addr or "@" not in addr or e.get("type") == "generic" or GENERIC_LOCAL.match(addr.split("@")[0]):
            continue
        if ((e.get("verification") or {}).get("status")) == "invalid":
            continue
        name = " ".join(x for x in (e.get("first_name"), e.get("last_name")) if x).strip()
        if not name or not e.get("last_name"):
            continue
        label = addr.split("@")[1].split(".")[0]
        if (e.get("first_name") or "").lower() in label:  # "rise@risecredit.com": a company inbox, not a person
            continue
        rank, cat = rank_title(e.get("position"), e.get("seniority"))
        if rank <= 0:
            continue
        out.append({**e, "_rank": rank, "_cat": cat, "_name": name, "_email": addr})
    out.sort(key=lambda e: (-e["_rank"], -(e.get("confidence") or 0)))
    return out[:n]


def _hunter_source(store: Store, domain: str, kind: str) -> m.Source:
    src = m.Source(source_type="hunter", title=f"Hunter {kind}: {domain}", url=f"https://hunter.io/search/{domain}",
                   retrieved_at=datetime.now(timezone.utc))
    store.s.add(src)
    store.s.flush()
    return src


def _save_person(store: Store, org: m.Organisation, e: dict, src: m.Source, status: str | None, score) -> m.Person:
    pages = [x.get("uri") for x in (e.get("sources") or []) if x.get("uri")][:3]
    person = m.Person(id=store._entity("person"), organisation_id=org.id, name=e["_name"][:200],
                      title=(e.get("position") or None), role_category=e["_cat"], email=e["_email"],
                      linkedin_url=(e.get("linkedin") if str(e.get("linkedin") or "").startswith("https://") else None),
                      source=f"Hunter domain search ({', '.join(pages) or 'no public pages listed'})"[:1000],
                      last_verified_at=datetime.now(timezone.utc), email_status=status or "unknown",
                      email_confidence=int(score) if score is not None else None, do_not_contact=False)
    store.s.add(person)
    store.s.flush()
    store.claim(person.id, "email", {"value": e["_email"], "status": status, "pages": pages}, src,
                confidence="high" if status == "valid" else "medium", method=METHOD)
    return person


def lookup_org(store: Store, org: m.Organisation, domain: str, client, ctx=None) -> dict:
    """One Hunter domain search for one organisation. Returns {'saved': [...], 'asked': bool}."""
    from . import research
    data = client.domain_search(domain)
    chosen = pick(data.get("emails") or [])
    src = _hunter_source(store, domain, "domain search")
    saved = []
    for e in chosen:
        status = (e.get("verification") or {}).get("status")
        score = e.get("confidence")
        if not status:
            v = client.verify(e["_email"])
            status, score = v.get("status") or "unknown", v.get("score", score)
        if status == "invalid":
            continue
        saved.append(_save_person(store, org, e, src, status, score).name)
    store.claim(org.id, "contacts_checked", {"found": len(saved), "domain": domain,
                                             "hunter_total": len(data.get("emails") or [])}, src,
                confidence="high", method=METHOD)
    asked = False
    if not saved:
        open_people = store.s.scalar(select(m.ResearchRequest.id).where(
            m.ResearchRequest.organisation_id == org.id, m.ResearchRequest.mode == "people",
            m.ResearchRequest.status.in_(("queued", "running"))))
        if open_people is None:
            research.ask(store, org.id, [
                "Who leads partnerships, affiliates or lead acquisition / marketing here? Give full names and job titles.",
                "Who are the CEO / founder and other senior leaders? Give full names and job titles."],
                mode="people", context=f"Hunter found no suitable contact at {domain}.", model=research.DEFAULT_MODEL)
            asked = True
    store.audit("contacts.lookup", "organisation", org.id, None, {"domain": domain, "saved": saved, "asked": asked})
    if ctx is not None:
        ctx.wrote(len(saved) + 1)
    return {"saved": saved, "asked": asked}


def _split(name: str) -> tuple[str, str] | None:
    parts = [x for x in re.split(r"\s+", re.sub(r"\b(dr|mr|mrs|ms|jr|sr|ii|iii)\.?\b", "", name, flags=re.I).strip()) if x]
    if len(parts) < 2:
        return None
    return parts[0].strip(",."), parts[-1].strip(",.")


def find_named(store: Store, client, limit: int = 20, ctx=None, org_ids: list[int] | None = None) -> int:
    """Email finder for named people (from approved research) who have no email yet and haven't been tried."""
    q = (select(m.Person).join(m.Organisation, m.Organisation.id == m.Person.organisation_id)
         .where(m.Person.email.is_(None), m.Person.email_status.is_(None), m.Person.do_not_contact.is_(False),
                m.Organisation.lifecycle.notin_(("merged", "screened_out")))
         .order_by(m.Person.id).limit(limit))
    if org_ids:
        q = q.where(m.Person.organisation_id.in_(org_ids))
    found = 0
    for person in store.s.scalars(q):
        domain = store.s.scalar(select(m.Domain.hostname).where(m.Domain.organisation_id == person.organisation_id)
                                .order_by(m.Domain.is_primary.desc(), m.Domain.id))
        split = _split(person.name)
        if not domain or not split:
            person.email_status = "none"
            continue
        data = client.email_finder(domain, *split)
        addr = (data.get("email") or "").strip().lower()
        status = (data.get("verification") or {}).get("status") or ("accept_all" if data.get("accept_all") else None)
        if not addr or status == "invalid" or GENERIC_LOCAL.match(addr.split("@")[0]):
            person.email_status = "none"
            continue
        src = _hunter_source(store, domain, f"email finder for {person.name}")
        person.email, person.email_status = addr, status or "unknown"
        person.email_confidence = data.get("score")
        person.last_verified_at = datetime.now(timezone.utc)
        pages = [x.get("uri") for x in (data.get("sources") or []) if x.get("uri")][:3]
        store.claim(person.id, "email", {"value": addr, "status": status, "pages": pages}, src,
                    confidence="high" if status == "valid" else "medium", method=METHOD)
        store.audit("contacts.email_found", "organisation", person.organisation_id, None, {"person": person.name})
        found += 1
        if ctx is not None:
            ctx.wrote()
    return found


def find_contacts(store: Store, limit: int = 10, org_ids: list[int] | None = None, client=None,
                  min_score: int = MIN_SCORE) -> dict:
    """The scheduled job. org_ids (the "Find contacts" button) skips the score threshold but not the safeguards."""
    from .sources.hunter import HunterClient, HunterError
    s = store.s
    out = {"looked_up": 0, "people": 0, "sent_to_research": 0, "emails_found": 0, "skipped": []}
    with job(s, "contacts", {"limit": limit, "org_ids": org_ids}, store.actor) as ctx:
        client = client or HunterClient(s, job_run_id=ctx.run.id)
        params = {"min_score": 0 if org_ids else min_score, "min_partner": 0 if org_ids else MIN_SCORE_PARTNER_LIST,
                  "limit": 5000 if org_ids else limit,
                  "since": datetime.now(timezone.utc) - timedelta(days=0 if org_ids else RECHECK_DAYS)}
        rows = s.execute(text(CANDIDATES_SQL), params).mappings().all()
        if org_ids:
            wanted = set(org_ids)
            rows = [r for r in rows if r["id"] in wanted]
            for oid in wanted - {r["id"] for r in rows}:
                out["skipped"].append(oid)
        try:
            out["emails_found"] = find_named(store, client, ctx=ctx, org_ids=org_ids)
            for r in rows:
                org = s.get(m.Organisation, r["id"])
                try:
                    res = lookup_org(store, org, r["domain"], client, ctx)
                except HunterError as e:
                    ctx.error(str(e), org=org.id)
                    if "quota" in str(e) or "refused" in str(e):
                        break
                    continue
                out["looked_up"] += 1
                out["people"] += len(res["saved"])
                out["sent_to_research"] += int(res["asked"])
                s.flush()
        except BudgetExceeded:
            ctx.run.args = {**ctx.run.args, "result": out}
            raise
        ctx.run.args = {**ctx.run.args, "result": out}
    return out


def set_do_not_contact(store: Store, org: m.Organisation, on: bool, note: str) -> None:
    src = m.Source(source_type="manual", title=f"Set by {store.actor}", retrieved_at=datetime.now(timezone.utc))
    store.s.add(src)
    store.s.flush()
    store.claim(org.id, "do_not_contact", bool(on), src, confidence="high", method=f"manual:{store.actor}")
    for person in store.s.scalars(select(m.Person).where(m.Person.organisation_id == org.id)):
        person.do_not_contact = bool(on) or person.do_not_contact
    store.audit("organisation.do_not_contact", "organisation", org.id, None, {"on": bool(on), "note": note})
