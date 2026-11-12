"""Review decisions, and suggested decisions.

`decide()` is the one place a review item is acted on (single decisions and bulk ones use it alike).
`suggest()` proposes an action for an item from simple, explainable rules. A suggestion marked `safe` is one a
person can apply in bulk with confidence (e.g. two records sharing a website and a name); anything that needs
judgement is suggested but never part of a bulk apply. Applying is always a person's click: nothing here runs
by itself.
"""
from datetime import datetime, timezone

from sqlalchemy import select, text

from . import models as m
from .normalise import contains_name, name_similarity, norm_name
from .store import Store

NON_LENDER_CATEGORIES = {"fintech_app", "bank_or_credit_union", "not_a_lender"}
LENDER_CATEGORIES = {"tribal", "state_licensed", "bank_partner", "cab_cso", "servicer"}


class DecisionError(ValueError):
    pass


def summaries(s, ids: set[int]) -> dict:
    if not ids:
        return {}
    rows = s.execute(text("""
        SELECT o.id, o.display_name, o.lifecycle, o.kind, o.merged_into_id, p.display_name AS parent,
               (SELECT string_agg(hostname, ', ' ORDER BY hostname) FROM domain d WHERE d.organisation_id = o.id) AS domains,
               (SELECT string_agg(DISTINCT role, ', ') FROM org_role r WHERE r.organisation_id = o.id) AS roles,
               ours.status AS our_status, ours.role AS our_role,
               (SELECT string_agg(DISTINCT c.value #>> '{}', ', ') FROM claim c WHERE c.entity_id = o.id
                  AND c.attribute = 'cfpb_company_name') AS cfpb_name,
               (SELECT count(*) FROM claim c WHERE c.entity_id = o.id) AS facts
        FROM organisation o LEFT JOIN organisation p ON p.id = o.parent_id
        LEFT JOIN our_relationship ours ON ours.organisation_id = o.id
        WHERE o.id = ANY(:ids)"""), {"ids": list(ids)})
    return {r.id: r for r in rows}


def _same_name(a: str, b: str) -> bool:
    """Identical apart from case, punctuation, legal suffixes or a trailing plural s."""
    na, nb = norm_name(a).rstrip("s"), norm_name(b).rstrip("s")
    return bool(na) and na == nb


def _domains(row) -> set[str]:
    return {d.strip().removeprefix("www.") for d in (row.domains or "").split(",") if d.strip()}


def suggest(store: Store, it: m.ReviewItem, summ: dict) -> dict | None:
    """{'action', 'label', 'why', 'safe'} or None when the item needs a person's judgement."""
    ids = list(it.entity_ids)
    reasons = " ".join(it.payload.get("reasons", []))
    if it.kind in ("possible_match", "dedup") and len(ids) == 2:
        a, b = (summ.get(i) for i in ids)
        if a is None or b is None:
            return None
        if store.org(a.id).id == store.org(b.id).id:
            return {"action": "approve", "label": "Close", "why": "both records were already merged", "safe": True}
        richer, poorer = (a, b) if a.facts >= b.facts else (b, a)
        keep = {"action": f"merge:{richer.id}", "label": f"Same: keep {richer.display_name}"}
        same_name = _same_name(a.display_name, b.display_name)
        da, db_ = _domains(a), _domains(b)
        if a.our_status and b.our_status:
            return {**keep, "why": "both carry a relationship; check which one is right before merging", "safe": False}
        if (da and db_ and not da & db_) or "different domains" in reasons:
            return {**keep, "why": "same name but different websites: could be two companies, or a rebrand", "safe": False}
        if it.kind == "possible_match":
            if same_name and ("same domain" in reasons or "same name" in reasons):
                return {**keep, "why": "same name" + (" and website" if "same domain" in reasons else
                                                       "; the smaller record is a stub from a list"), "safe": True}
            if "same domain" in reasons or "same name" in reasons:
                return {**keep, "why": "they share a name or website but the names differ: one may be a brand of "
                                       "the other (then use 'owns')", "safe": False}
            if "Matched buyer" in reasons:
                cfpb, buyer = (a, b) if a.cfpb_name else (b, a)
                return {"action": f"link:{cfpb.id}:{buyer.id}", "label": f"{cfpb.display_name} owns {buyer.display_name}",
                        "why": "the spreadsheet matched them by hand; the CFPB company is usually the legal owner of the "
                               "trading brand. Check before applying", "safe": False}
            if "same operator group or tribe" in reasons:
                return {"action": "reject", "label": "Not the same company",
                        "why": "they share an owner, which the group already shows", "safe": False}
            return None
        # dedup
        if "share the website" in reasons and name_similarity(a.display_name, b.display_name) >= 0.6:
            return {**keep, "why": "same website and similar name", "safe": True}
        if same_name and "share the name" in reasons:
            return {**keep, "why": "same name, and both records carry it", "safe": True}
        if same_name:
            return {**keep, "why": "names identical apart from suffix or plural"
                                   + ("; the smaller record is a stub" if poorer.facts <= 3 else
                                      "; both have their own facts, so check they aren't two companies in one group"),
                    "safe": poorer.facts <= 3}
        if a.kind in ("tribe", "operator_group") and b.kind in ("tribe", "operator_group") \
                and contains_name(a.display_name, b.display_name):
            longer = a if len(a.display_name) >= len(b.display_name) else b
            return {"action": f"merge:{longer.id}", "label": f"Same: keep {longer.display_name}",
                    "why": "one owner name contains the other", "safe": True}
        return None
    if it.kind == "research":
        cat = it.payload.get("lender_category")
        facts = sum(it.payload.get("counts", {}).values())
        if facts == 0:  # nothing verified (often a failed search): never screen out on that
            return {"action": "reject", "label": "Reject: no evidence found",
                    "why": "the research found no cited facts, so it says nothing either way; rejecting it lets the "
                           "company be researched again", "safe": True}
        if cat in NON_LENDER_CATEGORIES or (it.payload.get("is_us_consumer_lender") is False and cat != "unclear"):
            return {"action": "screen", "label": "Accept and screen out",
                    "why": f"research says it isn't a US consumer lender ({(cat or 'not a lender').replace('_', ' ')}), "
                           f"with {facts} cited fact{'s' if facts != 1 else ''}",
                    "safe": facts >= 2 and not it.payload.get("rejected_uncited")}
        if cat in LENDER_CATEGORIES and facts >= 2 and not it.payload.get("rejected_uncited"):
            return {"action": "approve", "label": "Accept these facts",
                    "why": f"{cat.replace('_', ' ')} lender, {facts} cited facts, none dropped", "safe": True}
        return None
    return None


def decide(store: Store, it: m.ReviewItem, action: str, note: str = "") -> str:
    """Apply one decision to one open review item. Returns a short description of what happened."""
    s = store.s
    if it.status != "open":
        raise DecisionError("This item is already decided")
    ids = list(it.entity_ids)
    if action.startswith("merge:"):
        keep = int(action.split(":")[1])
        if keep not in ids:
            raise DecisionError("Merge target must be one of the item's organisations")
        for other in ids:
            if other != keep:
                store.merge(keep, other, note=f"review #{it.id}: {note}")
        it.status, done = "approved", f"Merged into {store.org(keep).display_name}."
    elif action.startswith("link:"):
        parent, child = (int(x) for x in action.split(":")[1:3])
        if {parent, child} - set(ids):
            raise DecisionError("Link must be between the item's organisations")
        src = m.Source(source_type="manual", title=f"Review #{it.id} decided by {store.actor}",
                       retrieved_at=datetime.now(timezone.utc))
        s.add(src)
        s.flush()
        pname = store.org(parent).display_name
        c = store.claim(child, "owner_group", {"group": pname, "relation": "owns", "note": note or None}, src,
                        confidence="high", method=f"review:{store.actor}")
        store.edge(store.org(parent).id, store.org(child).id, "owns", "confirmed", c)
        child_org = store.org(child)
        if child_org.parent_id is None and child_org.id != store.org(parent).id:
            child_org.parent_id = store.org(parent).id
        store.audit("organisation.link", "organisation", child_org.id, None, {"parent": pname, "note": note})
        it.status, done = "approved", f"Linked: {pname} owns {child_org.display_name}."
    elif action == "approve" and it.kind == "research":
        from .matching import possible_match_sweep, shared_key_suggestions
        from .research import apply_research
        from .scoring import rescore
        n = apply_research(store, it)
        only = set(ids)
        shared_key_suggestions(store)
        possible_match_sweep(store, only=only)
        rescore(store, org_ids=only, audit=False)
        it.status, done = "approved", f"Accepted {n} researched facts."
    elif action == "screen" and it.kind == "research":
        from .research import apply_research, screen_out
        n = apply_research(store, it)
        org = store.org(ids[0])
        screen_out(store, org, note.strip() or (it.payload.get("summary") or "Not a US consumer lender (research)")[:300])
        it.status, done = "approved", f"Accepted {n} facts and screened out {org.display_name}."
    elif action == "reject" and it.kind == "research":
        from .research import reject_research
        n = reject_research(store, it)
        it.status, done = "rejected", f"Rejected {n} researched facts."
    elif action == "approve":
        it.status, done = "approved", "Approved."
    elif action == "reject":
        it.status, done = "rejected", "Rejected."
    else:
        raise DecisionError("Unknown action")
    it.decided_by, it.decided_at, it.note = store.actor, datetime.now(timezone.utc), note.strip() or None
    store.audit(f"review.{it.status}", "review_item", it.id, None, {"action": action, "note": note, "title": it.title})
    s.flush()
    return done


def undo_screen(store: Store, it: m.ReviewItem, note: str) -> str:
    """Put back an organisation that a research decision screened out, and reopen it for research.
    The original decision stays in the audit log; this one is logged next to it."""
    if it.kind != "research" or it.status == "open":
        raise DecisionError("Only a decided research item can be undone")
    org = store.org(it.entity_ids[0])
    if org.lifecycle != "screened_out":
        raise DecisionError("That organisation isn't screened out")
    before = {"lifecycle": org.lifecycle, "status": it.status, "decided_by": it.decided_by, "note": it.note}
    has_roles = store.s.scalar(select(m.OrgRole.id).where(m.OrgRole.organisation_id == org.id).limit(1))
    org.lifecycle = "active" if has_roles else "candidate"
    for c in store.s.scalars(select(m.Claim).where(m.Claim.entity_id == org.id, m.Claim.attribute == "screen_out_reason",
                                                 m.Claim.status == "accepted")):
        c.status = "rejected"
    it.status, it.decided_by, it.decided_at = "rejected", store.actor, datetime.now(timezone.utc)
    it.note = f"screen-out undone: {note}".strip()[:500]
    store.audit("organisation.unscreen", "organisation", org.id, before, {"note": note, "review_item": it.id})
    store.s.flush()
    return f"Restored {org.display_name}; it will be researched again."


def apply_safe(store: Store, kind: str, limit: int = 500, note_prefix: str = "bulk") -> dict:
    """Apply every safe suggestion on one tab of the inbox (a person's click, or the daily assistant once an admin
    has switched it on). Each decision is recorded per item with its reason."""
    s = store.s
    items = list(s.scalars(select(m.ReviewItem).where(m.ReviewItem.kind == kind, m.ReviewItem.status == "open")
                           .order_by(m.ReviewItem.id).limit(limit)))
    done, skipped, errors = 0, 0, []
    for it in items:
        summ = summaries(s, {store.org(i).id for i in it.entity_ids} | set(it.entity_ids))
        sug = suggest(store, it, summ)
        if not sug or not sug["safe"]:
            skipped += 1
            continue
        try:
            decide(store, it, sug["action"], f"{note_prefix}: {sug['why']}")
            done += 1
        except (DecisionError, ValueError) as e:
            errors.append(f"#{it.id}: {e}")
    if kind != "research" and done:
        from .scoring import rescore
        rescore(store, audit=False)  # merges move facts between records: refresh the scores once at the end
    if done:
        store.audit("review.bulk", "review_item", None, None, {"kind": kind, "applied": done, "skipped": skipped})
    return {"applied": done, "left_for_you": skipped, "errors": errors}
