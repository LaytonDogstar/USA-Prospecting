import pytest
from sqlalchemy import select

from ecosystem import models as m
from ecosystem.importers import import_targets, import_traffic
from ecosystem.scoring import MISSING_SCORE, _component, combine, current_weights, rescore, save_weights


def test_combine_rescales_unavailable_and_penalises_missing():
    comps = {"a": _component(100, None, ""), "b": _component(None, None, "")}
    # b has no data for this org but others have it -> scored as unknown
    v, missing = combine(dict(comps), {"a": 1, "b": 1, "c": 1}, available={"a", "b"})
    assert missing == ["b"] and v == pytest.approx((100 + MISSING_SCORE) / 2)
    # c is available for nobody -> left out entirely, not penalised
    v, _ = combine(dict(comps), {"a": 1, "c": 5}, available={"a"})
    assert v == 100


def test_rescore_components_and_history(store, targets_file, traffic_file):
    import_targets(store, targets_file)
    import_traffic(store, traffic_file)
    out = rescore(store)
    assert out["buyer_value"]["not_yet_available"] == ["state_overlap"]
    s = store.s
    prairie = s.scalar(select(m.Organisation).where(m.Organisation.display_name == "Prairie Holdings, LLC"))
    sc = s.scalar(select(m.Score).where(m.Score.entity_id == prairie.id, m.Score.score_type == "buyer_value"))
    comps = sc.components["components"]
    assert comps["type_fit"]["score"] == 90 and comps["type_fit"]["claim_ids"]
    assert comps["price_volume_fit"]["score"] == 100  # max loan $3,000
    assert comps["segment"]["score"] == 100
    assert comps["lead_buying"]["score"] == 30
    assert sc.components["weights_label"] == "v1 (default)"
    # supply value for a traffic row with priority, traffic and owner group
    rocket = s.scalar(select(m.Organisation).where(m.Organisation.display_name == "Rocket Bucks"))
    sv = s.scalar(select(m.Score).where(m.Score.entity_id == rocket.id, m.Score.score_type == "supply_value"))
    assert sv.components["components"]["sheet_priority"]["score"] == 60
    # new weights -> new version; re-score keeps history
    save_weights(s, "buyer_value", {"segment": 1}, "test", "only segment")
    rescore(store)
    rows = s.scalars(select(m.Score).where(m.Score.entity_id == prairie.id, m.Score.score_type == "buyer_value")).all()
    assert len(rows) == 2 and rows[-1].components["weights_label"] == "v2"
    assert current_weights(s, "buyer_value").label == "v2"


def test_save_weights_validation(session):
    with pytest.raises(ValueError):
        save_weights(session, "buyer_value", {"segment": -1}, "test")
    with pytest.raises(ValueError):
        save_weights(session, "buyer_value", {}, "test")


def test_fit_and_traffic_rules():
    from types import SimpleNamespace as NS
    from ecosystem.scoring import DEFAULT_TABLES, TRAFFIC_LOAN_SHARE, buyer_components, is_lender

    class F:
        def __init__(self, loan_max):
            self.roles = {1: [NS(role="lender_state_licensed", loan_max=loan_max, segment=None, claim_ids=[1])]}
            self.lbs, self.metrics, self.top = {}, {}, {}
    fit = lambda x: buyer_components(NS(id=1), F(x), DEFAULT_TABLES)["price_volume_fit"]["score"]
    assert fit(800) == 75 and fit(2500) == 100 and fit(5000) == 100 and fit(8000) == 70
    assert fit(30000) == 20  # prime-sized loans are a poor fit for subprime pingtree leads
    assert TRAFFIC_LOAN_SHARE["sponsored_publisher_agency"] < TRAFFIC_LOAN_SHARE["comparison_offer_wall"] < 1
    # marketplaces on lead-buying lists are supply, not lenders
    assert is_lender(set(), True) and not is_lender({"comparison_offer_wall"}, True)
    assert is_lender({"lender_tribal", "comparison_offer_wall"}, False)


def test_shared_name_or_website_raises_dedup(store):
    from ecosystem.matching import shared_key_suggestions
    a = store.new_org("Alpha Holdings, LLC")
    b = store.new_org("Alpha Lending dba Zentrix Cash")
    store.add_alias(a.id, "Zentrix Cash")
    store.add_alias(b.id, "Zentrix Cash")
    assert shared_key_suggestions(store) >= 1
    item = store.s.scalar(select(m.ReviewItem).where(m.ReviewItem.kind == "dedup",
                                                     m.ReviewItem.entity_ids.contains([a.id, b.id])))
    assert item is not None and "share the name zentrixcash" in item.payload["reasons"]
