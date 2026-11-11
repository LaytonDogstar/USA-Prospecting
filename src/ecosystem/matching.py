"""Possible-match sweep (brief §6) and name-level dedup suggestions.

Nothing here links or merges anything. It only raises review items; a person decides.
"""
from collections import defaultdict

from sqlalchemy import select

from . import models as m
from .normalise import compact, contains_name, domain_stem, is_generic, name_similarity
from .store import Store

LIVE_LIKE = ("live", "paused", "contracting", "in_talks", "former")


def _all_keys(store: Store, ids: set[int] | None = None) -> dict[int, dict]:
    """Matching keys for many organisations in a handful of queries (one query per organisation was ~3,000
    round trips, which made accepting a review card slow on a remote database)."""
    s = store.s
    q_orgs = select(m.Organisation.id, m.Organisation.parent_id).where(m.Organisation.lifecycle != "merged")
    if ids is not None:
        q_orgs = q_orgs.where(m.Organisation.id.in_(ids))
    keys = {oid: {"names": set(), "domains": set(), "stems": set(), "groups": {pid} - {None}, "tribes": set()}
            for oid, pid in s.execute(q_orgs)}
    want = set(keys)
    def rows(q):
        return s.execute(q).all() if ids is None else s.execute(q.where(q.selected_columns[0].in_(want))).all()
    for eid, comp in rows(select(m.NameAlias.entity_id, m.NameAlias.alias_compact)):
        if eid in keys and comp:
            keys[eid]["names"].add(comp)
    for oid, norm in rows(select(m.Brand.organisation_id, m.Brand.name_norm)):
        if oid in keys:
            keys[oid]["names"].add(norm.replace(" ", ""))
    for oid, host in rows(select(m.Domain.organisation_id, m.Domain.hostname)):
        if oid in keys:
            keys[oid]["domains"].add(host)
    for oid, val in rows(select(m.Claim.entity_id, m.Claim.value).where(m.Claim.attribute == "declared_domain",
                                                                         m.Claim.status == "accepted")):
        if oid in keys and isinstance(val, str):
            keys[oid]["domains"].add(val)
    for oid, tid in rows(select(m.OrgRole.organisation_id, m.OrgRole.tribe_id).where(m.OrgRole.tribe_id.is_not(None))):
        if oid in keys:
            keys[oid]["tribes"].add(tid)
    for k in keys.values():
        k["stems"] = {domain_stem(d) for d in k["domains"]}
    return keys


def _keys(store: Store, org_id: int) -> dict:
    return _all_keys(store, {org_id}).get(org_id) or {"names": set(), "domains": set(), "stems": set(),
                                                      "groups": set(), "tribes": set()}


def possible_match_sweep(store: Store, only: set[int] | None = None) -> dict:
    """Compare organisations against every organisation that has an our_relationship row.

    only: restrict the candidates to these organisations (e.g. the one just researched). If one of them is itself
    a relationship organisation, everything is compared against it, so the full sweep runs.
    """
    ours = list(store.s.scalars(select(m.OurRelationship).where(m.OurRelationship.status.in_(LIVE_LIKE))))
    rel_orgs = {store.org(o.organisation_id).id: o for o in ours}
    if only is not None and only & set(rel_orgs):
        only = None
    keys = _all_keys(store, None if only is None else set(only) | set(rel_orgs))
    others = list(keys)

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


def shared_key_suggestions(store: Store) -> int:
    """Organisations that share a non-generic name, brand or website but were kept apart on import
    (e.g. a CFPB record 'X Financial dba Brand' and a researched record that lists Brand and brand.com)."""
    s = store.s
    live = {r[0]: r[1] for r in s.execute(select(m.Organisation.id, m.Organisation.kind)
                                          .where(m.Organisation.lifecycle != "merged"))}
    by_key: dict[str, set[int]] = defaultdict(set)
    for eid, alias, comp in s.execute(select(m.NameAlias.entity_id, m.NameAlias.alias, m.NameAlias.alias_compact)):
        if eid in live and live[eid] == "company" and len(comp) >= 6 and not is_generic(alias):
            by_key[f"name:{comp}"].add(eid)
    for oid, host in s.execute(select(m.Domain.organisation_id, m.Domain.hostname)):
        if oid in live:
            by_key[f"domain:{host}"].add(oid)
    for oid, val in s.execute(select(m.Claim.entity_id, m.Claim.value).where(m.Claim.attribute == "declared_domain")):
        if oid in live and isinstance(val, str):
            by_key[f"domain:{val}"].add(oid)
    n = 0
    for key, ids in by_key.items():
        if not 2 <= len(ids) <= 4:  # bigger clusters are generic words or shared list sites, not one company
            continue
        ids = sorted(ids)
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                if s.scalar(select(m.Organisation.parent_id).where(m.Organisation.id == a)) == b or \
                        s.scalar(select(m.Organisation.parent_id).where(m.Organisation.id == b)) == a:
                    continue  # already linked as owner and brand
                kind, val = key.split(":", 1)
                item = store.review("dedup", f"dedup:{a}:{b}",
                                    f"Same organisation? {store.org(a).display_name} / {store.org(b).display_name}",
                                    [a, b], {"reasons": [f"share the {'website' if kind == 'domain' else 'name'} {val}"]})
                reasons = set(item.payload.get("reasons", []))
                new = f"share the {'website' if kind == 'domain' else 'name'} {val}"
                if new not in reasons and item.status == "open":
                    item.payload = {**item.payload, "reasons": sorted(reasons | {new})}
                n += 1
    return n

