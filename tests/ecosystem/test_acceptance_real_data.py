"""Phase 1 acceptance on the real spreadsheets.

Runs only when data/raw/ holds the workbooks and data/reference/acceptance.json lists the expected cases.
Both are git-ignored, so the client and prospect names stay out of the repository.
"""
import json

import pytest
from sqlalchemy import select

from ecosystem import models as m
from ecosystem.cli import import_all
from ecosystem.config import RAW_DIR, REFERENCE_DIR
from ecosystem.reconcile import report

EXPECT = REFERENCE_DIR / "acceptance.json"
pytestmark = pytest.mark.skipif(not EXPECT.exists() or not list(RAW_DIR.glob("targets_*.xlsx")),
                                reason="real data not present")


def _org(s, name):
    return s.scalar(select(m.Organisation).where(m.Organisation.display_name == name, m.Organisation.lifecycle != "merged"))


def test_real_data(store):
    expect = json.loads(EXPECT.read_text())
    import_all(store)
    s = store.s
    r = report(s)
    for sheet, n in expect["sheet_rows"].items():
        assert r["rows_imported_per_sheet"][sheet] == n, sheet
    for a, b in expect["possible_matches"]:
        oa, ob = _org(s, a), _org(s, b)
        assert oa and ob, (a, b)
        assert s.scalar(select(m.ReviewItem).where(m.ReviewItem.kind == "possible_match",
                                                   m.ReviewItem.entity_ids.contains([oa.id, ob.id]))), (a, b)
    for group, members in expect["owner_groups"].items():
        g = _org(s, group)
        kids = set(s.scalars(select(m.Organisation.display_name).where(m.Organisation.parent_id == g.id)))
        assert set(members) <= kids, group
    for name in expect["not_our_relationship"]:
        assert s.scalar(select(m.OurRelationship).where(m.OurRelationship.organisation_id == _org(s, name).id)) is None
    for name, status in expect["our_relationship_status"].items():
        orgs = s.scalars(select(m.Organisation.id).where(m.Organisation.display_name == name)).all()
        statuses = set(s.scalars(select(m.OurRelationship.status).where(m.OurRelationship.organisation_id.in_(orgs))))
        assert status in statuses, name
    assert all(o.needs_confirmation for o in s.scalars(select(m.OurRelationship)))
