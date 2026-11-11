"""Phase 1 importer tests on made-up data: missing domains, owner-group brand grouping, the
'lookalike of an existing relationship' case (a prospect owning a brand we already contract with), idempotency and merges."""
from sqlalchemy import select

from ecosystem import models as m
from ecosystem.cli import import_all
from ecosystem.importers import (import_owner_groups, import_relationship_seed, import_targets, import_traffic)
from ecosystem.matching import possible_match_sweep
from ecosystem.reconcile import report
from ecosystem.store import count


def org_named(s, name):
    return s.scalar(select(m.Organisation).where(m.Organisation.display_name == name, m.Organisation.lifecycle != "merged"))


def claims(s, org, attribute):
    return list(s.scalars(select(m.Claim.value).where(m.Claim.entity_id == org.id, m.Claim.attribute == attribute)))


def test_traffic_import_keeps_rows_without_domains_and_skips_legend(store, traffic_file):
    counts = import_traffic(store, traffic_file)
    assert counts["rows"] == 8
    assert counts["no_domain"] == 3
    for name in ("Comet Cash", "Orbit Money", "Nova Funds"):
        org = org_named(store.s, name)
        assert org is not None
        assert count(store.s, m.Domain, m.Domain.organisation_id == org.id) == 0
        assert store.s.scalar(select(m.OrgRole.role).where(m.OrgRole.organisation_id == org.id)) == "connector_brand"
    assert org_named(store.s, "Acme Ventures").tier == "A"
    # 'Earlier research this chat' rows are low confidence and queued for review
    low = store.s.scalars(select(m.ReviewItem).where(m.ReviewItem.kind == "low_confidence")).all()
    assert len(low) == 3
    assert set(claims(store.s, org_named(store.s, "Comet Cash"), "evidence_source")) == {"Earlier research this chat"}
    # FTC mention in the risk note becomes a compliance flag
    pulse = org_named(store.s, "Pulse Network")
    assert store.s.scalar(select(m.ComplianceFlag.flag_type).where(m.ComplianceFlag.entity_id == pulse.id)) == "ftc_action"


def test_every_fact_traces_to_file_sheet_and_row(store, traffic_file):
    import_traffic(store, traffic_file)
    org = org_named(store.s, "Rocket Bucks")
    c = store.s.scalar(select(m.Claim).where(m.Claim.entity_id == org.id, m.Claim.attribute == "priority"))
    src = store.s.get(m.Source, c.source_id)
    f = store.s.get(m.ImportFile, src.import_file_id)
    assert (f.file_name, src.sheet, src.row_number, c.value) == ("CLE_test_traffic_options.xlsx", "All options", 3, "B")
    d = store.s.scalar(select(m.Domain).where(m.Domain.hostname == "rocketbucks.com"))
    assert d.claim_ids and store.s.get(m.Claim, d.claim_ids[0]).attribute == "domain"


def test_owner_group_puts_all_brands_under_one_owner(store, traffic_file, owner_groups_file):
    import_traffic(store, traffic_file)
    counts = import_owner_groups(store, owner_groups_file)
    assert counts["members_created"] == 0  # all five matched the traffic rows, including the three without domains
    group = org_named(store.s, "Acme Ventures")
    children = store.s.scalars(select(m.Organisation.display_name).where(m.Organisation.parent_id == group.id)).all()
    assert sorted(children) == ["Comet Cash", "Jet Lend", "Nova Funds", "Orbit Money", "Rocket Bucks"]
    edges = store.s.scalars(select(m.Relationship).where(m.Relationship.from_id == group.id, m.Relationship.type == "owns")).all()
    assert len(edges) == 5 and {e.status for e in edges} == {"inferred"}


def test_targets_import(store, targets_file):
    counts = import_targets(store, targets_file)
    s = store.s
    assert counts["universe_rows"] == 5 and counts["targets_rows"] == 1 and counts["lbs_rows"] == 3
    prairie = org_named(s, "Prairie Holdings, LLC")
    # brands and their domains
    brands = s.scalars(select(m.Brand.name).where(m.Brand.organisation_id == prairie.id)).all()
    assert sorted(brands) == ["Quick Cash Now", "Sunrise Lending"]
    assert store.domain_owner("quickcashnowtest.com") == prairie.id
    # role with parsed attributes and tribe
    role = s.scalar(select(m.OrgRole).where(m.OrgRole.organisation_id == prairie.id))
    assert role.role == "lender_tribal" and float(role.loan_max) == 3000 and role.segment == "subprime"
    assert s.get(m.Organisation, role.tribe_id).kind == "tribe"
    # states parsed as exclusions
    states = s.scalars(select(m.EntityState.state).where(m.EntityState.entity_id == prairie.id,
                                                         m.EntityState.basis == "excluded")).all()
    assert sorted(states) == ["NY", "PA", "VT"]
    # operator group parent, compliance flag, supporting source URLs
    assert s.get(m.Organisation, prairie.parent_id).display_name == "Prairie Band / Plains Development Group"
    assert s.scalar(select(m.ComplianceFlag.flag_type).where(m.ComplianceFlag.entity_id == prairie.id)) == "rent_a_tribe_litigation"
    c = s.scalar(select(m.Claim).where(m.Claim.entity_id == prairie.id, m.Claim.attribute == "litigation_note"))
    extra = s.scalars(select(m.Source.source_type).join(m.ClaimSource, m.ClaimSource.source_id == m.Source.id)
                      .where(m.ClaimSource.claim_id == c.id)).all()
    assert sorted(extra) == ["news", "site_read"] and c.confidence == "high"
    # hedged group is only rumoured and does not become the parent
    quasar = org_named(s, "Quasar Lending, Inc.")
    edge = s.scalar(select(m.Relationship).where(m.Relationship.to_id == quasar.id, m.Relationship.type == "owns"))
    assert edge.status == "rumoured" and quasar.parent_id is None
    # CFPB names that differ only by legal suffix are not auto-merged
    assert org_named(s, "Quasar Lending Company") is not None
    assert s.scalar(select(m.ReviewItem).where(m.ReviewItem.kind == "dedup",
                                               m.ReviewItem.entity_ids.contains([quasar.id]))) is not None
    # screened-out companies are kept but hidden
    assert org_named(s, "Mega Bank NA").lifecycle == "screened_out"
    # partner-list appearance becomes an inferred sells_leads_to edge from the list owner
    lister = store.domain_owner("listsite.com")
    e = s.scalar(select(m.Relationship).where(m.Relationship.from_id == lister, m.Relationship.to_id == prairie.id))
    assert e.type == "sells_leads_to" and e.status == "inferred"
    # reference scores kept
    assert s.scalar(select(m.Score.value).where(m.Score.entity_id == prairie.id, m.Score.score_type == "sheet_reference")) == 80


def test_spreadsheet_clients_are_seeded_live_but_unconfirmed_and_hand_matches_go_to_review(store, targets_file):
    import_targets(store, targets_file)
    s = store.s
    zephyr = org_named(s, "Zephyr Loans")
    ours = s.scalar(select(m.OurRelationship).where(m.OurRelationship.organisation_id == zephyr.id))
    assert (ours.role, ours.status, ours.needs_confirmation, ours.source) == ("buyer", "live", True, "seed")
    # the CFPB company the sheet matched by hand is NOT given a relationship, only a review item
    cfpb = org_named(s, "Zephyr Tribal Lending Authority")
    assert s.scalar(select(m.OurRelationship).where(m.OurRelationship.organisation_id == cfpb.id)) is None
    item = s.scalar(select(m.ReviewItem).where(m.ReviewItem.kind == "possible_match",
                                               m.ReviewItem.entity_ids.contains([cfpb.id, zephyr.id])))
    assert item is not None and "Matched buyer" in item.payload["reasons"][0]
    # no prospect has a relationship row
    prairie = org_named(s, "Prairie Holdings, LLC")
    assert s.scalar(select(m.OurRelationship).where(m.OurRelationship.organisation_id == prairie.id)) is None


def test_lookalike_of_existing_relationship_is_raised_not_linked(store, targets_file, traffic_file, seed_file):
    """A prospect's brand is a buyer we are contracting with: must be raised for review, never linked."""
    import_targets(store, targets_file)
    import_traffic(store, traffic_file)
    import_relationship_seed(store, seed_file)
    possible_match_sweep(store)
    s = store.s
    prairie = org_named(s, "Prairie Holdings, LLC")
    seed = org_named(s, "Quick Cash Now / Plains Finance")
    assert seed.id != prairie.id  # never merged automatically
    assert s.scalar(select(m.OurRelationship).where(m.OurRelationship.organisation_id == prairie.id)) is None
    item = s.scalar(select(m.ReviewItem).where(m.ReviewItem.kind == "possible_match",
                                               m.ReviewItem.entity_ids.contains([prairie.id, seed.id])))
    assert item is not None
    assert "same domain quickcashnowtest.com" in item.payload["reasons"]
    assert item.payload["strength"] == "strong"
    # a seed whose name matches an existing relationship updates it instead of duplicating it
    assert len(s.scalars(select(m.Organisation).where(m.Organisation.display_name == "Zephyr Loans")).all()) == 1
    zephyr = org_named(s, "Zephyr Loans")
    ours = s.scalar(select(m.OurRelationship).where(m.OurRelationship.organisation_id == zephyr.id))
    assert "confirmed" in ours.notes and ours.needs_confirmation
    # all seeds need confirmation
    assert all(r.needs_confirmation for r in s.scalars(select(m.OurRelationship)))


def test_import_all_is_idempotent(store, tmp_path, targets_file, traffic_file, owner_groups_file, seed_file):
    ref = tmp_path / "ref"
    ref.mkdir()
    owner_groups_file.rename(ref / "owner_groups.csv")
    seed_file.rename(ref / "our_relationships_seed.csv")
    import_all(store, raw=tmp_path, ref=ref)
    first = report(store.s)
    out = import_all(store, raw=tmp_path, ref=ref)
    assert out["targets_20990101.xlsx"] == {"skipped": "already imported"}
    second = report(store.s)
    first.pop("import_files"), second.pop("import_files")
    assert first == second


def test_merge_moves_everything(store, targets_file, traffic_file, seed_file):
    import_targets(store, targets_file)
    import_traffic(store, traffic_file)
    import_relationship_seed(store, seed_file)
    s = store.s
    prairie = org_named(s, "Prairie Holdings, LLC")
    seed = org_named(s, "Quick Cash Now / Plains Finance")
    store.merge(prairie.id, seed.id, note="approved in test")
    s.flush()
    assert s.get(m.Organisation, seed.id).lifecycle == "merged"
    ours = s.scalar(select(m.OurRelationship).where(m.OurRelationship.organisation_id == prairie.id))
    assert ours.status == "contracting"
    assert "declared_domain" in {c.attribute for c in s.scalars(select(m.Claim).where(m.Claim.entity_id == prairie.id))}
    assert s.scalar(select(m.AuditLog).where(m.AuditLog.action == "organisation.merge")) is not None
    # resolving by the merged org's id now lands on the kept one
    assert store.org(seed.id).id == prairie.id
