// Ecosystem map: fetches /map/data with the current filters and draws it with Cytoscape (preset, layered layout).
// All text from the data is inserted with textContent, never as HTML.
(function () {
  const el = document.getElementById("cy");
  if (!el || typeof cytoscape === "undefined") return;
  const form = document.getElementById("map-filters");
  const detail = document.getElementById("map-detail");
  const stats = document.getElementById("map-stats");
  const css = getComputedStyle(document.documentElement);
  const v = (name) => css.getPropertyValue(name).trim();
  const C = {
    good: v("--good"), accent: v("--accent"), none: v("--line-strong"), ink: v("--ink"), muted: v("--muted"),
    warn: v("--warn"), bad: v("--bad"), panel: v("--panel"), line: v("--line"), bg: v("--bg"),
  };
  const ourColour = (s) => s === "self" ? C.ink : (s === "live" || s === "contracting") ? C.good
    : (s === "in_talks" || s === "paused") ? C.accent : C.none;
  const edgeColour = { sells_leads_to: C.muted, ours_sells_to: C.good, supplies_us: C.good,
    places_sponsored_content_on: C.warn, hosts_affiliate_program_for: C.accent, owns: C.ink, same_owner_as: C.ink };

  function params() {
    const p = new URLSearchParams(new FormData(form));
    for (const [k, val] of [...p.entries()]) if (!val) p.delete(k);
    return p;
  }
  function expandSet() {
    return new Set((form.elements.expand.value || "").split(",").filter(Boolean));
  }
  function setExpand(set) {
    form.elements.expand.value = [...set].join(",");
    history.replaceState(null, "", "/map?" + params().toString());
    load();
  }

  const cy = cytoscape({
    container: el, wheelSensitivity: 0.25, minZoom: 0.05, maxZoom: 3, boxSelectionEnabled: false,
    style: [
      { selector: "node", style: {
          "background-color": (n) => ourColour(n.data("our")), "width": "data(size)", "height": "data(size)",
          "label": "data(label)", "font-size": 11, "color": C.ink, "text-valign": "center", "text-halign": "right",
          "text-margin-x": 6, "text-max-width": 170, "text-wrap": "ellipsis", "min-zoomed-font-size": 7, "border-width": 1, "border-color": C.panel } },
      { selector: "node[compliance = 'review'], node[compliance = 'flags']", style: { "border-width": 4, "border-color": C.warn } },
      { selector: "node[compliance = 'flags']", style: { "border-style": "dashed" } },
      { selector: "node[compliance = 'blocked']", style: { "border-width": 4, "border-color": C.bad } },
      { selector: "node[kind = 'group']", style: { "shape": "round-rectangle", "font-weight": "bold" } },
      { selector: "node[kind = 'ours']", style: { "shape": "diamond", "color": C.ink, "font-weight": "bold", "font-size": 13 } },
      { selector: "node[kind = 'group_open']", style: {
          "shape": "round-rectangle", "background-color": C.bg, "background-opacity": 0.6, "border-width": 1,
          "border-style": "dashed", "border-color": C.muted, "text-valign": "top", "text-halign": "center",
          "font-weight": "bold", "padding": 14 } },
      { selector: "node.header", style: {
          "shape": "rectangle", "background-opacity": 0, "border-width": 0, "width": 1, "height": 1,
          "font-size": 15, "font-weight": "bold", "color": C.muted, "text-halign": "center", "text-valign": "center" } },
      { selector: "edge", style: {
          "width": (e) => 1 + Math.min(4, Math.log2(e.data("count") || 1)), "curve-style": "bezier",
          "line-color": (e) => edgeColour[e.data("type")] || C.muted, "target-arrow-shape": "triangle",
          "target-arrow-color": (e) => edgeColour[e.data("type")] || C.muted, "arrow-scale": 0.7, "opacity": 0.55 } },
      { selector: "edge[status = 'inferred']", style: { "line-style": "dashed" } },
      { selector: "edge[status = 'rumoured']", style: { "line-style": "dotted" } },
      { selector: ":selected", style: { "border-width": 4, "border-color": C.accent } },
      { selector: "edge.hl", style: { "opacity": 1, "width": 3 } },
      { selector: "node.dim, edge.dim", style: { "opacity": 0.15 } },
    ],
  });

  function el_(tag, text, cls) { const e = document.createElement(tag); if (text != null) e.textContent = text; if (cls) e.className = cls; return e; }

  function showDetail(n) {
    const d = n.data();
    detail.replaceChildren();
    detail.append(el_("h2", d.label));
    const facts = el_("p", null, "small");
    const bits = [];
    if (d.kind === "group") bits.push(`Owner group of ${d.count}`);
    if (d.kind === "group_open") bits.push(`Owner group of ${d.count}, open`);
    if (d.score != null) bits.push(`Score ${Math.round(d.score)}`);
    if (d.tier) bits.push(`Priority ${d.tier}`);
    if (d.group) bits.push(`Part of ${d.group}`);
    if (d.our && d.our !== "self") bits.push(`Ours: ${d.our.replace("_", " ")}${d.unconfirmed ? " (to confirm)" : ""}`);
    if (d.compliance) bits.push(`Compliance: ${d.compliance}`);
    facts.textContent = bits.join(" · ");
    detail.append(facts);
    const links = el_("p", null, "row");
    if (d.url) { const a = el_("a", d.kind === "org" ? "Open page" : "Open group page", "btn"); a.href = d.url; links.append(a); }
    if (d.kind === "group" || d.kind === "group_open") {
      const b = el_("button", d.kind === "group" ? "Show members" : "Fold group", "primary");
      b.type = "button";
      b.addEventListener("click", () => {
        const s = expandSet(); const id = String(d.org_id);
        d.kind === "group" ? s.add(id) : s.delete(id);
        setExpand(s);
      });
      links.append(b);
    }
    detail.append(links);
    const nb = n.connectedEdges();
    if (nb.length) {
      detail.append(el_("h3", `Links (${nb.length})`));
      const ul = el_("ul", null, "small");
      nb.slice(0, 25).forEach((e) => {
        const other = e.source().id() === n.id() ? e.target() : e.source();
        const dir = e.source().id() === n.id() ? "→" : "←";
        ul.append(el_("li", `${dir} ${other.data("label")} (${e.data("type").replaceAll("_", " ")}, ${e.data("status")}${e.data("count") > 1 ? ", ×" + e.data("count") : ""})`));
      });
      if (nb.length > 25) ul.append(el_("li", `… ${nb.length - 25} more`));
      detail.append(ul);
    }
  }

  cy.on("tap", "node", (evt) => {
    const n = evt.target;
    if (n.hasClass("header")) return;
    cy.elements().addClass("dim").removeClass("hl");
    n.closedNeighborhood().removeClass("dim");
    n.connectedEdges().addClass("hl");
    if (n.isParent()) n.descendants().removeClass("dim");
    showDetail(n);
  });
  cy.on("tap", (evt) => { if (evt.target === cy) cy.elements().removeClass("dim hl"); });
  cy.on("dbltap", "node", (evt) => { const u = evt.target.data("url"); if (u) window.location.href = u; });

  async function load() {
    stats.textContent = "Loading…";
    detail.replaceChildren(el_("p", "Click a node to see it here. Double-click opens its page.", "muted"));
    const r = await fetch("/map/data?" + params().toString(), { credentials: "same-origin" });
    if (!r.ok) { stats.textContent = "Could not load the map."; return; }
    const g = await r.json();
    const elements = [];
    g.layers.forEach((name, i) => elements.push({ group: "nodes", classes: "header", selectable: false, grabbable: false,
      data: { id: "h" + i, label: name }, position: { x: g.layer_x[i], y: -20 } }));
    g.nodes.forEach((n) => elements.push({ group: "nodes", data: n, position: n.x != null ? { x: n.x, y: n.y } : undefined }));
    g.edges.forEach((e) => elements.push({ group: "edges", data: e }));
    cy.elements().remove();
    cy.add(elements);
    cy.layout({ name: "preset", fit: true, padding: 40 }).run();
    readable();
    const s = g.stats;
    stats.textContent = `${s.organisations} organisations · ${s.edges} links` +
      (s.view === "focus" ? ` · focused view (${s.candidates} match the filters)` : "") +
      (s.truncated ? ` · ${s.truncated} more not shown, narrow the filters` : "");
  }
  // Start at a size where labels can be read, centred on our pingtree (or the top of the map).
  function readable() {
    if (cy.zoom() >= 0.7) return;
    const anchor = cy.getElementById("ours");
    cy.zoom(0.8);
    const pos = anchor && anchor.length ? anchor.position() : { x: 0, y: 0 };
    // our pingtree a third of the way across and near the top, so the headers and the busiest rows are in view
    cy.pan({ x: el.clientWidth / 3 - pos.x * 0.8, y: 70 - pos.y * 0.8 });
  }
  document.getElementById("map-fit").addEventListener("click", () => cy.fit(undefined, 40));
  document.getElementById("map-read").addEventListener("click", readable);
  document.getElementById("map-in").addEventListener("click", () => cy.zoom({ level: cy.zoom() * 1.4, renderedPosition: { x: el.clientWidth / 2, y: el.clientHeight / 2 } }));
  document.getElementById("map-out").addEventListener("click", () => cy.zoom({ level: cy.zoom() / 1.4, renderedPosition: { x: el.clientWidth / 2, y: el.clientHeight / 2 } }));
  form.addEventListener("submit", (e) => { e.preventDefault(); history.replaceState(null, "", "/map?" + params().toString()); load(); });
  load();
})();
