"""Entity page, evidence drawer and source pages, plus the edits made from the entity page."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select, text, or_, and_
from sqlalchemy.orm import Session

from ... import models as m
from ..deps import admin_user, current_user, flash, get_db, render, store_for
from ..views import ROLE_LABELS

router = APIRouter()

# Facts shown in the profile block, in this order. Everything else is under "All facts".
PROFILE = [
    ("description", "What it is"), ("why_it_fits", "Why it fits"), ("traffic_category", "Traffic category"),
    ("priority", "Priority (traffic file)"), ("lender_type_text", "Lender type"), ("products_text", "Products"),
    ("loan_range_text", "Loan range"), ("states_text", "States"), ("online_or_branch_text", "Online / branch"),
    ("segment_text", "Segment"), ("operator_group", "Operator group"), ("tribe", "Tribe"),
    ("parent_servicer_text", "Parent / servicer"), ("lead_buying_evidence", "Lead-buying evidence"),
    ("appears_on_partner_lists", "Appears on partner / consent lists"), ("affiliate_programme", "Affiliate programme"),
    ("disclosed_acquisition_data", "Disclosed acquisition data"), ("lead_acquisition_hiring", "Acquisition hiring"),
    ("conferences", "Conferences"), ("evidence", "Evidence"), ("how_to_work_with", "How to work with them"),
    ("notes_risk", "Notes / risk"), ("roles_to_approach", "Roles to approach"),
]
HIDDEN = {"brand", "domain", "declared_domain", "role", "website"}
METRIC_LABELS = {
    "cfpb_complaints_12m": "CFPB complaints (12m)", "cfpb_complaints_installment_12m": "… installment",
    "cfpb_complaints_payday_12m": "… payday", "cfpb_complaints_loc_12m": "… line of credit",
    "ahrefs_organic_visits": "Organic visits / month (Ahrefs)", "ahrefs_paid_spend_usd": "Paid search $ / month (Ahrefs)",
}


def _org_or_404(db: Session, org_id: int) -> m.Organisation:
    org = db.get(m.Organisation, org_id)
    if org is None:
        raise HTTPException(404, "No such organisation")
    return org


def _source_label(src: m.Source, files: dict) -> str:
    if src.url:
        return src.url
    f = files.get(src.import_file_id)
    return f"{f.file_name if f else 'import'} › {src.sheet} › row {src.row_number}"


@router.get("/org/{org_id}")
def org_page(org_id: int, request: Request, db: Session = Depends(get_db), user=Depends(current_user)):
    org = _org_or_404(db, org_id)
    if org.lifecycle == "merged" and org.merged_into_id:
        return RedirectResponse(f"/org/{org.merged_into_id}", status_code=303)
    oid = org.id
    names = {o.id: o.display_name for o in db.scalars(select(m.Organisation))}
    current = {r.attribute: r for r in db.execute(text(
        "SELECT ec.attribute, ec.value, ec.claim_id, ec.confidence, ec.created_at, "
        "(SELECT count(*) FROM claim c WHERE c.entity_id = ec.entity_id AND c.attribute = ec.attribute) AS n "
        "FROM entity_current ec WHERE ec.entity_id = :id ORDER BY ec.attribute"), {"id": oid})}
    profile = [(k, label, current[k]) for k, label in PROFILE if k in current]
    shown = {k for k, _ in PROFILE} | HIDDEN
    other_facts = [(k, current[k]) for k in current if k not in shown and not k.startswith(("cfpb_complaints", "ahrefs_"))]

    brands = list(db.scalars(select(m.Brand).where(m.Brand.organisation_id == oid).order_by(m.Brand.name)))
    domains = list(db.scalars(select(m.Domain).where(m.Domain.organisation_id == oid).order_by(m.Domain.is_primary.desc(), m.Domain.hostname)))
    declared = [r for r in db.scalars(select(m.Claim).where(m.Claim.entity_id == oid, m.Claim.attribute == "declared_domain",
                                                           m.Claim.status == "accepted"))
                if r.value not in {d.hostname for d in domains}]
    aliases = [a.alias for a in db.scalars(select(m.NameAlias).where(m.NameAlias.entity_id == oid).order_by(m.NameAlias.alias))
               if a.alias != org.display_name]
    roles = list(db.scalars(select(m.OrgRole).where(m.OrgRole.organisation_id == oid)))
    states = {}
    for es in db.scalars(select(m.EntityState).where(m.EntityState.entity_id == oid).order_by(m.EntityState.state)):
        states.setdefault(es.basis, []).append(es)
    metrics = db.execute(text("""
        SELECT DISTINCT ON (metric) metric, value, period, claim_id, retrieved_at,
               count(*) OVER (PARTITION BY metric) AS n
        FROM metric_snapshot WHERE entity_id = :id ORDER BY metric, retrieved_at DESC, id DESC"""), {"id": oid}).all()
    edges_out = list(db.scalars(select(m.Relationship).where(m.Relationship.from_id == oid).order_by(m.Relationship.type)))
    edges_in = list(db.scalars(select(m.Relationship).where(m.Relationship.to_id == oid).order_by(m.Relationship.type)))
    children = list(db.scalars(select(m.Organisation).where(m.Organisation.parent_id == oid, m.Organisation.lifecycle != "merged")
                               .order_by(m.Organisation.display_name)))
    merged_from = list(db.scalars(select(m.Organisation).where(m.Organisation.merged_into_id == oid)))
    scores = {r.score_type: r for r in db.execute(text("SELECT * FROM latest_score WHERE entity_id = :id"), {"id": oid})}
    score_history = {st: db.execute(text("""
        SELECT s.value, s.computed_at, w.label FROM score s LEFT JOIN weights_version w ON w.id = s.weights_version_id
        WHERE s.entity_id = :id AND s.score_type = :st ORDER BY s.computed_at DESC, s.id DESC LIMIT 6"""),
        {"id": oid, "st": st}).all() for st in ("buyer_value", "supply_value")}
    flags = list(db.scalars(select(m.ComplianceFlag).where(m.ComplianceFlag.entity_id == oid)))
    cstatus = db.get(m.ComplianceStatus, oid)
    chistory = list(db.scalars(select(m.ComplianceStatusHistory).where(m.ComplianceStatusHistory.entity_id == oid)
                               .order_by(m.ComplianceStatusHistory.set_at.desc())))
    people = list(db.scalars(select(m.Person).where(m.Person.organisation_id == oid).order_by(m.Person.name)))
    ours = db.scalar(select(m.OurRelationship).where(m.OurRelationship.organisation_id == oid))
    ours_history = list(db.scalars(select(m.OurRelationshipHistory).where(m.OurRelationshipHistory.our_relationship_id == ours.id)
                                   .order_by(m.OurRelationshipHistory.changed_at.desc()))) if ours else []
    reviews = list(db.scalars(select(m.ReviewItem).where(m.ReviewItem.status == "open", m.ReviewItem.entity_ids.contains([oid]))))
    timeline = _timeline(db, oid, ours.id if ours else None)
    return render(request, "org.html", user, org=org, names=names, parent=db.get(m.Organisation, org.parent_id) if org.parent_id else None,
                  profile=profile, other_facts=other_facts, brands=brands, domains=domains, declared=declared, aliases=aliases,
                  roles=roles, role_labels=ROLE_LABELS, states=states, metrics=metrics, metric_labels=METRIC_LABELS,
                  edges_out=edges_out, edges_in=edges_in, children=children, merged_from=merged_from, scores=scores,
                  score_history=score_history, flags=flags, cstatus=cstatus, chistory=chistory, people=people,
                  ours=ours, ours_history=ours_history, reviews=reviews, timeline=timeline,
                  our_roles=m.OUR_ROLES, our_statuses=m.OUR_STATUSES, person_roles=m.PERSON_ROLE_CATEGORIES)


def _timeline(db: Session, oid: int, ours_id: int | None) -> list[dict]:
    events = [dict(r._mapping) for r in db.execute(text("""
        SELECT max(c.created_at) AS at, 'facts' AS kind, c.method AS actor,
               count(*) || ' facts from ' || coalesce(f.file_name, s.source_type) AS what
        FROM claim c JOIN source s ON s.id = c.source_id LEFT JOIN import_file f ON f.id = s.import_file_id
        WHERE c.entity_id = :id GROUP BY date(c.created_at), c.method, coalesce(f.file_name, s.source_type)"""), {"id": oid})]
    events += [dict(r._mapping) for r in db.execute(text("""
        SELECT at, 'edit' AS kind, actor, action || coalesce(': ' || (after->>'note'), '') AS what FROM audit_log
        WHERE (table_name = 'organisation' AND row_id = :id) OR (table_name = 'our_relationship' AND row_id = :ours)"""),
        {"id": oid, "ours": ours_id or -1})]
    return sorted(events, key=lambda e: e["at"], reverse=True)[:40]


# ---------------------------------------------------------------- evidence
def _evidence(db: Session, claims: list[m.Claim], title: str, request: Request, user, org=None):
    files = {f.id: f for f in db.scalars(select(m.ImportFile))}
    items = []
    for c in claims:
        srcs = [db.get(m.Source, c.source_id)] + list(db.scalars(
            select(m.Source).join(m.ClaimSource, m.ClaimSource.source_id == m.Source.id).where(m.ClaimSource.claim_id == c.id)))
        items.append({"claim": c, "sources": [(s, _source_label(s, files)) for s in srcs]})
    partial = request.query_params.get("partial") == "1"
    return render(request, "evidence_partial.html" if partial else "evidence.html", user, items=items, title=title, org=org)


@router.get("/org/{org_id}/evidence/{attribute}")
def evidence_attribute(org_id: int, attribute: str, request: Request, db: Session = Depends(get_db), user=Depends(current_user)):
    org = _org_or_404(db, org_id)
    claims = list(db.scalars(select(m.Claim).where(m.Claim.entity_id == org_id, m.Claim.attribute == attribute)
                             .order_by(m.Claim.created_at.desc(), m.Claim.id.desc())))
    return _evidence(db, claims, f"{org.display_name}: {attribute.replace('_', ' ')}", request, user, org)


@router.get("/evidence")
def evidence_ids(request: Request, ids: str = "", db: Session = Depends(get_db), user=Depends(current_user)):
    wanted = [int(x) for x in ids.split(",") if x.strip().isdigit()][:200]
    claims = list(db.scalars(select(m.Claim).where(m.Claim.id.in_(wanted)).order_by(m.Claim.created_at.desc()))) if wanted else []
    org = db.get(m.Organisation, claims[0].entity_id) if claims else None
    return _evidence(db, claims, "Evidence", request, user, org)


@router.get("/source/{source_id}")
def source_page(source_id: int, request: Request, db: Session = Depends(get_db), user=Depends(current_user)):
    src = db.get(m.Source, source_id)
    if src is None:
        raise HTTPException(404, "No such source")
    files = {f.id: f for f in db.scalars(select(m.ImportFile))}
    if src.import_file_id:
        # every fact taken from the same spreadsheet row
        row_sources = select(m.Source.id).where(m.Source.import_file_id == src.import_file_id, m.Source.sheet == src.sheet,
                                                m.Source.row_number == src.row_number)
        claims = list(db.scalars(select(m.Claim).where(m.Claim.source_id.in_(row_sources)).order_by(m.Claim.id)))
    else:
        cited = select(m.ClaimSource.claim_id).where(m.ClaimSource.source_id == src.id)
        claims = list(db.scalars(select(m.Claim).where(or_(m.Claim.source_id == src.id, m.Claim.id.in_(cited)))
                                 .order_by(m.Claim.id).limit(500)))
    names = {o.id: o.display_name for o in db.scalars(select(m.Organisation).where(
        m.Organisation.id.in_({c.entity_id for c in claims} or {-1})))}
    return render(request, "source.html", user, src=src, label=_source_label(src, files), file=files.get(src.import_file_id),
                  claims=claims, names=names)


# ---------------------------------------------------------------- edits (admin)
@router.post("/org/{org_id}/relationship")
def set_relationship(org_id: int, request: Request, role: str = Form(...), status: str = Form(...), owner: str = Form(""),
                     notes: str = Form(""), needs_confirmation: str = Form(""), db: Session = Depends(get_db),
                     user=Depends(admin_user)):
    org = _org_or_404(db, org_id)
    if role not in m.OUR_ROLES or status not in m.OUR_STATUSES:
        raise HTTPException(400, "Unknown role or status")
    st = store_for(db, user)
    ours = db.scalar(select(m.OurRelationship).where(m.OurRelationship.organisation_id == org.id))
    if ours is not None:
        ours.notes = notes.strip() or None  # the form shows the full notes, so replace rather than append
        ours.source = "manual"
    st.set_our_relationship(org.id, role=role, status=status, source="manual", owner=owner.strip() or None,
                            notes=notes.strip() or None, needs_confirmation=bool(needs_confirmation))
    flash(request, "Relationship saved.")
    return RedirectResponse(f"/org/{org.id}#relationship", status_code=303)


@router.post("/org/{org_id}/compliance")
def set_compliance(org_id: int, request: Request, value: str = Form(...), note: str = Form(...), db: Session = Depends(get_db),
                   user=Depends(admin_user)):
    org = _org_or_404(db, org_id)
    if value not in m.COMPLIANCE_VALUES or not note.strip():
        raise HTTPException(400, "Choose clear, review or blocked and give a reason")
    cs = db.get(m.ComplianceStatus, org.id)
    before = {"value": cs.value, "note": cs.note} if cs else None
    now = datetime.now(timezone.utc)
    if cs is None:
        cs = m.ComplianceStatus(entity_id=org.id, value=value, set_by=user.username, set_at=now, note=note.strip())
        db.add(cs)
    else:
        cs.value, cs.set_by, cs.set_at, cs.note = value, user.username, now, note.strip()
    db.add(m.ComplianceStatusHistory(entity_id=org.id, value=value, set_by=user.username, set_at=now, note=note.strip()))
    store_for(db, user).audit("compliance.set", "organisation", org.id, before, {"value": value, "note": note.strip()})
    flash(request, f"Compliance status set to {value}.")
    return RedirectResponse(f"/org/{org.id}#compliance", status_code=303)


@router.post("/org/{org_id}/people")
def add_person(org_id: int, request: Request, name: str = Form(...), title: str = Form(""), role_category: str = Form("other"),
               linkedin_url: str = Form(""), email: str = Form(""), source: str = Form(...),
               lawful_basis_note: str = Form("UK GDPR legitimate interests (B2B contact)"),
               db: Session = Depends(get_db), user=Depends(admin_user)):
    org = _org_or_404(db, org_id)
    if not name.strip() or not source.strip():
        raise HTTPException(400, "Name and source are required")
    if linkedin_url and not linkedin_url.startswith("https://"):
        raise HTTPException(400, "LinkedIn URL must start with https://")
    st = store_for(db, user)
    p = m.Person(id=st._entity("person"), organisation_id=org.id, name=name.strip(), title=title.strip() or None,
                 role_category=role_category if role_category in m.PERSON_ROLE_CATEGORIES else "other",
                 linkedin_url=linkedin_url.strip() or None, email=email.strip() or None, source=source.strip(),
                 lawful_basis_note=lawful_basis_note.strip(), last_verified_at=datetime.now(timezone.utc))
    db.add(p)
    st.audit("person.add", "organisation", org.id, None, {"person": p.name, "source": p.source})
    flash(request, f"Added {p.name}.")
    return RedirectResponse(f"/org/{org.id}#people", status_code=303)


@router.post("/person/{person_id}/delete")
def delete_person(person_id: int, request: Request, reason: str = Form(...), db: Session = Depends(get_db),
                  user=Depends(admin_user)):
    """Hard delete (e.g. a deletion request). The audit log keeps the name and reason only."""
    p = db.get(m.Person, person_id)
    if p is None:
        raise HTTPException(404, "No such person")
    org_id = p.organisation_id
    store_for(db, user).audit("person.delete", "organisation", org_id, {"person": p.name}, {"note": reason.strip()})
    db.execute(m.NameAlias.__table__.delete().where(m.NameAlias.entity_id == p.id))
    db.delete(p)
    db.flush()
    db.execute(m.Entity.__table__.delete().where(m.Entity.id == person_id))
    flash(request, "Person deleted.")
    return RedirectResponse(f"/org/{org_id}#people" if org_id else "/people", status_code=303)
