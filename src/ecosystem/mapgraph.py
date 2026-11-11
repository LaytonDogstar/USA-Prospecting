"""Graph data for the ecosystem map (brief §8.2).

Layers, left to right: traffic sources → connectors and lead brands → pingtrees and networks (ours included) →
lenders → servicers and bank partners. Owner groups (operator groups, tribes, any parent organisation) collapse
into one node and expand on request. The default view is a focused subset: A/B priority supply, well-scored
lenders, and everything one hop from our live relationships. The full set is available but dense.

Our own pingtree is drawn as a synthetic node whose edges come from `our_relationship`; nothing is stored.
"""
import math
from collections import defaultdict

from sqlalchemy import text

from .footprint import lender_footprints, serves
from .scoring import LENDER_ROLES, SUPPLY_ROLES

LAYERS = ["Traffic sources", "Connectors and lead brands", "Pingtrees and networks", "Lenders",
          "Servicers and bank partners"]
TRAFFIC_ROLES = {"comparison_offer_wall", "content_affiliate", "sponsored_publisher_agency", "affiliate_cpa_network",
                 "paid_media_platform", "fintech_decline_source"}
LIVE_LIKE = ("live", "contracting", "in_talks", "paused")
OURS_ID = "ours"
MAX_NODES = 700
COLUMN_HEIGHT = 22      # nodes per sub-column
SUBCOLUMN_WIDTH = 240   # room for a label
ROW_HEIGHT = 56
LAYER_GAP = 160
FOCUS_MIN_BUYER_VALUE = 60


def layer_for(roles: set[str], kind: str, sells_leads: bool) -> int:
    lender = roles & (set(LENDER_ROLES) - {"servicer"})
    if lender:
        return 3
    if "servicer" in roles or kind == "bank":
        return 4
    if "lead_network_pingtree" in roles:
        return 2
    if "connector_brand" in roles:
        return 1
    if roles & TRAFFIC_ROLES:
        return 0
    return 1 if sells_leads else 3


def _load(db):
    orgs = {}
    for r in db.execute(text("""
        SELECT o.id, o.display_name, o.kind, o.parent_id, o.tier, o.lifecycle,
               coalesce((SELECT array_agg(DISTINCT role) FROM org_role WHERE organisation_id = o.id), '{}') AS roles,
               coalesce((SELECT array_agg(DISTINCT tribe_id) FROM org_role WHERE organisation_id = o.id
                         AND tribe_id IS NOT NULL), '{}') AS tribes,
               coalesce((SELECT array_agg(DISTINCT state) FROM entity_state WHERE entity_id = o.id
                         AND basis IN ('lends','licensed')), '{}') AS states,
               bv.value AS buyer_value, sv.value AS supply_value,
               (SELECT max(value) FROM metric_snapshot WHERE entity_id = o.id AND metric = 'cfpb_complaints_12m') AS complaints,
               (SELECT max(value) FROM metric_snapshot WHERE entity_id = o.id AND metric = 'ahrefs_organic_visits') AS organic,
               ours.status AS our_status, ours.role AS our_role, ours.needs_confirmation,
               cs.value AS compliance,
               (SELECT count(*) FROM compliance_flag f WHERE f.entity_id = o.id) AS flags
        FROM organisation o
        LEFT JOIN latest_score bv ON bv.entity_id = o.id AND bv.score_type = 'buyer_value'
        LEFT JOIN latest_score sv ON sv.entity_id = o.id AND sv.score_type = 'supply_value'
        LEFT JOIN our_relationship ours ON ours.organisation_id = o.id
        LEFT JOIN compliance_status cs ON cs.entity_id = o.id
        WHERE o.lifecycle <> 'merged'""")):
        d = dict(r._mapping)
        d["roles"] = set(d["roles"])
        d["tribes"] = set(d["tribes"])
        d["states"] = set(d["states"])
        for k in ("buyer_value", "supply_value", "complaints", "organic"):
            d[k] = float(d[k]) if d[k] is not None else None
        orgs[d["id"]] = d
    edges = [dict(r._mapping) for r in db.execute(text(
        "SELECT id, from_id, to_id, type, status, claim_ids FROM relationship"))]
    return orgs, edges


def build_graph(db, f: dict | None = None, expand: set[int] | None = None) -> dict:
    f = f or {}
    expand = expand or set()
    orgs, edges = _load(db)
    footprints = lender_footprints(db) if f.get("state") else {}
    sellers = {e["from_id"] for e in edges if e["type"] == "sells_leads_to"}
    for o in orgs.values():
        o["layer"] = layer_for(o["roles"], o["kind"], o["id"] in sellers)
        o["score"] = o["buyer_value"] if o["layer"] == 3 else (o["supply_value"] or o["buyer_value"])

    # ---- filters
    def keep(o) -> bool:
        if o["lifecycle"] == "screened_out" and not f.get("screened"):
            return False
        comp = f.get("compliance") or "hide_blocked"
        if comp == "hide_blocked" and o["compliance"] == "blocked":
            return False
        if comp in ("clear", "review", "blocked") and o["compliance"] != comp:
            return False
        if comp == "flagged" and not o["flags"]:
            return False
        if f.get("role") and f["role"] not in o["roles"]:
            return False
        if f.get("tribe"):
            t = int(f["tribe"]) if str(f["tribe"]).isdigit() else -1
            if t not in o["tribes"] and o["parent_id"] != t and o["id"] != t:
                return False
        if f.get("state") and o["layer"] == 3:
            fp = footprints.get(o["id"])
            if not fp or not serves(fp, f["state"].upper()):
                return False
        our = f.get("our")
        if our == "none" and o["our_status"]:
            return False
        if our == "any" and not o["our_status"]:
            return False
        if our and our not in ("none", "any") and o["our_status"] != our:
            return False
        if f.get("min_score"):
            try:
                if (o["score"] or 0) < float(f["min_score"]):
                    return False
            except ValueError:
                pass
        return True

    candidates = {i for i, o in orgs.items() if keep(o) and o["kind"] not in ("operator_group", "tribe")}

    # ---- focused default view
    view = f.get("view") or "focus"
    if view == "focus":
        ours = {i for i in candidates if orgs[i]["our_status"] in LIVE_LIKE}
        seed = ours | {i for i in candidates if orgs[i]["tier"] in ("A", "B")} | {
            i for i in candidates if orgs[i]["layer"] == 3 and (orgs[i]["buyer_value"] or 0) >= FOCUS_MIN_BUYER_VALUE}
        hop = set()
        for e in edges:
            if e["type"] == "owns":
                continue
            if e["from_id"] in ours and e["to_id"] in candidates:
                hop.add(e["to_id"])
            if e["to_id"] in ours and e["from_id"] in candidates:
                hop.add(e["from_id"])
        visible = seed | hop
    else:
        visible = set(candidates)

    truncated = 0
    if len(visible) > MAX_NODES:
        ranked = sorted(visible, key=lambda i: (orgs[i]["our_status"] is None, -(orgs[i]["score"] or 0)))
        truncated = len(visible) - MAX_NODES
        visible = set(ranked[:MAX_NODES])

    # ---- owner groups: a visible member's parent becomes its group
    def group_of(i):
        p = orgs[i]["parent_id"]
        return p if p in orgs and orgs[p]["lifecycle"] != "merged" else None

    # every member of a group that passes the filters, so a group shows its full size and expands completely
    all_members = defaultdict(set)
    for i in candidates:
        g = group_of(i)
        if g is not None:
            all_members[g].add(i)
    for g in expand:
        visible |= all_members.get(g, set())
    members = defaultdict(set)
    for i in visible:
        g = group_of(i)
        if g is not None:
            members[g].add(i)
    # Only groups of two or more fold: folding a group of one would just hide that company behind its owner's name.
    totals = {g: len(all_members.get(g, set()) | ({g} if g in candidates else set())) for g in members}
    folded = {g for g in members if totals[g] >= 2 and g not in expand}
    # a group that is itself a visible company (e.g. a network owning brands) still groups its members
    node_of = {}
    for i in visible:
        g = group_of(i)
        node_of[i] = f"g{g}" if g in folded else f"o{i}"
    for g in folded:
        if g in visible:
            node_of[g] = f"g{g}"

    # ---- size scale
    top_c = max([o["complaints"] or 0 for o in orgs.values()] + [1])
    top_o = max([o["organic"] or 0 for o in orgs.values()] + [1])

    def size(o) -> float:
        s = max(math.log10(1 + (o["complaints"] or 0)) / math.log10(1 + top_c),
                math.log10(1 + (o["organic"] or 0)) / math.log10(1 + top_o))
        return round(18 + 42 * s, 1)

    rank = {"live": 0, "contracting": 1, "in_talks": 2, "paused": 3, "former": 4, "prospect": 5, "parked": 6, "dropped": 7}

    nodes = {}
    for i in visible:
        nid = node_of[i]
        if nid.startswith("g"):
            continue
        o = orgs[i]
        g = group_of(i)
        nodes[nid] = {
            "id": nid, "org_id": i, "label": o["display_name"], "kind": "org", "layer": o["layer"],
            "size": size(o), "our": o["our_status"], "our_role": o["our_role"], "unconfirmed": bool(o["needs_confirmation"]),
            "compliance": o["compliance"] or ("flags" if o["flags"] else None), "score": o["score"],
            "tier": o["tier"], "url": f"/org/{i}", **({"parent": f"x{g}"} if g in expand and g is not None else {}),
            **({"group": orgs[g]["display_name"]} if g is not None else {}),
        }
    for g, ms in members.items():
        group_members = ms | ({g} if g in visible else set())
        total = totals[g]
        if g not in folded and g not in expand:
            continue  # a group of one: its member is drawn as itself
        if g in expand:
            nodes[f"x{g}"] = {"id": f"x{g}", "org_id": g, "label": f"{orgs[g]['display_name']} group", "kind": "group_open",
                              "count": len(group_members), "url": f"/org/{g}", "layer": None}
            if g in visible:  # the group company itself sits inside its own box
                nodes[f"o{g}"]["parent"] = f"x{g}"
            continue
        ms_o = [orgs[x] for x in group_members]
        layers = [x["layer"] for x in ms_o]
        statuses = [x["our_status"] for x in ms_o if x["our_status"]]
        comps = [x["compliance"] for x in ms_o if x["compliance"]]
        nodes[f"g{g}"] = {
            "id": f"g{g}", "org_id": g, "label": f"{orgs[g]['display_name']} ({total})", "kind": "group",
            "layer": max(set(layers), key=layers.count), "count": total,
            "size": round(min(70, max(size(x) for x in ms_o) + 4 * math.log2(1 + len(group_members))), 1),
            "our": min(statuses, key=lambda s: rank.get(s, 9)) if statuses else None,
            "compliance": "blocked" if "blocked" in comps else "review" if "review" in comps else
            ("flags" if any(x["flags"] for x in ms_o) else None),
            "score": max([x["score"] or 0 for x in ms_o]), "url": f"/org/{g}", "expandable": True,
        }

    # ---- edges between visible nodes (parallel edges merged, containment hidden)
    agg = {}
    for e in edges:
        a, b = node_of.get(e["from_id"]), node_of.get(e["to_id"])
        if not a or not b or a == b:
            continue
        if e["type"] == "owns" and (b == f"o{e['to_id']}" and nodes.get(b, {}).get("parent") == f"x{e['from_id']}"):
            continue
        key = (a, b, e["type"])
        x = agg.setdefault(key, {"id": f"e{len(agg)}", "source": a, "target": b, "type": e["type"],
                                 "status": e["status"], "count": 0, "claim_ids": []})
        x["count"] += 1
        x["claim_ids"] = (x["claim_ids"] + list(e["claim_ids"] or []))[:50]
        if e["status"] == "confirmed":
            x["status"] = "confirmed"

    # ---- our pingtree, from our_relationship only
    ours_edges = []
    for i in visible:
        o = orgs[i]
        if o["our_status"] not in LIVE_LIKE:
            continue
        nid = node_of[i]
        if o["our_role"] in ("buyer", "network", "both"):
            ours_edges.append((OURS_ID, nid, "ours_sells_to"))
        if o["our_role"] in ("supplier", "both"):
            ours_edges.append((nid, OURS_ID, "supplies_us"))
    if ours_edges or view == "focus":
        nodes[OURS_ID] = {"id": OURS_ID, "label": "Our pingtree (CLE)", "kind": "ours", "layer": 2, "size": 64,
                          "our": "self", "url": None}
        for a, b, t in ours_edges:
            key = (a, b, t)
            if key not in agg:
                agg[key] = {"id": f"e{len(agg)}", "source": a, "target": b, "type": t, "status": "confirmed",
                            "count": 1, "claim_ids": []}

    # ---- positions: each layer is a band of short columns (long single columns shrink the whole map to nothing)
    columns = defaultdict(list)
    for n in nodes.values():
        if n["kind"] == "group_open":
            continue
        layer = n["layer"]
        if n.get("parent"):
            layer = nodes[n["parent"]].setdefault("layer_hint", n["layer"])
        columns[layer].append(n)
    x0 = 0.0
    header_x = {}
    for layer in range(len(LAYERS)):
        col = columns.get(layer, [])
        col.sort(key=lambda n: (n["kind"] != "ours", n["our"] is None, -(n.get("score") or 0), n["label"]))
        # regular nodes fill short columns; each open group gets its own column(s) so its box covers only its members
        blocks = [[n for n in col if not n.get("parent")]]
        for parent in sorted({n["parent"] for n in col if n.get("parent")}):
            blocks.append([n for n in col if n.get("parent") == parent])
        start = x0
        c_total = 0
        for block in blocks:
            for k, n in enumerate(block):
                c, r = divmod(k, COLUMN_HEIGHT)
                n["x"], n["y"] = x0 + (c_total + c) * SUBCOLUMN_WIDTH, 40 + r * ROW_HEIGHT
            if block:
                c_total += math.ceil(len(block) / COLUMN_HEIGHT)
        c_total = max(c_total, 1)
        header_x[layer] = start + (c_total - 1) * SUBCOLUMN_WIDTH / 2
        x0 += c_total * SUBCOLUMN_WIDTH + LAYER_GAP

    return {
        "nodes": list(nodes.values()), "edges": list(agg.values()), "layers": LAYERS,
        "layer_x": [header_x[i] for i in range(len(LAYERS))],
        "stats": {"organisations": len(visible), "nodes": len(nodes), "edges": len(agg), "view": view,
                  "truncated": truncated, "candidates": len(candidates)},
    }
