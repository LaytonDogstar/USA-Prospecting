"""Ecosystem map and state map on made-up data."""
from sqlalchemy import select

from ecosystem import models as m
from ecosystem.footprint import serves, state_summary
from ecosystem.importers import import_owner_groups, import_relationship_seed, import_targets, import_traffic
from ecosystem.mapgraph import LAYERS, build_graph, layer_for
from ecosystem.matching import possible_match_sweep
from ecosystem.scoring import rescore


def _load_all(store, targets_file, traffic_file, owner_groups_file, seed_file):
    import_targets(store, targets_file)
    import_traffic(store, traffic_file)
    import_owner_groups(store, owner_groups_file)
    import_relationship_seed(store, seed_file)
    possible_match_sweep(store)
    rescore(store)
    store.s.flush()


def org_id(s, name):
    return s.scalar(select(m.Organisation.id).where(m.Organisation.display_name == name, m.Organisation.lifecycle != "merged"))


def test_layers():
    assert layer_for({"comparison_offer_wall"}, "company", False) == 0
    assert layer_for({"connector_brand"}, "company", False) == 1
    assert layer_for({"lead_network_pingtree"}, "company", False) == 2
    assert layer_for({"lender_tribal", "servicer"}, "company", False) == 3
    assert layer_for({"servicer"}, "company", False) == 4
    assert layer_for(set(), "company", True) == 1  # a site that sells leads but has no researched role
    assert layer_for(set(), "company", False) == 3


def test_graph_focus_groups_and_our_pingtree(store, targets_file, traffic_file, owner_groups_file, seed_file):
    _load_all(store, targets_file, traffic_file, owner_groups_file, seed_file)
    s = store.s
    g = build_graph(s, {"view": "all"})
    by_id = {n["id"]: n for n in g["nodes"]}
    assert g["layers"] == LAYERS and len(g["layer_x"]) == len(LAYERS)
    # the owner group is one collapsed node showing its full size
    acme = org_id(s, "Acme Ventures")
    grp = by_id[f"g{acme}"]
    assert grp["kind"] == "group" and grp["count"] == 6 and grp["label"] == "Acme Ventures (6)"
    assert grp["brands"] == 5 and grp["owner_listed"] is True  # shown as "owner plus 5 brands"
    assert "Rocket Bucks" in grp["members"]  # searchable while folded
    assert f"o{org_id(s, 'Rocket Bucks')}" not in by_id
    # our pingtree node is drawn from our_relationship only
    assert by_id["ours"]["kind"] == "ours"
    zephyr = f"o{org_id(s, 'Zephyr Loans')}"
    assert any(e["source"] == "ours" and e["target"] == zephyr and e["type"] == "ours_sells_to" for e in g["edges"])
    assert by_id[zephyr]["our"] == "live"
    # partner-list edge from the list site to the lender is kept, and lands on the collapsed group when relevant
    prairie = f"o{org_id(s, 'Prairie Holdings, LLC')}"
    assert any(e["target"] == prairie and e["type"] == "sells_leads_to" and e["status"] == "inferred" for e in g["edges"])
    # every edge joins two drawn nodes
    assert all(e["source"] in by_id and e["target"] in by_id for e in g["edges"])

    # expanding the group shows all its members inside a box, and hides the containment edges
    g2 = build_graph(s, {"view": "all"}, expand={acme})
    by_id2 = {n["id"]: n for n in g2["nodes"]}
    inside = sorted(n["label"] for n in g2["nodes"] if n.get("parent") == f"x{acme}")
    assert inside == ["Acme Ventures", "Comet Cash", "Jet Lend", "Nova Funds", "Orbit Money", "Rocket Bucks"]
    assert not any(e["type"] == "owns" and e["source"] == f"o{acme}" for e in g2["edges"])
    assert all(e["source"] in by_id2 and e["target"] in by_id2 for e in g2["edges"])


def test_graph_filters_and_compliance_gate(store, targets_file, traffic_file, owner_groups_file, seed_file):
    _load_all(store, targets_file, traffic_file, owner_groups_file, seed_file)
    s = store.s
    prairie = org_id(s, "Prairie Holdings, LLC")
    # nodes carry the trading brand as label and the legal name separately
    labels = lambda f: {n.get("legal") or n["label"] for n in build_graph(s, f)["nodes"]}
    node = next(n for n in build_graph(s, {"view": "all"})["nodes"] if n.get("org_id") == prairie)
    assert node["label"] == "Sunrise Lending" and node["legal"] == "Prairie Holdings, LLC"
    assert node["pending_match"] is True  # open possible match with the buyer we contract with
    assert "Prairie Holdings, LLC" in labels({"view": "all", "role": "lender_tribal"})
    assert "Zephyr Loans" not in labels({"view": "all", "role": "lender_tribal"})
    assert "Zephyr Loans" in labels({"view": "all", "our": "live"})
    s.add(m.ComplianceStatus(entity_id=prairie, value="blocked", set_by="test", note="x"))
    s.flush()
    assert "Prairie Holdings, LLC" not in labels({"view": "all"})
    assert "Prairie Holdings, LLC" in labels({"view": "all", "compliance": "blocked"})
    # Prairie lists 'Not available in NY, PA, VT': it serves Texas but not New York
    s.query(m.ComplianceStatus).delete()
    s.flush()
    assert "Prairie Holdings, LLC" in labels({"view": "all", "state": "TX"})
    assert "Prairie Holdings, LLC" not in labels({"view": "all", "state": "NY"})


def test_state_summary(store, targets_file, traffic_file, owner_groups_file, seed_file):
    _load_all(store, targets_file, traffic_file, owner_groups_file, seed_file)
    s = store.s
    summ = state_summary(s)
    assert summ["states"]["TX"]["total"] == 1 and summ["states"]["TX"]["by_type"] == {"lender_tribal": 1}
    assert summ["states"]["NY"]["total"] == 0
    assert summ["states"]["TX"]["by_basis"] == {"not excluded": 1}
    # our live buyer has no state data: counted, but as unknown footprint, never guessed
    assert summ["our_buyers"] >= 1 and summ["our_buyers_unknown"] == summ["our_buyers"]
    assert serves({"explicit": {"TX"}, "nationwide": False, "excluded": set()}, "TX") == "listed"
    assert serves({"explicit": set(), "nationwide": True, "excluded": set()}, "OH") == "nationwide"
    assert serves({"explicit": set(), "nationwide": False, "excluded": set()}, "OH") is None


def test_empty_layers_have_no_header(store, targets_file, traffic_file, owner_groups_file, seed_file):
    _load_all(store, targets_file, traffic_file, owner_groups_file, seed_file)
    g = build_graph(store.s, {"view": "all"})
    used = {n["layer"] for n in g["nodes"] if n.get("layer") is not None}
    for i, x in enumerate(g["layer_x"]):
        assert (x is None) == (i not in used)


def test_open_groups_are_reported(store, targets_file, traffic_file, owner_groups_file, seed_file):
    _load_all(store, targets_file, traffic_file, owner_groups_file, seed_file)
    acme = org_id(store.s, "Acme Ventures")
    g = build_graph(store.s, {"view": "all"}, expand={acme})
    assert g["stats"]["open_groups"] == [{"org_id": acme, "label": "Acme Ventures"}]
