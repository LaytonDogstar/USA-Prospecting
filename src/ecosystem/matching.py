"""Possible-match sweep (brief §6) and name-level dedup suggestions.

Nothing here links or merges anything. It only raises review items; a person decides.
"""
from collections import defaultdict

from sqlalchemy import select

from . import models as m
from .normalise import compact, contains_name, domain_stem, is_generic, name_similarity
from .store import Store

LIVE_LIKE = ("live", "paused", "contracting", "in_talks", "former")


def _keys(store: Store, org_id: int) -> dict:
    names = {compact(n) for n in store.org_names(org_id) if compact(n)}
    brands = {b for b in store.s.scalars(select(m.Brand.name_norm).where(m.Brand.organisation_id == org_id))}
    names |= {b.replace(" ", "") for b in brands}
    domains = set(store.org_domains(org_id))
    org = store.s.get(m.Organisation, org_id)
    groups = {org.parent_id} - {None}
    tribes = set(store.s.scalars(select(m.OrgRole.tribe_id).where(m.OrgRole.organisation_id == org_id,
                                                                  m.OrgRole.tribe_id.is_not(None))))
    return {"names": names, "domains": domains, "stems": {domain_stem(d) for d in domains}, "groups": groups, "tribes": tribes}


def possible_match_sweep(store: Store) -> dict:
    """Compare every organisation against every organisation that has an our_relationship row."""
    ours = list(store.s.scalars(select(m.OurRelationship).where(m.OurRelationship.status.in_(LIVE_LIKE))))
    rel_orgs = {store.org(o.organisation_id).id: o for o in ours}
    others = list(store.s.scalars(select(m.Organisation.id).where(m.Organisation.lifecycle != "merged")))
    keys = {oid: _keys(store, oid) for oid in others}

    # index other orgs by each key so the sweep is not O(n^2)
    by_name, by_domain, by_group = defaultdict(set), defaultdict(set), defaultdict(set)
    for oid, k in keys.items():
        for n in k["names"] | k["stems"]:
            by_name[n].add(oid)
        for d in k["domains"]:
            by_domain[d].add(oid)
        for g in k["groups"] | k["tribes"]:
            by_group[g].add(oid)
    long_names = [(n, oid) for oid, k in keys.items() for n in k["names"] if len(n) >= 7]

    raised = 0
    for rid, ours_row in rel_orgs.items():
        rk = keys.get(rid) or _keys(store, rid)
        hits: dict[int, list[str]] = defaultdict(list)
        for d in rk["domains"]:
            for o in by_domain[d]:
                hits[o].append(f"same domain {d}")
        for n in rk["names"] | rk["stems"]:
            if is_generic(n):
                continue
            for o in by_name[n]:
                hits[o].append(f"same name/brand '{n}'")
        for g in rk["groups"] | rk["tribes"]:
            for o in by_group[g]:
                hits[o].append(f"same operator group or tribe ({store.org(g).display_name})")
        for n in rk["names"]:
            if len(n) < 7 or is_generic(n):
                continue
            for other_name, o in long_names:
                if o not in hits and not is_generic(other_name) and contains_name(n, other_name):
                    hits[o].append(f"similar name '{n}' / '{other_name}'")
        # the relationship org's own parent/children are the same group, not a lookalike
        rel_org = store.s.get(m.Organisation, rid)
        for o, reasons in hits.items():
            other = store.s.get(m.Organisation, o)
            if o == rid or other.lifecycle == "merged" or o == rel_org.parent_id or o in rk["tribes"]:
                continue  # itself, or the group/tribe it already sits under
            strength = "strong" if any(r.startswith(("same domain", "same name")) for r in reasons) else "weak"
            item = store.review(
                "possible_match", f"possible_match:{min(o, rid)}:{max(o, rid)}",
                f"{other.display_name} may be existing {ours_row.role} {rel_org.display_name}",
                [o, rid],
                {"reasons": sorted(set(reasons)), "strength": strength, "relationship_org_id": rid,
                 "candidate_org_id": o, "relationship_status": ours_row.status})
            if item.payload.get("reasons") != sorted(set(reasons)) and item.status == "open":
                item.payload = {**item.payload, "reasons": sorted(set(item.payload.get("reasons", [])) | set(reasons))}
            raised += 1
    return {"relationship_orgs": len(rel_orgs), "possible_matches": raised}


def name_dedup_suggestions(store: Store, threshold: float = 0.9) -> int:
    """Pairs of organisations whose names are near-identical but were not linked on import."""
    rows = list(store.s.execute(select(m.Organisation.id, m.Organisation.display_name, m.Organisation.kind)
                                .where(m.Organisation.lifecycle != "merged")))
    blocks = defaultdict(list)
    for oid, name, kind in rows:
        c = compact(name)
        if len(c) >= 5:
            blocks[c[:4]].append((oid, name, kind))
    n = 0
    for items in blocks.values():
        for i, (a, an, ak) in enumerate(items):
            for b, bn, bk in items[i + 1:]:
                if (ak in ("tribe", "operator_group")) != (bk in ("tribe", "operator_group")):
                    continue
                sim = name_similarity(an, bn)
                if sim >= threshold:
                    store.review("dedup", f"dedup:{min(a, b)}:{max(a, b)}", f"Same organisation? {an} / {bn}",
                                 [a, b], {"reasons": [f"name similarity {sim:.2f}"]})
                    n += 1
    return n
