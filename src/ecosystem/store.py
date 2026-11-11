"""Write helpers shared by importers, matching and (later) jobs and the UI.

All writes go through `Store` so that every fact gets a claim and a source, projections carry claim ids,
and review items are created idempotently.
"""
from datetime import date

from sqlalchemy import select, update, delete, func
from sqlalchemy.orm import Session

from . import models as m
from .normalise import compact, name_similarity, norm_domain, norm_name, domain_stem, raw_key


class Store:
    def __init__(self, session: Session, actor: str = "system"):
        self.s = session
        self.actor = actor
        self._url_sources: dict[str, m.Source] = {}

    # ------------------------------------------------------------ entities
    def _entity(self, entity_type: str) -> int:
        e = m.Entity(entity_type=entity_type)
        self.s.add(e)
        self.s.flush()
        return e.id

    def new_org(self, display_name: str, *, legal_name: str | None = None, kind: str = "company",
                lifecycle: str = "active", notes: str | None = None) -> m.Organisation:
        assert kind in m.ORG_KINDS, kind
        org = m.Organisation(id=self._entity("organisation"), display_name=display_name, legal_name=legal_name,
                             kind=kind, lifecycle=lifecycle, notes=notes)
        self.s.add(org)
        self.s.flush()
        self.add_alias(org.id, display_name)
        if legal_name and legal_name != display_name:
            self.add_alias(org.id, legal_name)
        return org

    def org(self, org_id: int) -> m.Organisation:
        org = self.s.get(m.Organisation, org_id)
        while org is not None and org.lifecycle == "merged" and org.merged_into_id:
            org = self.s.get(m.Organisation, org.merged_into_id)
        return org

    def add_alias(self, entity_id: int, alias: str | None, claim_id: int | None = None) -> None:
        if not alias or not norm_name(alias):
            return
        n = norm_name(alias)
        exists = self.s.scalar(select(m.NameAlias.id).where(m.NameAlias.entity_id == entity_id, m.NameAlias.alias_norm == n))
        if exists:
            return
        self.s.add(m.NameAlias(entity_id=entity_id, alias=alias.strip(), alias_norm=n, alias_compact=n.replace(" ", ""),
                               source_claim_id=claim_id))
        self.s.flush()

    def add_brand(self, org_id: int, name: str, claim_id: int | None = None) -> m.Brand:
        n = norm_name(name) or name.lower()
        b = self.s.scalar(select(m.Brand).where(m.Brand.organisation_id == org_id, m.Brand.name_norm == n))
        if b is None:
            b = m.Brand(id=self._entity("brand"), organisation_id=org_id, name=name, name_norm=n, claim_ids=[])
            self.s.add(b)
            self.s.flush()
        if claim_id and claim_id not in b.claim_ids:
            b.claim_ids = b.claim_ids + [claim_id]
        self.add_alias(org_id, name, claim_id)
        return b

    def domain_owner(self, hostname: str) -> int | None:
        org_id = self.s.scalar(select(m.Domain.organisation_id).where(m.Domain.hostname == hostname))
        return self.org(org_id).id if org_id else None

    def add_domain(self, org_id: int, hostname: str, *, brand_id: int | None = None, claim_id: int | None = None,
                   primary: bool = False) -> m.Domain | None:
        """Attach a domain. Returns None (and leaves it alone) when another organisation already holds it."""
        d = self.s.scalar(select(m.Domain).where(m.Domain.hostname == hostname))
        if d is not None:
            if self.org(d.organisation_id).id != org_id:
                return None
        else:
            d = m.Domain(id=self._entity("domain"), organisation_id=org_id, hostname=hostname, claim_ids=[])
            self.s.add(d)
            self.s.flush()
        if brand_id and not d.brand_id:
            d.brand_id = brand_id
        if primary:
            d.is_primary = True
        if claim_id and claim_id not in d.claim_ids:
            d.claim_ids = d.claim_ids + [claim_id]
        return d

    def org_names(self, org_id: int) -> list[str]:
        return list(self.s.scalars(select(m.NameAlias.alias).where(m.NameAlias.entity_id == org_id)))

    def org_domains(self, org_id: int) -> list[str]:
        doms = list(self.s.scalars(select(m.Domain.hostname).where(m.Domain.organisation_id == org_id)))
        declared = self.s.scalars(select(m.Claim.value).where(
            m.Claim.entity_id == org_id, m.Claim.attribute == "declared_domain", m.Claim.status == "accepted"))
        return doms + [d for d in declared if d not in doms]

    # ------------------------------------------------------------ resolution
    def resolve(self, name: str, domains: list[str] = (), *, extra_names: list[str] = (),
                kinds: tuple[str, ...] | None = None, strict: bool = False, **create_kwargs) -> tuple[m.Organisation, str]:
        """Find or create an organisation: domain first, then normalised name.

        Returns (org, how) with how in created | domain | name | created_ambiguous. Ambiguous cases create a
        new organisation and a 'dedup' review item instead of merging. strict=True only links on a name that is
        identical apart from case and punctuation (used for CFPB names, where suffixes like Inc vs Company matter).
        """
        names = [n for n in [name, *extra_names] if n and compact(n)]
        reasons = []
        # 1. domain
        owners = {o for o in (self.domain_owner(d) for d in domains) if o}
        if len(owners) == 1:
            owner = next(iter(owners))
            best = max([name_similarity(n, a) for n in names for a in self.org_names(owner)]
                       + [name_similarity(n, domain_stem(d)) for n in names for d in domains] + [0])
            if best >= 0.6:
                return self._upgrade_name(self.org(owner), name), "domain"
            reasons.append(f"shares domain {', '.join(domains)} with a differently named organisation")
        elif len(owners) > 1:
            reasons.append(f"domains {', '.join(domains)} belong to {len(owners)} different organisations")
        # 2. normalised name
        if not reasons:
            cands = set()
            for n in names:
                q = select(m.NameAlias.entity_id).join(m.Organisation, m.Organisation.id == m.NameAlias.entity_id).where(
                    m.NameAlias.alias_compact == compact(n), m.Organisation.lifecycle != "merged")
                if kinds:
                    q = q.where(m.Organisation.kind.in_(kinds))
                cands |= set(self.s.scalars(q))
            if strict and cands:
                wanted = {raw_key(n) for n in names}
                exact = {c for c in cands if wanted & {raw_key(a) for a in self.org_names(c)}}
                if not exact:
                    reasons.append("similar legal name (differs in suffix)")
                    owners = cands
                cands = exact if exact else set()
            if len(cands) > 1:  # prefer the one whose own display name matches
                exact = {c for c in cands if compact(self.org(c).display_name) == compact(name)}
                if len(exact) == 1:
                    cands = exact
            if not reasons and len(cands) == 1:
                cand = next(iter(cands))
                theirs = set(self.org_domains(cand))
                if domains and theirs and not (set(domains) & theirs):
                    reasons.append("same name but different domains")
                    owners = {cand}
                else:
                    return self._upgrade_name(self.org(cand), name), "name"
            elif not reasons and len(cands) > 1:
                reasons.append(f"name matches {len(cands)} organisations")
                owners = cands
        org = self.new_org(name, **create_kwargs)
        for n in extra_names:
            self.add_alias(org.id, n)
        if reasons:
            for other in owners:
                self.review("dedup", f"dedup:{min(org.id, other)}:{max(org.id, other)}",
                            f"Same organisation? {name} / {self.org(other).display_name}",
                            [org.id, other], {"reasons": reasons})
            return org, "created_ambiguous"
        return org, "created"

    def _upgrade_name(self, org: m.Organisation, name: str) -> m.Organisation:
        """A record first seen only as a domain (e.g. on a partner list) takes the real name when one arrives."""
        if name and norm_domain(org.display_name) == org.display_name.lower() and not norm_domain(name):
            self.add_alias(org.id, name)
            org.display_name = name
        return org

    # ------------------------------------------------------------ evidence
    def row_source(self, import_file: m.ImportFile, sheet: str, row_number: int) -> m.Source:
        src = m.Source(source_type="spreadsheet_import", title=f"{import_file.file_name} / {sheet} / row {row_number}",
                       retrieved_at=import_file.imported_at, import_file_id=import_file.id, sheet=sheet,
                       row_number=row_number)
        self.s.add(src)
        self.s.flush()
        return src

    def url_source(self, url: str, source_type: str, title: str | None = None) -> m.Source:
        key = url.strip()
        if key in self._url_sources:
            return self._url_sources[key]
        src = self.s.scalar(select(m.Source).where(m.Source.url == key, m.Source.import_file_id.is_(None)))
        if src is None:
            src = m.Source(url=key, source_type=source_type, title=title)
            self.s.add(src)
            self.s.flush()
        self._url_sources[key] = src
        return src

    def claim(self, entity_id: int, attribute: str, value, source: m.Source, *, confidence: str = "medium",
              method: str, extra_sources: list[m.Source] = (), status: str = "accepted") -> m.Claim | None:
        if value is None or value == "" or value == []:
            return None
        assert confidence in m.CONFIDENCE, confidence
        c = m.Claim(entity_id=entity_id, attribute=attribute, value=value, source_id=source.id, method=method,
                    confidence=confidence, status=status, decided_by=self.actor if status == "accepted" else None)
        self.s.add(c)
        self.s.flush()
        for es in extra_sources:
            if es.id != source.id:
                self.s.add(m.ClaimSource(claim_id=c.id, source_id=es.id))
        return c

    # ------------------------------------------------------------ projections
    def role(self, org_id: int, role: str, claim: m.Claim | None, **attrs) -> m.OrgRole:
        assert role in m.ROLES, role
        r = self.s.scalar(select(m.OrgRole).where(m.OrgRole.organisation_id == org_id, m.OrgRole.role == role))
        if r is None:
            r = m.OrgRole(organisation_id=org_id, role=role, claim_ids=[])
            self.s.add(r)
        for k, v in attrs.items():
            if v is not None:
                setattr(r, k, v)
        if claim and claim.id not in r.claim_ids:
            r.claim_ids = r.claim_ids + [claim.id]
        self.s.flush()
        return r

    def state(self, entity_id: int, state: str, basis: str, claim: m.Claim) -> None:
        es = self.s.scalar(select(m.EntityState).where(
            m.EntityState.entity_id == entity_id, m.EntityState.state == state, m.EntityState.basis == basis))
        if es is None:
            self.s.add(m.EntityState(entity_id=entity_id, state=state, basis=basis, claim_ids=[claim.id]))
        elif claim.id not in es.claim_ids:
            es.claim_ids = es.claim_ids + [claim.id]
        self.s.flush()

    def edge(self, from_id: int, to_id: int, type_: str, status: str, claim: m.Claim | None,
             attrs: dict | None = None, seen: date | None = None) -> m.Relationship | None:
        assert type_ in m.EDGE_TYPES and status in m.EDGE_STATUSES, (type_, status)
        if from_id == to_id:
            return None
        e = self.s.scalar(select(m.Relationship).where(
            m.Relationship.from_id == from_id, m.Relationship.to_id == to_id, m.Relationship.type == type_))
        if e is None:
            e = m.Relationship(from_id=from_id, to_id=to_id, type=type_, status=status, attrs=attrs or {},
                               claim_ids=[], first_seen=seen, last_seen=seen)
            self.s.add(e)
        else:
            rank = {"rumoured": 0, "inferred": 1, "confirmed": 2}
            if rank[status] > rank[e.status]:
                e.status = status
            if attrs:
                e.attrs = {**e.attrs, **attrs}
            if seen and (not e.last_seen or seen > e.last_seen):
                e.last_seen = seen
        if claim and claim.id not in e.claim_ids:
            e.claim_ids = e.claim_ids + [claim.id]
        self.s.flush()
        return e

    def metric(self, entity_id: int, metric: str, value, period: str | None, source: m.Source,
               claim: m.Claim | None = None) -> None:
        if value is None or value == "":
            return
        try:
            v = float(str(value).replace(",", ""))
        except ValueError:
            return
        self.s.add(m.MetricSnapshot(entity_id=entity_id, metric=metric, value=v, period=period, source_id=source.id,
                                    claim_id=claim.id if claim else None, retrieved_at=source.retrieved_at))

    def compliance_flag(self, entity_id: int, flag_type: str, summary: str, claim: m.Claim) -> None:
        exists = self.s.scalar(select(m.ComplianceFlag.id).where(
            m.ComplianceFlag.entity_id == entity_id, m.ComplianceFlag.flag_type == flag_type,
            m.ComplianceFlag.summary == summary))
        if not exists:
            self.s.add(m.ComplianceFlag(entity_id=entity_id, flag_type=flag_type, summary=summary, claim_id=claim.id))

    def review(self, kind: str, dedupe_key: str, title: str, entity_ids: list[int], payload: dict) -> m.ReviewItem:
        assert kind in m.REVIEW_KINDS, kind
        item = self.s.scalar(select(m.ReviewItem).where(m.ReviewItem.dedupe_key == dedupe_key))
        if item is None:
            item = m.ReviewItem(kind=kind, dedupe_key=dedupe_key, title=title, entity_ids=entity_ids, payload=payload)
            self.s.add(item)
            self.s.flush()
        return item

    def audit(self, action: str, table: str | None = None, row_id: int | None = None,
              before: dict | None = None, after: dict | None = None) -> None:
        self.s.add(m.AuditLog(actor=self.actor, action=action, table_name=table, row_id=row_id, before=before, after=after))

    # ------------------------------------------------------------ our relationships
    def set_our_relationship(self, org_id: int, *, role: str, status: str, source: str, notes: str | None = None,
                             needs_confirmation: bool = True, owner: str | None = None) -> m.OurRelationship:
        assert role in m.OUR_ROLES and status in m.OUR_STATUSES and source in m.OUR_SOURCES
        r = self.s.scalar(select(m.OurRelationship).where(m.OurRelationship.organisation_id == org_id))
        before = None
        if r is None:
            r = m.OurRelationship(organisation_id=org_id, role=role, status=status, source=source, notes=notes,
                                  needs_confirmation=needs_confirmation, owner=owner, updated_by=self.actor)
            self.s.add(r)
        else:
            before = _row(r)
            r.role, r.status, r.needs_confirmation, r.updated_by = role, status, needs_confirmation, self.actor
            if notes:
                r.notes = notes if not r.notes or notes in r.notes else f"{r.notes}\n{notes}"
            if owner:
                r.owner = owner
        self.s.flush()
        self.s.add(m.OurRelationshipHistory(our_relationship_id=r.id, snapshot=_row(r), changed_by=self.actor))
        self.audit("our_relationship.set", "our_relationship", r.id, before, _row(r))
        return r

    # ------------------------------------------------------------ merge
    def merge(self, keep_id: int, drop_id: int, note: str | None = None) -> None:
        """Merge organisation drop_id into keep_id. Everything moves; drop_id is kept as lifecycle 'merged'."""
        keep, drop = self.org(keep_id), self.org(drop_id)
        if keep.id == drop.id:
            return
        k, d = keep.id, drop.id
        s = self.s
        # unique-constrained children: remove the duplicates first
        for alias in list(s.scalars(select(m.NameAlias).where(m.NameAlias.entity_id == d))):
            if s.scalar(select(m.NameAlias.id).where(m.NameAlias.entity_id == k, m.NameAlias.alias_norm == alias.alias_norm)):
                s.delete(alias)
        for b in list(s.scalars(select(m.Brand).where(m.Brand.organisation_id == d))):
            twin = s.scalar(select(m.Brand).where(m.Brand.organisation_id == k, m.Brand.name_norm == b.name_norm))
            if twin:
                twin.claim_ids = sorted(set(twin.claim_ids) | set(b.claim_ids))
                s.execute(update(m.Domain).where(m.Domain.brand_id == b.id).values(brand_id=twin.id))
                s.delete(b)
        for r in list(s.scalars(select(m.OrgRole).where(m.OrgRole.organisation_id == d))):
            twin = s.scalar(select(m.OrgRole).where(m.OrgRole.organisation_id == k, m.OrgRole.role == r.role))
            if twin:
                twin.claim_ids = sorted(set(twin.claim_ids) | set(r.claim_ids))
                for col in ("tribe_id", "bank_partner_id", "products", "loan_min", "loan_max", "segment", "online_or_branch"):
                    if getattr(twin, col) is None:
                        setattr(twin, col, getattr(r, col))
                s.delete(r)
        for es in list(s.scalars(select(m.EntityState).where(m.EntityState.entity_id == d))):
            if s.scalar(select(m.EntityState.id).where(m.EntityState.entity_id == k, m.EntityState.state == es.state,
                                                      m.EntityState.basis == es.basis)):
                s.delete(es)
        for e in list(s.scalars(select(m.Relationship).where((m.Relationship.from_id == d) | (m.Relationship.to_id == d)))):
            f, t = (k if e.from_id == d else e.from_id), (k if e.to_id == d else e.to_id)
            twin = s.scalar(select(m.Relationship).where(m.Relationship.from_id == f, m.Relationship.to_id == t,
                                                         m.Relationship.type == e.type, m.Relationship.id != e.id))
            if f == t or twin:
                if twin:
                    twin.claim_ids = sorted(set(twin.claim_ids) | set(e.claim_ids))
                s.delete(e)
        ours_d = s.scalar(select(m.OurRelationship).where(m.OurRelationship.organisation_id == d))
        ours_k = s.scalar(select(m.OurRelationship).where(m.OurRelationship.organisation_id == k))
        if ours_d and ours_k:
            ours_k.notes = "\n".join(x for x in (ours_k.notes, f"Merged from {drop.display_name}: {ours_d.role}/{ours_d.status}. {ours_d.notes or ''}") if x)
            s.delete(ours_d)
        cs = s.get(m.ComplianceStatus, d)
        if cs and s.get(m.ComplianceStatus, k):
            s.delete(cs)
        s.flush()
        # move the rest
        s.execute(update(m.NameAlias).where(m.NameAlias.entity_id == d).values(entity_id=k))
        s.execute(update(m.Brand).where(m.Brand.organisation_id == d).values(organisation_id=k))
        s.execute(update(m.Domain).where(m.Domain.organisation_id == d).values(organisation_id=k))
        s.execute(update(m.OrgRole).where(m.OrgRole.organisation_id == d).values(organisation_id=k))
        s.execute(update(m.EntityState).where(m.EntityState.entity_id == d).values(entity_id=k))
        s.execute(update(m.Relationship).where(m.Relationship.from_id == d).values(from_id=k))
        s.execute(update(m.Relationship).where(m.Relationship.to_id == d).values(to_id=k))
        s.execute(update(m.OurRelationship).where(m.OurRelationship.organisation_id == d).values(organisation_id=k))
        s.execute(update(m.Claim).where(m.Claim.entity_id == d).values(entity_id=k))
        s.execute(update(m.MetricSnapshot).where(m.MetricSnapshot.entity_id == d).values(entity_id=k))
        s.execute(update(m.ComplianceFlag).where(m.ComplianceFlag.entity_id == d).values(entity_id=k))
        s.execute(update(m.ComplianceStatus).where(m.ComplianceStatus.entity_id == d).values(entity_id=k))
        s.execute(update(m.Score).where(m.Score.entity_id == d).values(entity_id=k))
        s.execute(update(m.Person).where(m.Person.organisation_id == d).values(organisation_id=k))
        s.execute(update(m.OrgRole).where(m.OrgRole.tribe_id == d).values(tribe_id=k))
        s.execute(update(m.Organisation).where(m.Organisation.parent_id == d, m.Organisation.id != k).values(parent_id=k))
        if keep.parent_id == d:
            keep.parent_id = drop.parent_id if drop.parent_id != k else None
        if keep.parent_id is None and drop.parent_id not in (None, k):
            keep.parent_id = drop.parent_id
        if not keep.legal_name and drop.legal_name:
            keep.legal_name = drop.legal_name
        if keep.lifecycle in ("candidate", "screened_out") and drop.lifecycle == "active":
            keep.lifecycle = "active"
        rank = {"A": 0, "B": 1, "C": 2, None: 3}
        if rank[drop.tier] < rank[keep.tier]:
            keep.tier = drop.tier
        drop.lifecycle, drop.merged_into_id, drop.parent_id = "merged", k, None
        s.flush()
        self.audit("organisation.merge", "organisation", k, {"merged_id": d, "merged_name": drop.display_name},
                   {"note": note})


def _row(obj) -> dict:
    out = {}
    for c in obj.__table__.columns:
        v = getattr(obj, c.name)
        out[c.name] = v.isoformat() if hasattr(v, "isoformat") else v
    return out


def count(session: Session, model, *where) -> int:
    return session.scalar(select(func.count()).select_from(model).where(*where)) or 0
