// Ecosystem map: fetches /map/data with the current filters and draws it with Cytoscape (preset, layered layout).
// All text from the data is inserted with textContent, never as HTML.
(function () {
  const el = document.getElementById("cy");
  if (!el || typeof cytoscape === "undefined") return;
  const form = document.getElementById("map-filters");
  const detail = document.getElementById("map-detail");
  const openBox = document.getElementById("map-open");
  const stats = document.getElementById("map-stats");
  const find = document.getElementById("map-find");
  const names = document.getElementById("map-names");
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
  const COMPLIANCE_TEXT = { blocked: "Blocked", review: "Compliance review", clear: "Checked: clear",
    flags: "Flags found, not yet reviewed" };

  function el_(tag, text, cls) { const e = document.createElement(tag); if (text != null) e.textContent = text; if (cls) e.className = cls; return e; }
  function params() {
    const p = new URLSearchParams(new FormData(form));
    for (const [k, val] of [...p.entries()]) if (!val) p.delete(k);
    return p;
  }
  function expandSet() { return new Set((form.elements.expand.value || "").split(",").filter(Boolean)); }
  function setExpand(set) {
    form.elements.expand.value = [...set].join(",");
    history.replaceState(null, "", "/map?" + params().toString());
    load();
  }

  const cy = cytoscape({
    container: el, minZoom: 0.05, maxZoom: 3, boxSelectionEnabled: false,
    style: [
      { selector: "node", style: {
          "background-color": (n) => ourColour(n.data("our")), "width": "data(size)", "height": "data(size)",
          "label": "data(label)", "font-size": 13, "color": C.ink, "text-valign": "center", "text-halign": "right",
          "text-margin-x": 6, "text-max-width": 200, "text-wrap": "ellipsis", "min-zoomed-font-size": 6,
          "text-outline-color": C.panel, "text-outline-width": 2, "border-width": 1, "border-color": C.panel } },
      { selector: "node[compliance = 'clear']", style: { "border-width": 3, "border-color": C.good } },
      { selector: "node[compliance = 'review'], node[compliance = 'flags']", style: { "border-width": 4, "border-color": C.warn } },
      { selector: "node[compliance = 'flags']", style: { "border-style": "dashed" } },
      { selector: "node[compliance = 'blocked']", style: { "border-width": 4, "border-color": C.bad } },
      { selector: "node[?pending_match]", style: { "border-width": 5, "border-style": "double", "border-color": C.accent } },
      { selector: "node[kind = 'group']", style: { "shape": "round-rectangle", "font-weight": "bold" } },
      { selector: "node[kind = 'ours']", style: { "shape": "diamond", "color": C.ink, "font-weight": "bold", "font-size": 15 } },
      { selector: "node[kind = 'group_open']", style: {
          "shape": "round-rectangle", "background-color": C.bg, "background-opacity": 0.6, "border-width": 1,
          "border-style": "dashed", "border-color": C.muted, "text-valign": "top", "text-halign": "center",
          "font-weight": "bold", "padding": 14 } },
      { selector: "node.header", style: {
          "shape": "rectangle", "background-opacity": 0, "border-width": 0, "width": 1, "height": 1,
          "font-size": 17, "font-weight": "bold", "color": C.muted, "text-halign": "right", "text-valign": "center",
          "text-margin-x": 0, "text-max-width": 400, "min-zoomed-font-size": 0 } },
      { selector: "edge", style: {
          "width": (e) => 1 + Math.min(4, Math.log2(e.data("count") || 1)), "curve-style": "bezier",
          "line-color": (e) => edgeColour[e.data("type")] || C.muted, "target-arrow-shape": "triangle",
          "target-arrow-color": (e) => edgeColour[e.data("type")] || C.muted, "arrow-scale": 0.7, "opacity": 0.5 } },
      { selector: "edge[status = 'inferred']", style: { "line-style": "dashed" } },
      { selector: "edge[status = 'rumoured']", style: { "line-style": "dotted" } },
      { selector: ":selected", style: { "border-width": 5, "border-color": C.accent, "border-style": "solid" } },
      { selector: "edge.hl", style: { "opacity": 1, "width": 3 } },
      { selector: "node.dim, edge.dim", style: { "opacity": 0.12 } },
      { selector: "node.faded, edge.faded", style: { "opacity": 0.2 } },
    ],
  });

  // ---- side panel
  function showDetail(n) {
    const d = n.data();
    detail.replaceChildren();
    detail.append(el_("h2", d.label));
    if (d.legal) detail.append(el_("p", d.legal, "small muted"));
    if (d.pending_match) {
      const w = el_("p", "May already be a partner: an open possible match with one of our relationships. Check Review before contacting them.", "warnbox small");
      detail.append(w);
    }
    const bits = [];
    if (d.kind === "group" || d.kind === "group_open") {
      bits.push(d.owner_listed ? `Owner plus ${d.brands} brand${d.brands === 1 ? "" : "s"}` : `${d.brands} brand${d.brands === 1 ? "" : "s"}`);
    }
    if (d.score != null) bits.push(`Score ${Math.round(d.score)}`);
    if (d.tier) bits.push(`Priority ${d.tier}`);
    if (d.group) bits.push(`Part of ${d.group}`);
    if (d.our && d.our !== "self") bits.push(`Ours: ${d.our.replace("_", " ")}${d.unconfirmed ? " (to confirm)" : ""}`);
    if (d.kind !== "ours" && d.kind !== "group_open") bits.push(COMPLIANCE_TEXT[d.compliance] || "Compliance not checked yet");
    detail.append(el_("p", bits.join(" · "), "small"));
    if (d.members && d.members.length) {
      detail.append(el_("p", "Members: " + d.members.join(", "), "small muted"));
    }
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
      const ul = el_("ul", null, "small links");
      nb.forEach((e) => {
        const other = e.source().id() === n.id() ? e.target() : e.source();
        const dir = e.source().id() === n.id() ? "→" : "←";
        const li = el_("li");
        const a = el_("a", other.data("label")); a.href = "#";
        a.addEventListener("click", (ev) => { ev.preventDefault(); select(other); });
        li.append(dir + " ", a, ` (${e.data("type").replaceAll("_", " ")}, ${e.data("status")}${e.data("count") > 1 ? ", ×" + e.data("count") : ""})`);
        ul.append(li);
      });
      detail.append(ul);
    } else if (d.kind === "org") {
      detail.append(el_("p", "No links recorded for this organisation yet.", "small muted"));
    }
  }

  function showOpenGroups(list) {
    openBox.replaceChildren();
    if (!list || !list.length) return;
    openBox.append(el_("h3", "Open groups"));
    const p = el_("p", null, "row");
    list.forEach((g) => {
      const b = el_("button", `Fold ${g.label}`); b.type = "button";
      b.addEventListener("click", () => { const s = expandSet(); s.delete(String(g.org_id)); setExpand(s); });
      p.append(b);
    });
    openBox.append(p);
  }

  function select(n) {
    cy.elements().addClass("dim").removeClass("hl");
    n.closedNeighborhood().removeClass("dim");
    n.connectedEdges().addClass("hl");
    if (n.isParent()) n.descendants().removeClass("dim");
    if (n.isChild()) n.parent().removeClass("dim");
    cy.$(":selected").unselect(); n.select();
    showDetail(n);
  }

  cy.on("tap", "node", (evt) => { if (!evt.target.hasClass("header")) select(evt.target); });
  cy.on("tap", (evt) => { if (evt.target === cy) { cy.elements().removeClass("dim hl"); applyHighlight(); } });
  cy.on("dbltap", "node", (evt) => { const u = evt.target.data("url"); if (u) window.location.href = u; });

  // ---- highlight overlay: fade everything except what's asked for, keeping the context on screen
  function applyHighlight() {
    const h = form.elements.highlight.value;
    cy.elements().removeClass("faded");
    if (!h) return;
    const keep = h === "compliance"
      ? cy.nodes().filter((n) => n.data("compliance") && n.data("kind") !== "group_open")
      : cy.nodes().filter((n) => n.data("pending_match"));
    cy.elements().not(keep).not(keep.connectedEdges()).not(".header").addClass("faded");
  }

  // ---- views
  function bounds() { return cy.nodes().not(".header").boundingBox(); }
  function startView() {
    // Fit everything; if that makes labels unreadable, keep a readable zoom anchored at the top left so the
    // flow starts on screen (traffic sources first) and you scroll right along it.
    cy.fit(cy.nodes().not(".header"), 40);
    if (cy.zoom() < 0.5) readable(0.55);
  }
  function readable(z) {
    const bb = bounds();
    const zoom = typeof z === "number" ? z : 0.85;
    cy.zoom(zoom);
    cy.pan({ x: 30 - bb.x1 * zoom, y: 60 - bb.y1 * zoom });
  }
  document.getElementById("map-fit").addEventListener("click", () => cy.fit(cy.nodes().not(".header"), 40));
  document.getElementById("map-read").addEventListener("click", () => readable(0.85));
  const centre = () => ({ x: el.clientWidth / 2, y: el.clientHeight / 2 });
  document.getElementById("map-in").addEventListener("click", () => cy.zoom({ level: cy.zoom() * 1.4, renderedPosition: centre() }));
  document.getElementById("map-out").addEventListener("click", () => cy.zoom({ level: cy.zoom() / 1.4, renderedPosition: centre() }));

  // ---- search
  let index = [];
  function findAndShow(q) {
    q = (q || "").trim().toLowerCase();
    if (!q) return;
    const hit = index.find((x) => x.name.toLowerCase() === q) || index.find((x) => x.name.toLowerCase().includes(q));
    if (!hit) { stats.textContent = `"${find.value}" isn't on the map with these filters. Try View: Everything or Our relationship: Any.`; return; }
    const n = cy.getElementById(hit.node);
    if (cy.zoom() < 0.8) cy.zoom(0.9);
    cy.center(n);
    select(n);
    if (hit.inGroup) detail.prepend(el_("p", `${hit.name} is inside this group. Use "Show members" to open it.`, "small"));
  }
  // Search on Enter, or when a suggestion is picked from the list. Not on 'change': that fires again when the box
  // loses focus (e.g. clicking a button in the side panel) and would redraw the panel under the click.
  find.addEventListener("input", (e) => {
    if (e.inputType === "insertReplacementText" || index.some((x) => x.name === find.value)) findAndShow(find.value);
  });
  find.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); findAndShow(find.value); } });

  async function load() {
    stats.textContent = "Loading…";
    detail.replaceChildren(el_("p", "Click a node to see it here. Double-click opens its page.", "muted"));
    const r = await fetch("/map/data?" + params().toString(), { credentials: "same-origin" });
    if (!r.ok) { stats.textContent = "Could not load the map."; return; }
    const g = await r.json();
    const elements = [];
    g.layers.forEach((name, i) => {
      if (g.layer_x[i] == null) return;
      elements.push({ group: "nodes", classes: "header", selectable: false, grabbable: false,
        data: { id: "h" + i, label: name }, position: { x: g.layer_x[i] - 12, y: 0 } });
    });
    g.nodes.forEach((n) => elements.push({ group: "nodes", data: n, position: n.x != null ? { x: n.x, y: n.y } : undefined }));
    g.edges.forEach((e) => elements.push({ group: "edges", data: e }));
    cy.elements().remove();
    cy.add(elements);
    cy.layout({ name: "preset", fit: false }).run();
    startView();
    applyHighlight();
    // search index: every node, plus members folded inside groups
    index = [];
    g.nodes.forEach((n) => {
      if (n.kind === "org" || n.kind === "group" || n.kind === "ours") index.push({ name: n.label.replace(/ \(\d+\)$/, ""), node: n.id });
      if (n.kind === "group") (n.members || []).forEach((m) => index.push({ name: m, node: n.id, inGroup: true }));
    });
    names.replaceChildren(...index.map((x) => { const o = document.createElement("option"); o.value = x.name; return o; }));
    showOpenGroups(g.stats.open_groups);
    const s = g.stats;
    stats.textContent = `${s.organisations} organisations · ${s.edges} links` +
      (s.view === "focus" ? ` · focused view of ${s.candidates} that match the filters` : "") +
      (s.truncated ? ` · showing the ${s.organisations} highest-scored (our relationships first); ${s.truncated} more hidden, narrow the filters` : "");
  }
  form.addEventListener("submit", (e) => { e.preventDefault(); history.replaceState(null, "", "/map?" + params().toString()); load(); });
  form.elements.highlight.addEventListener("change", () => { history.replaceState(null, "", "/map?" + params().toString()); applyHighlight(); });
  load();
})();
