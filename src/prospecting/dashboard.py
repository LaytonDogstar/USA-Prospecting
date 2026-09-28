"""Self-contained interactive HTML dashboard of scored prospects.

    python -m prospecting dashboard   ->  output/prospect_dashboard_<date>.html

Everything (data, styles, script) is inlined so the file opens offline and can be emailed.
It contains the same data as the targets workbook plus a client benchmark, so treat it as confidential.
"""
import json
import sqlite3
from datetime import date
from pathlib import Path

from .profiles import TIERED_CATEGORIES, build_profiles
from .research import research_by_buyer
from .scoring import score

GROUPS = {  # scoring type_key -> dashboard setup group (3 colour slots + neutral)
    "operator": "Tribal / operator", "tribal": "Tribal / operator",
    "bank-partner": "Bank-partner / fintech", "other": "Bank-partner / fintech",
    "state-licensed": "State-licensed / branch", "branch": "State-licensed / branch",
}


def _channel(text: str | None) -> str:
    t = (text or "").lower()
    online = "online" in t or "app" in t or "web" in t
    branch = any(w in t for w in ("branch", "store", "storefront", "both", "location"))
    if online and branch:
        return "Online + branch"
    if online:
        return "Online"
    if branch:
        return "Branch"
    return "Unknown"


def _targets(conn) -> list[dict]:
    out = []
    for i, t in enumerate([t for t in score(conn) if t["researched"]], 1):
        identified = bool(t.get("r_company_type"))
        out.append({
            "rank": i,
            "company": t["company"],
            "website": t.get("website"),
            "score": t["score"],
            "fit": t.get("fit") or "",
            "group": GROUPS.get(t["type_key"], "Bank-partner / fintech") if identified else "Unidentified",
            "type": (t.get("r_company_type") or "").split(";")[0].strip(),
            "operator": t.get("operator_group") or "",
            "complaints": t["complaints_total"],
            "installment": int(t.get("complaints_installment_loan", 0) or 0),
            "payday": int(t.get("complaints_payday_loan", 0) or 0),
            "loc": int(t.get("complaints_personal_line_of_credit", 0) or 0),
            "channel": _channel(t.get("r_storefront_or_online")),
            "lead": t.get("r_lead_buying_signals") or "",
            "litigation": t.get("r_litigation_or_regulatory") or "",
            "brands": t.get("r_consumer_brands") or "",
            "parent": t.get("r_parent_or_servicer") or "",
            "tribe": t.get("r_tribe") or "",
            "products": t.get("r_products") or "",
            "loans": t.get("r_loan_amount_range") or "",
            "states": t.get("r_states_served") or "",
            "segment": t.get("r_customer_segment") or "",
            "roles": t["roles"],
            "why": t["why"],
            "confidence": t.get("r_research_confidence") or "",
            "sources": sorted(t["sources"])[:8],
            "parts": {k: t[f"score_{k}"] for k in ("size", "type", "online", "lead_signals", "segment")},
        })
    return out


def _benchmark(conn) -> list[dict]:
    research = research_by_buyer(conn)
    groups: dict[str, dict] = {}
    for p in build_profiles(conn):
        if p["category"] not in TIERED_CATEGORIES or not p["revenue"]:
            continue
        t = (research.get(p["buyer"], {}).get("r_company_type") or "unknown").split(";")[0].strip().lower()
        key = ("Servicer / operator" if "servicer" in t or "platform" in t else "Tribal lender brand" if "tribal" in t
               else "State-licensed / CAB" if "state" in t or "cab" in t else
               "Bank-partner / fintech" if "bank" in t or "fintech" in t else "Not identified")
        g = groups.setdefault(key, {"group": key, "buyers": 0, "revenue": 0.0, "leads": 0})
        g["buyers"] += 1
        g["revenue"] += p["revenue"]
        g["leads"] += p["accepted"]
    return sorted(groups.values(), key=lambda g: -g["revenue"])


def write_dashboard(conn: sqlite3.Connection, path: Path) -> dict:
    data = {"asof": date.today().isoformat(), "targets": _targets(conn), "benchmark": _benchmark(conn)}
    payload = json.dumps(data).replace("</", "<\\/")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TEMPLATE.replace("__DATA__", payload), encoding="utf-8")
    return {"targets": len(data["targets"])}


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Prospect Dashboard</title>
<style>
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --ring: rgba(11,11,11,0.10);
  --s1: #2a78d6; --s2: #eb6834; --s3: #1baf7a; --neutral: #898781;
  --seq: #2a78d6; --warn: #fab219; --critical: #d03b3b; --good: #006300;
  --hover: rgba(11,11,11,0.05); --chip: #f0efec;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --ring: rgba(255,255,255,0.10);
    --s1: #3987e5; --s2: #d95926; --s3: #199e70; --neutral: #898781;
    --seq: #3987e5; --good: #0ca30c; --hover: rgba(255,255,255,0.06); --chip: #2c2c2a;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
  --grid: #2c2c2a; --axis: #383835; --ring: rgba(255,255,255,0.10);
  --s1: #3987e5; --s2: #d95926; --s3: #199e70; --neutral: #898781;
  --seq: #3987e5; --good: #0ca30c; --hover: rgba(255,255,255,0.06); --chip: #2c2c2a;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--ink);
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
.wrap { max-width: 1280px; margin: 0 auto; padding: 24px 16px 48px; }
header { display: flex; flex-wrap: wrap; gap: 8px 16px; align-items: baseline; justify-content: space-between; }
h1 { font-size: 22px; margin: 0; }
h2 { font-size: 15px; margin: 0 0 4px; }
.sub { color: var(--ink-2); margin: 4px 0 0; }
.note { color: var(--muted); font-size: 12px; }
.theme { background: none; border: 1px solid var(--ring); color: var(--ink-2); border-radius: 6px; padding: 4px 10px; cursor: pointer; }
.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin: 20px 0; }
.card { background: var(--surface); border: 1px solid var(--ring); border-radius: 10px; padding: 14px 16px; min-width: 0; }
.kpi .v { font-size: 28px; font-weight: 600; margin-top: 2px; }
.kpi .l { color: var(--ink-2); font-size: 13px; }
.filters { display: flex; flex-wrap: wrap; gap: 8px 12px; align-items: center; margin: 0 0 16px; }
.filters label { color: var(--ink-2); font-size: 13px; display: flex; gap: 6px; align-items: center; }
select, input[type=search] { font: inherit; color: var(--ink); background: var(--surface); border: 1px solid var(--ring);
  border-radius: 6px; padding: 5px 8px; }
input[type=search] { min-width: 200px; }
.grid2 { display: grid; grid-template-columns: 3fr 2fr; gap: 16px; margin-bottom: 16px; }
.grid2b { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 16px; }
@media (max-width: 900px) { .grid2, .grid2b { grid-template-columns: 1fr; } }
svg { display: block; width: 100%; height: auto; overflow: visible; }
svg text { fill: var(--muted); font-size: 11px; }
svg .lbl { fill: var(--ink-2); font-size: 12px; }
svg .val { fill: var(--ink); font-size: 12px; font-variant-numeric: tabular-nums; }
.legend { display: flex; flex-wrap: wrap; gap: 4px 14px; margin: 6px 0 8px; color: var(--ink-2); font-size: 12px; }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.tip { position: fixed; pointer-events: none; background: var(--surface); color: var(--ink); border: 1px solid var(--ring);
  border-radius: 8px; padding: 8px 10px; font-size: 12px; box-shadow: 0 4px 16px rgba(0,0,0,.12); max-width: 300px; display: none; z-index: 10; }
.tip b { display: block; margin-bottom: 2px; }
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { text-align: left; padding: 7px 8px; border-bottom: 1px solid var(--grid); vertical-align: top; }
th { color: var(--ink-2); font-weight: 600; cursor: pointer; white-space: nowrap; user-select: none; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
tbody tr { cursor: pointer; }
tbody tr:hover { background: var(--hover); }
tbody tr.sel { background: var(--hover); box-shadow: inset 3px 0 0 var(--s1); }
.dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; }
.chip { display: inline-block; background: var(--chip); color: var(--ink-2); border-radius: 999px; padding: 1px 8px; font-size: 12px; margin: 0 4px 2px 0; }
.flag { color: var(--ink-2); font-size: 12px; white-space: nowrap; }
.flag.lead::before { content: "✓ "; color: var(--good); font-weight: 700; }
.flag.lit::before { content: "⚠ "; color: var(--critical); }
.detail h3 { margin: 0 0 2px; font-size: 17px; }
.detail dl { display: grid; grid-template-columns: 150px 1fr; gap: 6px 12px; margin: 12px 0 0; }
.detail dt { color: var(--ink-2); }
.detail dd { margin: 0; overflow-wrap: anywhere; }
.detail a { color: var(--s1); }
.bars .row { display: grid; grid-template-columns: 150px 1fr 56px; gap: 8px; align-items: center; margin: 5px 0; }
.bars .name { color: var(--ink-2); font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.bars .track { height: 14px; }
.bars .fill { height: 14px; border-radius: 0 4px 4px 0; min-width: 2px; }
.bars .v { text-align: right; font-size: 12px; font-variant-numeric: tabular-nums; }
.empty { color: var(--muted); padding: 24px 0; text-align: center; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>Prospect dashboard</h1>
      <p class="sub">US lead-buying prospects from the CFPB complaint database, researched and scored against Dogstar's client profile. Confidential.</p>
    </div>
    <div><span class="note" id="asof"></span> <button class="theme" id="theme" type="button">Toggle theme</button></div>
  </header>

  <section class="kpis" id="kpis"></section>

  <div class="filters" role="group" aria-label="Filters">
    <label>Setup <select id="f-group"><option value="">All</option></select></label>
    <label>Fit <select id="f-fit"><option value="">All</option><option value="price">Price</option><option value="volume">Volume</option></select></label>
    <label>Channel <select id="f-channel"><option value="">All</option></select></label>
    <label><input type="checkbox" id="f-lead"> Lead-buying evidence only</label>
    <input type="search" id="f-q" placeholder="Search company, brand, tribe…" aria-label="Search">
    <span class="note" id="count"></span>
  </div>

  <div class="grid2">
    <div class="card">
      <h2>Size vs fit</h2>
      <p class="note">Each dot is a prospect. Across: CFPB loan complaints in 12 months (log scale; a rough size signal). Up: fit score. Top-right is big and a good fit. Click a dot for detail.</p>
      <div class="legend" id="legend"></div>
      <div id="scatter"></div>
    </div>
    <div class="card detail" id="detail"><p class="empty">Select a prospect in the chart or table.</p></div>
  </div>

  <div class="grid2b">
    <div class="card">
      <h2>Largest prospects by complaint volume</h2>
      <p class="note">Top 15 in the current filter. Bar colour = setup.</p>
      <div class="bars" id="sizebars"></div>
    </div>
    <div class="card">
      <h2>Your direct revenue by client setup (benchmark)</h2>
      <p class="note">Last 30 days, direct lenders and service providers only. This is why operators score highest.</p>
      <div class="bars" id="bench"></div>
      <h2 style="margin-top:18px">Prospects by setup and channel</h2>
      <div class="bars" id="setupbars"></div>
    </div>
  </div>

  <div class="card">
    <h2>Targets</h2>
    <p class="note">Click a column to sort, a row for detail. ✓ = evidence they buy leads. ⚠ = lawsuit or regulator action on record.</p>
    <div class="table-wrap"><table id="tbl">
      <thead><tr>
        <th data-k="rank" class="num">#</th><th data-k="company">Company</th><th data-k="group">Setup</th>
        <th data-k="fit">Fit</th><th data-k="score" class="num">Score</th><th data-k="complaints" class="num">Complaints</th>
        <th data-k="channel">Channel</th><th data-k="lead">Signals</th><th data-k="website">Website</th>
      </tr></thead>
      <tbody></tbody>
    </table></div>
  </div>
  <p class="note" style="margin-top:16px">Complaint counts are a rough size signal: larger lenders get more complaints, but so do badly run ones. Research facts come from public pages (see Sources in each detail panel); check them before relying on them. Existing clients and screened-out companies are excluded.</p>
</div>
<div class="tip" id="tip" role="tooltip"></div>

<script>
const DATA = __DATA__;
const GROUP_ORDER = ["Tribal / operator", "Bank-partner / fintech", "State-licensed / branch", "Unidentified"];
const GROUP_VAR = {"Tribal / operator": "--s1", "Bank-partner / fintech": "--s2", "State-licensed / branch": "--s3", "Unidentified": "--neutral"};
const SHAPE = {"Tribal / operator": "circle", "Bank-partner / fintech": "square", "State-licensed / branch": "triangle", "Unidentified": "diamond"};
const $ = s => document.querySelector(s);
const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const col = g => `var(${GROUP_VAR[g]})`;
const fmt = n => n.toLocaleString("en-US");
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
let sortKey = "rank", sortDir = 1, selected = null;

// Theme toggle (remembered per viewer when storage is available)
(function () {
  try { const t = localStorage.getItem("pd-theme"); if (t) document.documentElement.dataset.theme = t; } catch (e) {}
  $("#theme").onclick = () => {
    const dark = document.documentElement.dataset.theme ? document.documentElement.dataset.theme === "dark"
      : matchMedia("(prefers-color-scheme: dark)").matches;
    document.documentElement.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("pd-theme", document.documentElement.dataset.theme); } catch (e) {}
    render();
  };
})();

$("#asof").textContent = "Data as of " + DATA.asof;
const groups = GROUP_ORDER.filter(g => DATA.targets.some(t => t.group === g));
groups.forEach(g => $("#f-group").insertAdjacentHTML("beforeend", `<option>${esc(g)}</option>`));
[...new Set(DATA.targets.map(t => t.channel))].sort().forEach(c => $("#f-channel").insertAdjacentHTML("beforeend", `<option>${esc(c)}</option>`));
["#f-group", "#f-fit", "#f-channel", "#f-lead", "#f-q"].forEach(s => $(s).addEventListener("input", render));

function filtered() {
  const g = $("#f-group").value, f = $("#f-fit").value, c = $("#f-channel").value, lead = $("#f-lead").checked;
  const q = $("#f-q").value.trim().toLowerCase();
  return DATA.targets.filter(t => (!g || t.group === g) && (!f || t.fit.includes(f)) && (!c || t.channel === c)
    && (!lead || t.lead) && (!q || [t.company, t.brands, t.tribe, t.parent, t.operator, t.website].join(" ").toLowerCase().includes(q)));
}

function shape(kind, x, y, r, fill) {
  const ring = `stroke="var(--surface)" stroke-width="2"`;
  if (kind === "square") return `<rect x="${x-r}" y="${y-r}" width="${2*r}" height="${2*r}" rx="1.5" fill="${fill}" ${ring}/>`;
  if (kind === "triangle") return `<path d="M${x},${y-r*1.2} L${x+r*1.1},${y+r*0.8} L${x-r*1.1},${y+r*0.8}Z" fill="${fill}" ${ring}/>`;
  if (kind === "diamond") return `<path d="M${x},${y-r*1.2} L${x+r*1.1},${y} L${x},${y+r*1.2} L${x-r*1.1},${y}Z" fill="${fill}" ${ring}/>`;
  return `<circle cx="${x}" cy="${y}" r="${r}" fill="${fill}" ${ring}/>`;
}
function legendIcon(g) {
  return `<svg width="12" height="12" viewBox="-6 -6 12 12" style="width:12px">${shape(SHAPE[g], 0, 0, 4.5, col(g))}</svg>`;
}

function kpis(rows) {
  const ops = rows.filter(t => t.group === "Tribal / operator").length;
  const lead = rows.filter(t => t.lead).length;
  const online = rows.filter(t => t.channel.startsWith("Online")).length;
  const pct = n => rows.length ? Math.round(100 * n / rows.length) + "%" : "–";
  $("#kpis").innerHTML = [
    ["Prospects in view", fmt(rows.length)],
    ["Tribal groups / operators", fmt(ops)],
    ["With lead-buying evidence", `${fmt(lead)} <span class="note">(${pct(lead)})</span>`],
    ["Lend online", `${fmt(online)} <span class="note">(${pct(online)})</span>`],
    ["CFPB complaints, total", fmt(rows.reduce((a, t) => a + t.complaints, 0))],
  ].map(([l, v]) => `<div class="card kpi"><div class="l">${l}</div><div class="v">${v}</div></div>`).join("");
}

function scatter(rows) {
  const W = 720, H = 400, m = {l: 44, r: 16, t: 10, b: 38};
  const all = DATA.targets;
  const xmax = Math.max(...all.map(t => t.complaints), 10), ymax = Math.max(...all.map(t => t.score), 10);
  const lx = v => Math.log10(Math.max(v, 1));
  const X = v => m.l + (lx(v) / lx(xmax * 1.15)) * (W - m.l - m.r);
  const Y = v => H - m.b - (Math.max(v, 0) / (Math.ceil(ymax / 10) * 10)) * (H - m.t - m.b);
  const ytop = Math.ceil(ymax / 10) * 10;
  let s = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Scatter of prospect size against fit score">`;
  for (let v = 0; v <= ytop; v += 20) s += `<line x1="${m.l}" x2="${W-m.r}" y1="${Y(v)}" y2="${Y(v)}" stroke="var(--grid)"/><text x="${m.l-8}" y="${Y(v)+4}" text-anchor="end">${v}</text>`;
  [1, 3, 10, 30, 100, 300, 1000].filter(v => v <= xmax * 1.15).forEach(v =>
    s += `<text x="${X(v)}" y="${H-m.b+16}" text-anchor="middle">${v}</text>`);
  s += `<line x1="${m.l}" x2="${W-m.r}" y1="${H-m.b}" y2="${H-m.b}" stroke="var(--axis)"/>`;
  s += `<text x="${(W+m.l)/2}" y="${H-4}" text-anchor="middle" class="lbl">CFPB loan complaints, 12 months (log)</text>`;
  s += `<text transform="translate(12 ${(H-m.b)/2}) rotate(-90)" text-anchor="middle" class="lbl">Fit score</text>`;
  [...rows].sort((a, b) => a.score - b.score).forEach(t => {
    const x = X(t.complaints), y = Y(t.score), sel = selected === t.company;
    s += `<g class="pt" data-c="${esc(t.company)}" style="cursor:pointer">${shape(SHAPE[t.group], x, y, sel ? 7 : 5.5, col(t.group))}
      <circle cx="${x}" cy="${y}" r="12" fill="transparent"/></g>`;
  });
  // Label the selected point and the top scorers, skipping any label that would collide with one already placed.
  const placed = [];
  const cand = [...rows].sort((a, b) => b.score - a.score).slice(0, 8);
  const sel = rows.find(t => t.company === selected);
  if (sel) cand.unshift(sel);
  cand.forEach(t => {
    const name = short(t.company), w = name.length * 7.2, x0 = X(t.complaints), y0 = Y(t.score);
    const x = x0 + w + 12 > W ? x0 - w - 9 : x0 + 9, y = y0 - 8;
    const box = [x - 4, y - 13, x + w + 4, y + 5];
    if (placed.some(b => !(box[2] < b[0] || box[0] > b[2] || box[3] < b[1] || box[1] > b[3]))) return;
    placed.push(box);
    s += `<text x="${x}" y="${y}" class="lbl" style="pointer-events:none">${esc(name)}</text>`;
  });
  s += `</svg>`;
  $("#scatter").innerHTML = s;
  $("#scatter").querySelectorAll(".pt").forEach(el => {
    const t = DATA.targets.find(d => d.company === el.dataset.c);
    el.addEventListener("mousemove", e => tip(e, `<b>${esc(t.company)}</b>${esc(t.group)} · score ${t.score}<br>${fmt(t.complaints)} complaints · ${esc(t.channel)}${t.lead ? "<br>✓ lead-buying evidence" : ""}`));
    el.addEventListener("mouseleave", hideTip);
    el.addEventListener("click", () => select(t.company));
  });
  $("#legend").innerHTML = groups.map(g => `<span>${legendIcon(g)}${esc(g)}</span>`).join("");
}
function short(n) { return n.replace(/,? (LLC|Inc\.?|INC\.?|Corporation|CORPORATION|Corp\.?|L\.?P\.?|Holdings|HOLDINGS)\b.*$/i, "").slice(0, 26); }

function bars(el, items, max, fmtv) {
  $(el).innerHTML = items.length ? items.map(it => `<div class="row" data-c="${esc(it.key || "")}">
    <div class="name" title="${esc(it.name)}">${esc(it.name)}</div>
    <div class="track"><div class="fill" style="width:${Math.max(0.5, 100 * it.v / max)}%;background:${it.color}"></div></div>
    <div class="v">${fmtv(it.v)}</div></div>`).join("") : `<p class="empty">No prospects match.</p>`;
}

function sizeBars(rows) {
  const top = [...rows].sort((a, b) => b.complaints - a.complaints).slice(0, 15);
  bars("#sizebars", top.map(t => ({name: short(t.company), key: t.company, v: t.complaints, color: col(t.group)})),
    Math.max(1, ...top.map(t => t.complaints)), fmt);
  document.querySelectorAll("#sizebars .row").forEach(r => {
    r.style.cursor = "pointer"; r.onclick = () => select(r.dataset.c);
    const t = DATA.targets.find(d => d.company === r.dataset.c);
    r.onmousemove = e => tip(e, `<b>${esc(t.company)}</b>${fmt(t.installment)} installment · ${fmt(t.payday)} payday · ${fmt(t.loc)} line of credit`);
    r.onmouseleave = hideTip;
  });
}

function setupBars(rows) {
  const b = DATA.benchmark, tot = b.reduce((a, g) => a + g.revenue, 0);
  bars("#bench", b.map(g => ({name: g.group, v: g.revenue, color: "var(--seq)", key: g.group})), Math.max(...b.map(g => g.revenue), 1),
    v => tot ? Math.round(100 * v / tot) + "%" : "–");
  document.querySelectorAll("#bench .row").forEach((r, i) => {
    const g = b[i]; r.onmousemove = e => tip(e, `<b>${esc(g.group)}</b>${g.buyers} clients · $${fmt(Math.round(g.revenue))} · ${fmt(g.leads)} leads`);
    r.onmouseleave = hideTip;
  });
  const items = [];
  groups.forEach(g => {
    const n = rows.filter(t => t.group === g);
    if (!n.length) return;
    const on = n.filter(t => t.channel.startsWith("Online")).length;
    items.push({name: g, v: n.length, color: col(g), key: g, on});
  });
  bars("#setupbars", items, Math.max(1, ...items.map(i => i.v)), fmt);
  document.querySelectorAll("#setupbars .row").forEach((r, i) => {
    const it = items[i]; r.onmousemove = e => tip(e, `<b>${esc(it.name)}</b>${it.v} prospects · ${it.on} lend online`);
    r.onmouseleave = hideTip;
  });
}

function table(rows) {
  const sorted = [...rows].sort((a, b) => {
    const x = a[sortKey], y = b[sortKey];
    return (typeof x === "number" ? x - y : String(x).localeCompare(String(y))) * sortDir;
  });
  $("#tbl tbody").innerHTML = sorted.map(t => `<tr data-c="${esc(t.company)}" class="${selected === t.company ? "sel" : ""}">
    <td class="num">${t.rank}</td><td>${esc(t.company)}${t.operator ? `<br><span class="note">${esc(t.operator)}</span>` : ""}</td>
    <td><span class="dot" style="background:${col(t.group)}"></span> ${esc(t.group)}</td>
    <td>${esc(t.fit || "–")}</td><td class="num">${t.score.toFixed(1)}</td><td class="num">${fmt(t.complaints)}</td>
    <td>${esc(t.channel)}</td>
    <td>${t.lead ? '<span class="flag lead">buys leads</span><br>' : ""}${t.litigation ? '<span class="flag lit">litigation</span>' : ""}</td>
    <td>${t.website ? esc(t.website) : '<span class="note">–</span>'}</td></tr>`).join("")
    || `<tr><td colspan="9" class="empty">No prospects match these filters.</td></tr>`;
  $("#tbl tbody").querySelectorAll("tr[data-c]").forEach(tr => tr.onclick = () => select(tr.dataset.c));
  document.querySelectorAll("#tbl th").forEach(th => {
    th.textContent = th.textContent.replace(/ [▲▼]$/, "") + (th.dataset.k === sortKey ? (sortDir > 0 ? " ▲" : " ▼") : "");
    th.onclick = () => { sortDir = th.dataset.k === sortKey ? -sortDir : (["score", "complaints"].includes(th.dataset.k) ? -1 : 1); sortKey = th.dataset.k; render(); };
  });
}

function detail() {
  const t = DATA.targets.find(d => d.company === selected);
  if (!t) { $("#detail").innerHTML = `<p class="empty">Select a prospect in the chart or table.</p>`; return; }
  const link = u => /^https?:/.test(u) ? `<a href="${esc(u)}" target="_blank" rel="noopener">${esc(u.replace(/^https?:\/\/(www\.)?/, "").slice(0, 60))}</a>` : esc(u);
  const row = (k, v) => v ? `<dt>${k}</dt><dd>${v}</dd>` : "";
  const p = t.parts;
  $("#detail").innerHTML = `<h3>#${t.rank} ${esc(t.company)}</h3>
    <div><span class="chip">${esc(t.group)}</span>${t.fit ? `<span class="chip">${esc(t.fit)} fit</span>` : ""}<span class="chip">${esc(t.channel)}</span><span class="chip">research: ${esc(t.confidence)}</span></div>
    <dl>
      ${row("Score", `${t.score.toFixed(1)} <span class="note">= size ${p.size} + type ${p.type} + online ${p.online} + lead ${p.lead_signals} + segment ${p.segment}</span>`)}
      ${row("Why it fits", esc(t.why))}
      ${row("Website", t.website ? link("https://" + t.website.replace(/^https?:\/\//, "")) : "")}
      ${row("Setup", esc(t.type))}
      ${row("Operator group", esc(t.operator))}
      ${row("Tribe", esc(t.tribe))}
      ${row("Parent / servicer", esc(t.parent))}
      ${row("Brands", esc(t.brands))}
      ${row("Products", esc(t.products))}
      ${row("Loan range", esc(t.loans))}
      ${row("States", esc(t.states))}
      ${row("Segment", esc(t.segment))}
      ${row("CFPB complaints", `${fmt(t.complaints)} <span class="note">(${fmt(t.installment)} installment · ${fmt(t.payday)} payday · ${fmt(t.loc)} line of credit)</span>`)}
      ${row("Lead-buying evidence", esc(t.lead))}
      ${row("Litigation / regulatory", esc(t.litigation))}
      ${row("Roles to approach", esc(t.roles))}
      ${row("Sources", t.sources.map(link).join("<br>"))}
    </dl>`;
}

function select(c) { selected = selected === c ? null : c; render(); if (selected) $("#detail").scrollIntoView({block: "nearest", behavior: "smooth"}); }
const tipEl = $("#tip");
function tip(e, html) {
  tipEl.innerHTML = html; tipEl.style.display = "block";
  const w = tipEl.offsetWidth, h = tipEl.offsetHeight;
  tipEl.style.left = Math.min(e.clientX + 14, innerWidth - w - 8) + "px";
  tipEl.style.top = Math.min(e.clientY + 14, innerHeight - h - 8) + "px";
}
function hideTip() { tipEl.style.display = "none"; }

function render() {
  const rows = filtered();
  $("#count").textContent = `${rows.length} of ${DATA.targets.length} shown`;
  kpis(rows); scatter(rows); sizeBars(rows); setupBars(rows); table(rows); detail();
}
render();
</script>
</body>
</html>
"""
