"""Spreadsheet and reference-file importers.

Idempotent: a file is identified by its SHA-256 and kind; importing the same file again does nothing.
Every cell that carries a fact becomes a claim whose source points at file / sheet / row, plus the row's
original 'Sources' URLs where present.
"""
import csv
import hashlib
import re
from datetime import date
from pathlib import Path

import openpyxl
from sqlalchemy import select

from . import models as m
from . import parsing as p
from .normalise import compact, norm_domain, split_dba
from .store import Store

METHOD = "import:v1"
EMPTY = {"", "—", "–", "-", "n/a", "N/A", "None"}
TRIBAL_WORDS = re.compile(r"\b(tribe|tribal|band|nation|indian|indians|village|rancheria|pueblo|sioux|chippewa|"
                          r"ojibwe|pomo|community|reservation)\b", re.I)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def clean(v):
    if v is None:
        return None
    if isinstance(v, str):
        v = v.strip()
        return None if v in EMPTY else v
    return v


def read_sheet(path: Path, sheet: str) -> list[tuple[int, dict]]:
    """[(row_number, {header: value})] for non-empty rows; row_number is the spreadsheet row (header = 1)."""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    rows = list(wb[sheet].iter_rows(values_only=True))
    header = [str(h).strip() if h is not None else f"col{i}" for i, h in enumerate(rows[0])]
    out = []
    for i, r in enumerate(rows[1:], start=2):
        if any(c is not None and str(c).strip() for c in r):
            out.append((i, {h: clean(v) for h, v in zip(header, r)}))
    return out


def begin(store: Store, path: Path, kind: str) -> m.ImportFile | None:
    """Create the import_file row, or return None if this exact file was already imported."""
    digest = sha256(path)
    if store.s.scalar(select(m.ImportFile.id).where(m.ImportFile.sha256 == digest, m.ImportFile.kind == kind)):
        return None
    f = m.ImportFile(file_name=path.name, sha256=digest, kind=kind, imported_by=store.actor, row_counts={})
    store.s.add(f)
    store.s.flush()
    return f


def _group_kind(name: str) -> str:
    return "tribe" if TRIBAL_WORDS.search(name) else "operator_group"


def link_group(store: Store, org: m.Organisation, text: str | None, src: m.Source, confidence: str,
               attribute: str = "operator_group") -> m.Organisation | None:
    """Operator group / tribe cell -> group organisation, 'owns' edge and parent_id (unless only rumoured)."""
    if not text:
        return None
    c = store.claim(org.id, attribute, text, src, confidence=confidence, method=METHOD)
    name = p.strip_hedge(text)
    if not name or compact(name) in {compact(a) for a in store.org_names(org.id)}:
        if _group_kind(name or "") == "tribe" and org.kind == "company":
            org.kind = "tribe"
        return None
    kind = _group_kind(name)
    group, _ = store.resolve(name, kinds=("operator_group", "tribe"), kind=kind)
    status = p.hedge_status(text, confidence)
    store.edge(group.id, org.id, "owns", status, c)
    if status != "rumoured" and org.parent_id is None and group.id != org.id:
        org.parent_id = group.id
    return group


# ====================================================================== targets workbook
def import_targets(store: Store, path: Path) -> dict:
    f = begin(store, path, "targets")
    if f is None:
        return {"skipped": "already imported"}
    counts: dict = {}
    by_cfpb_name: dict[str, m.Organisation] = {}
    matched_pairs: list[tuple[m.Organisation, str, m.Source]] = []
    retrieved = "12m to 2026-09-29"

    # ---- Universe
    rows = read_sheet(path, "Universe")
    counts["universe_rows"] = len(rows)
    for rn, r in rows:
        name = r["Company (CFPB name)"]
        if not name:
            continue
        src = store.row_source(f, "Universe", rn)
        legal, dbas = split_dba(name)
        status = r.get("Status") or ""
        lifecycle = "screened_out" if status.startswith("screened out") else "active"
        org, how = store.resolve(name, extra_names=[legal, *dbas], strict=True, legal_name=name, lifecycle=lifecycle)
        counts[f"universe_{how}"] = counts.get(f"universe_{how}", 0) + 1
        by_cfpb_name[name] = org
        store.claim(org.id, "cfpb_company_name", name, src, confidence="high", method=METHOD)
        store.claim(org.id, "sheet_universe_status", status, src, confidence="high", method=METHOD)
        if status.startswith("screened out"):
            store.claim(org.id, "screen_out_reason", status.split(":", 1)[-1].strip(), src, confidence="medium", method=METHOD)
        for col, metric in (("Complaints (12m)", "cfpb_complaints_12m"), ("Installment", "cfpb_complaints_installment_12m"),
                            ("Payday", "cfpb_complaints_payday_12m"), ("Line of credit", "cfpb_complaints_loc_12m")):
            if r.get(col) not in (None, ""):
                c = store.claim(org.id, metric, float(str(r[col]).replace(",", "")), src, confidence="high", method=METHOD)
                store.metric(org.id, metric, r[col], retrieved, src, c)
        link_group(store, org, r.get("Operator group"), src, "medium")
        if r.get("Matched buyer"):
            store.claim(org.id, "sheet_matched_buyer", r["Matched buyer"], src, confidence="low", method=METHOD)
            matched_pairs.append((org, r["Matched buyer"], src))

    # ---- Targets
    rows = read_sheet(path, "Targets")
    counts["targets_rows"] = len(rows)
    for rn, r in rows:
        name = r["Company (CFPB name)"]
        if not name:
            continue
        org = by_cfpb_name.get(name) or store.resolve(name)[0]
        src = store.row_source(f, "Targets", rn)
        conf = (r.get("Research confidence") or "medium").lower()
        conf = conf if conf in m.CONFIDENCE else "medium"
        extra = [store.url_source(u, p.source_type_for_url(u)) for u in p.urls(r.get("Sources"))]

        def C(attr, value, confidence=conf, method=METHOD):
            return store.claim(org.id, attr, value, src, confidence=confidence, method=method, extra_sources=extra)

        for col, attr in (("Why it fits", "why_it_fits"), ("Type", "lender_type_text"), ("Products", "products_text"),
                          ("Loan range", "loan_range_text"), ("States", "states_text"), ("Online / branch", "online_or_branch_text"),
                          ("Segment", "segment_text"), ("Lead-buying evidence", "lead_buying_evidence"),
                          ("Litigation / regulatory", "litigation_regulatory"), ("Parent / servicer", "parent_servicer_text"),
                          ("Roles to approach", "roles_to_approach"), ("Consumer brands", "consumer_brands_text"),
                          ("Fit", "sheet_fit"), ("Research confidence", "research_confidence")):
            C(attr, r.get(col))
        for col in ("Decision (pursue / park / drop)", "Owner", "Your notes"):
            C("sheet_team_" + re.sub(r"\W+", "_", col.lower()).strip("_"), r.get(col), "high")

        # website and brands
        site = norm_domain(r.get("Website"))
        if site:
            c = C("website", site)
            store.add_domain(org.id, site, claim_id=c.id, primary=True) or store.claim(
                org.id, "declared_domain", site, src, confidence=conf, method=METHOD)
        for b in p.brands(r.get("Consumer brands")):
            c = C("brand", b, method=p.PARSE_METHOD)
            brand = store.add_brand(org.id, b["name"], c.id)
            if b["entity"]:
                store.add_alias(org.id, b["entity"], c.id)
            for d in b["domains"]:
                if not store.add_domain(org.id, d, brand_id=brand.id, claim_id=c.id):
                    store.claim(org.id, "declared_domain", d, src, confidence=conf, method=p.PARSE_METHOD)

        # roles
        tribe = None
        if r.get("Tribe"):
            c = C("tribe", r["Tribe"])
            tribe_name = p.strip_hedge(re.sub(r"\s+via\s+.*$", "", r["Tribe"]))
            tribe, _ = store.resolve(tribe_name, kinds=("tribe", "operator_group"), kind="tribe")
            if tribe.kind == "operator_group":
                tribe.kind = "tribe"
        roles = p.lender_roles(r.get("Type"))
        lo, hi = p.loan_range(r.get("Loan range"))
        seg = p.segment(r.get("Segment"))
        oob = p.online_or_branch(r.get("Online / branch"))
        products = [x.strip() for x in re.split(r"[;,]", r.get("Products") or "") if x.strip()] or None
        if r.get("Type") and not roles:
            store.review("parse_failure", f"parse:type:{org.id}", f"Lender type not recognised: {org.display_name}",
                         [org.id], {"field": "Type", "text": r["Type"]})
        if r.get("Loan range") and lo is None and hi is None:
            store.review("parse_failure", f"parse:loan:{org.id}", f"Loan range not parsed: {org.display_name}",
                         [org.id], {"field": "Loan range", "text": r["Loan range"]})
        if lo is not None:
            C("loan_min", lo, method=p.PARSE_METHOD)
        if hi is not None:
            C("loan_max", hi, method=p.PARSE_METHOD)
        for role in roles:
            c = C("role", role, method=p.PARSE_METHOD)
            store.role(org.id, role, c, products=products, loan_min=lo, loan_max=hi, segment=seg, online_or_branch=oob,
                       tribe_id=tribe.id if (tribe and role == "lender_tribal") else None)
        if tribe and "lender_tribal" not in roles:
            store.edge(tribe.id, org.id, "owns", "inferred", None)

        # states
        st = p.states(r.get("States"))
        if st["count"]:
            C("state_count", st["count"], method=p.PARSE_METHOD)
        if st["nationwide"]:
            C("nationwide", True, method=p.PARSE_METHOD)
        if st["basis"]:
            c = C("states_" + st["basis"], st["states"], method=p.PARSE_METHOD)
            for s in st["states"]:
                store.state(org.id, s, st["basis"], c)

        # owner group, compliance, metrics, reference score
        link_group(store, org, r.get("Operator group"), src, conf)
        lit = r.get("Litigation / regulatory")
        if lit:
            c = C("litigation_note", lit)
            for flag in p.compliance_flags(lit):
                store.compliance_flag(org.id, flag, lit, c)
        for col, metric in (("Organic visits/mo (Ahrefs)", "ahrefs_organic_visits"), ("Paid search $/mo (Ahrefs)", "ahrefs_paid_spend_usd")):
            if r.get(col) is not None:
                c = C(metric, r[col], "medium")
                store.metric(org.id, metric, r[col], "as at 2026-09-29", src, c)
        if r.get("Score") is not None:
            comps = {k.replace("Score: ", ""): r.get(k) for k in r if k.startswith("Score: ")}
            comps.update({"rank": r.get("Rank"), "fit": r.get("Fit"), "lead_buying_strength": r.get("Lead-buying strength (0-100)")})
            store.s.add(m.Score(entity_id=org.id, score_type="sheet_reference", value=r["Score"],
                                components={"sheet": "targets_20260929/Targets", "row": rn, **comps}))
        org.lifecycle = "active"

    # ---- Lead-buying strength
    rows = read_sheet(path, "Lead-buying strength")
    counts["lbs_rows"] = len(rows)
    lbs_by_name: dict[str, m.Organisation] = {}
    for rn, r in rows:
        name = r["Company"]
        if not name:
            continue
        src = store.row_source(f, "Lead-buying strength", rn)
        names_seen = [x.strip() for x in (r.get("Names seen") or "").split(",") if x.strip() and len(x.strip()) > 2]
        doms = [d for d in (norm_domain(x) for x in (r.get("Domains sized") or "").split(",")) if d]
        rel = r.get("Relationship") or ""
        display = names_seen[0] if (name.islower() and " " not in name and names_seen) else name
        org = by_cfpb_name.get(name)
        how = "cfpb_name"
        if org is None:
            # Resolve on the row's own name and domains only. 'Names seen' are added as aliases afterwards:
            # the sheet sometimes groups several companies under one key, so they must not drive the link.
            org, how = store.resolve(display, doms, extra_names=[name],
                                     lifecycle="candidate" if rel == "not on our lists yet" else "active")
        counts[f"lbs_{how}"] = counts.get(f"lbs_{how}", 0) + 1
        lbs_by_name[name] = org
        for n in names_seen:
            store.add_alias(org.id, n)

        def C(attr, value, confidence="medium"):
            return store.claim(org.id, attr, value, src, confidence=confidence, method=METHOD)

        C("sheet_relationship_label", rel, "low")
        for d in doms:
            c = C("domain", d)
            if not store.add_domain(org.id, d, claim_id=c.id):
                C("declared_domain", d)
        for col, attr in (("Disclosed acquisition data", "disclosed_acquisition_data"), ("Affiliate programme", "affiliate_programme"),
                          ("Conferences", "conferences"), ("Lead-acquisition hiring", "lead_acquisition_hiring")):
            C(attr, r.get(col))
        for col, metric in (("Organic visits/mo", "ahrefs_organic_visits"), ("Paid search $/mo", "ahrefs_paid_spend_usd")):
            if r.get(col) is not None:
                c = C(metric, r[col])
                store.metric(org.id, metric, r[col], "as at 2026-09-29", src, c)
        lists = p.list_appearances(r.get("Lists it appears on"))
        if lists:
            c = C("appears_on_partner_lists", lists)
            for item in lists:
                owner_id = store.domain_owner(item["domain"])
                if owner_id is None:
                    lister, _ = store.resolve(item["domain"], [item["domain"]], lifecycle="candidate")
                    store.add_domain(lister.id, item["domain"], claim_id=c.id, primary=True)
                    owner_id = lister.id
                store.edge(owner_id, org.id, "sells_leads_to", "inferred", c,
                           {"evidence": "partner/consent list", "list_family_size": item["family_size"]})
        if r.get("Strength (0-100)") is not None:
            comps = {k.replace("Pts: ", ""): r.get(k) for k in r if k.startswith("Pts: ")}
            store.s.add(m.Score(entity_id=org.id, score_type="lbs_index", value=r["Strength (0-100)"],
                                components={"sheet": "targets_20260929/Lead-buying strength", "row": rn,
                                            "rank": r.get("Rank"), "partner_lists": r.get("Partner lists (#)"), **comps}))
        if rel == "existing client":
            store.set_our_relationship(
                org.id, role="buyer", status="live", source="seed", needs_confirmation=True,
                notes="Seeded from the 'existing client' list in targets_20260929 (Lead-buying strength). Confirm.")
            counts["our_relationships_seeded"] = counts.get("our_relationships_seeded", 0) + 1

    # ---- Universe 'Matched buyer' -> possible match with the buyer record (never an automatic link)
    buyers = {compact(n): o for n, o in lbs_by_name.items()}
    for org, buyer_name, src in matched_pairs:
        buyer = buyers.get(compact(buyer_name))
        if buyer is None:
            buyer, _ = store.resolve(buyer_name)
            store.set_our_relationship(
                buyer.id, role="buyer", status="live", source="seed", needs_confirmation=True,
                notes="Seeded from the 'Matched buyer' column in targets_20260929 (Universe). Confirm.")
            counts["our_relationships_seeded"] = counts.get("our_relationships_seeded", 0) + 1
        buyer = store.org(buyer.id)
        if buyer.id != store.org(org.id).id:
            store.review("possible_match", f"possible_match:{min(org.id, buyer.id)}:{max(org.id, buyer.id)}",
                         f"{org.display_name} may be existing buyer {buyer.display_name}",
                         [org.id, buyer.id],
                         {"reasons": [f"spreadsheet 'Matched buyer' says {buyer_name} (hand match, unverified)"],
                          "relationship_org_id": buyer.id, "candidate_org_id": org.id})
    f.row_counts = counts
    return counts


# ====================================================================== traffic workbook
CONF_BY_EVIDENCE = {
    "site read": "medium", "ahrefs data": "medium", "third-party listing": "medium",
    "earlier research this chat": "low", "known platform (fit unverified)": "low",
}


def import_traffic(store: Store, path: Path) -> dict:
    f = begin(store, path, "traffic_options")
    if f is None:
        return {"skipped": "already imported"}
    counts: dict = {"rows": 0, "no_domain": 0}
    for rn, r in read_sheet(path, "All options"):
        if not isinstance(r.get("#"), (int, float)) or not r.get("Name"):
            continue  # legend / notes rows
        counts["rows"] += 1
        src = store.row_source(f, "All options", rn)
        full = r["Name"]
        display = re.sub(r"\s*\([^)]*\)\s*$", "", full).strip() or full
        paren = re.findall(r"\(([^)]*)\)", full)
        dom = norm_domain(r.get("Domain"))
        if not dom:
            counts["no_domain"] += 1
        role = p.traffic_role(r.get("Category"))
        kind = "platform" if role == "paid_media_platform" else "company"
        org, how = store.resolve(display, [dom] if dom else [], kind=kind)
        counts[how] = counts.get(how, 0) + 1
        evidence_src = (r.get("Source of evidence") or "").strip()
        conf = next((v for k, v in CONF_BY_EVIDENCE.items() if evidence_src.lower().startswith(k)), "medium")

        def C(attr, value, confidence=conf, method=METHOD):
            return store.claim(org.id, attr, value, src, confidence=confidence, method=method)

        C("traffic_category", r.get("Category"), "high")
        C("description", r.get("What it is"))
        C("evidence", r.get("Evidence"))
        C("how_to_work_with", r.get("How to work with them"))
        C("evidence_source", evidence_src or None)
        C("notes_risk", r.get("Notes / risk"))
        for col in ("Status", "Owner", "Comments"):
            C("sheet_team_" + col.lower(), r.get(col), "high")
        if r.get("Priority") in ("A", "B", "C"):
            C("priority", r["Priority"], "high")
            rank = {"A": 0, "B": 1, "C": 2, None: 3}
            if rank[r["Priority"]] < rank[org.tier]:
                org.tier = r["Priority"]
        if role:
            store.role(org.id, role, C("role", role, "high"))
        if dom:
            c = C("domain", dom, "high")
            brand = store.add_brand(org.id, display, c.id)
            if not store.add_domain(org.id, dom, brand_id=brand.id, claim_id=c.id, primary=True):
                C("declared_domain", dom, "high")
        for pc in paren:
            # '(MaxCash)' next to maxcash.com is the brand; anything else is a note, not a name.
            if dom and compact(pc) == compact(dom.split(".")[0]):
                store.add_brand(org.id, pc, C("brand", pc).id)
            else:
                C("name_qualifier", pc)
        visits = r.get("Ahrefs est. US organic visits/mo")
        if isinstance(visits, (int, float)):
            c = C("ahrefs_organic_visits", visits, "medium")
            store.metric(org.id, "ahrefs_organic_visits", visits, "2026-11", src, c)
        risk = r.get("Notes / risk")
        if risk:
            for flag in p.compliance_flags(risk):
                if flag != "other":
                    store.compliance_flag(org.id, flag, risk, C("compliance_note", risk))
        if conf == "low":
            store.review("low_confidence", f"low_conf:traffic:{org.id}", f"Unverified research: {org.display_name}",
                         [org.id], {"evidence_source": evidence_src, "evidence": r.get("Evidence"), "row": rn})
    f.row_counts = counts
    return counts


# ====================================================================== reference CSVs (kept out of git)
def import_owner_groups(store: Store, path: Path) -> dict:
    """CSV: group, member, relation, status, note.

    relation: owns | operates | same_owner_as | hosts_affiliate_program_for | places_sponsored_content_on
    """
    f = begin(store, path, "owner_groups")
    if f is None:
        return {"skipped": "already imported"}
    counts = {"rows": 0, "members_created": 0}
    with open(path, newline="") as fh:
        for i, r in enumerate(csv.DictReader(fh), start=2):
            counts["rows"] += 1
            src = store.row_source(f, path.name, i)
            rel, status = r["relation"].strip(), (r.get("status") or "inferred").strip()
            group, _ = store.resolve(r["group"].strip(), kind=(r.get("group_kind") or "operator_group").strip() or "operator_group")
            member, how = store.resolve(r["member"].strip(), lifecycle="candidate")
            if how.startswith("created"):
                counts["members_created"] += 1
            c = store.claim(member.id, "owner_group" if rel in ("owns", "operates") else rel,
                            {"group": group.display_name, "relation": rel, "note": r.get("note")}, src,
                            confidence="medium" if status == "confirmed" else "low", method=METHOD)
            store.edge(group.id, member.id, rel, status, c)
            if rel in ("owns", "operates") and member.parent_id is None and status != "rumoured":
                member.parent_id = group.id
    f.row_counts = counts
    return counts


def import_relationship_seed(store: Store, path: Path) -> dict:
    """CSV: name, aliases, domains, role, status, notes  (aliases/domains separated by '|').

    Seeds attach to an organisation only when it already has an our_relationship under the same name.
    Otherwise a new organisation is created, and the possible-match sweep raises lookalikes for review.
    """
    f = begin(store, path, "relationship_seed")
    if f is None:
        return {"skipped": "already imported"}
    counts = {"rows": 0, "attached_existing": 0, "created": 0}
    with open(path, newline="") as fh:
        for i, r in enumerate(csv.DictReader(fh), start=2):
            counts["rows"] += 1
            src = store.row_source(f, path.name, i)
            names = [r["name"].strip()] + [a.strip() for a in (r.get("aliases") or "").split("|") if a.strip()]
            domains = [d for d in (norm_domain(x) for x in (r.get("domains") or "").split("|")) if d]
            keys = {compact(n) for n in names}
            existing = None
            for ours in store.s.scalars(select(m.OurRelationship)):
                if keys & {compact(a) for a in store.org_names(ours.organisation_id)}:
                    existing = store.org(ours.organisation_id)
                    break
            if existing:
                org = existing
                counts["attached_existing"] += 1
            else:
                org = store.new_org(names[0], lifecycle="active")
                counts["created"] += 1
            for n in names:
                store.add_alias(org.id, n)
            for d in domains:
                c = store.claim(org.id, "declared_domain", d, src, confidence="medium", method="seed")
                if not existing:
                    store.add_domain(org.id, d, claim_id=c.id)
            store.set_our_relationship(org.id, role=r["role"].strip(), status=r["status"].strip(), source="seed",
                                       needs_confirmation=True, notes=(r.get("notes") or "").strip() or None)
    f.row_counts = counts
    return counts


def default_attribute_defs(store: Store) -> None:
    """Attributes that are sets rather than single values, and what counts as a material change (Stage 2)."""
    defs = {
        "brand": ("set", {"material": "any_change"}),
        "domain": ("set", {"material": "any_change"}),
        "declared_domain": ("set", {"material": "any_change"}),
        "role": ("set", {"material": "any_change"}),
        "states_lends": ("one", {"material": "any_change"}),
        "states_excluded": ("one", {"material": "any_change"}),
        "appears_on_partner_lists": ("one", {"material": "any_change"}),
        "litigation_note": ("one", {"material": "any_change"}),
        "affiliate_programme": ("one", {"material": "any_change"}),
        "lead_acquisition_hiring": ("one", {"material": "any_change"}),
        "ahrefs_organic_visits": ("one", {"material": "relative_change", "threshold": 0.3}),
        "ahrefs_paid_spend_usd": ("one", {"material": "relative_change", "threshold": 0.3}),
        "cfpb_complaints_12m": ("one", {"material": "relative_change", "threshold": 0.5}),
        "owner_group": ("set", {"material": "any_change"}),
    }
    for key, (card, mat) in defs.items():
        if store.s.get(m.AttributeDef, key) is None:
            store.s.add(m.AttributeDef(key=key, cardinality=card, materiality=mat))
    store.s.flush()
