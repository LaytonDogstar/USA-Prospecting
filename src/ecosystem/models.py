"""Database schema. Postgres only (JSONB, arrays).

Every factual attribute is a `Claim` with a `Source`. The structural tables (brand, domain, org_role,
entity_state, relationship) are projections of claims and store the claim ids behind each row, so any
field can be traced to its source. `OurRelationship` is separate and authoritative.
"""
import datetime as dt
from datetime import date, datetime, timezone

from sqlalchemy import (
    BigInteger, Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String, Text,
    UniqueConstraint, Index,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


TS = DateTime(timezone=True)

# --- vocabularies (checked in code; kept as text so adding a value needs no migration) ---
ORG_KINDS = ("operator_group", "tribe", "company", "bank", "agency", "platform")
LIFECYCLES = ("candidate", "active", "screened_out", "merged")
ROLES = (
    "lender_tribal", "lender_state_licensed", "lender_bank_partner", "lender_cab_cso", "servicer",
    "lead_network_pingtree", "connector_brand", "comparison_offer_wall", "content_affiliate",
    "sponsored_publisher_agency", "affiliate_cpa_network", "paid_media_platform", "fintech_decline_source",
)
EDGE_TYPES = (
    "owns", "operates", "same_owner_as", "sells_leads_to", "buys_leads_from", "lists_offer_of",
    "hosts_affiliate_program_for", "bank_partner_of", "services_loans_for", "places_sponsored_content_on",
)
EDGE_STATUSES = ("confirmed", "inferred", "rumoured")
SOURCE_TYPES = (
    "sec_filing", "regulator", "site_read", "ahrefs", "similarweb", "cfpb", "court", "news", "job_post",
    "spreadsheet_import", "manual",
)
CONFIDENCE = ("high", "medium", "low")
CLAIM_STATUSES = ("proposed", "accepted", "rejected", "superseded")
OUR_ROLES = ("buyer", "supplier", "both", "network")
OUR_STATUSES = ("live", "paused", "contracting", "in_talks", "former", "prospect", "parked", "dropped")
OUR_SOURCES = ("internal_db", "csv", "manual", "seed")
COMPLIANCE_FLAG_TYPES = (
    "cfpb_action", "ftc_action", "state_ag", "state_licence_alert", "rent_a_tribe_litigation", "class_action", "other",
)
COMPLIANCE_VALUES = ("clear", "review", "blocked")
REVIEW_KINDS = (
    "possible_match", "dedup", "new_entrant", "material_change", "low_confidence", "parse_failure",
    "relationship_confirm", "manual_check",
)
USER_ROLES = ("admin", "viewer")


# ---------------------------------------------------------------- entities
class Entity(Base):
    """Shared id space for everything that can carry claims, edges or metrics."""
    __tablename__ = "entity"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(20))  # organisation | brand | domain | person | page
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class Organisation(Base):
    __tablename__ = "organisation"
    id: Mapped[int] = mapped_column(ForeignKey("entity.id"), primary_key=True)
    legal_name: Mapped[str | None] = mapped_column(Text)
    display_name: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(20), default="company")
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("organisation.id"), index=True)
    hq_country: Mapped[str | None] = mapped_column(String(2))
    hq_state: Mapped[str | None] = mapped_column(String(2))
    notes: Mapped[str | None] = mapped_column(Text)
    lifecycle: Mapped[str] = mapped_column(String(20), default="active")
    merged_into_id: Mapped[int | None] = mapped_column(ForeignKey("organisation.id"))
    tier: Mapped[str | None] = mapped_column(String(1))  # A/B/C, drives refresh cadence


class Brand(Base):
    __tablename__ = "brand"
    id: Mapped[int] = mapped_column(ForeignKey("entity.id"), primary_key=True)
    organisation_id: Mapped[int] = mapped_column(ForeignKey("organisation.id"), index=True)
    name: Mapped[str] = mapped_column(Text)
    name_norm: Mapped[str] = mapped_column(Text, index=True)
    claim_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), default=list)
    __table_args__ = (UniqueConstraint("organisation_id", "name_norm"),)


class Domain(Base):
    __tablename__ = "domain"
    id: Mapped[int] = mapped_column(ForeignKey("entity.id"), primary_key=True)
    organisation_id: Mapped[int] = mapped_column(ForeignKey("organisation.id"), index=True)
    brand_id: Mapped[int | None] = mapped_column(ForeignKey("brand.id"))
    hostname: Mapped[str] = mapped_column(Text, unique=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(10), default="unknown")  # live | redirect | dead | unknown
    redirect_target: Mapped[str | None] = mapped_column(Text)
    claim_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), default=list)


class Person(Base):
    __tablename__ = "person"
    id: Mapped[int] = mapped_column(ForeignKey("entity.id"), primary_key=True)
    organisation_id: Mapped[int | None] = mapped_column(ForeignKey("organisation.id"), index=True)
    name: Mapped[str] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    role_category: Mapped[str | None] = mapped_column(String(40))
    linkedin_url: Mapped[str | None] = mapped_column(Text)  # manual entry or licensed enrichment only
    email: Mapped[str | None] = mapped_column(Text)  # licensed sources only
    source: Mapped[str] = mapped_column(Text)
    last_verified_at: Mapped[datetime | None] = mapped_column(TS)
    lawful_basis_note: Mapped[str] = mapped_column(Text, default="UK GDPR legitimate interests (B2B contact)")


class Page(Base):
    __tablename__ = "page"
    id: Mapped[int] = mapped_column(ForeignKey("entity.id"), primary_key=True)
    domain_id: Mapped[int | None] = mapped_column(ForeignKey("domain.id"), index=True)
    url: Mapped[str] = mapped_column(Text, unique=True)
    page_type: Mapped[str | None] = mapped_column(String(30))


class NameAlias(Base):
    """Every name an entity has been seen under. Drives matching."""
    __tablename__ = "name_alias"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entity.id"), index=True)
    alias: Mapped[str] = mapped_column(Text)
    alias_norm: Mapped[str] = mapped_column(Text, index=True)
    alias_compact: Mapped[str] = mapped_column(Text, index=True)
    source_claim_id: Mapped[int | None] = mapped_column(BigInteger)
    __table_args__ = (UniqueConstraint("entity_id", "alias_norm"),)


# ---------------------------------------------------------------- roles and footprint
class OrgRole(Base):
    __tablename__ = "org_role"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    organisation_id: Mapped[int] = mapped_column(ForeignKey("organisation.id"), index=True)
    role: Mapped[str] = mapped_column(String(40))
    tribe_id: Mapped[int | None] = mapped_column(ForeignKey("organisation.id"))
    bank_partner_id: Mapped[int | None] = mapped_column(ForeignKey("organisation.id"))
    products: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    loan_min: Mapped[float | None] = mapped_column(Numeric(12, 2))
    loan_max: Mapped[float | None] = mapped_column(Numeric(12, 2))
    segment: Mapped[str | None] = mapped_column(String(20))
    online_or_branch: Mapped[str | None] = mapped_column(String(10))
    claim_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), default=list)
    __table_args__ = (UniqueConstraint("organisation_id", "role"),)


class EntityState(Base):
    __tablename__ = "entity_state"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entity.id"), index=True)
    state: Mapped[str] = mapped_column(String(2))
    basis: Mapped[str] = mapped_column(String(10))  # licensed | lends | excluded
    claim_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), default=list)
    __table_args__ = (UniqueConstraint("entity_id", "state", "basis"),)


# ---------------------------------------------------------------- edges
class Relationship(Base):
    __tablename__ = "relationship"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    from_id: Mapped[int] = mapped_column(ForeignKey("entity.id"), index=True)
    to_id: Mapped[int] = mapped_column(ForeignKey("entity.id"), index=True)
    type: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(10))
    attrs: Mapped[dict] = mapped_column(JSONB, default=dict)
    claim_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), default=list)
    first_seen: Mapped[date | None] = mapped_column(Date)
    last_seen: Mapped[date | None] = mapped_column(Date)
    __table_args__ = (UniqueConstraint("from_id", "to_id", "type"),)


class OurRelationship(Base):
    """The only source of truth for 'existing client / supplier'. Never inferred from names."""
    __tablename__ = "our_relationship"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    organisation_id: Mapped[int] = mapped_column(ForeignKey("organisation.id"), index=True)
    role: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(12))
    owner: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(12))
    needs_confirmation: Mapped[bool] = mapped_column(Boolean, default=True)
    external_ref: Mapped[str | None] = mapped_column(Text, unique=True)
    updated_by: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    __table_args__ = (UniqueConstraint("organisation_id", "role"),)


class OurRelationshipHistory(Base):
    __tablename__ = "our_relationship_history"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    our_relationship_id: Mapped[int] = mapped_column(BigInteger, index=True)
    snapshot: Mapped[dict] = mapped_column(JSONB)
    changed_by: Mapped[str] = mapped_column(Text)
    changed_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


# ---------------------------------------------------------------- claims and evidence
class ImportFile(Base):
    __tablename__ = "import_file"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    file_name: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(30))  # traffic_options | targets | owner_groups | relationship_seed
    imported_by: Mapped[str] = mapped_column(Text)
    imported_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    row_counts: Mapped[dict] = mapped_column(JSONB, default=dict)
    __table_args__ = (UniqueConstraint("sha256", "kind"),)


class Source(Base):
    __tablename__ = "source"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    url: Mapped[str | None] = mapped_column(Text, index=True)
    source_type: Mapped[str] = mapped_column(String(20))
    title: Mapped[str | None] = mapped_column(Text)
    retrieved_at: Mapped[datetime | None] = mapped_column(TS)
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    snapshot_ref: Mapped[str | None] = mapped_column(Text)
    import_file_id: Mapped[int | None] = mapped_column(ForeignKey("import_file.id"))
    sheet: Mapped[str | None] = mapped_column(Text)
    row_number: Mapped[int | None] = mapped_column(Integer)  # 1-based spreadsheet row, header = 1


class Claim(Base):
    __tablename__ = "claim"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entity.id"), index=True)
    attribute: Mapped[str] = mapped_column(String(60), index=True)
    value: Mapped[object] = mapped_column(JSONB)
    source_id: Mapped[int] = mapped_column(ForeignKey("source.id"))
    method: Mapped[str] = mapped_column(Text)
    job_run_id: Mapped[int | None] = mapped_column(BigInteger)
    confidence: Mapped[str] = mapped_column(String(6))
    status: Mapped[str] = mapped_column(String(12), default="accepted")
    superseded_by: Mapped[int | None] = mapped_column(ForeignKey("claim.id"))
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    decided_by: Mapped[str | None] = mapped_column(Text)


class ClaimSource(Base):
    """Extra supporting sources for a claim (e.g. the 'Sources' URLs on a researched spreadsheet row)."""
    __tablename__ = "claim_source"
    claim_id: Mapped[int] = mapped_column(ForeignKey("claim.id"), primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("source.id"), primary_key=True)


class AttributeDef(Base):
    __tablename__ = "attribute_def"
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    cardinality: Mapped[str] = mapped_column(String(4), default="one")  # one | set
    description: Mapped[str | None] = mapped_column(Text)
    materiality: Mapped[dict] = mapped_column(JSONB, default=dict)


# ---------------------------------------------------------------- metrics
class MetricSnapshot(Base):
    __tablename__ = "metric_snapshot"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entity.id"), index=True)
    metric: Mapped[str] = mapped_column(String(60))
    value: Mapped[float] = mapped_column(Numeric(18, 2))
    period: Mapped[str | None] = mapped_column(Text)
    source_id: Mapped[int] = mapped_column(ForeignKey("source.id"))
    claim_id: Mapped[int | None] = mapped_column(ForeignKey("claim.id"))
    retrieved_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    __table_args__ = (Index("ix_metric_entity_metric", "entity_id", "metric"),)


class TrackedKeyword(Base):
    __tablename__ = "tracked_keyword"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    keyword: Mapped[str] = mapped_column(Text)
    state: Mapped[str | None] = mapped_column(String(2))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (UniqueConstraint("keyword", "state"),)


# ---------------------------------------------------------------- compliance
class ComplianceFlag(Base):
    __tablename__ = "compliance_flag"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entity.id"), index=True)
    flag_type: Mapped[str] = mapped_column(String(30))
    summary: Mapped[str] = mapped_column(Text)
    date: Mapped[dt.date | None] = mapped_column(Date)
    claim_id: Mapped[int] = mapped_column(ForeignKey("claim.id"))


class ComplianceStatus(Base):
    """Set by a human only. No row = not yet reviewed."""
    __tablename__ = "compliance_status"
    entity_id: Mapped[int] = mapped_column(ForeignKey("entity.id"), primary_key=True)
    value: Mapped[str] = mapped_column(String(8))
    set_by: Mapped[str] = mapped_column(Text)
    set_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    note: Mapped[str | None] = mapped_column(Text)


class ComplianceStatusHistory(Base):
    __tablename__ = "compliance_status_history"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    entity_id: Mapped[int] = mapped_column(BigInteger, index=True)
    value: Mapped[str] = mapped_column(String(8))
    set_by: Mapped[str] = mapped_column(Text)
    set_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    note: Mapped[str | None] = mapped_column(Text)


# ---------------------------------------------------------------- scores
class WeightsVersion(Base):
    __tablename__ = "weights_version"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    score_type: Mapped[str] = mapped_column(String(30))
    label: Mapped[str] = mapped_column(Text)
    weights: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_by: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    note: Mapped[str | None] = mapped_column(Text)


class Score(Base):
    __tablename__ = "score"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entity.id"), index=True)
    score_type: Mapped[str] = mapped_column(String(30))  # buyer_value | supply_value | lbs_index | sheet_reference
    value: Mapped[float] = mapped_column(Numeric(8, 2))
    components: Mapped[dict] = mapped_column(JSONB, default=dict)
    weights_version_id: Mapped[int | None] = mapped_column(ForeignKey("weights_version.id"))
    computed_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


# ---------------------------------------------------------------- review, audit, users
class ReviewItem(Base):
    __tablename__ = "review_item"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(25), index=True)
    entity_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), default=list)
    title: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(10), default="open")  # open | approved | rejected | edited
    dedupe_key: Mapped[str] = mapped_column(Text, unique=True)  # keeps re-runs from duplicating items
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    decided_by: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None] = mapped_column(TS)
    note: Mapped[str | None] = mapped_column(Text)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    actor: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    table_name: Mapped[str | None] = mapped_column(Text)
    row_id: Mapped[int | None] = mapped_column(BigInteger)
    before: Mapped[dict | None] = mapped_column(JSONB)
    after: Mapped[dict | None] = mapped_column(JSONB)
    at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class AppUser(Base):
    """Username/password login (SSO can replace this later)."""
    __tablename__ = "app_user"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str] = mapped_column(Text, unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(10), default="viewer")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(TS)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(TS)


# ---------------------------------------------------------------- jobs (Stage 2)
class JobRun(Base):
    __tablename__ = "job_run"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    job: Mapped[str] = mapped_column(Text)
    args: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    started_at: Mapped[datetime | None] = mapped_column(TS)
    ended_at: Mapped[datetime | None] = mapped_column(TS)
    claims_written: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Numeric(10, 4), default=0)
    errors: Mapped[list] = mapped_column(JSONB, default=list)


class ApiUsage(Base):
    __tablename__ = "api_usage"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    job_run_id: Mapped[int | None] = mapped_column(ForeignKey("job_run.id"))
    provider: Mapped[str] = mapped_column(String(20))
    units: Mapped[float] = mapped_column(Numeric(14, 2))
    cost_usd: Mapped[float] = mapped_column(Numeric(10, 4))
    at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class HttpCache(Base):
    __tablename__ = "http_cache"
    request_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(20))
    url: Mapped[str] = mapped_column(Text)
    status: Mapped[int] = mapped_column(Integer)
    body: Mapped[bytes] = mapped_column()
    fetched_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(TS)
