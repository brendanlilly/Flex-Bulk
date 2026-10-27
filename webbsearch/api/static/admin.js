"use strict";
// Webb Search merchandising dashboard. Every change is a draft until Publish; Preview and the relevance audit
// run the draft against the live settings without saving anything.
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const TOKEN = $('meta[name="admin-token"]').content;
const clone = (o) => JSON.parse(JSON.stringify(o));
const pct = (x, d = 1) => (x == null ? "—" : (x * 100).toFixed(d) + "%");
const num = (x) => (x == null ? "—" : Number(x).toLocaleString());
const A = { page: "bulk", days: 30, live: null, draft: null, defaults: null, limits: null, users: [], meta: {}, branches: [], branchName: {},
  recApplied: new Map(), recs: null, recFilter: "all", recPoll: null,
  pv: { q: "copper elbow", who: "" }, timers: [], auditPoll: null };

const ls = { get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : v; } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* storage unavailable */ } } };

async function adm(path, opt = {}) {
  const r = await fetch(path, { method: opt.method || "GET", headers: { "X-Admin-Token": TOKEN, ...(opt.body ? { "Content-Type": "application/json" } : {}) },
    body: opt.body ? JSON.stringify(opt.body) : undefined });
  if (!r.ok) { let m = `HTTP ${r.status}`; try { const j = await r.json(); m = j.detail || m; } catch (e) { /* not json */ } const er = new Error(m); er.status = r.status; throw er; }
  return r.headers.get("content-type")?.includes("json") ? r.json() : r.text();
}
let toastT;
function toast(m, err) { const t = $("#atoast"); t.textContent = m; t.className = "atoast on" + (err ? " err" : ""); clearTimeout(toastT); toastT = setTimeout(() => (t.className = "atoast"), err ? 4200 : 2200); }
const who = () => $("#who").value.trim();

// ------------------------------------------------------------------ what each setting does
const WEIGHTS = {
  purchased: ["Previously purchased", "Added when the signed-in account has bought the item before, however many times.", "Search results for signed-in customers"],
  purchased_times: ["… times bought", "× ln(1 + times bought): 1 purchase ×0.69, 3 purchases ×1.39, 10 purchases ×2.40.", ""],
  purchased_recent: ["… bought recently", "× recency: 1.0 if bought in the last few weeks, fading toward 0 over about a year.", ""],
  branch_stock: ["On the shelf at the pickup branch", "Added when the pickup branch has the item on hand now.", "All results"],
  stocked: ["Stocked item", "× stocking level: branch-stocked 1.0, regional 0.67, CD 0.33, special order 0.", "All results"],
  popularity: ["Popularity", "× relative popularity (orders in the last 365 days; the most-ordered item in the results = 1.0).", "All results"],
};
const BULK = {
  bought_before: ["Bought before", "Added to a Bulk order recommendation the account has bought."],
  bought_times: ["… per purchase", "× ln(1 + times bought)."],
  invoiced: ["On a recent invoice", "Added when the item is on the account's recent invoices."],
  preferred_brand: ["Usual brand for the category", "× the account's share in that brand (40% of it for a secondary brand)."],
  on_shelf: ["Enough on the shelf", "Added when the pickup branch has the full quantity (a quarter of it when it has some)."],
  webb_brand: ["Webb's brand order", "× Webb's preference for the brand in that category, for lines that name no brand (#1 of 3 = 1.0, #2 = 0.67, #3 = 0.33). Set on Brand preferences."],
  learned: ["Learned crosses", "× how often people chose this item for the same line (full weight at 10 choices company-wide; half again at 5 by the account's own team)."],
  learned_min: ["… choices before it counts", "Company-wide choices a line needs before the learned bonus applies. One choice by the account's own team always counts."],
};
const LAYOUT = {
  brand_cap: ["Most items from one brand in the first 24", "Keeps page one from being one brand's whole line when the search doesn't name a brand. 0 turns it off. Searches that name a brand or a part number are never capped. When too few other brands match, the cap relaxes evenly.", "All results"],
  group_variants: ["Group sizes into one card", "Items that differ only by size (same brand, type, connection and material) show as one card with size chips. Customers can switch it off in the toolbar; their choice is kept in their browser.", "Cards and List views"],
  results_mode: ["Default results mode", "How much surrounds the results for people who haven't picked a mode: Minimal (a short rail of base filters, one line about how the search was read), Normal (today's page, tidied) or Advanced (every filter open, all reasons and controls). The results themselves are the same in every mode.", "Results page"],
  mode_switch: ["Mode switch for customers", "Off: everyone gets the default mode, including people who picked a mode before. Links with #mode= still work, for staff and support.", "Results page toolbar"],
  simple_home: ["Simple start page", "The start page shows only the logo, the search box (with its photo button) and links to Bulk order, Job kits and Ask Webb, plus Your orders and Invoices when signed in. Customers switch views from the bar at the bottom of the start page, where Simple is listed first; their choice is kept in their browser.", "Start page"],
};
const PERS = {
  personal_model: ["Personal ranking model", "Signed-in searches use the ranking model trained with account features (what they buy, which brands, what they view). Off: everyone gets the anonymous model.", "Search results"],
  history_lane: ["Pull in past purchases", "The account's previously bought items are added to the candidates, so a search finds them even when the words match loosely.", "Search results"],
  purchase_boost: ["Previously purchased boost", "The Previously purchased weights (Ranking weights) apply. Off: bought items rank on the model alone.", "Search results"],
  reasons: ["Show “why this” reasons", "Chips such as “You've bought this 3×” and “Your usual brand” on results.", "Result cards"],
  buy_again: ["Buy again on the start page", "The start page lists the account's most-bought items with branch stock.", "Start page"],
  bulk_personal: ["Personalized Bulk order", "Bulk order recommendations use the account's history, invoices and brands (the Bulk weights). Off: catalog order and stock only.", "Bulk order"],
  learned_crosses: ["Learn from Bulk order choices", "What people pick for a line (confirm, change, swap) adds weight to the same pick next time: company-wide, and more for the same account. Off: choices are still logged but don't change matches.", "Bulk order"],
};
const LISTS = ["rules", "rewrites", "crosses", "brand_prefs", "learned_off"], OBJS = ["affinity", "facets", "minimal_rail", "availability", "relevance"];
const segName = (k) => (A.meta.segments || {})[k] || String(k || "").replace(/_/g, " ");
const crossLabel = (c) => c.kind === "brand" ? `${c.from_brand} → ${c.to_brand}` : `${c.from_brand ? c.from_brand + " " : ""}${c.from_part || c.from_text} → #${c.to_sku}`;
const scopeName = (sc) => sc === "company" || !sc ? "Company-wide" : `Branch ${sc}${A.branchName[sc] ? " · " + A.branchName[sc] : ""}`;

// ------------------------------------------------------------------ draft vs live
const SECTS = ["weights", "personalization", "bulk", "layout"];
function patch() {
  const p = {}, L = A.live, D = A.draft;
  for (const s of SECTS) for (const k of Object.keys(D[s])) if (D[s][k] !== L[s][k]) (p[s] = p[s] || {})[k] = D[s][k];
  for (const k of LISTS.concat(OBJS)) if (JSON.stringify(D[k]) !== JSON.stringify(L[k])) p[k] = D[k];
  return p;
}
function changes() {
  const out = [], L = A.live, D = A.draft;
  for (const s of SECTS) for (const k of Object.keys(D[s])) if (D[s][k] !== L[s][k]) {
    const lab = ({ weights: WEIGHTS, bulk: BULK, layout: LAYOUT, personalization: PERS }[s][k] || [k])[0];
    out.push(s === "personalization" || k === "group_variants" || k === "simple_home" || k === "mode_switch" ? `${lab} ${D[s][k] ? "on" : "off"}` : `${s === "bulk" ? "Bulk: " : ""}${lab} ${L[s][k]} → ${D[s][k]}`);
  }
  const named = (kind, a, b, key) => { const o = Object.fromEntries(a.map((r) => [r.id, r])), n = Object.fromEntries(b.map((r) => [r.id, r]));
    for (const id in n) if (!o[id]) out.push(`added ${kind} “${key(n[id])}”`); else if (JSON.stringify(o[id]) !== JSON.stringify(n[id])) out.push(`changed ${kind} “${key(n[id])}”`);
    for (const id in o) if (!n[id]) out.push(`removed ${kind} “${key(o[id])}”`); };
  named("rule", L.rules, D.rules, (r) => r.name);
  named("rewrite", L.rewrites, D.rewrites, (r) => `${r.from} → ${r.to}`);
  named("cross", L.crosses, D.crosses, crossLabel);
  named("brand preference", L.brand_prefs, D.brand_prefs, (b) => `${b.family}: ${b.brands.join(" > ")}${b.scope !== "company" ? ` (branch ${b.scope})` : ""}`);
  const la = L.affinity, da = D.affinity;
  if (la.enabled !== da.enabled) out.push(`Brand affinity ${da.enabled ? "on" : "off"}`);
  if (la.min_lines !== da.min_lines) out.push(`Brand affinity minimum lines ${la.min_lines} → ${da.min_lines}`);
  for (const sg of Object.keys(da.segments)) for (const k of ["within", "across"]) if (da.segments[sg][k] !== la.segments[sg][k]) out.push(`Brand affinity ${segName(sg)} ${k} ${la.segments[sg][k]} → ${da.segments[sg][k]}`);
  if (JSON.stringify(L.facets) !== JSON.stringify(D.facets)) out.push("Filter rail order / visibility");
  if (JSON.stringify(L.minimal_rail) !== JSON.stringify(D.minimal_rail)) out.push(`Minimal rail: ${D.minimal_rail.order.map((x) => (A.meta.minimal_rail || {})[x] || x).join(", ")} (max ${D.minimal_rail.max})`);
  if (JSON.stringify(L.learned_off) !== JSON.stringify(D.learned_off)) out.push(`Learned crosses switched off: ${L.learned_off.length} → ${D.learned_off.length}`);
  if (D.availability && JSON.stringify(L.availability) !== JSON.stringify(D.availability)) out.push("Availability ranking");   // v2.3
  if (D.relevance && L.relevance) {        // v2.44 relevance by field and attribute
    for (const f of Object.keys(D.relevance.fields)) if (D.relevance.fields[f] !== L.relevance.fields[f]) out.push(`Field ${(REL.labels[f] || f)} ×${L.relevance.fields[f]} → ×${D.relevance.fields[f]}`);
    for (const k of new Set([...Object.keys(L.relevance.attrs), ...Object.keys(D.relevance.attrs)])) { const a = L.relevance.attrs[k] || 0, b = D.relevance.attrs[k] || 0; if (a !== b) out.push(`Spec ${k} ${a > 0 ? "+" : ""}${a} → ${b > 0 ? "+" : ""}${b}`); }
  }
  return out;
}
function renderDraftBar() {
  const c = changes(), bar = $("#draftbar");
  bar.hidden = !c.length;
  if (!c.length) return;
  bar.innerHTML = `<span class="dot"></span><b>${c.length} unpublished change${c.length > 1 ? "s" : ""}</b><span class="chg" title="${esc(c.join("\n"))}">${esc(c.join(" · "))}</span><span class="sp"></span>
    <input id="note" placeholder="Why (shows in the change log)" maxlength="200">
    <button class="btn sm" data-a="discard">Discard</button><button class="btn sm" data-a="go" data-p="audit">Test in audit</button><button class="btn sm" data-a="go" data-p="tests" title="Run these changes on a share of sessions and compare">A/B test</button><button class="btn sm pri" data-a="publish">Publish</button>`;
}
async function publish() {
  if (!who()) { $("#who").focus(); toast("Add your name (top right) so the change log says who published", true); return; }
  try {
    const r = await adm("/api/admin/settings", { method: "PUT", body: { patch: patch(), note: ($("#note") || {}).value || "", who: who(), base_version: A.live.version } });
    A.live = r.settings; A.draft = clone(A.live); A.recApplied = new Map(); toast(`Published: version ${A.live.version} is live`); render();
  } catch (e) { toast(e.status === 409 ? e.message : `Not published: ${e.message}`, true); }
}
function discard() { A.draft = clone(A.live); A.recApplied = new Map(); render(); toast("Draft discarded"); }

// ------------------------------------------------------------------ small widgets
function delta(cur, prev, lowerBetter, rate) {
  if (cur == null || prev == null) return `<span class="d eq">no earlier data</span>`;
  const d = cur - prev; if (Math.abs(d) < 1e-9) return `<span class="d eq">no change</span>`;
  const good = lowerBetter ? d < 0 : d > 0;
  const t = rate ? `${d > 0 ? "+" : ""}${(d * 100).toFixed(1)} pts` : `${d > 0 ? "+" : ""}${Number.isInteger(d) ? d.toLocaleString() : d.toFixed(1)}`;
  return `<span class="d ${good ? "up" : "dn"}">${good ? "▲" : "▼"} ${t}</span>`;
}
function kpi(label, val, cur, prev, lowerBetter, rate) {
  return `<div class="kpi"><small>${label}</small><b>${val}</b>${delta(cur, prev, lowerBetter, rate)}</div>`;
}
function hbars(rows, total, red) {
  const mx = Math.max(1, ...rows.map((r) => r.n));
  return rows.map((r) => `<div class="hbar ${red ? "red" : ""}"><span class="l" title="${esc(r.name)}">${esc(r.name)}</span><span class="b"><i style="width:${(100 * r.n / mx).toFixed(1)}%"></i></span><span class="v">${total ? pct(r.n / total, 1) : num(r.n)}</span></div>`).join("");
}
// bars for a count, optional line for a rate (0-1) on the same x axis
function chart(rows, xKey, barKey, lineKey, { h = 170, label } = {}) {
  if (!rows.length) return `<div class="empty">No data in this period.</div>`;
  const W = 760, H = h, pl = 36, pr = 36, pt = 10, pb = 22, n = rows.length, bw = (W - pl - pr) / n;
  const mx = Math.max(1, ...rows.map((r) => r[barKey]));
  const nice = Math.pow(10, Math.floor(Math.log10(mx))), top = Math.ceil(mx / nice) * nice;
  const y = (v) => pt + (H - pt - pb) * (1 - v / top), yl = (v) => pt + (H - pt - pb) * (1 - v);
  let s = `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(label || barKey)}">`;
  for (const f of [0, 0.5, 1]) s += `<line x1="${pl}" x2="${W - pr}" y1="${y(top * f)}" y2="${y(top * f)}" stroke="var(--line2)"/><text x="${pl - 6}" y="${y(top * f) + 3}" text-anchor="end">${num(Math.round(top * f))}</text>`;
  const w = Math.max(1, Math.min(36, bw * 0.7));
  rows.forEach((r, i) => { const x = pl + (i + 0.5) * bw - w / 2; s += `<rect x="${x}" y="${y(r[barKey])}" width="${w}" height="${Math.max(0, H - pb - y(r[barKey]))}" rx="1.5" fill="var(--bar)"><title>${esc(r[xKey])}: ${num(r[barKey])}${lineKey ? ` · ${pct(r[lineKey])}` : ""}</title></rect>`; });
  if (lineKey) {
    s += `<polyline fill="none" stroke="var(--webb-red)" stroke-width="2" points="${rows.map((r, i) => `${pl + (i + 0.5) * bw},${yl(r[lineKey] || 0)}`).join(" ")}"/>`;
    for (const f of [0, 0.5, 1]) s += `<text x="${W - pr + 6}" y="${yl(f) + 3}">${f * 100}%</text>`;
  }
  const step = Math.ceil(n / 8), span = n > 1 ? (new Date(rows[n - 1][xKey]) - new Date(rows[0][xKey])) / 864e5 : 0;
  const M = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const lab = (d) => { d = String(d); return span > 150 ? `${M[+d.slice(5, 7) - 1]} ’${d.slice(2, 4)}` : `${M[+d.slice(5, 7) - 1]} ${+d.slice(8, 10)}`; };
  rows.forEach((r, i) => { if (i % step === 0) s += `<text x="${pl + (i + 0.5) * bw}" y="${H - 6}" text-anchor="middle">${esc(lab(r[xKey]))}</text>`; });
  return s + "</svg>";
}
function periodSeg() {
  return `<div class="seg">${[7, 30, 90, 365].map((d) => `<button class="${A.days === d ? "on" : ""}" data-a="days" data-d="${d}">${d === 365 ? "1 yr" : d + " days"}</button>`).join("")}</div>`;
}
const outcomeChip = (o) => o === "fallback" ? `<span class="chip warn" title="Nothing matched every word; the nearest products were shown">no full match</span>` : o === "zero" ? `<span class="chip red">no results</span>` : o === "answered" ? `<span class="chip info" title="An exact Webb item or cross-reference answered it">answered</span>` : "";

// ------------------------------------------------------------------ pages
const PAGES = {};

PAGES.overview = async (el) => {
  el.innerHTML = `<div class="ph"><div><h1>Overview</h1><p>How search is doing on this server, and what to fix next. Numbers come from the live log (searches, clicks, add-to-cart, Bulk order lines, Ask questions made in this copy of Webb Search).</p></div><span class="sp"></span>${periodSeg()}</div><div id="ov">Loading…</div>`;
  const [ins, sug] = await Promise.all([adm(`/api/admin/insights?days=${A.days}`), adm(`/api/admin/suggestions?days=${A.days}`)]);
  const k = ins.kpis || {}, p = ins.prev_kpis || {};
  const emptyLive = !k.searches;
  $("#ov").innerHTML = `${ins.disabled ? `<div class="explain">Analytics are off on this server (WEBB_ANALYTICS=0).</div>` : ""}
    ${emptyLive ? `<div class="explain"><b>No searches logged in the last ${A.days} days on this server.</b> Search in the app (Open search, top right) and they appear here within seconds. Meanwhile the <span class="link" data-a="go" data-p="history">Historical log</span> has the demo search log and the <span class="link" data-a="go" data-p="audit">Relevance audit</span> replays it.</div>` : ""}
    <div class="kpis">${kpi("Searches", num(k.searches || 0), k.searches, p.searches)}${kpi("No results", pct(k.zero_rate), k.zero_rate, p.zero_rate, true, true)}
      ${kpi("Click-through", pct(k.ctr), k.ctr, p.ctr, false, true)}${kpi("Add to cart", pct(k.atc_rate), k.atc_rate, p.atc_rate, false, true)}
      ${kpi("Avg clicked position", k.avg_pos ?? "—", k.avg_pos, p.avg_pos, true)}${kpi("p95 response", k.p95_ms != null ? Math.round(k.p95_ms) + " ms" : "—", k.p95_ms, p.p95_ms, true)}</div>
    <div class="grid g21 mt">
      <div class="card"><h3>Searches per day <span class="sp"></span><span class="legend"><span><i style="background:var(--bar)"></i>searches</span><span><i style="background:var(--webb-red)"></i>click-through</span></span></h3>
        ${chart((ins.series || []).map((d) => ({ ...d, ctr: d.searches ? d.clicked / d.searches : 0 })), "day", "searches", "ctr", { label: "Searches per day" })}</div>
      <div class="card"><h3>What to fix next <span class="sub">${sug.length} suggestions</span><span class="sp"></span><span class="link small" data-a="go" data-p="recs">All recommendations →</span></h3><div class="scroll" style="max-height:260px">${sug.length ? sug.slice(0, 12).map(suggHTML).join("") : `<div class="empty"><b>Nothing flagged yet.</b>Suggestions come from searches with no results or few clicks, Bulk order lines with no catalog match, and relevance-audit misses.</div>`}</div></div>
    </div>
    <div class="grid g2 mt">
      <div class="card"><h3>Top searches <span class="sub">click-through · avg position</span></h3>${queryTable(ins.top_queries || [], 12)}</div>
      <div class="card"><h3>Searches that found nothing <span class="sub">rewrite or pin</span></h3>${(ins.zero_results || []).length ? `<table class="t"><tr><th>Search</th><th class="n">Times</th><th>Last</th><th></th></tr>${ins.zero_results.slice(0, 12).map((z) => `<tr><td class="q"><span class="link" data-a="query" data-q="${esc(z.q)}">${esc(z.q)}</span> ${outcomeChip(z.outcome)}</td><td class="n">${z.zero}</td><td class="muted">${esc(z.last)}</td><td><button class="btn sm" data-a="newrw" data-q="${esc(z.qn)}">Add rewrite</button></td></tr>`).join("")}</table>` : `<div class="empty">Every search found products.</div>`}</div>
    </div>
    <div class="grid g3 mt">
      <div class="card"><h3>Personalization</h3>${liftHTML(ins.personalization)}</div>
      <div class="card"><h3>Where people click</h3>${hbars((ins.clicked_positions || []).map((b) => ({ name: "Position " + b.bucket, n: b.n })), (ins.clicked_positions || []).reduce((s, b) => s + b.n, 0))}</div>
      <div class="card"><h3>Bulk order</h3>${bulkHTML(ins.bulk || {})}</div>
    </div>
    <div class="card mt"><h3>Customer feedback <span class="sub">thumbs on results and Ask answers</span><span class="sp"></span><span class="link small" data-a="go" data-p="feedback">All feedback →</span></h3>${feedbackCard(ins.feedback)}</div>`;
};
function suggHTML(s) {
  const ic = { zero: "0", low_ctr: "↓", gap: "?", audit: "#", head: "★", brand_cross: "→", learned: "✓", feedback: "▼" }[s.kind] || "·";
  const a = s.action;
  const btn = !a ? "" : a.type === "rewrite" ? `<button class="btn sm" data-a="newrw" data-q="${esc(a.from)}">Add rewrite</button>`
    : a.type === "cross" ? `<button class="btn sm" data-a="addbrandcross" data-b="${esc(a.from_brand)}">Add cross</button>`
    : a.type === "promote" ? `<button class="btn sm" data-a="promote" data-k="${esc(a.key)}" data-sku="${esc(a.sku)}">Make permanent</button>`
    : a.type === "pin" ? `<button class="btn sm" data-a="newrule" data-q="${esc(a.q)}" data-sku="${esc(a.sku)}">Pin it</button>`
    : `<button class="btn sm" data-a="query" data-q="${esc(a.q)}">Look</button>`;
  return `<div class="sugg"><span class="ic ${s.kind}">${ic}</span><div><b>${esc(s.title)}</b><span>${esc(s.detail)}</span></div><span class="act">${btn}</span></div>`;
}
function queryTable(rows, n) {
  if (!rows.length) return `<div class="empty">No searches yet.</div>`;
  return `<table class="t"><tr><th>Search</th><th class="n">Searches</th><th class="n">Click-through</th><th class="n">Add to cart</th><th class="n">Avg pos</th></tr>${rows.slice(0, n).map((r) => `<tr class="click" data-a="query" data-q="${esc(r.q)}"><td class="q clip">${esc(r.q)} ${r.rewritten ? `<span class="chip">rewritten</span>` : ""}${outcomeChip(r.outcome)}</td><td class="n">${num(r.n)}</td><td class="n">${pct(r.ctr, 0)}</td><td class="n">${pct(r.atc, 0)}</td><td class="n">${r.avg_pos ?? "—"}</td></tr>`).join("")}</table>`;
}
function liftHTML(l) {
  if (!l || (!l.personalized.n && !l.anonymous.n)) return `<div class="empty">No searches yet.</div>`;
  const row = (lab, a) => `<tr><td>${lab}</td><td class="n">${num(a.n)}</td><td class="n">${pct(a.ctr, 0)}</td><td class="n">${pct(a.atc, 0)}</td><td class="n">${a.avg_pos ?? "—"}</td></tr>`;
  return `<table class="t"><tr><th></th><th class="n">Searches</th><th class="n">Clicked</th><th class="n">Cart</th><th class="n">Pos</th></tr>${row("Signed in", l.personalized)}${row("Guest", l.anonymous)}</table>
    <div class="pvnote">Signed-in customers searching their own trade usually click higher up; the gap is what personalization is worth on this traffic.</div>`;
}
function startPageHTML(sp) {
  if (!sp || !(sp.to_simple || sp.to_full)) return `<div class="empty">Nobody has switched start pages in this period.</div>`;
  return `<div class="row" style="gap:18px"><div class="metric"><small>To Simple</small><b>${num(sp.to_simple)}</b></div><div class="metric"><small>Away from Simple</small><b>${num(sp.to_full)}</b></div><div class="metric"><small>Sessions</small><b>${num(sp.sessions)}</b></div></div>
    <div class="pvnote">Switches to the Simple view in the start page bar, and from it to any other view. Their searches are in the log like any other; to compare them, run an A/B test on the Simple start page setting.</div>`;
}
function modeSwitchesHTML(m) {
  if (!m || !(m.to_minimal || m.to_normal || m.to_advanced)) return `<div class="pvnote">Nobody has switched results modes in this period.</div>`;
  return `<div class="sect">Results mode</div><div class="row" style="gap:18px"><div class="metric"><small>To Minimal</small><b>${num(m.to_minimal)}</b></div><div class="metric"><small>To Normal</small><b>${num(m.to_normal)}</b></div><div class="metric"><small>To Advanced</small><b>${num(m.to_advanced)}</b></div><div class="metric"><small>Sessions</small><b>${num(m.sessions)}</b></div></div>`;
}
// which rail filters people use, and which values (v2.42): the evidence for what the rail should lead with
const FILTER_NAMES = { family: "Category", brand: "Brand", series: "Line", size: "Size", type: "Type", connection: "Connection",
  material: "Material", spec: "Specs", price: "Price", avail: "Availability", ec: "Extended Catalog", req: "From your search", account: "Your account" };
function filtersUsedHTML(f) {
  if (!f || !f.events) return `<div class="pvnote">Nobody has used a filter in this period.</div>`;
  const rows = f.facets.map((e) => ({ name: FILTER_NAMES[e.facet] || e.facet, n: e.on + e.off }));
  return `<div class="row" style="gap:18px;margin-bottom:8px"><div class="metric"><small>Filter changes</small><b>${num(f.events)}</b></div>
      <div class="metric"><small>Searches with a filter</small><b>${num(f.searches_with_filter)}</b></div>
      <div class="metric"><small>Share of searches</small><b>${f.share_of_searches == null ? "–" : pct(f.share_of_searches, 1)}</b></div>
      <div class="metric"><small>Sessions</small><b>${num(f.sessions)}</b></div></div>
    <div class="sect">By filter</div>${hbars(rows.slice(0, 10), 0, false)}
    ${f.facets.slice(0, 4).map((e) => e.values.length ? `<div class="sect">${esc(FILTER_NAMES[e.facet] || e.facet)}: top values${e.off ? ` <span class="sub">${num(e.off)} turned off</span>` : ""}</div><div class="row">${e.values.map((v) => `<span class="chip">${esc(v.value)} ${num(v.n)}</span>`).join("")}</div>` : "").join("")}`;
}
function bulkHTML(b) {
  if (!b.lines) return `<div class="empty">No Bulk order lines yet.</div>`;
  const st = b.status || {};
  return `<div class="row" style="gap:18px;margin-bottom:8px"><div class="metric"><small>Lines</small><b>${num(b.lines)}</b></div><div class="metric"><small>Matched</small><b>${pct(b.matched_rate, 0)}</b></div><div class="metric"><small>Not in catalog</small><b>${num(st.none || 0)}</b></div></div>
    ${(b.not_in_catalog || []).length ? `<div class="sect">Asked for, not in the catalog</div>${hbars(b.not_in_catalog.slice(0, 6).map((g) => ({ name: g.what, n: g.n })), 0, true)}` : ""}
    ${Object.keys(b.sources || {}).length ? `<div class="sect">From</div><div class="row">${Object.entries(b.sources).map(([k, v]) => `<span class="chip">${esc(k)} ${v}</span>`).join("")}</div>` : ""}`;
}

PAGES.log = async (el) => {
  el.innerHTML = `<div class="ph"><div><h1>Live search log</h1><p>Every results page this server served, with what happened next. Filter changes on the same search count once. Refreshes every 10 seconds.</p></div><span class="sp"></span>${periodSeg()}</div><div id="lg">Loading…</div>`;
  const draw = async () => {
    const [rec, ins] = await Promise.all([adm("/api/admin/recent?limit=80"), adm(`/api/admin/insights?days=${A.days}`)]);
    if (A.page !== "log") return;
    $("#lg").innerHTML = `<div class="grid g21"><div class="card"><h3>Latest searches <span class="sub">${rec.length} shown</span></h3>
      ${rec.length ? `<div class="scroll" style="max-height:620px"><table class="t"><tr><th>When</th><th>Search</th><th>Who</th><th class="n">Results</th><th class="n">Clicks</th><th class="n">Cart</th><th class="n">ms</th></tr>
        ${rec.map((r) => `<tr class="click" data-a="query" data-q="${esc(r.q)}"><td class="muted num" style="white-space:nowrap">${esc(r.at.slice(5))}</td><td class="q clip">${esc(r.q)}${r.rewritten_from ? ` <span class="chip" title="typed: ${esc(r.rewritten_from)}">rewritten</span>` : ""}${r.rules.length ? ` <span class="chip dark">${esc(r.rules[0])}</span>` : ""} ${outcomeChip(r.outcome)}</td><td>${esc(r.role)}${r.personalized ? "" : ""}</td><td class="n">${r.total >= 600 ? "600+" : num(r.total)}</td><td class="n">${r.clicks || ""}</td><td class="n">${r.atc || ""}</td><td class="n muted">${Math.round(r.ms)}</td></tr>`).join("")}</table></div>` : `<div class="empty"><b>Nothing logged yet.</b>Searches made in the app show here.</div>`}</div>
      <div><div class="card"><h3>Few clicks <span class="sub">3+ searches, under 25% clicked</span></h3>${(ins.low_ctr || []).length ? queryTable(ins.low_ctr, 10) : `<div class="empty">None in this period.</div>`}</div>
        <div class="card mt"><h3>Who searched</h3>${hbars((ins.roles || []).map((r) => ({ name: r.role, n: r.n })), (ins.roles || []).reduce((s, r) => s + r.n, 0))}</div>
        <div class="card mt"><h3>Start page and results mode switches <span class="sub">people choosing</span></h3>${startPageHTML(ins.start_page)}${modeSwitchesHTML(ins.results_mode)}</div>
        <div class="card mt"><h3>Filters used <span class="sub">results-page rail, v2.42</span></h3>${filtersUsedHTML(ins.filters)}</div>
        <div class="card mt"><h3>Ask Webb questions</h3>${(ins.ask || []).length ? ins.ask.slice(0, 10).map((a) => `<div class="small" style="padding:4px 0;border-bottom:1px solid var(--line2)"><span class="muted">${esc(a.at.slice(5))} · ${esc(a.role)}</span><br>${esc(a.q)}</div>`).join("") : `<div class="empty">No questions yet.</div>`}</div>
        <div class="card mt"><h3>Response time</h3>${ins.latency && ins.latency.p50 != null ? `<div class="row" style="gap:18px">${["p50", "p90", "p95", "max"].map((k) => `<div class="metric"><small>${k}</small><b>${Math.round(ins.latency[k])} ms</b></div>`).join("")}</div>` : `<div class="empty">No searches yet.</div>`}</div></div></div>`;
  };
  await draw();
  A.timers.push(setInterval(() => { if (!document.hidden) draw().catch(() => {}); }, 10000));
};

PAGES.history = async (el) => {
  el.innerHTML = `<div class="ph"><div><h1>Historical log</h1><p id="hsSub">The demo search log shipped with Webb Search: every search with the item the customer clicked. Use it to see what people look for, and whether one item wins a query.</p></div></div><div id="hs">Loading…</div>`;
  const h = await adm("/api/admin/history");
  if (!h.available) { $("#hs").innerHTML = `<div class="empty"><b>No search log on this computer.</b>It lives at data/canonical/search_log.parquet.</div>`; return; }
  A.hist = h;
  $("#hsSub").textContent += ` ${h.from} to ${h.to}.`;
  const segs = h.segments.map((s) => ({ name: s.segment.replace(/_/g, " "), n: s.n }));
  $("#hs").innerHTML = `<div class="kpis">${[["Searches", num(h.searches)], ["Accounts", num(h.accounts)], ["Distinct searches", num(h.unique_queries)],
      ["Searched only once", pct(h.one_off_share, 0), "of searches are one-offs"], ["Top 60 searches", pct(h.top60_share, 0), "of all volume"], ["Part-number searches", pct(h.part_number_share, 0), "item or part #"]].map(([l, v, sub]) => `<div class="kpi"><small>${l}</small><b>${v}</b><span class="d eq">${sub || "&nbsp;"}</span></div>`).join("")}</div>
    <div class="card mt"><h3>Searches per week <span class="sub">${esc(h.source)}</span></h3>${chart(h.weekly.map((w) => ({ ...w, day: w.week })), "day", "searches", null, { h: 150 })}</div>
    <div class="grid g21 mt"><div class="card"><h3>Most-searched <span class="sub">and the item customers pick</span></h3><div class="scroll" style="max-height:560px"><table class="t"><tr><th>Search</th><th class="n">Searches</th><th class="n">Accounts</th><th>Most-clicked item</th><th class="n">Share</th><th></th></tr>
      ${h.top_queries.map((t) => `<tr><td class="q"><span class="link" data-a="query" data-q="${esc(t.q)}">${esc(t.q)}</span></td><td class="n">${num(t.n)}</td><td class="n">${num(t.accounts)}</td><td><div class="clip" style="max-width:260px" title="${esc(t.top_title)}"><span class="muted">#${esc(t.top_sku)}</span> ${esc(t.top_title)}</div></td><td class="n">${pct(t.top_share, 0)}</td><td><button class="btn sm ghost" data-a="newrule" data-q="${esc(t.qn)}" data-sku="${esc(t.top_sku)}" title="Pin this item first for this search">Pin</button></td></tr>`).join("")}</table></div>
      <div class="pvnote">Share: of that search's clicks, how many went to the one item. High share (60%+) means customers agree on the answer; check it's first.</div></div>
      <div><div class="card"><h3>By trade</h3>${hbars(segs, h.searches)}</div><div class="card mt"><h3>Categories clicked</h3>${hbars(h.families.slice(0, 10), h.searches)}</div><div class="card mt"><h3>Brands clicked</h3>${hbars(h.brands.slice(0, 10), h.searches)}</div></div></div>`;
};

PAGES.audit = async (el) => {
  const hasDraft = changes().length > 0;
  el.innerHTML = `<div class="ph"><div><h1>Relevance audit</h1><p>Replays logged searches through today's ranking, as the account that searched, and finds where the item that customer clicked lands now. With unpublished changes it runs both and shows what they would move.</p></div></div>
    <div class="card"><div class="row"><label class="fl">Searches to replay<select class="in" id="auN">${[50, 100, 150, 250].map((n) => `<option ${n === 150 ? "selected" : ""}>${n}</option>`).join("")}</select></label>
      <label class="fl">Which<select class="in" id="auMode"><option value="top">Most-searched (one each)</option><option value="recent">Latest searches</option></select></label>
      <label class="fl">Settings<select class="in" id="auDraft"><option value="0">Live only</option><option value="1" ${hasDraft ? "selected" : "disabled"}>Live vs my unpublished changes${hasDraft ? "" : " (none)"}</option></select></label>
      <span class="sp"></span><button class="btn pri" data-a="audit">Run audit</button></div><div id="auProg" class="mt" hidden></div></div><div id="auRes" class="mt"></div>`;
  const st = await adm("/api/admin/audit");
  drawAudit(st);
  if (st.running) pollAudit();
};
function pollAudit() {
  clearInterval(A.auditPoll);
  A.auditPoll = setInterval(async () => { const st = await adm("/api/admin/audit").catch(() => null); if (!st || A.page !== "audit") { clearInterval(A.auditPoll); return; }
    drawAudit(st); if (!st.running) clearInterval(A.auditPoll); }, 1000);
}
function drawAudit(st) {
  const pg = $("#auProg"); if (!pg) return;
  pg.hidden = !st.running;
  if (st.running) pg.innerHTML = `<div class="row small"><b>Replaying ${st.done} of ${st.total}…</b><span class="muted">started ${esc(st.started)}${st.with_draft ? " · live and draft" : ""}</span></div><div class="prog mt"><i style="width:${st.total ? (100 * st.done / st.total).toFixed(0) : 0}%"></i></div>`;
  if (st.error) pg.innerHTML = `<div class="explain">The audit stopped: ${esc(st.error)}</div>`, pg.hidden = false;
  const r = st.result, out = $("#auRes");
  if (!r) { out.innerHTML = st.running ? "" : `<div class="empty"><b>No audit run yet.</b>Run one to get a baseline before changing weights or rules.</div>`; return; }
  const m = (k, lab, lower) => { const a = r.live[k], d = r.draft ? r.draft[k] : null;
    return `<div class="kpi"><small>${lab}</small><b>${pct(a, 0)}</b>${d != null ? `<span class="diff ${(lower ? d < a : d > a) ? "d up" : d === a ? "d eq" : "d dn"}">draft ${pct(d, 0)}</span>` : `<span class="d eq">&nbsp;</span>`}</div>`; };
  out.innerHTML = `<div class="kpis">${m("hit1", "Clicked item is #1")}${m("hit3", "In the top 3")}${m("hit10", "In the top 10")}${m("hit24", "On page one (24)")}${m("missing", "Not found", true)}
      <div class="kpi"><small>MRR</small><b>${r.live.mrr.toFixed(2)}</b>${r.draft ? `<span class="d ${r.draft.mrr > r.live.mrr ? "up" : r.draft.mrr < r.live.mrr ? "dn" : "eq"}">draft ${r.draft.mrr.toFixed(2)}</span>` : `<span class="d eq">&nbsp;</span>`}</div></div>
    <div class="small muted mt">${num(r.n)} searches (${r.mode === "recent" ? "latest" : "most-searched"}), run ${esc(r.at)}${r.by ? " by " + esc(r.by) : ""} on settings version ${r.settings_version} in ${r.seconds}s.${(st.stale || []).length ? ` <span class="chip warn" title="Saved before the current settings or dictionary: not a baseline for today">Stale: run on ${esc(st.stale.join(", "))}. Re-run before comparing</span>` : ""}${r.moved ? ` Draft moved the clicked item <b>up for ${r.moved.better}</b> and <b>down for ${r.moved.worse}</b>.` : ""}</div>
    <div class="grid g12 mt"><div class="card"><h3>Where the clicked item lands</h3>${hbars(["1", "2-3", "4-10", "11-24", "25+", "not found"].map((b) => ({ name: b === "not found" ? "Not found" : "Position " + b, n: r.live.positions[b] || 0 })), r.n)}
      <div class="pvnote">Synthetic clicks are noisy: a customer who clicked a 1/2" valve for “ball valve” is one right answer among many. Watch the trend between runs, not the absolute number.</div></div>
      <div class="card"><h3>Misses <span class="sub">clicked item below #10, most-searched first</span></h3><div class="scroll" style="max-height:480px"><table class="t"><tr><th>Search</th><th>Clicked item</th><th class="n">Now</th>${r.draft ? `<th class="n">Draft</th>` : ""}<th></th></tr>
        ${r.misses.map((x) => `<tr><td class="q"><span class="link" data-a="query" data-q="${esc(x.q)}">${esc(x.q)}</span><div class="muted small">${num(x.searches)} searches</div></td><td class="clip" title="${esc(x.title)}"><span class="muted">#${esc(x.sku)}</span> ${esc(x.title)}</td><td class="n">${x.pos ?? "—"}</td>${r.draft ? `<td class="n">${x.pos_draft ?? "—"}</td>` : ""}<td><button class="btn sm ghost" data-a="newrule" data-q="${esc(x.qn)}" data-sku="${esc(x.sku)}">Pin</button></td></tr>`).join("") || `<tr><td colspan="5" class="muted">None.</td></tr>`}</table></div></div></div>`;
}

PAGES.weights = async (el) => {
  const D = A.draft, L = A.live, df = A.defaults;
  const row = (sect, k, meta, lo, hi, step) => {
    const v = D[sect][k], ch = v !== L[sect][k];
    return `<div class="wrow"><div><b class="${ch ? "chg" : ""}">${esc(meta[0])}</b><div class="desc">${esc(meta[1])}</div>${meta[2] ? `<div class="def">Affects: ${esc(meta[2])}</div>` : ""}</div>
      <input type="range" min="${lo}" max="${hi}" step="${step}" value="${v}" data-w="${sect}.${k}" aria-label="${esc(meta[0])}">
      <input class="in num" value="${v}" data-wn="${sect}.${k}" inputmode="decimal" aria-label="${esc(meta[0])} value"><button class="rst" data-a="wdef" data-k="${sect}.${k}" title="Default ${df[sect][k]}"><svg viewBox="0 0 24 24"><path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/></svg></button></div>`;
  };
  el.innerHTML = `<div class="ph"><div><h1>Ranking weights</h1><p>Adjustments added to the learned ranker's score. Preview any search as any demo account before publishing.</p></div><span class="sp"></span><button class="btn" data-a="reset" data-s="weights">Reset this page to defaults</button></div>
    <div class="grid g12"><div><div class="card"><h3>Search results</h3><div class="scale">Scale: on a typical search the ranker's top 20 are 1 to 2 points apart, so +1.0 moves an item several places and +3 puts it near the top. Exact part-number matches carry +100 and stay first. Negative values push down.</div>
      ${Object.entries(WEIGHTS).map(([k, m]) => row("weights", k, m, A.limits.weights[0], A.limits.weights[1], 0.05)).join("")}</div>
      <div class="card mt"><h3>Bulk order recommendations</h3><div class="scale">Bulk order scores each candidate 0 to 10 by rank position, then adds these. With the defaults a previously bought item outranks a better text match unless that match is 8+ places higher.</div>
      ${Object.entries(BULK).map(([k, m]) => row("bulk", k, m, A.limits.bulk[0], A.limits.bulk[1], 0.5)).join("")}</div></div>
      <div>${previewCard()}</div></div>`;
  runPreview();
};

// ------------------------------------------------------------------ relevance by field and attribute (v2.44)
const REL = { meta: null, filter: "", labels: { title: "Title", brand: "Brand", cat: "Category", attrs: "Specs", syn: "Synonyms", desc: "Description", ids: "Part numbers" } };
const RST = `<svg viewBox="0 0 24 24"><path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/></svg>`;
PAGES.relevance = async (el) => {
  if (!REL.meta) REL.meta = await adm("/api/admin/relevance/fields");
  const D = A.draft.relevance, L = A.live.relevance, M = REL.meta;
  const fieldRow = (f) => { const v = D.fields[f.key], ch = v !== L.fields[f.key];
    return `<div class="wrow"><div><b class="${ch ? "chg" : ""}">${esc(f.label)} <span class="muted small">built-in ${f.builtin}</span></b><div class="desc">${esc(f.help)}</div><div class="def">${f.final_order ? "Acts on: the final order (chip in the preview) · 0 also leaves it out of what's gathered" : "Acts on: what's gathered and the ranker's text score"}</div></div>
      <input type="range" min="0" max="3" step="0.1" value="${v}" data-rf="${f.key}" aria-label="${esc(f.label)} weight"><input class="in num" value="${v}" data-rfn="${f.key}" inputmode="decimal" aria-label="${esc(f.label)} weight value"><button class="rst" data-a="rfdef" data-k="${f.key}" title="Default ×1">${RST}</button></div>`; };
  const q = REL.filter.toLowerCase();
  const attrs = M.attrs.filter((a) => !q || a.key.includes(q) || a.examples.some((x) => x.toLowerCase().includes(q)));
  const isSet = (a) => D.attrs[a.key] || L.attrs[a.key];
  const set = M.attrs.filter(isSet), rest = attrs.filter((a) => !isSet(a));     // weighted specs show whatever the filter
  const attrRow = (a) => { const v = D.attrs[a.key] || 0, ch = v !== (L.attrs[a.key] || 0);
    return `<div class="wrow"><div><b class="${ch ? "chg" : ""}">${esc(a.key)} <span class="muted small">${num(a.items)} items</span></b><div class="desc">${a.examples.map(esc).join(" · ")}</div></div>
      <input type="range" min="-3" max="3" step="0.1" value="${v}" data-ra="${esc(a.key)}" aria-label="${esc(a.key)} weight"><input class="in num" value="${v}" data-ran="${esc(a.key)}" inputmode="decimal" aria-label="${esc(a.key)} weight value"><button class="rst" data-a="radef" data-k="${esc(a.key)}" title="Default 0">${RST}</button></div>`; };
  el.innerHTML = `<div class="ph"><div><h1>Relevance by field</h1><p>How much it counts where a search word is found: in the title, the brand, the category, the specs. And extra weight for single specs, so a search that names a material or a port size ranks the items that have it first.</p></div><span class="sp"></span><button class="btn" data-a="reset" data-s="relevance">Reset this page to defaults</button></div>
    <div class="grid g12"><div><div class="card"><h3>Fields</h3><div class="scale">×1 is the built-in weight. Title, brand, category and specs act on the final order: an item gets (weight − 1) × the share of the search's words found in that field, in ranker points. Title ×1.5 on “brass ball valve” adds +0.5 to an item whose title has all three words and +0.17 to one whose title has one. Items that all have the words in the same place move together; the weight sorts items whose words are in different places.</div>
      ${M.fields.map(fieldRow).join("")}</div>
      <div class="card mt"><h3>Specs</h3><div class="scale">Points added to the ranker's score when a search word matches that spec on the item (+1.0 moves an item several places). A spec matches when all its words are in the search (“Full Port”, “Direct Vent”), or half of them including one that tells its values apart, so “Standard Port” doesn't match “full port ball valve”. Negative values push matching items down.</div>
      <form class="row" data-f="relf" style="margin:6px 0 4px"><input class="in" id="relq" value="${esc(REL.filter)}" placeholder="Find a spec (material, port, gpm, finish…)" aria-label="Find a spec"></form>
      ${set.length ? `<h4 class="small muted" style="margin:10px 0 0">Weighted</h4>${set.map(attrRow).join("")}` : ""}
      <h4 class="small muted" style="margin:10px 0 0">${set.length ? "Other specs" : "All specs"} <span class="muted">(${rest.length}, most common first)</span></h4>${rest.slice(0, q ? 80 : 24).map(attrRow).join("")}
      ${!q && rest.length > 24 ? `<div class="muted small" style="padding:8px 0">${rest.length - 24} more: find one by name above.</div>` : ""}</div></div>
      <div>${previewCard()}</div></div>`;
  runPreview();
};
function setRel(kind, key, v) {
  const D = A.draft.relevance, L = A.live.relevance, [lo, hi] = kind === "fields" ? [0, 3] : [-3, 3];
  v = Math.max(lo, Math.min(hi, Math.round(parseFloat(v) * 100) / 100)); if (Number.isNaN(v)) return;
  if (kind === "attrs") { if (v) D.attrs[key] = v; else delete D.attrs[key]; } else D.fields[key] = v;
  const a = kind === "fields" ? "rf" : "ra", r = $(`[data-${a}="${key}"]`), n = $(`[data-${a}n="${key}"]`);
  if (r) r.value = v; if (n && document.activeElement !== n) n.value = v;
  const live = kind === "fields" ? L.fields[key] : (L.attrs[key] || 0);
  const b = r && r.closest(".wrow").querySelector("b"); if (b) b.classList.toggle("chg", v !== live);
  renderDraftBar(); previewSoon();
}

PAGES.personal = async (el) => {
  const D = A.draft, L = A.live;
  el.innerHTML = `<div class="ph"><div><h1>Personalization</h1><p>Switch each personalized part of search on or off for every signed-in customer. Guests are never personalized.</p></div><span class="sp"></span><button class="btn" data-a="reset" data-s="personalization">Reset to defaults</button></div>
    <div class="grid g12"><div class="card">${Object.entries(PERS).map(([k, m]) => `<div class="trow"><label class="sw"><input type="checkbox" data-t="${k}" ${D.personalization[k] ? "checked" : ""} aria-label="${esc(m[0])}"><span></span></label>
      <div><b class="${D.personalization[k] !== L.personalization[k] ? "chg" : ""}" style="${D.personalization[k] !== L.personalization[k] ? "color:var(--red)" : ""}">${esc(m[0])}</b><div class="desc">${esc(m[1])}</div><div class="where">Shows in: ${esc(m[2])}</div></div></div>`).join("")}</div>
      <div>${previewCard()}</div></div>`;
  runPreview();
};

// ------------------------------------------------------------------ preview (live vs draft)
function previewCard() {
  const opts = [`<option value="">Guest (not signed in)</option>`].concat(A.users.map((a) => `<optgroup label="${esc(a.name)} · ${esc(a.segment.replace(/_/g, " "))}">${a.users.map((u) => `<option value="${esc(a.account_id)}|${esc(u.user_id)}" ${A.pv.who === `${a.account_id}|${u.user_id}` ? "selected" : ""}>${esc(u.name)} (${esc(u.role)})</option>`).join("")}</optgroup>`));
  return `<div class="card pv"><h3>Preview <span class="sub">live vs your draft</span></h3>
    <form class="pvctl" data-f="preview"><input class="in" id="pvq" value="${esc(A.pv.q)}" placeholder="Search words" aria-label="Preview search"><select class="in" id="pvwho" aria-label="As account">${opts.join("")}</select><button class="btn dark">Compare</button></form>
    <div id="pvout"><div class="muted small">Loading…</div></div></div>`;
}
async function runPreview() {
  const out = $("#pvout"); if (!out) return;
  const q = ($("#pvq") || {}).value || A.pv.q, w = ($("#pvwho") || {}).value ?? A.pv.who;
  A.pv = { q, who: w };
  const [account, user] = w ? w.split("|") : [null, null];
  let r;
  try { r = await adm("/api/admin/preview", { method: "POST", body: { q, account, user, patch: patch(), n: 15 } }); }
  catch (e) { out.innerHTML = `<div class="explain">${esc(e.message)}</div>`; return; }
  if (!$("#pvout")) return;
  const item = (it, k, draft) => {
    const mv = draft ? (it.was == null ? `<span class="mv new" title="not in the live top ${300}">new</span>` : it.move > 0 ? `<span class="mv up">▲${it.move}</span>` : it.move < 0 ? `<span class="mv dn">▼${-it.move}</span>` : "") : "";
    return `<div class="pit ${it.history ? "bought" : ""} ${it.pinned ? "pinned" : ""} ${draft && it.was == null ? "new" : ""}" title="${esc(it.title)}"><span class="k">${k + 1}</span><span class="tt">${esc(it.title)}<br><small>${esc(it.brand)} · #${esc(it.sku)}${it.history ? ` · bought ${it.history.times}×` : ""}${it.pinned ? ` · ${esc(it.badge || "pinned")}` : ""}${it.at_branch ? ` · ${it.at_branch} on shelf` : ""}${it.tier ? ` · tier ${esc(it.tier)}` : ""}</small>${adjChips(it.adj)}</span>${mv}</div>`;
  };
  const mb = (m) => m && (m.rewritten_from || m.rules.length || m.banner) ? `<div class="row small" style="margin-bottom:6px">${m.rewritten_from ? `<span class="chip">searched “${esc(r.q)}” as rewritten</span>` : ""}${m.rules.map((x) => `<span class="chip dark">${esc(x.name)}</span>`).join("")}${m.banner ? `<span class="chip info">banner</span>` : ""}</div>` : "";
  out.innerHTML = `${r.changed.length ? "" : `<div class="pvnote" style="margin:0 0 8px">No unpublished changes: both columns use the live settings. Move a slider or flip a switch to compare.</div>`}
    <div class="pvcols"><div><h4>Live</h4>${mb(r.live.merch)}${r.live.items.map((it, k) => item(it, k, false)).join("") || `<div class="muted small">No products.</div>`}</div>
    <div><h4>Draft ${r.changed.length ? `<span class="chip red">${r.changed.length} change${r.changed.length > 1 ? "s" : ""}</span>` : ""}</h4>${mb(r.draft.merch)}${r.draft.items.map((it, k) => item(it, k, true)).join("") || `<div class="muted small">No products.</div>`}</div></div>
    <div class="pvnote">${r.principal.account ? `As ${esc(r.principal.name)} (${esc(r.principal.role)}, ${esc(segName(r.principal.segment))}).` : "As a guest: purchase and brand weights don't apply."} Green rows: bought before. Red outline: pinned. Chips: what the settings added to the ranker's score.</div>`;
}
// what moved an item: the merchandising adjustments added to the ranker's score (bought before, brand affinity, rules...)
function adjChips(adj) {
  const e = Object.entries(adj || {}).filter(([, v]) => Math.abs(v) >= 0.01);
  return e.length ? `<span class="adjs">${e.map(([k, v]) => `<span class="adj ${v > 0 ? "up" : "dn"}" title="Added to the ranker's score">${esc(k)} ${v > 0 ? "+" : ""}${(+v).toFixed(2)}</span>`).join("")}</span>` : "";
}
let pvT;
function previewSoon() { clearTimeout(pvT); pvT = setTimeout(runPreview, 350); }

// ------------------------------------------------------------------ rules
function ruleStatus(r) {
  const today = new Date().toISOString().slice(0, 10);
  if (!r.enabled) return "off"; if (r.start && today < r.start) return "scheduled"; if (r.end && today > r.end) return "expired"; return "active";
}
const STOCK_LABEL = { dc: "CD" };
const condText = (c) => c.field === "leadfree" ? "lead-free" : `${{ family: "category", sku: "item", stock: "stocking", title: "title has", attr: "spec", status: "status" }[c.field] || c.field} ${c.field === "stock" ? STOCK_LABEL[c.value] || c.value : c.value}`;
function ruleSummary(r) {
  const p = [];
  if (r.pin.length) p.push(`${r.pin.length} pinned${r.badge && r.badge !== "Featured" ? ` (“${r.badge}”)` : ""}`); if (r.hide.length) p.push(`${r.hide.length} hidden`);
  const up = r.boost.filter((b) => b.weight > 0).length, dn = r.boost.filter((b) => b.weight < 0).length;
  if (up) p.push(`${up} boosted`); if (dn) p.push(`${dn} buried`);
  if ((r.only || []).length) p.push(`only ${r.only.map(condText).join(", ")}`);
  if ((r.exclude || []).length) p.push(`hides ${r.exclude.map(condText).join(", ")}`);
  if (r.banner) p.push("banner"); if (r.redirect) p.push(r.redirect.auto ? "redirects" : "link to a page");
  return p.join(" · ") || "no actions";
}
function audText(a) {
  a = a || {};
  const w = { all: "Everyone", guests: "Guests", signed_in: "Signed-in" }[a.who || "all"];
  const x = [...(a.segments || []).map(segName), ...(a.roles || [])];
  return x.length ? `${w === "Everyone" ? "Signed-in" : w}: ${x.join(", ")}` : w;
}
PAGES.rules = async (el) => {
  const R = A.draft.rules, liveIds = new Set(A.live.rules.map((r) => r.id));
  el.innerHTML = `<div class="ph"><div><h1>Merchandising rules</h1><p>A rule acts on the searches it matches (exact words, containing words, several phrases as one campaign, or every search), for the customers it targets (everyone, guests, signed-in; by discipline and role), between its dates. It can pin items with a badge, hide items, boost or bury by brand, category, item, stocking level, title words, spec or lead-free, filter the page to what matches, show a banner, and send the search to a page.</p></div><span class="sp"></span>${periodSeg()}<button class="btn pri" data-a="newrule">New rule</button></div>
    <div class="card" id="rulesTbl">Loading…</div>`;
  const rep = await adm(`/api/admin/rules/report?days=${A.days}`).catch(() => ({ all: {}, rules: {} }));
  if (!$("#rulesTbl")) return;
  const all = rep.all || {};
  const perf = (r) => { const x = (rep.rules || {})[r.id]; if (!x) return `<span class="muted small">no searches yet</span>`;
    const c = (v, b) => v == null ? "—" : `<span class="${b != null && v > b ? "okc" : b != null && v < b ? "badc" : ""}">${pct(v, 0)}</span>`;
    return `<span class="small">${num(x.searches)} searches · clicked ${c(x.ctr, all.ctr)} · cart ${c(x.atc_rate, all.atc_rate)}</span>`; };
  $("#rulesTbl").innerHTML = R.length ? `<table class="t"><tr><th style="width:50px">On</th><th>Rule</th><th>When someone searches</th><th>Who</th><th>Does</th><th>Dates</th><th>Last ${A.days} days <span class="muted" title="Compared with all searches: ${pct(all.ctr, 0)} clicked, ${pct(all.atc_rate, 0)} added to cart">vs all</span></th><th>Status</th><th></th></tr>
      ${R.map((r) => `<tr><td><label class="sw"><input type="checkbox" data-rule-on="${esc(r.id)}" ${r.enabled ? "checked" : ""} aria-label="Rule on"><span></span></label></td>
        <td class="q"><span class="link" data-a="editrule" data-id="${esc(r.id)}">${esc(r.name)}</span>${liveIds.has(r.id) ? "" : ` <span class="chip red">unpublished</span>`}${r.note ? `<div class="muted small">${esc(r.note)}</div>` : ""}</td>
        <td>${r.match.type === "any" ? `<span class="chip">any search</span>` : `<span class="chip">${r.match.type === "exact" ? "is" : "contains"}</span> ${r.match.q.split(" | ").map((x) => `“${esc(x)}”`).join(" or ")}`}</td>
        <td class="small">${esc(audText(r.audience))}</td><td class="small">${esc(ruleSummary(r))}</td>
        <td class="small muted">${esc(r.start || "")}${r.start || r.end ? " → " : "always"}${esc(r.end || "")}</td><td>${perf(r)}</td><td><span class="status ${ruleStatus(r)}">${ruleStatus(r)}</span></td>
        <td style="white-space:nowrap"><button class="btn sm" data-a="editrule" data-id="${esc(r.id)}">Edit</button> <button class="btn sm ghost danger" data-a="delrule" data-id="${esc(r.id)}">Delete</button></td></tr>`).join("")}</table>
      <div class="pvnote">Green: the rule's searches are clicked or carted more than searches overall; red: less. A rule on a busy or hard search starts from a different baseline, so compare it with the same search before the rule (the search drill-down) or run it as an A/B test.</div>`
    : `<div class="empty"><b>No rules yet.</b>Start from a search that needs help: the Overview's suggestions and the relevance audit's misses have a Pin button that fills one in.</div>`;
  if (A.openRule) { const r = A.draft.rules.find((x) => x.id === A.openRule); A.openRule = null; if (r) editRule(r, false); }
};
function ruleDefaults(r) {
  return { audience: { who: "all", segments: [], roles: [] }, only: [], exclude: [], boost: [], pin: [], hide: [], badge: "Featured", redirect: null, banner: null, note: "", ...r };
}
function newRule(q, sku) {
  const r = ruleDefaults({ id: "r" + Date.now().toString(36), name: q ? `Pin for “${q}”` : "New rule", enabled: true, match: { type: q ? "exact" : "contains", q: q || "" },
    start: null, end: null, pin: sku ? [{ sku, pos: 1 }] : [] });
  editRule(r, true);
}
function editRule(r0, isNew) {
  A.ed = { r: ruleDefaults(clone(r0)), isNew, titles: {}, cf: { boost: "brand", only: "family", exclude: "brand" } };
  openDrawer(`${isNew ? "New" : "Edit"} rule`, `<div id="ed"></div>`);
  drawEditor();
}
const LOOKUP_FIELDS = { brand: "brand", family: "family", sku: "sku" };
const FIELD_NAMES = { brand: "Brand", family: "Category", sku: "Item", stock: "Stocking level", title: "Title words", attr: "Spec (key=value)", leadfree: "Lead-free", status: "Item status" };
function condBuilder(kind, withWeight) {
  const r = A.ed.r, f = A.ed.cf[kind], list = r[kind];
  const rows = list.map((c, i) => `<div class="li"><span class="tt"><span class="chip">${esc(FIELD_NAMES[c.field] || c.field)}</span> <b>${esc(c.field === "leadfree" ? "yes" : c.value)}</b></span>
    ${withWeight ? `<span class="row small"><span class="muted">bury</span><input type="range" min="-10" max="10" step="0.5" value="${c.weight}" data-bw="${i}" aria-label="Weight"><span class="muted">boost</span><b class="num" style="width:36px;text-align:right">${c.weight > 0 ? "+" : ""}${c.weight}</b></span>` : "<span></span>"}
    <button class="btn sm ghost" data-a="rmcond" data-k="${kind}" data-i="${i}">Remove</button></div>`).join("");
  const valueInput = f === "leadfree" ? `<span class="muted small" style="flex:1">items with “lead-free” in the title</span>`
    : f === "stock" || f === "status" ? `<select class="in" data-cbv="${kind}" style="flex:1">${((f === "stock" ? A.meta.stock_values : A.meta.status_values) || []).map((v) => `<option value="${v}">${f === "stock" ? STOCK_LABEL[v] || v : v}</option>`).join("")}</select>`
    : `<div class="ac" style="flex:1"><input class="in" data-cbv="${kind}" ${LOOKUP_FIELDS[f] ? `data-ac="${LOOKUP_FIELDS[f]}" data-add="${kind}"` : ""} placeholder="${esc({ brand: "Brand name", family: "Category, e.g. Ball Valves", sku: "Item # or search words", title: "e.g. propress", attr: "e.g. material=Brass" }[f] || "")}" style="width:100%"></div>`;
  return `<div class="lst">${rows}</div><div class="row mt"><select class="in" data-cbf="${kind}">${Object.entries(FIELD_NAMES).map(([k, l]) => `<option value="${k}" ${k === f ? "selected" : ""}>${l}</option>`).join("")}</select>${valueInput}<button class="btn sm" data-a="cbadd" data-k="${kind}">Add</button></div>`;
}
function drawEditor() {
  const r = A.ed.r, T = A.ed.titles, au = r.audience;
  const skuRow = (list, i, withPos) => { const x = withPos ? list[i].sku : list[i];
    return `<div class="li"><span class="tt"><b>#${esc(x)}</b> ${esc(T[x] || "")}</span>${withPos ? `<label class="small muted">position <input class="in num" data-pos="${i}" value="${list[i].pos}" inputmode="numeric"></label>` : "<span></span>"}<button class="btn sm ghost" data-a="${withPos ? "rmpin" : "rmhide"}" data-i="${i}">Remove</button></div>`; };
  const H = (t, sub) => `<div class="sect">${t}${sub ? ` <span class="muted" style="text-transform:none;letter-spacing:0;font-weight:400">${sub}</span>` : ""}</div>`;
  $("#ed").innerHTML = `<div class="edgrid"><label class="fl">Name<input class="in" id="edName" value="${esc(r.name)}" maxlength="80"></label>
      <label class="fl">Note (optional)<input class="in" id="edNote" value="${esc(r.note || "")}" maxlength="300" placeholder="e.g. Viega promo, Oct–Nov"></label></div>
    ${H("When someone searches", "several phrases separated by | make one campaign")}
    <div class="row"><div class="seg" id="edType">${[["exact", "Exactly"], ["contains", "Containing"], ["any", "Any search"]].map(([k, l]) => `<button type="button" class="${r.match.type === k ? "on" : ""}" data-a="edtype" data-v="${k}">${l}</button>`).join("")}</div>
      <input class="in" id="edQ" value="${esc(r.match.q)}" placeholder="e.g. propress | press fitting | viega press" style="flex:1" ${r.match.type === "any" ? "disabled" : ""}></div>
    ${H("Who sees it")}
    <div class="row"><div class="seg">${[["all", "Everyone"], ["guests", "Guests"], ["signed_in", "Signed-in customers"]].map(([k, l]) => `<button type="button" class="${au.who === k ? "on" : ""}" data-a="audwho" data-v="${k}">${l}</button>`).join("")}</div></div>
    ${au.who === "guests" ? "" : `<div class="chips mt"><span class="muted small">Disciplines</span>${Object.keys(A.meta.segments || {}).map((k) => `<button type="button" class="pick ${au.segments.includes(k) ? "on" : ""}" data-a="audseg" data-v="${k}">${esc(segName(k))}</button>`).join("")}</div>
      <div class="chips mt"><span class="muted small">Roles</span>${(A.meta.roles || []).map((k) => `<button type="button" class="pick ${au.roles.includes(k) ? "on" : ""}" data-a="audrole" data-v="${k}">${esc(k)}</button>`).join("")}</div>
      <div class="pvnote">None picked: every signed-in customer${au.who === "all" ? " and guests" : ""}. Picking a discipline or role limits the rule to signed-in customers of it.</div>`}
    <div class="row mt"><label class="fl">Starts<input class="in" type="date" id="edStart" value="${esc(r.start || "")}"></label><label class="fl">Ends<input class="in" type="date" id="edEnd" value="${esc(r.end || "")}"></label>
      <label class="fl" style="margin-left:auto">Status<span class="row" style="height:32px"><label class="sw"><input type="checkbox" id="edOn" ${r.enabled ? "checked" : ""}><span></span></label> on</span></label></div>
    ${H("Pin items", "up to 12, positions 1–24")}
    <div class="lst">${r.pin.map((_, i) => skuRow(r.pin, i, true)).join("")}</div>
    <div class="ac mt"><input class="in" data-ac="sku" data-add="pin" placeholder="Add an item: item # or search words" style="width:100%"></div>
    <div class="row mt"><label class="fl">Badge on pinned items<input class="in" id="edBadge" value="${esc(r.badge || "Featured")}" maxlength="24" list="badgeList"></label><datalist id="badgeList"><option>Featured</option><option>Sponsored</option><option>New</option><option>Webb pick</option><option>On sale</option></datalist></div>
    ${H("Hide items")}
    <div class="lst">${r.hide.map((_, i) => skuRow(r.hide, i, false)).join("")}</div>
    <div class="ac mt"><input class="in" data-ac="sku" data-add="hide" placeholder="Hide an item: item # or search words" style="width:100%"></div>
    ${H("Boost or bury")}${condBuilder("boost", true)}
    <div class="pvnote">+3 moves matching items well up the page; +10 puts them on top of everything but exact part-number matches. Negative values bury. Boosts on several conditions add up.</div>
    ${H("Only show", "the page keeps only items matching any of these")}${condBuilder("only", false)}
    ${H("Hide by condition", "items matching any of these leave the page")}${condBuilder("exclude", false)}
    ${H("Banner", "shown above the results")}
    <div class="edgrid"><input class="in" id="edBanner" value="${esc((r.banner || {}).text || "")}" placeholder="e.g. Viega ProPress: 10% off through Nov 30" maxlength="200">
      <input class="in" id="edLink" value="${esc((r.banner || {}).link || "")}" placeholder="Link (https://… or /…), optional" maxlength="300"></div>
    ${H("Send to a page", "e.g. “returns” → the returns page")}
    <div class="row"><input class="in" id="edRedir" value="${esc((r.redirect || {}).url || "")}" placeholder="https://www.fwwebb.com/… , /… or #tab=kits" maxlength="300" style="flex:1">
      <label class="row small" title="On: a search on this site goes straight to the page. Off, or a link to another site: the results page shows a “Go to it” link."><label class="sw"><input type="checkbox" id="edRedirAuto" ${(r.redirect || {}).auto ? "checked" : ""}><span></span></label> go straight there</label></div>
    <div class="row mt" style="border-top:1px solid var(--line);padding-top:12px"><button class="btn" data-a="edpreview">Preview</button><span class="sp"></span><span class="small muted">Saves to your draft; Publish makes it live.</span><button class="btn" data-a="closedrawer">Cancel</button><button class="btn pri" data-a="edsave">Save to draft</button></div>
    <div id="edpv" class="mt"></div>`;
  const missing = [...r.pin.map((p) => p.sku), ...r.hide].filter((s) => !T[s]);
  if (missing.length) Promise.all(missing.map((s) => adm(`/api/admin/lookup?kind=sku&q=${encodeURIComponent(s)}&limit=1`).then((x) => { if (x[0] && x[0].value === s) T[s] = x[0].label; }).catch(() => {}))).then(() => { if ($("#ed") && missing.some((s) => T[s])) { readEditor(); drawEditor(); } });
}
function readEditor() {
  const r = A.ed.r; if (!$("#edName")) return r;
  r.name = $("#edName").value.trim() || r.name; r.note = $("#edNote").value.trim();
  r.match.q = r.match.type === "any" ? "" : $("#edQ").value.trim().toLowerCase();
  r.start = $("#edStart").value || null; r.end = $("#edEnd").value || null; r.enabled = $("#edOn").checked;
  $$("[data-pos]").forEach((i) => (r.pin[+i.dataset.pos].pos = Math.max(1, Math.min(24, parseInt(i.value) || 1))));
  const t = $("#edBanner").value.trim(); r.banner = t ? { text: t, link: $("#edLink").value.trim() } : null;
  const u = $("#edRedir").value.trim(); r.redirect = u ? { url: u, auto: $("#edRedirAuto").checked } : null;
  r.badge = $("#edBadge").value.trim() || "Featured";
  return r;
}
function addCond(kind, value) {
  const r = readEditor(), f = A.ed.cf[kind];
  let v = String(value ?? "").trim();
  if (f === "leadfree") v = "yes";
  if (!v) return toast("Pick or type a value first", true);
  if (f === "attr" && !v.includes("=")) return toast("A spec looks like key=value, e.g. material=Brass", true);
  if (r[kind].some((c) => c.field === f && c.value.toLowerCase() === v.toLowerCase())) return toast("Already there", true);
  r[kind].push(kind === "boost" ? { field: f, value: v, weight: 3 } : { field: f, value: v });
  drawEditor();
}
function draftWithEdited() {
  const r = readEditor(), rules = A.draft.rules.filter((x) => x.id !== r.id);
  const i = A.draft.rules.findIndex((x) => x.id === r.id);
  rules.splice(i < 0 ? rules.length : i, 0, r);
  return rules;
}
async function edPreview() {
  const rules = draftWithEdited(), r = A.ed.r, q = r.match.type === "any" ? (A.pv.q || "ball valve") : r.match.q.split(" | ")[0];
  if (!q) { toast("Add the search words first", true); return; }
  let [account, user] = A.pv.who ? A.pv.who.split("|") : [null, null];
  if (r.audience.who === "guests") account = user = null;
  else if (r.audience.segments.length || r.audience.who === "signed_in") {
    // preview as a customer the rule targets
    const cur = A.users.find((u) => u.account_id === account);
    if (!cur || (r.audience.segments.length && !r.audience.segments.includes(cur.segment))) {
      const pick = A.users.find((u) => !r.audience.segments.length || r.audience.segments.includes(u.segment));
      if (pick) { account = pick.account_id; user = (pick.users.find((x) => !r.audience.roles.length || r.audience.roles.includes(x.role)) || pick.users[0]).user_id; }
    }
  }
  try {
    const p = { ...patch(), rules };
    const x = await adm("/api/admin/preview", { method: "POST", body: { q, account, user, patch: p, n: 12 } });
    const rd = x.draft.merch && x.draft.merch.redirect;
    $("#edpv").innerHTML = `<div class="card"><h3>“${esc(q)}” <span class="sub">live vs with this rule, as ${esc(x.principal.name || "a guest")}${x.principal.segment ? ` (${esc(segName(x.principal.segment))})` : ""}</span></h3>
      ${!x.draft.merch || !x.draft.merch.rules.some((m) => m.id === r.id) ? `<div class="explain" style="margin-bottom:8px">This rule doesn't apply to this search for this customer (check the words, who sees it, dates and status).</div>` : ""}
      ${rd ? `<div class="explain" style="margin-bottom:8px">Sends the search to ${esc(rd.url)}${rd.auto ? " straight away" : " (a “Go to it” link on the page)"}.</div>` : ""}
      <div class="pvcols">${["live", "draft"].map((k) => `<div><h4>${k === "live" ? "Live" : "With this rule"} <span class="muted small">${num(x[k].total)} results</span></h4>${x[k].merch && x[k].merch.banner ? `<div class="explain" style="margin-bottom:6px">${esc(x[k].merch.banner.text)}</div>` : ""}${x[k].items.map((it, i) => `<div class="pit ${it.pinned ? "pinned" : ""}"><span class="k">${i + 1}</span><span class="tt">${esc(it.title)}<br><small>${esc(it.brand)} · #${esc(it.sku)}${it.pinned ? ` · ${esc(it.badge || "pinned")}` : ""}</small>${k === "draft" ? adjChips(it.adj) : ""}</span>${k === "draft" && it.move ? `<span class="mv ${it.move > 0 ? "up" : "dn"}">${it.move > 0 ? "▲" : "▼"}${Math.abs(it.move)}</span>` : k === "draft" && it.was == null ? `<span class="mv new">new</span>` : ""}</div>`).join("")}</div>`).join("")}</div></div>`;
  } catch (e) { $("#edpv").innerHTML = `<div class="explain">${esc(e.message)}</div>`; }
}
async function edSave() {
  const rules = draftWithEdited();
  try { await adm("/api/admin/preview", { method: "POST", body: { q: "validate", patch: { rules }, n: 5 } }); }
  catch (e) { toast(e.message, true); return; }
  A.draft.rules = rules; closeDrawer(); render(); toast("Rule saved to your draft. Publish to make it live.");
}

// autocomplete for items, brands, categories (the rule editor, and the Bulk order forms with data-add="fill")
let acT, acSeq = 0;
async function acSearch(inp) {
  const my = ++acSeq, q = inp.value.trim(), box = inp.parentElement;
  let list = $(".acl", box); if (!list) { list = document.createElement("div"); list.className = "acl"; box.appendChild(list); }
  if (!q) { list.remove(); return; }
  const rows = await adm(`/api/admin/lookup?kind=${inp.dataset.ac}&q=${encodeURIComponent(q)}`).catch(() => []);
  if (my !== acSeq) return;
  list.innerHTML = rows.map((r) => `<div data-a="acpick" data-v="${esc(r.value)}" data-l="${esc(r.label)}">${inp.dataset.ac === "sku" ? `<b>#${esc(r.value)}</b> ${esc(r.label)}<br><small>${esc(r.brand)} · ${esc(r.family)}</small>` : `${esc(r.label)} <small>${num(r.n)} items</small>`}</div>`).join("") || `<div class="muted">No matches</div>`;
}
function acPick(el) {
  const inp = $("input", el.closest(".ac")), v = el.dataset.v, add = inp.dataset.add;
  if (add === "fill") { inp.value = inp.dataset.ac === "sku" ? v : el.dataset.l; inp.dataset.val = v; inp.title = el.dataset.l; const l = $(".acl", el.closest(".ac")); if (l) l.remove(); inp.dispatchEvent(new Event("picked", { bubbles: true })); return; }
  if (add === "bp") { bpAdd(v); return; }
  const r = readEditor();
  if (add === "pin") { if (r.pin.length >= 12) return toast("Up to 12 pinned items", true); if (!r.pin.some((p) => p.sku === v)) r.pin.push({ sku: v, pos: r.pin.length + 1 }); A.ed.titles[v] = el.dataset.l; }
  if (add === "hide") { if (!r.hide.includes(v)) r.hide.push(v); A.ed.titles[v] = el.dataset.l; }
  if (["boost", "only", "exclude"].includes(add)) return addCond(add, v);
  drawEditor();
}

// ------------------------------------------------------------------ rewrites
PAGES.synonyms = async (el) => {
  const R = A.draft.rewrites, liveIds = new Set(A.live.rewrites.map((r) => r.id));
  el.innerHTML = `<div class="ph"><div><h1>Rewrites</h1><p>When a search contains these words (whole words, any case), search the replacement instead. One way only: “tstat → thermostat” doesn't change searches for “thermostat”. The results page says what it searched.</p></div></div>
    <div class="grid g21"><div class="card"><h3>Rewrites <span class="sub">${R.length}</span></h3>
      <form class="row" data-f="addrw" style="margin-bottom:10px"><input class="in" id="rwFrom" placeholder="People type… e.g. tstat" style="flex:1" maxlength="60"><span class="muted">→</span><input class="in" id="rwTo" placeholder="Search for… e.g. thermostat" style="flex:1" maxlength="120"><button class="btn pri">Add</button></form>
      ${R.length ? `<table class="t"><tr><th style="width:50px">On</th><th>People type</th><th>Searched as</th><th></th></tr>${R.map((r) => `<tr><td><label class="sw"><input type="checkbox" data-rw-on="${esc(r.id)}" ${r.enabled ? "checked" : ""} aria-label="Rewrite on"><span></span></label></td><td class="q">${esc(r.from)}${liveIds.has(r.id) ? "" : ` <span class="chip red">unpublished</span>`}</td><td>${esc(r.to)}</td><td><button class="btn sm ghost" data-a="pvrw" data-q="${esc(r.from)}">Preview</button> <button class="btn sm ghost danger" data-a="delrw" data-id="${esc(r.id)}">Delete</button></td></tr>`).join("")}</table>` : `<div class="empty">No rewrites yet.</div>`}</div>
      <div class="card"><h3>Searches that found nothing <span class="sub">last 90 days</span></h3><div id="rwSug">Loading…</div></div></div>`;
  const ins = await adm("/api/admin/insights?days=90");
  if (!$("#rwSug")) return;
  const have = new Set(R.map((r) => r.from));
  const z = (ins.zero_results || []).filter((x) => !have.has(x.qn));
  $("#rwSug").innerHTML = z.length ? `<table class="t">${z.slice(0, 15).map((x) => `<tr><td class="q">${esc(x.q)} ${outcomeChip(x.outcome)}</td><td class="n">${x.zero}×</td><td><button class="btn sm" data-a="newrw" data-q="${esc(x.qn)}">Rewrite…</button></td></tr>`).join("")}</table>` : `<div class="empty">None logged. Searches with no full match show here.</div>`;
  if (A.rwPrefill) { $("#rwFrom").value = A.rwPrefill; A.rwPrefill = null; $("#rwTo").focus(); }
};

// ------------------------------------------------------------------ insights and recommendations
const PRI = { high: "High", medium: "Medium", low: "Low" };
function recCard(r) {
  const done = A.recApplied.get(r.id);
  const acts = r.actions.map((a, k) => a.type === "draft"
      ? (done === k ? `<span class="chip ok indraft">✓ ${esc(a.label.replace(/ (to|in) draft$/i, ""))}: in your draft</span>`
        : `<button class="btn sm ${done == null ? "pri" : ""}" data-a="recapply" data-id="${r.id}" data-k="${k}" ${done != null ? "disabled" : ""}>${esc(a.label)}</button>`)
      : a.type === "query" ? `<button class="btn sm" data-a="query" data-q="${esc(a.q)}">${esc(a.label)}</button>`
      : `<button class="btn sm" data-a="recgo" data-id="${r.id}" data-k="${k}">${esc(a.label)}</button>`).join("");
  return `<div class="rec p-${r.priority} ${r.dismissed ? "dis" : ""}" data-rec="${r.id}">
    <div class="rh"><span class="prio ${r.priority}">${PRI[r.priority]}</span><span class="chip">${esc((A.recs.labels || {})[r.cat] || r.cat)}</span>
      ${r.source ? `<span class="src">${esc(r.source)}${/replay/.test(r.source) ? " · tested" : ""}</span>` : ""}<span class="sp"></span>
      ${r.dismissed ? `<span class="muted small">dismissed by ${esc(r.dismissed.who || "someone")}, ${esc(r.dismissed.at)}</span><button class="btn sm ghost" data-a="recundo" data-id="${r.id}">Restore</button>`
        : `<button class="btn sm ghost" data-a="recdismiss" data-id="${r.id}" title="Hide it for everyone; it stays under Dismissed">Dismiss</button>`}</div>
    <b class="rt">${esc(r.title)}</b><div class="rd">${esc(r.detail)}</div>
    ${r.evidence.length ? `<ul class="re">${r.evidence.map((e) => `<li>${esc(e)}</li>`).join("")}</ul>` : ""}
    ${r.impact ? `<div class="ri">Expected: ${esc(r.impact)}</div>` : ""}
    ${acts ? `<div class="ra">${acts}</div>` : ""}</div>`;
}
PAGES.recs = async (el) => {
  el.innerHTML = `<div class="ph"><div><h1>Recommendations</h1><p>Trends, search gaps, data issues and changes worth making, from the live log, the historical log, Bulk order, the catalog and the settings themselves. Changes marked <b>tested</b> were replayed on logged searches, as the accounts that searched, before being recommended. <b>Add to draft</b> puts a change in your draft: preview it, test it in the audit or an A/B test, then publish.</p></div><span class="sp"></span>${periodSeg()}</div>
    <div class="card" id="rcDeep">Loading…</div><div id="rc" class="mt"><div class="muted small">Reading the logs…</div></div>`;
  const d = await adm(`/api/admin/recs?days=${A.days}`);
  if (A.page !== "recs") return;
  A.recs = d;
  $("#nRecs").textContent = d.counts.high || "";
  drawDeep(d.deep);
  if (d.deep.running) pollDeep();
  drawRecs();
};
function drawRecs() {
  const d = A.recs, f = A.recFilter || "all", out = $("#rc"); if (!out || !d) return;
  const live = d.recs.filter((r) => !r.dismissed), dis = d.recs.filter((r) => r.dismissed);
  const shown = (f === "dismissed" ? dis : live).filter((r) => f === "all" || f === "dismissed" || (f === "high" ? r.priority === "high" : r.cat === f));
  const chip = (k, lab, n) => `<button class="${f === k ? "on" : ""}" data-a="recfilter" data-f="${k}">${lab}${n != null ? ` <b>${n}</b>` : ""}</button>`;
  const T = d.trends || {}, dp = d.deep || {};
  out.innerHTML = `<div class="seg recseg">${chip("all", "All", live.length)}${chip("high", "High priority", d.counts.high)}${Object.entries(d.labels).filter(([k]) => d.counts[k] || f === k).map(([k, l]) => chip(k, l, d.counts[k])).join("")}${dis.length ? chip("dismissed", "Dismissed", dis.length) : ""}</div>
    <div class="recs mt">${(f === "all" && !A.recMore ? shown.slice(0, 12) : shown).map(recCard).join("") || `<div class="empty">${f === "dismissed" ? "Nothing dismissed." : "Nothing in this group. Run the replay analysis for tested ranking and rule changes."}</div>`}</div>
    ${f === "all" && !A.recMore && shown.length > 12 ? `<div class="row mt" style="justify-content:center"><button class="btn" data-a="recmore">Show all ${shown.length} (${shown.length - 12} more, lower priority)</button></div>` : ""}
    <div class="grid g2 mt">
      <div><div class="card"><h3>Search volume by week <span class="sub">${esc((T.window || {}).source || "")}</span></h3>${T.weekly && T.weekly.length ? chart(T.weekly.map((w) => ({ day: w.week, n: w.n })), "day", "n", null, { h: 140, label: "Searches per week" }) : `<div class="empty">No historical log.</div>`}
        ${T.window ? `<div class="pvnote">Movers compare the last 8 full weeks (${esc(T.window.now[0])} to ${esc(T.window.now[1])}) with the 8 before. z is the change in standard deviations; below 2 is noise.</div>` : ""}</div>
      <div class="mt"></div><div class="card"><h3>Searches rising or falling <span class="sub">historical log, z ≥ 2</span></h3>${(T.queries || []).length ? `<table class="t"><tr><th>Search</th><th class="n">Before</th><th class="n">Now</th><th class="n">z</th></tr>${T.queries.map((q) => `<tr class="click" data-a="query" data-q="${esc(q.name)}"><td class="q">${esc(q.name)}</td><td class="n">${q.before}</td><td class="n">${q.now}</td><td class="n ${q.z > 0 ? "okc" : "badc"}">${q.z > 0 ? "+" : ""}${q.z}</td></tr>`).join("")}</table>` : `<div class="empty">No search moved beyond noise.</div>`}
        ${(T.live_queries || []).length ? `<div class="sect">Live log, this period vs the one before</div><table class="t">${T.live_queries.map((q) => `<tr class="click" data-a="query" data-q="${esc(q.name)}"><td class="q">${esc(q.name)}</td><td class="n">${q.before} → ${q.now}</td></tr>`).join("")}</table>` : ""}</div></div>
      <div class="card"><h3>Categories moving <span class="sub">by searches whose click landed in them</span></h3>${(T.categories || []).length ? `<table class="t"><tr><th>Category</th><th class="n">Before</th><th class="n">Now</th><th>Change</th><th class="n">z</th></tr>${T.categories.slice(0, 10).map((c) => `<tr class="${Math.abs(c.z) < 2 ? "faint" : ""}"><td>${esc(c.name)}</td><td class="n">${num(c.before)}</td><td class="n">${num(c.now)}</td><td><span class="mvbar ${c.change >= 0 ? "up" : "dn"}"><i style="width:${Math.min(100, Math.abs(c.change) * 100).toFixed(0)}%"></i></span> ${c.change >= 0 ? "+" : ""}${(c.change * 100).toFixed(0)}%</td><td class="n">${c.z}</td></tr>`).join("")}</table>` : `<div class="empty">No historical log.</div>`}</div>
    </div>
    <div class="card mt"><h3>Data checks <span class="sub">catalog and accounts</span></h3><table class="t"><tr><th>Check</th><th>Status</th><th class="n">Affected</th><th>Detail</th></tr>${(d.checks || []).map((c) => `<tr><td>${esc(c.check)}</td><td><span class="chk ${c.status}">${c.status === "ok" ? "OK" : c.status === "issue" ? "Issue" : "Info"}</span></td><td class="n" style="white-space:nowrap">${esc(c.affected)}</td><td class="muted small">${esc(c.detail)}</td></tr>`).join("")}</table>
      <div class="pvnote">${d.sampled ? `Every ${Math.round(d.sampled.of / d.sampled.n)}th item of ${num(d.sampled.of)} sampled (${num(d.sampled.n)}); counts marked ≈ are scaled up. ` : ""}On real data these run nightly on the full catalog and feed the PIM's work queue.</div></div>
    ${dp.tested && dp.tested.length ? `<div class="card mt"><h3>What the replay tested <span class="sub">${num(dp.n)} logged searches, one change at a time from live settings v${dp.settings_version}</span></h3>
      <table class="t"><tr><th>Change</th><th class="n">Clicked #1</th><th class="n">Top 10</th><th class="n">MRR</th><th class="n">Δ MRR</th><th class="n">Better / worse</th><th>Verdict</th></tr>
      <tr class="base"><td>Live settings (baseline)</td><td class="n">${pct(dp.baseline.hit1)}</td><td class="n">${pct(dp.baseline.hit10)}</td><td class="n">${dp.baseline.mrr.toFixed(3)}</td><td></td><td></td><td></td></tr>
      ${dp.tested.map((t) => `<tr><td>${esc(t.label)}</td><td class="n">${pct(t.metrics.hit1)}</td><td class="n">${pct(t.metrics.hit10)}</td><td class="n">${t.metrics.mrr.toFixed(3)}</td><td class="n ${t.d_mrr > 0 ? "okc" : t.d_mrr < 0 ? "badc" : ""}">${t.d_mrr > 0 ? "+" : ""}${t.d_mrr.toFixed(3)}</td><td class="n">${t.better} / ${t.worse}</td><td><span class="chk ${t.verdict === "recommended" ? "ok" : t.verdict === "worse" ? "issue" : "info"}">${esc(t.verdict)}</span></td></tr>`).join("")}</table>
      <div class="pvnote">A change is recommended when MRR rises by 0.004 or more, clicked-#1 doesn't fall, and at least 3 more searches improve than get worse. Brand affinity is judged per discipline (below), since each discipline has its own weights.</div>
      ${dp.segments && dp.segments.length ? `<div class="sect">Brand affinity by discipline (MRR)</div><table class="t"><tr><th>Discipline</th><th class="n">Searches</th><th class="n">× 0.5</th><th class="n">Live</th><th class="n">× 2</th></tr>${dp.segments.map((g) => { const best = Math.max(g.half.mrr, g.base.mrr, g.double.mrr); const c = (v) => `<td class="n ${v === best && v !== g.base.mrr ? "okc" : ""}">${v.toFixed(3)}</td>`; return `<tr><td>${esc(segName(g.segment))}</td><td class="n">${g.n}</td>${c(g.half.mrr)}${c(g.base.mrr)}${c(g.double.mrr)}</tr>`; }).join("")}</table>` : ""}
      ${dp.rules_checked && dp.rules_checked.length ? `<div class="sect">Rules replayed with and without</div><table class="t"><tr><th>Rule</th><th class="n">Searches</th><th class="n">With it</th><th class="n">Without</th><th class="n">Exact phrases only</th></tr>${dp.rules_checked.map((r) => { const m = (x) => x ? `${pct(x.hit1, 0)} · ${x.mrr.toFixed(2)}` : "—"; return `<tr><td>${esc(r.rule)}</td><td class="n">${r.searches || 0}</td><td class="n">${r.live ? m(r.live) : esc(r.note || "")}</td><td class="n">${m(r.without)}</td><td class="n">${m(r.narrowed)}</td></tr>`; }).join("")}</table><div class="pvnote">Each cell: clicked item #1 · MRR on the logged searches the rule matches.</div>` : ""}
      ${dp.pins_checked ? `<div class="pvnote">Head searches checked: ${dp.pins_checked.queries}; ${dp.pins_checked.candidates} had the item most customers pick below #3 for guests; ${dp.pins_checked.recommended} got better with it lifted.${dp.discontinued ? ` Discontinued items on page one: ${dp.discontinued.queries} of those searches.` : ""}</div>` : ""}</div>` : ""}`;
}
function drawDeep(dp) {
  const el = $("#rcDeep"); if (!el) return;
  const pctDone = dp.total ? Math.min(99, Math.round(100 * dp.done / dp.total)) : 0;
  el.innerHTML = `<div class="row"><div style="flex:1"><h3 style="margin:0 0 4px">Replay analysis</h3>
      <div class="small muted">Tries each ranking weight up and down, the brand cap, brand affinity per discipline, lifting the item customers pick on head searches, and each active rule with and without it, on logged searches replayed as the accounts that searched. About two minutes; searches keep working meanwhile.</div>
      ${dp.at ? `<div class="small mt">Last run ${esc(dp.at)}${dp.by ? ` by ${esc(dp.by)}` : ""} on ${num(dp.n)} searches in ${dp.seconds} s, settings v${dp.settings_version}.${dp.stale ? ` <span class="chip warn">settings changed since: re-run</span>` : ""}</div>` : `<div class="small mt"><b>Not run yet.</b> The list below has everything that doesn't need a replay.</div>`}
      ${dp.error ? `<div class="explain mt">The last run stopped: ${esc(dp.error)}</div>` : ""}</div>
    <button class="btn pri" data-a="recrun" ${dp.running ? "disabled" : ""}>${dp.running ? "Running…" : dp.at ? "Run again" : "Run analysis"}</button></div>
    ${dp.running ? `<div class="row small mt"><b>${esc(dp.step || "")}</b><span class="muted">${pctDone}%</span></div><div class="prog mt"><i style="width:${pctDone}%"></i></div>` : ""}`;
}
function pollDeep() {
  clearInterval(A.recPoll);
  A.recPoll = setInterval(async () => {
    const st = await adm("/api/admin/recs/run").catch(() => null);
    if (!st || A.page !== "recs") { clearInterval(A.recPoll); return; }
    drawDeep(st);
    if (!st.running) { clearInterval(A.recPoll); toast(st.error ? "The analysis stopped" : "Analysis done"); render(); }
  }, 1500);
}
function recById(id) { return ((A.recs || {}).recs || []).find((r) => r.id === id); }
function applyOps(ops) {
  for (const o of ops) {
    if (o.op === "set") { let t = A.draft; for (const k of o.path.slice(0, -1)) t = t[k]; t[o.path[o.path.length - 1]] = o.value; }
    else if (o.op === "append") { const L = A.draft[o.list]; const v = clone(o.value); if (o.dedupe && L.some((x) => x[o.dedupe] === v[o.dedupe])) continue; if (L.some((x) => x.id === v.id)) v.id = v.id + Date.now().toString(36).slice(-3); L.push(v); }
    else if (o.op === "patch") { const x = A.draft[o.list].find((r) => r.id === o.id); if (x) Object.assign(x, clone(o.value)); }
    else if (o.op === "remove") { A.draft[o.list] = A.draft[o.list].filter((r) => r.id !== o.id); }
    else if (o.op === "upsert") {
      const L = A.draft[o.list], i = L.findIndex((r) => Object.entries(o.key).every(([k, v]) => String(r[k]).toLowerCase() === String(v).toLowerCase()));
      if (i >= 0) L[i] = { ...L[i], ...clone(o.value), id: L[i].id }; else L.push(clone(o.value));
    }
  }
}

// ------------------------------------------------------------------ brand affinity (per customer discipline)
PAGES.affinity = async (el) => {
  const D = A.draft.affinity, L = A.live.affinity, lim = A.limits.affinity || [-5, 5];
  const cell = (sg, k) => { const v = D.segments[sg][k], ch = v !== L.segments[sg][k];
    return `<td><div class="afc ${ch ? "chg" : ""}"><input type="range" min="${lim[0]}" max="${lim[1]}" step="0.1" value="${v}" data-af="${sg}.${k}" aria-label="${esc(segName(sg))} ${k}"><input class="in num" value="${v}" data-afn="${sg}.${k}" inputmode="decimal" aria-label="${esc(segName(sg))} ${k} value"></div></td>`; };
  const demo = (sg) => A.users.filter((u) => u.segment === sg).map((u) => u.name).slice(0, 2).join(", ");
  el.innerHTML = `<div class="ph"><div><h1>Brand affinity</h1><p>Weights the brands an account buys, not only the items it bought. A contractor who buys NIBCO ball valves sees NIBCO first on a ball valve they've never ordered; one who buys mostly Viega sees Viega press fittings first. Set how much it counts for each customer discipline.</p></div><span class="sp"></span><button class="btn" data-a="reset" data-s="affinity">Reset to defaults</button></div>
    <div class="grid g12"><div>
      <div class="card"><div class="trow"><label class="sw"><input type="checkbox" data-afon ${D.enabled ? "checked" : ""} aria-label="Brand affinity on"><span></span></label>
        <div><b class="${D.enabled !== L.enabled ? "chg" : ""}">Brand affinity ${D.enabled ? "on" : "off"}</b><div class="desc">Signed-in searches only. Guests and accounts with too little history get none.</div></div></div>
        <div class="wrow" style="grid-template-columns:1fr 90px"><div><b class="${D.min_lines !== L.min_lines ? "chg" : ""}">Minimum order lines</b><div class="desc">Accounts with fewer order lines than this get no brand affinity: a handful of orders doesn't say which brands they prefer.</div></div>
          <input class="in num" value="${D.min_lines}" data-afmin inputmode="numeric" aria-label="Minimum order lines"></div>
        <div class="scale">Added to the ranker's score for each item: <b>within</b> × the account's share of this category bought in the item's brand (0 to 1) + <b>across</b> × the brand's share of all the account's order lines. Example: 70% of a plumber's ball valve lines are NIBCO and 20% of all their lines are NIBCO; with within 1.0 and across 1.5 a NIBCO ball valve gets 0.7 + 0.3 = +1.0, a few places up on a typical page. Previously purchased (Ranking weights) is separate and adds on top.</div></div>
      <div class="card mt"><h3>By customer discipline</h3><table class="t aff"><tr><th>Discipline</th><th>Within the category</th><th>Across categories</th></tr>
        ${Object.keys(D.segments).map((sg) => `<tr><td><b>${esc(segName(sg))}</b>${demo(sg) ? `<div class="muted small">e.g. ${esc(demo(sg))}</div>` : ""}</td>${cell(sg, "within")}${cell(sg, "across")}</tr>`).join("")}
        <tr class="allrow"><td class="muted">Set every discipline to</td><td><input class="in num" id="afAllW" placeholder="within" inputmode="decimal"></td><td class="row"><input class="in num" id="afAllA" placeholder="across" inputmode="decimal"><button class="btn sm" data-a="afall">Apply</button></td></tr></table>
        <div class="pvnote">Plumbing and mechanical contractors tend to standardize on a brand per category (within matters more); facilities and MRO buyers follow the brands their buildings already have across categories (across matters more). Start from the defaults, change one discipline, and check it in the audit below before publishing.</div></div>
      <div class="card mt"><h3>Relevance audit by discipline <span class="sub" id="afAuSub"></span></h3><div id="afAu">Loading…</div></div></div>
      <div>${previewCard()}</div></div>`;
  runPreview();
  const st = await adm("/api/admin/audit").catch(() => null);
  if (!$("#afAu")) return;
  const r = st && st.result;
  if (!r || !r.by_segment) { $("#afAu").innerHTML = `<div class="empty"><b>No audit yet.</b>Run the relevance audit (with your draft to compare) to see how each discipline's clicked items rank.</div><div class="row"><button class="btn" data-a="go" data-p="audit">Relevance audit</button></div>`; return; }
  $("#afAuSub").textContent = `${r.n} searches, ${r.at}${r.draft ? ", live vs draft" : ""}`;
  $("#afAu").innerHTML = `<table class="t"><tr><th>Discipline</th><th class="n">Searches</th><th class="n">Clicked item #1</th><th class="n">Top 10</th><th class="n">MRR</th></tr>${r.by_segment.map((g) => {
    const d = (k, f) => g.draft ? ` <span class="d ${g.draft[k] > g.live[k] ? "up" : g.draft[k] < g.live[k] ? "dn" : "eq"}">→ ${f(g.draft[k])}</span>` : "";
    return `<tr><td>${esc(segName(g.segment))}</td><td class="n">${g.n}</td><td class="n">${pct(g.live.hit1, 0)}${d("hit1", (x) => pct(x, 0))}</td><td class="n">${pct(g.live.hit10, 0)}${d("hit10", (x) => pct(x, 0))}</td><td class="n">${g.live.mrr.toFixed(2)}${d("mrr", (x) => x.toFixed(2))}</td></tr>`; }).join("")}</table>
    <div class="pvnote">Disciplines with few searches in the sample move by whole percentage points on one search. Use the latest 250 searches for a steadier read.</div><div class="row mt"><button class="btn" data-a="go" data-p="audit">Run the audit again</button></div>`;
};
function setAf(key, v) {
  const [sg, k] = key.split("."), [lo, hi] = A.limits.affinity || [-5, 5];
  v = Math.max(lo, Math.min(hi, Math.round(parseFloat(v) * 100) / 100)); if (Number.isNaN(v)) return;
  A.draft.affinity.segments[sg][k] = v;
  const r = $(`[data-af="${key}"]`), n = $(`[data-afn="${key}"]`);
  if (r) r.value = v; if (n && document.activeElement !== n) n.value = v;
  if (r) r.closest(".afc").classList.toggle("chg", v !== A.live.affinity.segments[sg][k]);
  renderDraftBar(); previewSoon();
}

// ------------------------------------------------------------------ results page: layout and filter rail
PAGES.layout = async (el) => {
  const D = A.draft, L = A.live, F = D.facets, lab = A.meta.facets || {};
  const row = (k, m, lo, hi, step) => { const v = D.layout[k], ch = v !== L.layout[k];
    return `<div class="wrow"><div><b class="${ch ? "chg" : ""}">${esc(m[0])}</b><div class="desc">${esc(m[1])}</div></div>
      <input type="range" min="${lo}" max="${hi}" step="${step}" value="${v}" data-w="layout.${k}" aria-label="${esc(m[0])}"><input class="in num" value="${v}" data-wn="layout.${k}" inputmode="numeric"><button class="rst" data-a="wdef" data-k="layout.${k}" title="Default ${A.defaults.layout[k]}"><svg viewBox="0 0 24 24"><path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/></svg></button></div>`; };
  const g = D.layout.group_variants, sh = D.layout.simple_home;
  el.innerHTML = `<div class="ph"><div><h1>Page layout</h1><p>How the pages are put together: which start page people get, brand mix on page one, sizes grouped into one card, and the filter rail's order and defaults.</p></div><span class="sp"></span><button class="btn" data-a="reset" data-s="layout">Reset to defaults</button></div>
    <div class="grid g12"><div>
      <div class="card"><h3>Page one</h3>${row("brand_cap", LAYOUT.brand_cap, A.limits.layout[0], A.limits.layout[1], 1)}
        <div class="trow"><label class="sw"><input type="checkbox" data-lay="group_variants" ${g ? "checked" : ""} aria-label="Group sizes"><span></span></label>
          <div><b class="${!!g !== !!L.layout.group_variants ? "chg" : ""}">${esc(LAYOUT.group_variants[0])}: ${g ? "on by default" : "off by default"}</b><div class="desc">${esc(LAYOUT.group_variants[1])}</div><div class="where">Card: the best-ranked size is shown, size chips switch the price, stock and Add button; a green dot marks sizes on the shelf at the pickup branch.</div></div></div></div>
      <div class="card mt"><h3>Start page</h3><div class="trow"><label class="sw"><input type="checkbox" data-lay="simple_home" ${sh ? "checked" : ""} aria-label="Simple start page by default"><span></span></label>
          <div><b class="${!!sh !== !!L.layout.simple_home ? "chg" : ""}">${esc(LAYOUT.simple_home[0])}: ${sh ? "the default" : "off (Hero search is the default)"}</b><div class="desc">${esc(LAYOUT.simple_home[1])}</div><div class="where">Applies to people who haven\'t picked a view in the bar. To measure it before switching everyone, change it in the draft and start an A/B test: the test splits browser sessions and compares their searches.</div></div></div></div>
      ${resultsModesCard(D, L)}
      <div class="card mt"><h3>Filter rail <span class="sub">top to bottom</span></h3><table class="t"><tr><th style="width:70px">Order</th><th>Filter</th><th>Shown</th><th>Starts collapsed</th></tr>
        ${F.order.map((id, i) => `<tr><td style="white-space:nowrap"><button class="btn sm ghost" data-a="fup" data-i="${i}" ${i ? "" : "disabled"} aria-label="Up">▲</button><button class="btn sm ghost" data-a="fdn" data-i="${i}" ${i < F.order.length - 1 ? "" : "disabled"} aria-label="Down">▼</button></td>
          <td><b>${esc(lab[id] || id)}</b>${id === "acct" ? `<div class="muted small">signed-in customers only</div>` : id === "spec" ? `<div class="muted small">the category's own specs (pressure, voltage…)</div>` : ""}</td>
          <td><label class="sw"><input type="checkbox" data-fshow="${id}" ${F.hidden.includes(id) ? "" : "checked"}><span></span></label></td>
          <td><label class="sw"><input type="checkbox" data-fcol="${id}" ${F.collapsed.includes(id) ? "checked" : ""} ${F.hidden.includes(id) ? "disabled" : ""}><span></span></label></td></tr>`).join("")}</table>
        <div class="pvnote">A filter shows only when it has two or more values for the search. Hiding one removes it from the rail but keeps links that already use it working. People can still open or close any section; collapsed only sets how it starts.</div></div></div>
      <div><div class="card"><h3>How other platforms do it</h3><div class="small" style="line-height:1.6">Constructor, HawkSearch and Coveo all let merchandisers order facets, hide them and set their default state, and HawkSearch can do it per landing page. Webb Search sets one rail for every search; per-category rails are listed in the production plan.</div></div>
      <div class="card mt"><h3>See it</h3><div class="small">Publish, then open a search (e.g. <a href="/#q=ball%20valve" target="_blank" rel="noopener">ball valve</a>) in the app. Start pages: <a href="/#home=s" target="_blank" rel="noopener">Simple</a> · <a href="/#home=a" target="_blank" rel="noopener">Hero search</a>.</div></div></div></div>`;
};

// the Results modes card on Page layout (v1.10): default mode, the customer switch, Minimal's rail, links to see each mode
function resultsModesCard(D, L) {
  const M = A.meta.minimal_rail || {}, mr = D.minimal_rail, rm = D.layout.results_mode, sw = D.layout.mode_switch;
  const off = Object.keys(M).filter((id) => !mr.order.includes(id)), q = A.mrq || "ball valve";
  const row = (id, i, on) => `<tr><td style="white-space:nowrap">${on ? `<button class="btn sm ghost" data-a="mrup" data-i="${i}" ${i ? "" : "disabled"} aria-label="Up">▲</button><button class="btn sm ghost" data-a="mrdn" data-i="${i}" ${i < mr.order.length - 1 ? "" : "disabled"} aria-label="Down">▼</button>` : ""}</td>
    <td><b>${esc(M[id] || id)}</b>${id === "purchased" || id === "lists" ? `<div class="muted small">signed-in customers only</div>` : ""}</td>
    <td><label class="sw"><input type="checkbox" data-mron="${id}" ${on ? "checked" : ""}><span></span></label></td></tr>`;
  return `<div class="card mt"><h3>Results modes</h3>
    <div class="wrow"><div><b class="${rm !== L.layout.results_mode ? "chg" : ""}">${esc(LAYOUT.results_mode[0])}</b><div class="desc">${esc(LAYOUT.results_mode[1])}</div></div>
      <div class="seg">${(A.meta.results_modes || ["minimal", "normal", "advanced"]).map((m) => `<button class="${rm === m ? "on" : ""}" data-a="rmode" data-m="${m}">${m[0].toUpperCase() + m.slice(1)}</button>`).join("")}</div></div>
    <div class="trow"><label class="sw"><input type="checkbox" data-lay="mode_switch" ${sw ? "checked" : ""} aria-label="Show the mode switch"><span></span></label>
      <div><b class="${!!sw !== !!L.layout.mode_switch ? "chg" : ""}">${esc(LAYOUT.mode_switch[0])}: ${sw ? "shown" : "hidden"}</b><div class="desc">${esc(LAYOUT.mode_switch[1])}</div></div></div>
    <div class="sect">Minimal rail <span class="sub">top to bottom</span> <button class="btn sm ghost" data-a="reset" data-s="minimal_rail">Reset</button></div>
    <table class="t"><tr><th style="width:70px">Order</th><th>Filter</th><th>In Minimal</th></tr>
      ${mr.order.map((id, i) => row(id, i, true)).join("")}${off.map((id) => row(id, 0, false)).join("")}</table>
    <div class="wrow"><div><b class="${mr.max !== L.minimal_rail.max ? "chg" : ""}">Show at most</b></div>
      <select class="in" data-mrmax aria-label="Show at most">${[2, 3, 4, 5].map((n) => `<option ${n === mr.max ? "selected" : ""}>${n}</option>`).join("")}</select></div>
    <div class="pvnote">A filter shows only when it applies to the search; one that doesn't apply doesn't use a slot. Size, type, connection, material, price and spec filters are never in Minimal. Filters someone set elsewhere stay applied and show as chips above the results.</div>
    <div class="sect">See it</div><div class="row"><input class="in" id="mrq" value="${esc(q)}" aria-label="Search to open"> ${["minimal", "normal", "advanced"].map((m) => `<a href="/#q=${encodeURIComponent(q)}&mode=${m}" target="_blank" rel="noopener">${m[0].toUpperCase() + m.slice(1)}</a>`).join(" · ")}</div>
    <div class="pvnote">Opens the live settings; publish first to see a draft.</div></div>`;
}

// ------------------------------------------------------------------ A/B tests
function sampleSize(p, lift = 0.1) {
  p = p > 0 && p < 1 ? p : 0.3; const p2 = Math.min(0.99, p * (1 + lift));
  return Math.ceil(Math.pow(1.96 + 0.84, 2) * (p * (1 - p) + p2 * (1 - p2)) / Math.pow(p2 - p, 2));
}
function sigLine(label, t) {
  if (!t || t.p == null) return `<span class="muted">${label}: not enough data</span>`;
  const sig = t.p < 0.05;
  return `<span>${label}: B ${t.lift > 0 ? "+" : ""}${(t.lift * 100).toFixed(1)}% vs A <span class="chip ${sig ? (t.lift > 0 ? "ok" : "red") : ""}">${sig ? "significant" : "not significant yet"} · p ${t.p.toFixed(3)}</span></span>`;
}
function abChart(series) {
  if (!series || series.length < 2) return `<div class="muted small">A daily chart appears after two days of data.</div>`;
  const W = 520, H = 120, pl = 30, pr = 10, pt = 8, pb = 18, n = series.length, x = (i) => pl + i * (W - pl - pr) / (n - 1);
  const mx = Math.max(0.05, ...series.flatMap((d) => [d.A, d.B])), y = (v) => pt + (H - pt - pb) * (1 - v / mx);
  const line = (k, c) => `<polyline fill="none" stroke="${c}" stroke-width="2" points="${series.map((d, i) => `${x(i)},${y(d[k])}`).join(" ")}"/>`;
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Click-through per day, A and B"><text x="${pl - 4}" y="${y(mx) + 4}" text-anchor="end">${pct(mx, 0)}</text><text x="${pl - 4}" y="${y(0)}" text-anchor="end">0%</text>${line("A", "var(--muted)")}${line("B", "var(--webb-red)")}
    <text x="${x(0)}" y="${H - 4}">${esc(series[0].day.slice(5))}</text><text x="${x(n - 1)}" y="${H - 4}" text-anchor="end">${esc(series[n - 1].day.slice(5))}</text></svg>`;
}
PAGES.tests = async (el) => {
  const ch = changes(), running = (A.live.experiments || []).find((x) => x.status === "running");
  el.innerHTML = `<div class="ph"><div><h1>A/B tests</h1><p>Run your unpublished changes on a share of browser sessions and compare them with the live settings on real traffic: click-through, add to cart, no-result rate and clicked position. A session stays in its group for the whole test. One test at a time.</p></div></div>
    <div class="card"><h3>New test</h3>${running ? `<div class="explain">“${esc(running.name)}” is running. Stop it before starting another.</div>` : ch.length ? `
      <div class="small" style="margin-bottom:8px"><b>Variant B = live + your ${ch.length} unpublished change${ch.length > 1 ? "s" : ""}:</b> ${esc(ch.join(" · "))}</div>
      <div class="row"><label class="fl" style="flex:1">Name<input class="in" id="abName" maxlength="80" placeholder="e.g. Brand cap 2 on page one" value="${esc(ch[0].slice(0, 60))}"></label>
        <label class="fl">Sessions in B<select class="in" id="abSplit">${[10, 20, 30, 50].map((v) => `<option value="${v}" ${v === 50 ? "selected" : ""}>${v}%</option>`).join("")}</select></label>
        <label class="fl" style="flex:1">Why (optional)<input class="in" id="abNote" maxlength="300" placeholder="what you expect to change"></label><button class="btn pri" data-a="abstart" style="align-self:flex-end">Start test</button></div>
      <div class="pvnote">Starting the test moves these changes out of your draft and into the test. Publishing B later makes them live for everyone.</div>`
      : `<div class="empty"><b>No unpublished changes to test.</b>Change a weight, brand affinity, a rule or the results page in your draft, then come back (or use A/B test in the draft bar).</div>`}</div>
    <div id="abList" class="mt">Loading…</div>`;
  const xs = await adm("/api/admin/experiments");
  if (!$("#abList")) return;
  $("#abList").innerHTML = xs.length ? xs.map((x) => {
    const R = x.report || { arms: { A: {}, B: {} } }, a = R.arms.A, b = R.arms.B;
    const need = sampleSize(a.ctr), have = Math.min(a.searches || 0, b.searches || 0);
    const row = (k, lab, f, lower, neutral) => `<tr><td>${lab}</td><td class="n">${f(a[k])}</td><td class="n">${f(b[k])}</td><td class="n">${!neutral && a[k] != null && b[k] != null && a[k] !== b[k] ? `<span class="d ${(lower ? b[k] < a[k] : b[k] > a[k]) ? "up" : "dn"}">${b[k] > a[k] ? "▲" : "▼"}</span>` : ""}</td></tr>`;
    return `<div class="card mt"><h3>${esc(x.name)} <span class="status ${x.status === "running" ? "active" : "off"}">${x.status}</span><span class="sub">${x.split}% of sessions in B · started ${esc(x.started)}${x.by ? " by " + esc(x.by) : ""}${x.stopped ? " · stopped " + esc(x.stopped) : ""}</span></h3>
      <div class="small muted" style="margin-bottom:8px">B changes: ${esc((x.changes || []).join(" · ") || "—")}${x.note ? ` · ${esc(x.note)}` : ""}</div>
      <div class="grid g2"><div><table class="t"><tr><th></th><th class="n">A (live)</th><th class="n">B (test)</th><th></th></tr>
        ${row("sessions", "Sessions", num, false, true)}${row("searches", "Searches", num, false, true)}${row("ctr", "Click-through", (v) => pct(v))}${row("atc_rate", "Add to cart", (v) => pct(v))}${row("zero_rate", "No results", (v) => pct(v), true)}${row("avg_pos", "Avg clicked position", (v) => v ?? "—", true)}</table>
        <div class="small mt" style="display:flex;flex-direction:column;gap:4px">${sigLine("Click-through", R.ctr)}${sigLine("Add to cart", R.atc_rate)}</div>
        <div class="pvnote">To detect a 10% change in click-through at A's rate, each group needs about ${num(need)} searches; it has ${num(have)}${have < need ? ` (${Math.round(100 * have / need)}%)` : ", enough"}. Decide on the result once, when that's reached, not the first day it looks good.</div></div>
      <div><h4 class="small muted" style="margin:0 0 4px">Click-through per day <span style="color:var(--muted)">— A</span> <span style="color:var(--webb-red)">— B</span></h4>${abChart(R.series)}</div></div>
      <div class="row mt">${x.status === "running" ? `<button class="btn" data-a="abstop" data-id="${esc(x.id)}">Stop test</button>` : ""}<button class="btn pri" data-a="abpublish" data-id="${esc(x.id)}">Publish B for everyone</button>${x.status !== "running" ? `<button class="btn ghost danger" data-a="abdel" data-id="${esc(x.id)}">Delete</button>` : ""}</div></div>`;
  }).join("") : `<div class="empty"><b>No tests yet.</b></div>`;
};
async function abSave(exps, extra, msg) {
  if (!who()) { $("#who").focus(); toast("Add your name (top right) first", true); return false; }
  try {
    const r = await adm("/api/admin/settings", { method: "PUT", body: { patch: { ...(extra || {}), experiments: exps }, who: who(), note: msg, base_version: A.live.version } });
    A.live = r.settings; return true;
  } catch (e) { toast(e.status === 409 ? e.message : `Not saved: ${e.message}`, true); return false; }
}

// ------------------------------------------------------------------ Bulk order: analytics, crosses, brand order, learned crosses
function tryBox(title) {
  const opts = [`<option value="">Guest</option>`].concat(A.users.map((a) => `<option value="${esc(a.account_id)}|${esc(a.users[0].user_id)}">${esc(a.name)}</option>`));
  return `<div class="card"><h3>${title || "Try lines"} <span class="sub">live vs your draft</span></h3>
    <textarea class="in" id="tryText" rows="5" style="width:100%;font:13px/1.4 ui-monospace,Menlo,monospace">${esc(A.tryText || "2 kohler toilet\n10 3/4 ball valve\n4 1/2 copper 90")}</textarea>
    <div class="row mt"><select class="in" id="tryWho" style="flex:1">${opts.join("")}</select><select class="in" id="tryBranch" style="flex:1"><option value="">Account's home branch</option>${A.branches.map((b) => `<option value="${esc(b.branch_id)}">${esc(b.branch_id)} · ${esc(b.name)}</option>`).join("")}</select><button class="btn dark" data-a="trybulk">Match</button></div>
    <div id="tryOut" class="mt"></div></div>`;
}
async function runTry() {
  const out = $("#tryOut"); if (!out) return;
  A.tryText = $("#tryText").value;
  const [account, user] = $("#tryWho").value ? $("#tryWho").value.split("|") : [null, null];
  out.innerHTML = `<div class="muted small">Matching…</div>`;
  let r; try { r = await adm("/api/admin/bulk/try", { method: "POST", body: { text: A.tryText, account, user, branch: $("#tryBranch").value || null, patch: patch() } }); }
  catch (e) { out.innerHTML = `<div class="explain">${esc(e.message)}</div>`; return; }
  const cell = (x) => !x.top ? `<span class="muted">${esc(x.note || "no match")}</span>` : `${x.top.title.toLowerCase().startsWith(x.top.brand.toLowerCase()) ? "" : `<b>${esc(x.top.brand)}</b> `}${esc(x.top.title)} <span class="muted">#${esc(x.top.sku)}</span>
    ${x.brand_cross ? `<div><span class="chip dark">cross ${esc(x.brand_cross.from)} → ${esc(x.brand_cross.to)}</span></div>` : ""}${x.cross ? `<div><span class="chip dark">item cross</span></div>` : ""}
    ${x.top.why.length ? `<div class="whys">${x.top.why.map((w) => `<span class="chip">${esc(w)}</span>`).join("")}</div>` : ""}`;
  out.innerHTML = `${r.changed.length ? "" : `<div class="pvnote" style="margin:0 0 8px">No unpublished changes: both columns use the live settings.</div>`}<table class="t trytbl"><tr><th style="width:26%">Line</th><th>Live</th><th>Draft</th></tr>${r.lines.map((l) => `<tr><td class="q">${esc(l.text)}<div class="muted small">key “${esc(l.key)}”</div></td><td class="small">${cell(l.live)}</td><td class="small ${JSON.stringify(l.live.top) !== JSON.stringify(l.draft.top) ? "chgcell" : ""}">${cell(l.draft)}</td></tr>`).join("")}</table>`;
}
function scopeSelect(id, v) {
  return `<select class="in" id="${id}"><option value="company">Company-wide</option>${A.branches.map((b) => `<option value="${esc(b.branch_id)}" ${v === b.branch_id ? "selected" : ""}>Branch ${esc(b.branch_id)} · ${esc(b.name)}</option>`).join("")}</select>`;
}
const STATUS_NAMES = { exact: "Exact item or part #", webb: "Webb item", xref: "Cross-reference", rec: "Recommended", none: "Not in catalog", skip: "Skipped (not an item)" };

PAGES.bulk = async (el) => {
  el.innerHTML = `<div class="ph"><div><h1>Bulk order</h1><p>What customers upload (takeoffs, BOMs, emails, photos of notes), how well the lines match, and what people change. Choices feed the learned crosses; brands Webb doesn't carry point to crosses worth adding.</p></div><span class="sp"></span>${periodSeg()}</div><div id="bk">Loading…</div>`;
  const d = await adm(`/api/admin/bulk?days=${A.days}`);
  const b = d.report; if (!$("#bk")) return;
  if (!b) { $("#bk").innerHTML = `<div class="explain">Analytics are off on this server (WEBB_ANALYTICS=0).</div>`; return; }
  const crossed = new Set(A.draft.crosses.filter((c) => c.kind === "brand").map((c) => c.from_brand.toLowerCase()));
  const st = b.status || {};
  $("#bk").innerHTML = `${!b.lines ? `<div class="explain"><b>No Bulk order lines in the last ${A.days} days on this server.</b> Upload a takeoff in the app's Bulk order tab and it shows here.</div>` : ""}
    <div class="kpis"><div class="kpi"><small>Documents read</small><b>${num(b.uploads)}</b><span class="d eq">${num(b.upload_lines)} lines · ${num(b.set_aside)} set aside</span></div>
      <div class="kpi"><small>Lines matched</small><b>${num(b.lines)}</b><span class="d eq">&nbsp;</span></div><div class="kpi"><small>Matched to an item</small><b>${pct(b.matched_rate, 0)}</b><span class="d eq">${num(st.none || 0)} not in catalog</span></div>
      <div class="kpi"><small>Choices recorded</small><b>${num(b.choices)}</b><span class="d eq">confirm, change, swap</span></div><div class="kpi"><small>Kept the first pick</small><b>${pct(b.top_pick_rate, 0)}</b><span class="d eq">of choices</span></div>
      <div class="kpi"><small>Changed the pick</small><b>${pct(b.override_rate, 0)}</b><span class="d eq">chose another option</span></div></div>
    <div class="grid g21 mt"><div class="card"><h3>Lines per day</h3>${chart(b.series || [], "day", "lines", null, { h: 150, label: "Bulk order lines per day" })}</div>
      <div class="card"><h3>How lines matched</h3>${hbars(Object.entries(st).map(([k, v]) => ({ name: STATUS_NAMES[k] || k, n: v })).sort((x, y) => y.n - x.n), b.lines)}</div></div>
    <div class="grid g3 mt"><div class="card"><h3>Brands asked for, not carried</h3>${(b.brands_asked || []).length ? `<table class="t">${b.brands_asked.map((x) => `<tr><td><b>${esc(x.brand)}</b></td><td class="n">${num(x.n)} lines</td><td>${x.crossed_to ? `<span class="chip">→ ${esc(x.crossed_to)}</span>` : crossed.has(x.brand.toLowerCase()) ? `<span class="chip">cross in draft</span>` : `<button class="btn sm" data-a="addbrandcross" data-b="${esc(x.brand)}">Add cross</button>`}</td></tr>`).join("")}</table>` : `<div class="empty">None yet.</div>`}</div>
      <div class="card"><h3>Asked for, not in the catalog</h3>${(b.not_in_catalog || []).length ? hbars(b.not_in_catalog.slice(0, 10).map((g) => ({ name: g.what, n: g.n })), 0, true) : `<div class="empty">None yet.</div>`}</div>
      <div class="card"><h3>Where lines came from</h3>${hbars(Object.entries(b.sources || {}).map(([k, v]) => ({ name: k, n: v })), b.lines)}
        ${Object.keys(b.upload_kinds || {}).length ? `<div class="sect">Documents</div><div class="row">${Object.entries(b.upload_kinds).map(([k, v]) => `<span class="chip">${esc(k)} ${v}</span>`).join("")}</div>` : ""}
        ${(b.crosses_used || []).length ? `<div class="sect">Crosses used</div>${b.crosses_used.map((c) => `<div class="small">${esc(c.what)} <b>${c.n}</b></div>`).join("")}` : ""}
        ${Object.keys(b.choice_how || {}).length ? `<div class="sect">How people chose</div><div class="row">${Object.entries(b.choice_how).map(([k, v]) => `<span class="chip">${esc({ confirm: "confirmed", confirmall: "confirm all", sub: "took a substitute", choose: "picked another option", closest: "took the nearest" }[k] || k)} ${v}</span>`).join("")}</div>` : ""}</div></div>
    <div class="grid g2 mt"><div class="card"><h3>Large takeoffs</h3><div class="small" style="line-height:1.6">A document can carry up to 2,000 lines. The page matches them in chunks of 8 lines, three chunks at a time, and paints results as they arrive; repeated lines (same words, quantity and unit) are matched once and reused for 15 minutes. Excel, CSV, Word, email with attachments and text PDFs are read directly; scans and photos go through handwriting OCR first, which is the slow part.</div></div>
      ${tryBox("Try lines")}</div>`;
};

PAGES.crosses = async (el) => {
  const C = A.draft.crosses, liveIds = new Set(A.live.crosses.map((c) => c.id));
  const tbl = (kind) => { const rows = C.filter((c) => c.kind === kind);
    return rows.length ? `<table class="t"><tr><th style="width:50px">On</th><th>${kind === "brand" ? "Lines naming" : "Part # or line"}</th><th>${kind === "brand" ? "Matched as" : "Item"}</th><th>Where</th><th>Note</th><th></th></tr>${rows.map((c) => `<tr>
      <td><label class="sw"><input type="checkbox" data-xon="${esc(c.id)}" ${c.enabled ? "checked" : ""}><span></span></label></td>
      <td><b>${esc(kind === "brand" ? c.from_brand : (c.from_brand ? c.from_brand + " " : "") + (c.from_part || c.from_text))}</b>${liveIds.has(c.id) ? "" : ` <span class="chip red">unpublished</span>`}${kind === "item" ? `<div class="muted small">${c.from_part ? "part number" : "line words"}</div>` : ""}</td>
      <td>${kind === "brand" ? esc(c.to_brand) : `#${esc(c.to_sku)} <span class="muted small" data-title="${esc(c.to_sku)}"></span>`}</td><td class="small">${esc(scopeName(c.scope))}</td><td class="small muted">${esc(c.note || "")}</td>
      <td><button class="btn sm ghost danger" data-a="delx" data-id="${esc(c.id)}">Delete</button></td></tr>`).join("")}</table>` : `<div class="empty">None yet.</div>`; };
  el.innerHTML = `<div class="ph"><div><h1>Crosses &amp; substitutes</h1><p>Tell Bulk order what to match when a line asks for something Webb doesn't sell. A <b>brand cross</b> matches lines naming a brand Webb doesn't carry as one it does (Kohler → American Standard), keeping the rest of the line. An <b>item cross</b> sends a part number or a line's words to one item. Set them company-wide, or for one branch where the local choice differs; a branch's own cross wins there.</p></div></div>
    <div class="grid g12"><div>
      <div class="card"><h3>Brand crosses</h3>
        <div class="xform"><label class="fl">Lines naming<input class="in" id="xbFrom" placeholder="e.g. Kohler" maxlength="60" value="${esc(A.xbPrefill || "")}"></label><span class="muted arr">→</span>
          <label class="fl">Match as<span class="ac"><input class="in" id="xbTo" data-ac="brand" data-add="fill" placeholder="a brand Webb sells" style="width:100%"></span></label>
          <label class="fl">Where${scopeSelect("xbScope")}</label><label class="fl">Note<input class="in" id="xbNote" placeholder="optional" maxlength="200"></label><button class="btn pri" data-a="addxb">Add</button></div>${tbl("brand")}</div>
      <div class="card mt"><h3>Item crosses</h3>
        <div class="xform"><label class="fl">Part # or line words<input class="in" id="xiFrom" placeholder="K-3999, or kohler highline toilet" maxlength="120"></label><span class="muted arr">→</span>
          <label class="fl">Item<span class="ac"><input class="in" id="xiTo" data-ac="sku" data-add="fill" placeholder="item # or search words" style="width:100%"></span></label>
          <label class="fl">Where${scopeSelect("xiScope")}</label><label class="fl">Note<input class="in" id="xiNote" placeholder="optional" maxlength="200"></label><button class="btn pri" data-a="addxi">Add</button></div>${tbl("item")}
        <div class="pvnote">Line words match the whole line after quantities and shorthand are removed (“2 - 3/4 BV” and “3/4 ball valve” are the same line). The Learned crosses page lists the lines customers keep matching the same way, ready to make permanent.</div></div></div>
      <div>${tryBox("Try lines")}<div class="card mt"><h3>Order of precedence</h3><div class="small" style="line-height:1.6">1. An exact item or part number on the line.<br>2. An item cross (branch, then company).<br>3. The cross-reference lake (OEM part numbers).<br>4. A brand cross rewrites the brand, then recommendations rank by the account's history, Webb's brand order for the category, learned choices and branch stock.</div></div></div></div>`;
  A.xbPrefill = null;
  const skus = [...new Set(C.filter((c) => c.kind === "item").map((c) => c.to_sku))];
  for (const s_ of skus) adm(`/api/admin/lookup?kind=sku&q=${encodeURIComponent(s_)}&limit=1`).then((x) => { $$(`[data-title="${s_}"]`).forEach((e) => (e.textContent = x[0] && x[0].value === s_ ? x[0].label : "not in the catalog")); }).catch(() => {});
};

PAGES.brandprefs = async (el) => {
  const P = A.draft.brand_prefs, liveIds = new Set(A.live.brand_prefs.map((b) => b.id)), E = A.bp || (A.bp = { family: "", brands: [], scope: "company", id: null });
  const fams = [...new Set(P.map((b) => b.family))].sort();
  el.innerHTML = `<div class="ph"><div><h1>Brand preferences</h1><p>For Bulk order lines that name no brand (“3/4 ball valve”), the brands Webb prefers in each category, in order. The account's own history still counts first; Webb's order breaks the tie between comparable items. A branch can keep its own order for a category.</p></div></div>
    <div class="grid g12"><div>
      <div class="card"><h3>${E.id ? "Edit" : "Add a"} category order</h3>
        <div class="xform bp"><label class="fl">Category<span class="ac"><input class="in" id="bpFam" data-ac="family" data-add="fill" placeholder="e.g. Ball Valves" value="${esc(E.family)}" style="width:100%"></span></label><label class="fl">Where${scopeSelect("bpScope", E.scope)}</label></div>
        <div class="lst mt">${E.brands.map((b, i) => `<div class="li"><span class="tt"><b>#${i + 1}</b> ${esc(b)}</span><span><button class="btn sm ghost" data-a="bpup" data-i="${i}" ${i ? "" : "disabled"}>▲</button><button class="btn sm ghost" data-a="bpdn" data-i="${i}" ${i < E.brands.length - 1 ? "" : "disabled"}>▼</button></span><button class="btn sm ghost" data-a="bprm" data-i="${i}">Remove</button></div>`).join("")}</div>
        <div class="ac mt"><input class="in" data-ac="brand" data-add="bp" placeholder="Add a brand (up to 6)" style="width:100%" ${E.brands.length >= 6 ? "disabled" : ""}></div>
        <div class="row mt"><span class="sp"></span>${E.id ? `<button class="btn" data-a="bpcancel">Cancel</button>` : ""}<button class="btn pri" data-a="bpsave">${E.id ? "Save" : "Add"} to draft</button></div></div>
      <div class="card mt"><h3>Categories <span class="sub">${P.length} lists</span></h3>${fams.length ? `<table class="t"><tr><th style="width:50px">On</th><th>Category</th><th>Order</th><th>Where</th><th></th></tr>${fams.map((f) => P.filter((b) => b.family === f).sort((a, b) => (a.scope === "company" ? -1 : 1) - (b.scope === "company" ? -1 : 1)).map((b) => `<tr>
        <td><label class="sw"><input type="checkbox" data-bpon="${esc(b.id)}" ${b.enabled ? "checked" : ""}><span></span></label></td><td><b>${esc(b.family)}</b>${liveIds.has(b.id) ? "" : ` <span class="chip red">unpublished</span>`}</td>
        <td>${b.brands.map((x, i) => `<span class="chip">${i + 1}. ${esc(x)}</span>`).join(" ")}</td><td class="small">${esc(scopeName(b.scope))}</td>
        <td style="white-space:nowrap"><button class="btn sm" data-a="bpedit" data-id="${esc(b.id)}">Edit</button> <button class="btn sm ghost danger" data-a="bpdel" data-id="${esc(b.id)}">Delete</button></td></tr>`).join("")).join("")}</table>` : `<div class="empty">No category orders yet.</div>`}
        <div class="pvnote">Bonus: Webb's brand order weight (Ranking weights → Bulk order) × (#1 of 3 = 1.0, #2 = 0.67, #3 = 0.33). Brands not on the list get nothing. A line that names a brand is matched to that brand, not re-ordered.</div></div></div>
      <div>${tryBox("Try lines")}</div></div>`;
};
function bpAdd(v) {
  const E = A.bp; if (E.brands.some((b) => b.toLowerCase() === v.toLowerCase())) return toast("Already on the list", true);
  if (E.brands.length >= 6) return toast("Up to 6 brands", true);
  E.family = ($("#bpFam") || {}).value || E.family; E.scope = ($("#bpScope") || {}).value || E.scope;
  E.brands.push(v); render();
}

PAGES.learned = async (el) => {
  const D = A.draft, L = A.live, off = new Set(D.learned_off);
  el.innerHTML = `<div class="ph"><div><h1>Learned crosses</h1><p>Every time someone confirms, changes or swaps a Bulk order match, the line and their pick are recorded. The next time anyone uploads the same line, the item people picked gets extra weight: company-wide once enough people agree, and more for the same account. When a line names a brand Webb doesn't carry, the brand people take instead is learned too.</p></div></div>
    <div class="card"><div class="trow"><label class="sw"><input type="checkbox" data-t="learned_crosses" ${D.personalization.learned_crosses ? "checked" : ""} aria-label="Learn from choices"><span></span></label>
      <div><b class="${D.personalization.learned_crosses !== L.personalization.learned_crosses ? "chg" : ""}">${esc(PERS.learned_crosses[0])}: ${D.personalization.learned_crosses ? "on" : "off"}</b><div class="desc">${esc(PERS.learned_crosses[1])}</div></div></div>
      <div class="row small muted" style="gap:18px">Weight <b>${D.bulk.learned}</b> · counts after <b>${D.bulk.learned_min}</b> choices · one vote per order session per line · last 365 days <span class="link" data-a="go" data-p="weights">Change on Ranking weights</span></div></div>
    <div id="lr" class="mt">Loading…</div>`;
  const d = await adm(`/api/admin/bulk?days=${A.days}`);
  if (!$("#lr")) return;
  const T = d.learned, itemCrossed = new Set(D.crosses.filter((c) => c.kind === "item").map((c) => `${c.from_text}::${c.to_sku}`));
  const brandCrossed = new Set(D.crosses.filter((c) => c.kind === "brand").map((c) => c.from_brand.toLowerCase()));
  $("#lr").innerHTML = `<div class="card"><h3>Lines and the item people pick <span class="sub">${T.items.length} shown</span></h3>${T.items.length ? `<div class="scroll" style="max-height:560px"><table class="t"><tr><th>Line</th><th>Picked</th><th class="n">Times</th><th class="n">Share</th><th class="n">Accounts</th><th>Last</th><th></th></tr>${T.items.map((r) => {
      const k = `${r.key}::${r.sku}`, isOff = off.has(k), perm = itemCrossed.has(k);
      return `<tr class="${isOff ? "offrow" : ""}"><td class="q">${esc(r.key)}</td><td class="clip" title="${esc(r.title)}">${r.title.toLowerCase().startsWith(r.brand.toLowerCase()) ? "" : `<b>${esc(r.brand)}</b> `}${esc(r.title)} <span class="muted">#${esc(r.sku)}</span></td><td class="n">${num(r.n)}</td><td class="n">${pct(r.share, 0)}</td><td class="n">${num(r.accounts)}</td><td class="muted small">${esc(r.last || "")}</td>
        <td style="white-space:nowrap">${perm ? `<span class="chip ok">permanent cross</span>` : `<button class="btn sm" data-a="promote" data-k="${esc(r.key)}" data-sku="${esc(r.sku)}" title="Add an item cross for this line (company-wide)">Make permanent</button>`} <button class="btn sm ghost" data-a="lroff" data-k="${esc(k)}">${isOff ? "Switch on" : "Switch off"}</button></td></tr>`; }).join("")}</table></div>` : `<div class="empty"><b>Nothing learned yet.</b>Choices appear here after people confirm or change Bulk order matches.</div>`}
      <div class="pvnote">Share: of all picks for that line, how many were this item. Switch off a pick that's wrong (a one-off job, a mistake); it stops counting but stays logged. Make permanent turns a pick everyone agrees on into an item cross, so it's matched first even for accounts that never picked it.</div></div>
    <div class="card mt"><h3>Brands asked for, and the brand people take</h3>${T.brands.length ? `<table class="t"><tr><th>Asked for</th><th>Category</th><th>Taken instead</th><th class="n">Times</th><th class="n">Share</th><th></th></tr>${T.brands.map((r) => {
      const k = `brand::${r.from}::${r.to.toLowerCase()}`, isOff = off.has(k);
      return `<tr class="${isOff ? "offrow" : ""}"><td><b>${esc(r.from)}</b></td><td class="small">${esc(r.family_label || r.family || "any")}</td><td>${esc(r.to)}</td><td class="n">${num(r.n)}</td><td class="n">${pct(r.share, 0)}</td>
        <td style="white-space:nowrap">${brandCrossed.has(r.from.toLowerCase()) ? `<span class="chip">brand cross set</span>` : `<button class="btn sm" data-a="addbrandcross" data-b="${esc(r.from)}" data-to="${esc(r.to)}">Make a brand cross</button>`} <button class="btn sm ghost" data-a="lroff" data-k="${esc(k)}">${isOff ? "Switch on" : "Switch off"}</button></td></tr>`; }).join("")}</table>` : `<div class="empty">None yet. Lines naming a brand Webb doesn't carry (Kohler, Grohe, A.O. Smith…) show here once people pick a replacement.</div>`}</div>`;
};

// ------------------------------------------------------------------ change log, data
PAGES.changes = async (el) => {
  el.innerHTML = `<div class="ph"><div><h1>Change log</h1><p>Every published version: who, when, why and what changed. Revert publishes an earlier version as a new one, so nothing is lost.</p></div></div><div id="cl">Loading…</div>`;
  const c = await adm("/api/admin/changes");
  $("#cl").innerHTML = c.length ? `<div class="card"><table class="t"><tr><th>Version</th><th>When</th><th>Who</th><th>Why</th><th>What changed</th><th></th></tr>${c.map((x) => `<tr><td><b>v${x.version}</b>${x.version === A.live.version ? ` <span class="chip ok">live</span>` : ""}</td><td class="muted num" style="white-space:nowrap">${esc(x.at)}</td><td>${esc(x.who)}</td><td>${esc(x.note)}</td><td class="small">${x.changes.map(esc).join("<br>")}</td><td>${x.version === A.live.version ? "" : `<button class="btn sm" data-a="revert" data-v="${x.version}">Revert to v${x.version}</button>`}</td></tr>`).join("")}</table></div>`
    : `<div class="empty"><b>Nothing published yet.</b>The defaults are live (version 0).</div>`;
};
PAGES.data = async (el) => {
  const s = await adm("/api/admin/settings");
  const a = s.analytics;
  el.innerHTML = `<div class="ph"><div><h1>Data &amp; access</h1><p>What the dashboard records, where it lives, and who can reach it.</p></div></div>
    <div class="grid g2"><div class="card"><h3>Live log</h3>${a ? `<div class="row" style="gap:22px;margin-bottom:10px"><div class="metric"><small>Searches</small><b>${num(a.searches)}</b></div><div class="metric"><small>Events</small><b>${num(a.events)}</b></div><div class="metric"><small>Feedback</small><b>${num(a.feedback || 0)}</b></div><div class="metric"><small>Since</small><b style="font-size:16px">${esc(a.since || "—")}</b></div></div>
      <div class="row"><select class="in" id="exDays"><option value="7">Last 7 days</option><option value="30" selected>Last 30 days</option><option value="365">Last year</option></select><button class="btn" data-a="export" data-t="searches">Export searches (CSV)</button><button class="btn" data-a="export" data-t="events">Export events (CSV)</button><button class="btn" data-a="export" data-t="feedback">Export feedback (CSV)</button></div>
      <div class="sect">Clear</div><div class="row"><button class="btn danger" data-a="clear">Clear the live log</button><span class="small muted">For after testing and before a demo. The historical log and settings aren't touched.</span></div>` : `<div class="empty">Analytics are off (WEBB_ANALYTICS=0).</div>`}</div>
      <div class="card"><h3>What's recorded</h3><div class="small" style="line-height:1.6">
        <b>Each results page:</b> the words, result count, whether nothing matched, response time, account, user and role (for signed-in searches), pickup branch, the rules and rewrite that applied, and the top 24 item numbers.<br>
        <b>A/B tests:</b> which group (A or B) the browser session was in, on each search while a test runs.<br>
        <b>Feedback:</b> each thumbs up or down on a results page, an account search or an Ask answer, the reasons and comment that followed, the search it was about (words, result count, top 10 items, filters, sort) or the answer's text, the account, user and role, and the status and note the team sets.<br>
        <b>Events:</b> click, quick view, expand, add to cart (quantity and value), add to list, with the result position; each document read in Bulk order (kind, lines, lines set aside, time); each Bulk order line's match status, the brand it asked for, the cross used and, for no-match lines, what was asked for; each Bulk order choice (the line, the item and brand picked, its rank, how); each Ask question.<br>
        <b>Claude:</b> each API call's feature, model, token counts, estimated cost and outcome (no prompts or answers).<br>
        <b>Sign-ins</b> (when the site has a password): each attempt's time, address, browser, which sign-in and whether it worked; deleted after 90 days.<br>
        <b>Where:</b> <code>data/merch/analytics.sqlite</code> on this computer. Settings and their history: <code>data/merch/settings.json</code>, <code>changes.jsonl</code>. The shared zip doesn't include <code>data/</code>.</div></div>
      <div class="card"><h3>Access</h3><div class="small" style="line-height:1.6">The dashboard and its API answer requests from this computer, or, on the public demo, from someone past both the site and dashboard passwords (<code>WEBB_SITE_PASSWORD</code>, <code>WEBB_ADMIN_PASSWORD</code>). Every API call carries a token that changes each time Webb Search starts, so a web page in another tab can't change ranking. <code>WEBB_ADMIN=0</code> turns the dashboard off; <code>WEBB_ANALYTICS=0</code> stops logging.<br>On a shared server this would sit behind F.W. Webb employee sign-in with a merchandiser role; the prototype doesn't have that.</div></div>
      <div class="card"><h3>Settings in effect</h3><div class="small">Version ${s.settings.version}${s.settings.updated_at ? `, published ${esc(s.settings.updated_at)} by ${esc(s.settings.updated_by)}` : " (defaults)"}. ${s.settings.rules.length} rules, ${s.settings.rewrites.length} rewrites, ${s.settings.crosses.length} crosses, ${s.settings.brand_prefs.length} brand orders, ${(s.settings.experiments || []).filter((x) => x.status === "running").length} A/B test running.</div><div class="row mt"><button class="btn" data-a="go" data-p="changes">Change log</button></div></div></div>`;
};

// ------------------------------------------------------------------ Claude usage and sign-ins (v1.10.2)
const LLM_PURPOSE = { interpretation: "Reading the search", ranking: "Reranking results", agent: "Ask Webb and the account assistant",
  note_lines: "Reading handwritten notes", identify_part: "Photo identification", message: "Other" };
const LLM_MODEL = (m) => m.startsWith("claude-haiku-4-5") ? "Haiku 4.5" : m.startsWith("claude-sonnet-5") ? "Sonnet 5" : m.startsWith("claude-opus-5-5") ? "Opus 5.5" : m.replace(/^claude-/, "");
const localTs = (ts, secs) => { if (!ts) return ""; const d = new Date(ts * 1000), p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}${secs ? ":" + p(d.getSeconds()) : ""}`; };   // this browser's time zone
const usd = (x) => "$" + (x >= 100 ? Math.round(x).toLocaleString() : Number(x || 0).toFixed(x >= 1 ? 2 : 4));
const tok = (x) => (x || 0) >= 1e6 ? (x / 1e6).toFixed(1) + "M" : (x || 0) >= 1e3 ? Math.round(x / 1e3) + "k" : String(x || 0);
A.usageDays = 30;
PAGES.usage = async (el) => {
  el.innerHTML = `<div class="ph"><div><h1>Claude usage</h1><p>Every Claude API call this server makes: which feature, which model, the tokens and an estimated cost at list price. Anthropic's console has the actual bill.</p></div></div><div id="uB">Loading…</div>`;
  const u = await adm(`/api/admin/usage?days=${A.usageDays}`);
  const t = u.today, sp = u.span, cap = u.cap;
  const pct = cap ? Math.min(100, Math.round(100 * (t.calls + t.errors + (t.timeouts || 0)) / cap)) : 0;
  const tbl = (rows, first, label) => rows.length ? `<table class="t"><tr><th>${first}</th><th class="num">Calls</th><th class="num">Avg time</th><th class="num">Input</th><th class="num">Output</th><th class="num">Cache read</th><th class="num">Est. cost</th></tr>${rows.map((r) => `<tr><td>${label(r)}</td><td class="num">${num(r.calls)}</td><td class="num">${r.avg_ms ? (r.avg_ms / 1000).toFixed(1) + " s" : "—"}</td><td class="num">${tok(r.input_tokens)}</td><td class="num">${tok(r.output_tokens)}</td><td class="num">${tok(r.cache_read_tokens)}</td><td class="num"><b>${usd(r.cost_usd)}</b></td></tr>`).join("")}</table>` : `<div class="muted small">No calls in this period.</div>`;
  $("#uB").innerHTML = `${u.key_set ? "" : `<div class="card" style="border-color:var(--warn)"><b>No Anthropic API key on this server.</b> <span class="small">Claude features answer with rules; nothing is spent.</span></div>`}
    <div class="card"><div class="row" style="gap:26px;flex-wrap:wrap">
      <div class="metric"><small>Calls today</small><b>${num(t.calls)}${cap ? `<span class="muted" style="font-size:14px"> / ${num(cap)}</span>` : ""}</b></div>
      <div class="metric"><small>Est. cost today</small><b>${usd(t.cost_usd)}</b></div>
      <div class="metric"><small>Last ${A.usageDays} days</small><b>${usd(sp.cost_usd)}</b></div>
      <div class="metric"><small>Calls, last ${A.usageDays} days</small><b>${num(sp.calls)}</b></div>
      <div class="metric"><small>Tokens in / out</small><b>${tok(sp.input_tokens + sp.cache_read_tokens)} / ${tok(sp.output_tokens)}</b></div>
      <div class="metric"><small>Errors today</small><b>${num(t.errors)}</b></div>
      <div class="metric" title="The quick search-reading step stops waiting on purpose and finishes in the background"><small>Timeouts today</small><b>${num(t.timeouts || 0)}</b></div>
      <div class="metric"><small>Refused by the cap today</small><b>${num(t.capped)}</b></div></div>
      ${cap ? `<div class="mt small muted">Daily cap ${num(cap)} calls (resets at midnight UTC, 7 or 8 pm Eastern); ${pct}% used today.</div><div style="height:6px;background:var(--line);border-radius:3px;margin-top:6px"><div style="height:6px;width:${pct}%;background:${pct >= 90 ? "var(--red)" : "var(--ink)"};border-radius:3px"></div></div>` : `<div class="mt small muted">No daily cap on this server (WEBB_CLAUDE_DAILY_CALLS).</div>`}
      <div class="row mt"><div class="seg">${[7, 30, 90].map((d) => `<button class="${A.usageDays === d ? "on" : ""}" data-a="usageDays" data-d="${d}">${d} days</button>`).join("")}</div></div></div>
    <div class="grid g2"><div class="card"><h3>By feature</h3>${tbl(u.purposes, "Feature", (r) => esc(LLM_PURPOSE[r.purpose] || r.purpose))}</div>
      <div class="card"><h3>By model</h3>${tbl(u.models, "Model", (r) => `<span title="${esc(r.model)}">${esc(LLM_MODEL(r.model))}</span>`)}</div></div>
    <div class="card"><h3>By day</h3>${u.days.length ? `<table class="t"><tr><th>Day</th><th class="num">Calls</th><th class="num">Errors</th><th class="num" title="The quick search-reading step stops waiting on purpose and finishes in the background">Timeouts</th><th class="num">Refused</th><th class="num">Input</th><th class="num">Output</th><th class="num">Est. cost</th></tr>${u.days.map((d) => `<tr><td class="num">${esc(d.day)}</td><td class="num">${num(d.calls)}</td><td class="num">${num(d.errors)}</td><td class="num">${num(d.timeouts || 0)}</td><td class="num">${num(d.capped)}</td><td class="num">${tok(d.input_tokens + d.cache_read_tokens)}</td><td class="num">${tok(d.output_tokens)}</td><td class="num"><b>${usd(d.cost_usd)}</b></td></tr>`).join("")}</table>` : `<div class="muted small">No Claude calls recorded yet${u.first_day ? "" : " (recording started with v1.10.2)"}.</div>`}</div>
    <div class="grid g2"><div class="card"><h3>Recent errors and refusals</h3>${u.recent_errors.length ? `<table class="t">${u.recent_errors.map((x) => `<tr><td class="num muted">${esc(localTs(x.ts) || x.at)}</td><td>${esc(LLM_PURPOSE[x.purpose] || x.purpose)}</td><td><span class="chip ${x.status === "capped" ? "warn" : "red"}">${x.status === "capped" ? "refused: cap" : esc(x.status)}</span>${x.detail ? `<div class="small muted" style="max-width:420px">${esc(x.detail)}</div>` : ""}</td></tr>`).join("")}</table>` : `<div class="muted small">None.</div>`}</div>
      <div class="card"><h3>How it's counted</h3><div class="small" style="line-height:1.6">One row per call to Anthropic. Answers served from the cache don't call Claude and cost nothing${u.since_start ? ` (${num(u.since_start.cache_hits)} since this server started)` : ""}. Cost uses list prices per million tokens: Haiku 4.5 $1 in / $5 out, Sonnet 5 $2 / $10; cache reads at a tenth of input, cache writes at 1.25×. Refused calls never reach Anthropic: past the cap, the feature answers with rules. Kept in <code>data/merch/analytics.sqlite</code>.</div></div></div>`;
};
A.si = { days: 90, failed: false };
function uaShort(ua) {
  ua = ua || "";
  const os = /iPhone/.test(ua) ? "iPhone" : /iPad/.test(ua) ? "iPad" : /Android/.test(ua) ? "Android" : /Mac OS X|Macintosh/.test(ua) ? "Mac" : /Windows/.test(ua) ? "Windows" : /Linux/.test(ua) ? "Linux" : "";
  const br = /Edg\//.test(ua) ? "Edge" : /OPR\//.test(ua) ? "Opera" : /Firefox\//.test(ua) ? "Firefox" : /Chrome\//.test(ua) ? "Chrome" : /Safari\//.test(ua) ? "Safari" : (ua.split(/[\/ ]/)[0] || "unknown");
  return os ? `${br} · ${os}` : br;
}
PAGES.signins = async (el) => {
  el.innerHTML = `<div class="ph"><div><h1>Sign-ins</h1><p>Every attempt at the site and dashboard sign-in: when, from which address, which browser, and whether the password was right. Kept ${90} days.</p></div></div><div id="siB">Loading…</div>`;
  const s = await adm(`/api/admin/signins?days=${A.si.days}&failed=${A.si.failed}`);
  const m = s.summary;
  $("#siB").innerHTML = `<div class="card"><div class="row" style="gap:26px;flex-wrap:wrap">
      <div class="metric"><small>Attempts</small><b>${num(m.attempts)}</b></div><div class="metric"><small>Signed in</small><b>${num(m.ok)}</b></div>
      <div class="metric"><small>Wrong password</small><b>${num(m.failed)}</b></div><div class="metric"><small>Addresses</small><b>${num(m.addresses)}</b></div></div>
      <div class="row mt"><div class="seg">${[7, 30, 90].map((d) => `<button class="${A.si.days === d ? "on" : ""}" data-a="siDays" data-d="${d}">${d} days</button>`).join("")}</div>
        <div class="seg"><button class="${A.si.failed ? "" : "on"}" data-a="siFailed" data-f="0">All</button><button class="${A.si.failed ? "on" : ""}" data-a="siFailed" data-f="1">Wrong password</button></div><span class="sp"></span><button class="btn sm" data-a="siCsv">Export CSV</button></div>
      <div class="small muted mt">Everyone shares one password, so an address tells you where a sign-in came from, not who. Addresses change (phones, offices behind one address, VPNs); Look up shows the network and rough location.</div></div>
    <div class="grid g2" style="grid-template-columns:minmax(0,2fr) minmax(0,1fr)"><div class="card"><h3>Attempts</h3>${s.rows.length ? `<table class="t"><tr><th>When</th><th>Address</th><th>Sign-in</th><th>Result</th><th>Browser</th></tr>${s.rows.map((r) => `<tr><td class="num muted" style="white-space:nowrap">${esc(localTs(r.ts, true) || r.at)}</td><td><code>${esc(r.ip)}</code> <a class="small muted" href="https://ipinfo.io/${encodeURIComponent(r.ip)}" target="_blank" rel="noopener noreferrer">Look up</a></td><td>${r.kind === "admin" ? "Dashboard" : "Site"}</td><td>${r.ok ? `<span class="chip ok">signed in</span>` : `<span class="chip red">wrong password</span>`}</td><td class="small" title="${esc(r.ua)}">${esc(uaShort(r.ua))}</td></tr>`).join("")}</table>` : `<div class="muted small">No sign-in attempts in this period${A.si.failed ? " with a wrong password" : ""}.</div>`}</div>
      <div class="card"><h3>By address</h3>${s.by_ip.length ? `<table class="t"><tr><th>Address</th><th class="num">Tries</th><th class="num">OK</th><th>Last</th></tr>${s.by_ip.map((r) => `<tr><td><code>${esc(r.ip)}</code></td><td class="num">${num(r.attempts)}</td><td class="num">${num(r.ok)}</td><td class="num muted small">${esc((localTs(r.ts) || r.last).slice(5))}</td></tr>`).join("")}</table>` : `<div class="muted small">None.</div>`}</div></div>`;
};

// ------------------------------------------------------------------ customer feedback (v1.9)
A.fbf = { vote: "", kind: "", status: "new", comment: false, q: "" };
const FB_REASON_LABEL = { wrong: "Wrong products or answer", missing: "Didn't find what I needed", price_stock: "Price or stock looks wrong", slow: "Too slow", other: "Something else" };
const FB_KIND = (r) => r.kind === "ask" ? (r.context.page === "results" ? "Ask on results page" : "Ask answer") : r.context.page === "account" ? ({ orders: "Orders", invoices: "Invoices", lists: "Lists", content: "Content" }[r.context.tab] || "Account") : "Results";
const fbVote = (v) => v === 1 ? `<span class="chip ok" title="Helpful">▲ helpful</span>` : `<span class="chip red" title="Not helpful">▼ not helpful</span>`;
function fbSaw(r) {
  const c = r.context || {};
  if (r.kind === "ask") return `<div class="clip2 small" title="${esc(c.answer || "")}">${esc((c.answer || "").slice(0, 160))}${(c.answer || "").length > 160 ? "…" : ""}</div>${c.engine ? `<span class="muted small">${c.engine === "claude" ? "Claude" : "rules"}${c.ask_kind ? " · " + esc(c.ask_kind) : ""}</span>` : ""}`;
  const f = Object.entries(c.filters || {}).map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join(", ") : v}`).join(" · ");
  return `<div class="small">${c.total != null ? `<b>${num(c.total)}</b> results` : ""}${c.sort && c.sort !== "best" ? ` · sort ${esc(c.sort)}` : ""}${c.notice ? ` <span class="chip warn">${esc(c.notice.replace(/_/g, " "))}</span>` : ""}${c.photo ? ` <span class="chip info">photo</span>` : ""}${c.variant ? ` <span class="chip" title="A/B test group">${esc(c.variant.split(":")[1] || "")}</span>` : ""}</div>
    ${f ? `<div class="muted small clip" title="${esc(f)}">${esc(f)}</div>` : ""}
    ${(r.top_items || []).filter((t) => t.title).slice(0, 2).map((t, i) => `<div class="muted small clip" title="${esc(t.title)}">${i + 1}. ${esc(t.title)}</div>`).join("")}`;
}
function fbRow(r) {
  const words = r.kind === "ask" || r.context.page === "account" ? `<span class="q">${esc(r.q)}</span>` : `<span class="link q" data-a="query" data-q="${esc(r.q)}">${esc(r.q)}</span>`;
  return `<tr data-fb="${esc(r.id)}" class="${r.status === "new" ? "fbnew" : ""}"><td class="muted num" style="white-space:nowrap">${esc(r.at.slice(5))}</td>
    <td><span class="chip">${esc(FB_KIND(r))}</span><div>${words || `<span class="muted">(no words)</span>`}</div></td>
    <td>${fbVote(r.vote)}</td>
    <td>${r.reasons.map((x) => `<span class="chip">${esc((A.fbLabels || {})[x] || x)}</span>`).join(" ")}${r.comment ? `<div class="fbc">“${esc(r.comment)}”</div>` : r.reasons.length ? "" : `<span class="muted small">no comment</span>`}</td>
    <td class="small">${esc(r.role)}${r.segment ? `<div class="muted">${esc(r.segment.replace(/_/g, " "))}</div>` : ""}${r.account ? `<div class="muted">#${esc(r.account)}</div>` : ""}</td>
    <td style="max-width:280px">${fbSaw(r)}</td>
    <td style="min-width:150px"><select class="in" data-fbst="${esc(r.id)}" aria-label="Status">${[["new", "New"], ["reviewed", "Reviewed"], ["done", "Done"]].map(([x, l]) => `<option value="${x}" ${r.status === x ? "selected" : ""}>${l}</option>`).join("")}</select>
      <input class="in fbnote" data-fbnote="${esc(r.id)}" value="${esc(r.note)}" placeholder="Note" maxlength="500" aria-label="Note">${r.handled_by ? `<div class="muted small">${esc(r.handled_by)} · ${esc(r.handled_at.slice(5))}</div>` : ""}</td></tr>`;
}
PAGES.feedback = async (el) => {
  el.innerHTML = `<div class="ph"><div><h1>Customer feedback</h1><p>Thumbs up and down on results pages, account searches and Ask Webb answers, with the reasons and comments that followed and the search each one was about. Mark each one reviewed or done; a note says what you did about it.</p></div><span class="sp"></span>${periodSeg()}</div><div id="fbp">Loading…</div>`;
  await drawFeedback(true);
};
async function drawFeedback(full) {
  const f = A.fbf;
  const qs_ = new URLSearchParams({ days: A.days, limit: 300, ...(f.vote ? { vote: f.vote } : {}), ...(f.kind ? { kind: f.kind } : {}),
    ...(f.status ? { status: f.status } : {}), ...(f.comment ? { has_comment: true } : {}), ...(f.q ? { q: f.q } : {}) });
  const d = await adm(`/api/admin/feedback?${qs_}`);
  if (A.page !== "feedback") return;
  if (d.disabled) { $("#fbp").innerHTML = `<div class="explain">Analytics are off on this server (WEBB_ANALYTICS=0), so feedback isn't recorded.</div>`; return; }
  const s = d.summary; A.fbLabels = { ...FB_REASON_LABEL, ...Object.fromEntries((s.reasons || []).map((r) => [r.reason, r.label])) };
  $("#nFb").textContent = s.new || "";
  const list = `<table class="t fbt"><tr><th>When</th><th>Search or question</th><th>Vote</th><th>What they said</th><th>Who</th><th>What they saw</th><th>Status · note</th></tr>${d.items.map(fbRow).join("")}</table>`;
  const listCard = `<div class="card mt" id="fblist"><h3>Feedback <span class="sub">${num(d.items.length)} shown${d.items.length >= 300 ? " (the latest 300)" : ""}</span><span class="sp"></span><button class="btn sm" data-a="fbexport">Export CSV</button></h3>
    <div class="row fbfilters">
      <div class="seg">${[["", "All votes"], ["-1", "Not helpful"], ["1", "Helpful"]].map(([v, l]) => `<button class="${f.vote === v ? "on" : ""}" data-a="fbf" data-k="vote" data-v="${v}">${l}</button>`).join("")}</div>
      <div class="seg">${[["", "Everywhere"], ["search", "Searches"], ["ask", "Ask answers"]].map(([v, l]) => `<button class="${f.kind === v ? "on" : ""}" data-a="fbf" data-k="kind" data-v="${v}">${l}</button>`).join("")}</div>
      <div class="seg">${[["new", "To review"], ["reviewed", "Reviewed"], ["done", "Done"], ["", "All"]].map(([v, l]) => `<button class="${f.status === v ? "on" : ""}" data-a="fbf" data-k="status" data-v="${v}">${l}</button>`).join("")}</div>
      <label class="small"><input type="checkbox" data-fbcomment ${f.comment ? "checked" : ""}> with a comment</label>
      <input class="in" id="fbq" placeholder="Filter by words or comment" value="${esc(f.q)}" style="width:220px"></div>
    ${d.items.length ? `<div class="scroll" style="max-height:760px">${list}</div>` : `<div class="empty"><b>Nothing here.</b>${s.n ? "Change the filters above." : "Customers vote with the thumbs beside the result count and under each Ask answer; their votes land here within a second."}</div>`}</div>`;
  if (!full && $("#fblist")) { $("#fblist").outerHTML = listCard; return; }
  const byk = s.by_kind || {};
  $("#fbp").innerHTML = `<div class="kpis">${kpi("Votes", num(s.n), s.n, s.prev_n)}${kpi("Helpful", pct(s.helpful_rate), s.helpful_rate, s.prev_helpful_rate, false, true)}
      ${kpi("Not helpful", num(s.down))}${kpi("With a comment", num(s.with_comment))}${kpi("To review", num(s.new))}
      ${kpi("Ask answers helpful", byk.ask && byk.ask.n ? pct(byk.ask.up / byk.ask.n) : "—")}</div>
    <div class="grid g21 mt">
      <div class="card"><h3>Votes per day <span class="sp"></span><span class="legend"><span><i style="background:var(--bar)"></i>votes</span><span><i style="background:var(--webb-red)"></i>helpful</span></span></h3>
        ${chart((s.series || []).map((x) => ({ day: x.day, n: x.up + x.down, rate: (x.up + x.down) ? x.up / (x.up + x.down) : 0 })), "day", "n", "rate", { label: "Votes per day" })}</div>
      <div class="card"><h3>What went wrong <span class="sub">reasons on not-helpful votes</span></h3>${(s.reasons || []).length ? hbars(s.reasons.map((r) => ({ name: r.label, n: r.n })), 0, true) : `<div class="empty">No reasons picked yet.</div>`}
        <div class="sect">Helpful, by who voted</div>${(s.roles || []).length ? `<table class="t">${s.roles.map((r) => `<tr><td>${esc(r.role)}</td><td class="n">${num(r.n)} vote${r.n === 1 ? "" : "s"}</td><td class="n">${pct(r.helpful_rate, 0)} helpful</td></tr>`).join("")}</table>` : `<div class="muted small">No votes yet.</div>`}</div>
    </div>
    <div class="card mt"><h3>Searches marked not helpful <span class="sub">open one for its log, results now and a rule or rewrite</span></h3>${(s.top_down || []).length ? `<table class="t"><tr><th>Search</th><th class="n">Not helpful</th><th class="n">Helpful</th><th class="n">Comments</th><th>Last</th><th></th></tr>${s.top_down.map((g) => `<tr><td class="q"><span class="link" data-a="query" data-q="${esc(g.q)}">${esc(g.q)}</span></td><td class="n">${g.down}</td><td class="n">${g.up}</td><td class="n">${g.comments}</td><td class="muted">${esc(g.last.slice(5))}</td><td style="white-space:nowrap"><button class="btn sm" data-a="query" data-q="${esc(g.q)}">Look</button> <button class="btn sm ghost" data-a="newrw" data-q="${esc(g.qn)}">Add rewrite</button></td></tr>`).join("")}</table>` : `<div class="empty">No search has a not-helpful vote in this period.</div>`}</div>
    ${listCard}`;
}
function fbStatusSave(id, body) {
  return adm(`/api/admin/feedback/${encodeURIComponent(id)}`, { method: "PUT", body: { ...body, who: who() } })
    .then((r) => { toast(body.status ? `Marked ${body.status}` : "Note saved"); return r; }).catch((err) => toast(err.message, true));
}
document.addEventListener("change", (e) => {
  const t = e.target;
  if (t.dataset.fbst) fbStatusSave(t.dataset.fbst, { status: t.value }).then(() => { if (A.fbf.status && A.fbf.status !== t.value) drawFeedback(false); else refreshFbCount(); });
  if (t.dataset.fbnote) fbStatusSave(t.dataset.fbnote, { note: t.value });
  if (t.hasAttribute("data-fbcomment")) { A.fbf.comment = t.checked; drawFeedback(false); }
});
let fbqT;
document.addEventListener("input", (e) => { if (e.target.id === "fbq") { clearTimeout(fbqT); fbqT = setTimeout(() => { A.fbf.q = e.target.value.trim(); drawFeedback(false).then(() => { const i = $("#fbq"); if (i) { i.focus(); i.setSelectionRange(i.value.length, i.value.length); } }); }, 300); } });
document.addEventListener("click", async (e) => {
  const t = e.target.closest("[data-a]"); if (!t) return;
  if (t.dataset.a === "fbf") { A.fbf[t.dataset.k] = t.dataset.v; drawFeedback(false); }
  if (t.dataset.a === "fbexport") {
    try { const csv = await adm(`/api/admin/export.csv?table=feedback&days=${A.days}`); const u = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
      const l = document.createElement("a"); l.href = u; l.download = "webb-search-feedback.csv"; document.body.appendChild(l); l.click(); l.remove(); setTimeout(() => URL.revokeObjectURL(u), 2000); } catch (err) { toast(err.message, true); }
  }
});
function refreshFbCount() { return; adm("/api/admin/feedback?days=30&limit=1").then((d) => { if (d.summary) $("#nFb").textContent = d.summary.new || ""; }).catch(() => {}); }
function feedbackCard(fb) {
  if (!fb || !fb.n) return `<div class="empty"><b>No votes yet.</b>Customers vote with the thumbs beside the result count and under each Ask answer.</div>`;
  return `<div class="row" style="gap:22px;margin-bottom:8px"><div class="metric"><small>Helpful</small><b>${pct(fb.helpful_rate, 0)}</b></div><div class="metric"><small>Votes</small><b>${num(fb.n)}</b></div><div class="metric"><small>Not helpful</small><b>${num(fb.down)}</b></div><div class="metric"><small>To review</small><b>${num(fb.new)}</b></div></div>
    ${(fb.latest_down || []).length ? `<div class="sect">Latest not helpful</div>${fb.latest_down.map((r) => `<div class="small" style="padding:4px 0;border-bottom:1px solid var(--line2)"><span class="muted">${esc(r.at.slice(5))} · ${esc(r.role)}</span> ${r.kind === "ask" ? `<span class="chip">Ask</span> ${esc(r.q)}` : `<span class="link" data-a="query" data-q="${esc(r.q)}">${esc(r.q)}</span>`}${r.comment ? `<div class="fbc">“${esc(r.comment)}”</div>` : ""}</div>`).join("")}` : ""}`;
}

// ------------------------------------------------------------------ query drill-down
async function openQuery(q) {
  openDrawer(`“${q}”`, `<div id="qd">Loading…</div>`);
  const d = await adm(`/api/admin/query?q=${encodeURIComponent(q)}`);
  const L = d.live, H = d.history;
  const hasRule = A.draft.rules.find((r) => r.match.type === "exact" && r.match.q === q.toLowerCase().trim());
  $("#qd").innerHTML = `<div class="row" style="margin-bottom:12px"><button class="btn pri" data-a="${hasRule ? "editrule" : "newrule"}" data-id="${hasRule ? esc(hasRule.id) : ""}" data-q="${esc(q.toLowerCase().trim())}">${hasRule ? "Edit its rule" : "Create a rule for this search"}</button><button class="btn" data-a="newrw" data-q="${esc(q.toLowerCase().trim())}">Add a rewrite</button><a class="btn" href="/#q=${encodeURIComponent(q)}" target="_blank" rel="noopener">Open in search ↗</a></div>
    <div class="card"><h3>Live log <span class="sub">last 90 days</span></h3>${L && L.kpis.searches ? `<div class="row" style="gap:22px">${[["Searches", num(L.kpis.searches)], ["No results", pct(L.kpis.zero_rate, 0)], ["Clicked", pct(L.kpis.ctr, 0)], ["Cart", pct(L.kpis.atc_rate, 0)], ["Avg pos", L.kpis.avg_pos ?? "—"]].map(([l, v]) => `<div class="metric"><small>${l}</small><b>${v}</b></div>`).join("")}</div>
      ${L.skus.length ? `<div class="sect">Clicked</div><table class="t">${L.skus.map((s) => `<tr><td>#${esc(s.sku)}</td><td class="n">${s.clicks} clicks</td><td class="n">${s.atc} to cart</td></tr>`).join("")}</table>` : ""}` : `<div class="muted small">Not searched on this server yet.</div>`}</div>
    <div class="card mt"><h3>Historical log</h3>${H && H.n ? `<div class="row small muted" style="margin-bottom:8px">${num(H.n)} searches by ${num(H.accounts)} accounts, ${esc(H.first)} to ${esc(H.last)}</div>
      ${chart(H.monthly.map((m) => ({ day: m.month + "-01", n: m.n })), "day", "n", null, { h: 120 })}
      <div class="sect">What customers clicked</div><table class="t">${H.clicked.map((c) => `<tr><td class="clip" title="${esc(c.title)}"><span class="muted">#${esc(c.sku)}</span> ${esc(c.title)}</td><td class="n">${num(c.n)}</td><td class="n">${pct(c.share, 0)}</td><td><button class="btn sm ghost" data-a="newrule" data-q="${esc(q.toLowerCase().trim())}" data-sku="${esc(c.sku)}">Pin</button></td></tr>`).join("")}</table>
      <div class="sect">By trade</div>${hbars(H.segments.map((s) => ({ name: s.segment.replace(/_/g, " "), n: s.n })), H.n)}` : `<div class="muted small">Not in the historical log.</div>`}</div>
    <div class="card mt"><h3>Customer feedback <span class="sub">last 90 days</span></h3>${L && L.feedback && L.feedback.items.length ? `<div class="row" style="gap:22px;margin-bottom:6px"><div class="metric"><small>Helpful</small><b>${L.feedback.up}</b></div><div class="metric"><small>Not helpful</small><b>${L.feedback.down}</b></div></div>
      ${L.feedback.items.slice(0, 10).map((r) => `<div class="small" style="padding:4px 0;border-bottom:1px solid var(--line2)">${fbVote(r.vote)} <span class="muted">${esc(r.at.slice(5))} · ${esc(r.role)}</span> ${r.reasons.map((x) => `<span class="chip">${esc((A.fbLabels || FB_REASON_LABEL)[x] || x)}</span>`).join(" ")}${r.comment ? `<div class="fbc">“${esc(r.comment)}”</div>` : ""}</div>`).join("")}` : `<div class="muted small">No votes on this search.</div>`}</div>
    <div class="card mt"><h3>Results now <span class="sub">live vs your draft, as a guest</span></h3><div id="qdpv">Loading…</div></div>`;
  const x = await adm("/api/admin/preview", { method: "POST", body: { q, patch: patch(), n: 10 } }).catch((e) => ({ error: e.message }));
  if (!$("#qdpv")) return;
  $("#qdpv").innerHTML = x.error ? esc(x.error) : `<div class="pvcols">${["live", "draft"].map((k) => `<div><h4>${k}</h4>${x[k].items.map((it, i) => `<div class="pit ${it.pinned ? "pinned" : ""}"><span class="k">${i + 1}</span><span class="tt">${esc(it.title)}<br><small>${esc(it.brand)} · #${esc(it.sku)}</small></span></div>`).join("") || `<div class="muted small">No products (${x[k].total}).</div>`}</div>`).join("")}</div>`;
}

// ------------------------------------------------------------------ drawer, routing, events
function openDrawer(title, body) {
  const d = $("#adrawer");
  d.innerHTML = `<div class="dh"><b>${esc(title)}</b><span class="sp"></span><button class="btn sm" data-a="closedrawer">Close</button></div><div class="db">${body}</div>`;
  d.classList.add("on"); $("#ascrim").classList.add("on");
}
function closeDrawer() { $("#adrawer").classList.remove("on"); $("#ascrim").classList.remove("on"); A.ed = null; }

async function render() {
  A.timers.forEach(clearInterval); A.timers = [];
  $$("#side a").forEach((a) => a.classList.toggle("on", a.dataset.page === A.page));
  $("#nRules").textContent = A.draft.rules.length || ""; $("#nRw").textContent = A.draft.rewrites.length || "";
  $("#nX").textContent = A.draft.crosses.length || ""; $("#nBp").textContent = A.draft.brand_prefs.length || "";
  $("#nAb").textContent = (A.live.experiments || []).some((x) => x.status === "running") ? "1 running" : "";
  renderDraftBar();
  const el = $("#page");
  try { await (PAGES[A.page] || PAGES.overview)(el); }
  catch (e) { el.innerHTML = `<div class="explain">This section couldn't load: ${esc(e.message)}${e.status === 403 ? ". The server restarted: reload the page." : ""}</div>`; }
}
function go(p) { A.page = PAGES[p] ? p : "bulk"; if (location.hash.slice(1) !== A.page) history.replaceState(null, "", "#" + A.page); closeDrawer(); render(); window.scrollTo(0, 0); }
function setW(key, v) {
  const [s, k] = key.split(".");
  const [lo, hi] = A.limits[s];
  v = Math.max(lo, Math.min(hi, Math.round(parseFloat(v) * 1000) / 1000));
  if (Number.isNaN(v)) return;
  A.draft[s][k] = v;
  const r = $(`[data-w="${key}"]`), n = $(`[data-wn="${key}"]`);
  if (r) r.value = v; if (n && document.activeElement !== n) n.value = v;
  const b = r && r.closest(".wrow").querySelector("b"); if (b) b.classList.toggle("chg", v !== A.live[s][k]);
  renderDraftBar(); previewSoon();
}

document.addEventListener("click", async (e) => {
  const t = e.target.closest("[data-a]"); if (!t) return;
  const a = t.dataset.a;
  switch (a) {
    case "go": e.preventDefault(); go(t.dataset.p); break;
    case "days": A.days = +t.dataset.d; render(); break;
    case "query": openQuery(t.dataset.q); break;
    case "publish": publish(); break;
    case "discard": discard(); break;
    case "reset": { const s = t.dataset.s, secs = { weights: ["weights", "bulk"], layout: ["layout", "facets"] }[s] || [s];
      for (const x of secs) A.draft[x] = clone(A.defaults[x]); render(); toast(`${{ minimal_rail: "Minimal rail", weights: "Weights", layout: "Results page", affinity: "Brand affinity", personalization: "Personalization" }[s] || s} set to defaults in your draft`); break; }
    case "wdef": { const [s, k] = t.dataset.k.split("."); setW(t.dataset.k, A.defaults[s][k]); break; }
    case "rfdef": setRel("fields", t.dataset.k, 1); break;
    case "radef": setRel("attrs", t.dataset.k, 0); break;
    case "audit": {
      const body = { n: +$("#auN").value, mode: $("#auMode").value, who: who(), patch: $("#auDraft").value === "1" ? patch() : null };
      try { const st = await adm("/api/admin/audit", { method: "POST", body }); if (!st.started) toast("An audit is already running"); drawAudit(st); pollAudit(); } catch (err) { toast(err.message, true); }
      break;
    }
    case "newrule": if (t.dataset.id) { const r = A.draft.rules.find((x) => x.id === t.dataset.id); if (r) { editRule(r, false); break; } }
      closeDrawer(); newRule(t.dataset.q || "", t.dataset.sku || ""); break;
    case "editrule": { const r = A.draft.rules.find((x) => x.id === t.dataset.id); if (r) editRule(r, false); break; }
    case "delrule": A.draft.rules = A.draft.rules.filter((x) => x.id !== t.dataset.id); render(); toast("Rule removed from your draft"); break;
    case "edtype": { const r = readEditor(); r.match.type = t.dataset.v; drawEditor(); break; }
    case "rmpin": readEditor().pin.splice(+t.dataset.i, 1); drawEditor(); break;
    case "rmhide": readEditor().hide.splice(+t.dataset.i, 1); drawEditor(); break;
    case "rmcond": readEditor()[t.dataset.k].splice(+t.dataset.i, 1); drawEditor(); break;
    case "cbadd": { const k = t.dataset.k, inp = $(`[data-cbv="${k}"]`); addCond(k, inp ? (inp.dataset.val || inp.value) : "yes"); break; }
    case "audwho": { const r = readEditor(); r.audience.who = t.dataset.v; if (t.dataset.v === "guests") { r.audience.segments = []; r.audience.roles = []; } drawEditor(); break; }
    case "audseg": case "audrole": { const r = readEditor(), k = a === "audseg" ? "segments" : "roles", L = r.audience[k], v = t.dataset.v;
      r.audience[k] = L.includes(v) ? L.filter((x) => x !== v) : L.concat([v]); drawEditor(); break; }
    case "edpreview": edPreview(); break;
    case "edsave": edSave(); break;
    case "acpick": acPick(t); break;
    case "closedrawer": closeDrawer(); break;
    case "recfilter": A.recFilter = t.dataset.f; drawRecs(); break;
    case "recmore": A.recMore = true; drawRecs(); break;
    case "recapply": {
      const r = recById(t.dataset.id), act = r && r.actions[+t.dataset.k]; if (!act) break;
      try { applyOps(act.ops); } catch (err) { toast(`Couldn't apply: ${err.message}`, true); break; }
      A.recApplied.set(r.id, +t.dataset.k); renderDraftBar(); drawRecs();
      toast("In your draft. Preview it, Test in audit or start an A/B test, then Publish."); break; }
    case "recgo": {
      const r = recById(t.dataset.id), act = r && r.actions[+t.dataset.k]; if (!act) break;
      if (act.page === "synonyms") A.rwPrefill = act.prefill || null;
      if (act.page === "crosses") A.xbPrefill = act.prefill || null;
      if (act.page === "rules") A.openRule = act.rule || null;
      go(act.page); break; }
    case "recdismiss": case "recundo": {
      try { await adm("/api/admin/recs/dismiss", { method: "POST", body: { id: t.dataset.id, who: who(), undo: a === "recundo" } }); } catch (err) { toast(err.message, true); break; }
      const r = recById(t.dataset.id); if (r) r.dismissed = a === "recundo" ? null : { who: who(), at: "just now" };
      const live = A.recs.recs.filter((x) => !x.dismissed);
      A.recs.counts.high = live.filter((x) => x.priority === "high").length;
      for (const c of Object.keys(A.recs.labels)) A.recs.counts[c] = live.filter((x) => x.cat === c).length;
      $("#nRecs").textContent = A.recs.counts.high || ""; drawRecs(); toast(a === "recundo" ? "Restored" : "Dismissed for everyone (Dismissed shows it)"); break; }
    case "recrun": {
      try { const st = await adm("/api/admin/recs/run", { method: "POST", body: { who: who(), n: 150 } }); drawDeep(st); pollDeep(); } catch (err) { toast(err.message, true); }
      break; }
    case "afall": { const w = parseFloat($("#afAllW").value), ac = parseFloat($("#afAllA").value);
      for (const sg of Object.keys(A.draft.affinity.segments)) { if (!Number.isNaN(w)) A.draft.affinity.segments[sg].within = w; if (!Number.isNaN(ac)) A.draft.affinity.segments[sg].across = ac; }
      render(); break; }
    case "rmode": A.draft.layout.results_mode = t.dataset.m; render(); break;
    case "mrup": case "mrdn": { const o = A.draft.minimal_rail.order, i = +t.dataset.i, j = a === "mrup" ? i - 1 : i + 1; [o[i], o[j]] = [o[j], o[i]]; render(); break; }
    case "fup": case "fdn": { const o = A.draft.facets.order, i = +t.dataset.i, j = a === "fup" ? i - 1 : i + 1; [o[i], o[j]] = [o[j], o[i]]; render(); break; }
    case "abstart": {
      const x = { name: $("#abName").value.trim() || "A/B test", patch: patch(), split: +$("#abSplit").value, status: "running", by: who(), note: $("#abNote").value.trim(), changes: changes() };
      if (await abSave((A.live.experiments || []).concat([x]), null, `started A/B test “${x.name}”`)) { A.draft = clone(A.live); toast("Test started: your draft changes are now variant B"); render(); }
      break; }
    case "abstop": case "abpublish": case "abdel": {
      const id = t.dataset.id, xs = clone(A.live.experiments || []), x = xs.find((e_) => e_.id === id); if (!x) break;
      if (a === "abpublish" && t.dataset.confirm !== "1") { t.dataset.confirm = "1"; t.textContent = "Click again: make B live for everyone"; break; }
      if (a === "abdel") { if (await abSave(xs.filter((e_) => e_.id !== id), null, `deleted A/B test “${x.name}”`)) { A.draft = clone(A.live); render(); } break; }
      x.status = "stopped";
      if (await abSave(xs, a === "abpublish" ? x.patch : null, a === "abpublish" ? `published B of A/B test “${x.name}”` : `stopped A/B test “${x.name}”`)) {
        A.draft = clone(A.live); toast(a === "abpublish" ? `B is live for everyone (version ${A.live.version})` : "Test stopped"); render(); }
      break; }
    case "trybulk": runTry(); break;
    case "addbrandcross": {
      if (t.dataset.to) { A.draft.crosses.push({ id: "x" + Date.now().toString(36), kind: "brand", from_brand: t.dataset.b, to_brand: t.dataset.to, from_part: "", from_text: "", to_sku: "", scope: "company", enabled: true, note: "from learned choices" });
        render(); toast(`Brand cross ${t.dataset.b} → ${t.dataset.to} added to your draft`); break; }
      A.xbPrefill = t.dataset.b; go("crosses"); setTimeout(() => { const i = $("#xbTo"); if (i) i.focus(); }, 50); break; }
    case "addxb": {
      const fb = $("#xbFrom").value.trim(), tb = $("#xbTo").dataset.val || $("#xbTo").value.trim();
      if (!fb || !tb) { toast("Fill in both brands", true); break; }
      if (fb.toLowerCase() === tb.toLowerCase()) { toast("A brand can't cross to itself", true); break; }
      const sc = $("#xbScope").value;
      if (A.draft.crosses.some((c) => c.kind === "brand" && c.scope === sc && c.from_brand.toLowerCase() === fb.toLowerCase())) { toast(`${fb} already has a cross ${sc === "company" ? "company-wide" : "at that branch"}`, true); break; }
      A.draft.crosses.push({ id: "x" + Date.now().toString(36), kind: "brand", from_brand: fb, to_brand: tb, from_part: "", from_text: "", to_sku: "", scope: sc, enabled: true, note: $("#xbNote").value.trim() });
      render(); toast("Brand cross added to your draft"); break; }
    case "addxi": {
      const f = $("#xiFrom").value.trim(), sku = $("#xiTo").dataset.val || $("#xiTo").value.trim();
      if (!f || !sku) { toast("Fill in the part or line and the item", true); break; }
      const part = /^[A-Za-z]*-?\d[\w./-]*$/.test(f) && !/\s/.test(f);
      A.draft.crosses.push({ id: "x" + Date.now().toString(36), kind: "item", from_brand: "", from_part: part ? f : "", from_text: part ? "" : f.toLowerCase().replace(/\s+/g, " "), to_brand: "", to_sku: sku, scope: $("#xiScope").value, enabled: true, note: $("#xiNote").value.trim() });
      render(); toast("Item cross added to your draft"); break; }
    case "delx": A.draft.crosses = A.draft.crosses.filter((c) => c.id !== t.dataset.id); render(); break;
    case "promote": {
      const k = t.dataset.k, sku = t.dataset.sku;
      if (A.draft.crosses.some((c) => c.kind === "item" && c.from_text === k && c.to_sku === sku)) { toast("Already a permanent cross in your draft"); break; }
      A.draft.crosses.push({ id: "x" + Date.now().toString(36), kind: "item", from_brand: "", from_part: "", from_text: k, to_brand: "", to_sku: sku, scope: "company", enabled: true, note: "from learned choices" });
      render(); toast("Item cross added to your draft. Publish to make it permanent."); break; }
    case "lroff": { const k = t.dataset.k, L = A.draft.learned_off; A.draft.learned_off = L.includes(k) ? L.filter((x) => x !== k) : L.concat([k]).sort(); render(); break; }
    case "bpup": case "bpdn": { const E = A.bp, i = +t.dataset.i, j = a === "bpup" ? i - 1 : i + 1; E.family = $("#bpFam").value; E.scope = $("#bpScope").value; [E.brands[i], E.brands[j]] = [E.brands[j], E.brands[i]]; render(); break; }
    case "bprm": { const E = A.bp; E.family = $("#bpFam").value; E.scope = $("#bpScope").value; E.brands.splice(+t.dataset.i, 1); render(); break; }
    case "bpcancel": A.bp = null; render(); break;
    case "bpedit": { const b = A.draft.brand_prefs.find((x) => x.id === t.dataset.id); if (b) { A.bp = { id: b.id, family: b.family, brands: [...b.brands], scope: b.scope }; render(); window.scrollTo(0, 0); } break; }
    case "bpdel": A.draft.brand_prefs = A.draft.brand_prefs.filter((x) => x.id !== t.dataset.id); render(); break;
    case "bpsave": {
      const E = A.bp, fam = ($("#bpFam").dataset.val || $("#bpFam").value).trim(), sc = $("#bpScope").value;
      if (!fam || !E.brands.length) { toast("Pick a category and at least one brand", true); break; }
      if (A.draft.brand_prefs.some((b) => b.id !== E.id && b.scope === sc && b.family.toLowerCase() === fam.toLowerCase())) { toast(`${fam} already has an order ${sc === "company" ? "company-wide" : "at that branch"}: edit that one`, true); break; }
      const rec = { id: E.id || "b" + Date.now().toString(36), family: fam, brands: E.brands, scope: sc, enabled: true, note: "" };
      const i = A.draft.brand_prefs.findIndex((b) => b.id === rec.id); if (i >= 0) A.draft.brand_prefs[i] = { ...A.draft.brand_prefs[i], ...rec }; else A.draft.brand_prefs.push(rec);
      A.bp = null; render(); toast("Brand order saved to your draft"); break; }
    case "newrw": closeDrawer(); A.rwPrefill = t.dataset.q; if (A.page === "synonyms") { $("#rwFrom").value = t.dataset.q; $("#rwTo").focus(); } else go("synonyms"); break;
    case "delrw": A.draft.rewrites = A.draft.rewrites.filter((x) => x.id !== t.dataset.id); render(); break;
    case "pvrw": A.pv.q = t.dataset.q; go("weights"); break;
    case "revert": {
      if (!who()) { $("#who").focus(); toast("Add your name (top right) first", true); break; }
      if (t.dataset.confirm !== "1") { t.dataset.confirm = "1"; t.textContent = "Click again to revert"; break; }
      try { const r = await adm(`/api/admin/revert/${t.dataset.v}`, { method: "POST", body: { who: who() } }); A.live = r.settings; A.draft = clone(A.live); toast(`Reverted: version ${A.live.version} is live`); render(); } catch (err) { toast(err.message, true); }
      break;
    }
    case "usageDays": A.usageDays = +t.dataset.d; render(); break;
    case "siDays": A.si.days = +t.dataset.d; render(); break;
    case "siFailed": A.si.failed = t.dataset.f === "1"; render(); break;
    case "siCsv": {
      try { const csv = await adm(`/api/admin/signins.csv?days=${A.si.days}`); const u = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
        const l = document.createElement("a"); l.href = u; l.download = "flexsearch-signins.csv"; document.body.appendChild(l); l.click(); l.remove(); setTimeout(() => URL.revokeObjectURL(u), 2000); } catch (err) { toast(err.message, true); }
      break;
    }
    case "export": {
      try { const csv = await adm(`/api/admin/export.csv?table=${t.dataset.t}&days=${$("#exDays").value}`); const u = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
        const l = document.createElement("a"); l.href = u; l.download = `webb-search-${t.dataset.t}.csv`; document.body.appendChild(l); l.click(); l.remove(); setTimeout(() => URL.revokeObjectURL(u), 2000); } catch (err) { toast(err.message, true); }
      break;
    }
    case "clear": {
      if (t.dataset.confirm !== "1") { t.dataset.confirm = "1"; t.textContent = "Click again to clear everything in the live log"; break; }
      try { const r = await adm("/api/admin/analytics/clear", { method: "POST" }); toast(`Cleared ${num(r.cleared.searches)} searches, ${num(r.cleared.events)} events and ${num(r.cleared.feedback || 0)} feedback`); render(); } catch (err) { toast(err.message, true); }
      break;
    }
  }
});
document.addEventListener("submit", async (e) => {
  const f = e.target.dataset.f; if (!f) return; e.preventDefault();
  if (f === "preview") runPreview();
  if (f === "relf") render();
  if (f === "addrw") {
    const from = $("#rwFrom").value.trim().toLowerCase().replace(/\s+/g, " "), to = $("#rwTo").value.trim();
    if (!from || !to) { toast("Fill in both sides", true); return; }
    if (A.draft.rewrites.some((r) => r.from === from)) { toast(`“${from}” already has a rewrite`, true); return; }
    A.draft.rewrites.push({ id: "w" + Date.now().toString(36), from, to, enabled: true }); render(); toast("Rewrite added to your draft");
  }
});
document.addEventListener("input", (e) => {
  const t = e.target;
  if (t.dataset.rf) setRel("fields", t.dataset.rf, t.value);
  if (t.dataset.rfn && t.value.trim() !== "" && !Number.isNaN(parseFloat(t.value))) setRel("fields", t.dataset.rfn, t.value);
  if (t.dataset.ra) setRel("attrs", t.dataset.ra, t.value);
  if (t.dataset.ran && t.value.trim() !== "" && !Number.isNaN(parseFloat(t.value))) setRel("attrs", t.dataset.ran, t.value);
  if (t.id === "relq") { REL.filter = t.value; clearTimeout(REL.t); REL.t = setTimeout(() => { const pos = t.selectionStart; render(); const i = $("#relq"); if (i) { i.focus(); i.setSelectionRange(pos, pos); } }, 250); }
  if (t.dataset.w) setW(t.dataset.w, t.value);
  if (t.dataset.wn && t.value.trim() !== "" && !Number.isNaN(parseFloat(t.value))) setW(t.dataset.wn, t.value);
  if (t.dataset.ac) { if (t.dataset.val) delete t.dataset.val; clearTimeout(acT); acT = setTimeout(() => acSearch(t), 180); }
  if (t.dataset.af) setAf(t.dataset.af, t.value);
  if (t.dataset.afn && t.value.trim() !== "" && !Number.isNaN(parseFloat(t.value))) setAf(t.dataset.afn, t.value);
  if (t.dataset.bw != null && A.ed) { const r = readEditor(); r.boost[+t.dataset.bw].weight = +t.value; t.parentElement.querySelector("b").textContent = (+t.value > 0 ? "+" : "") + t.value; }
});
document.addEventListener("change", (e) => {
  const t = e.target;
  if (t.dataset.t) { A.draft.personalization[t.dataset.t] = t.checked; render(); }
  if (t.dataset.ruleOn) { const r = A.draft.rules.find((x) => x.id === t.dataset.ruleOn); if (r) { r.enabled = t.checked; render(); } }
  if (t.dataset.rwOn) { const r = A.draft.rewrites.find((x) => x.id === t.dataset.rwOn); if (r) { r.enabled = t.checked; render(); } }
  if (t.id === "pvwho") runPreview();
  if (t.dataset.cbf) { readEditor(); A.ed.cf[t.dataset.cbf] = t.value; drawEditor(); }
  if (t.dataset.afon != null) { A.draft.affinity.enabled = t.checked; render(); }
  if (t.dataset.afmin != null) { const v = parseInt(t.value); if (!Number.isNaN(v)) { A.draft.affinity.min_lines = Math.max(0, Math.min(1000, v)); render(); } }
  if (t.dataset.lay) { A.draft.layout[t.dataset.lay] = t.checked ? 1 : 0; render(); }
  if (t.dataset.mron) {
    const o = A.draft.minimal_rail.order, id = t.dataset.mron;
    if (t.checked) o.push(id);
    else if (o.length <= 2) { t.checked = true; toast("Minimal needs at least two filters", true); return; }
    else o.splice(o.indexOf(id), 1);
    render();
  }
  if (t.dataset.mrmax != null) { A.draft.minimal_rail.max = +t.value; render(); }
  if (t.id === "mrq") { A.mrq = t.value.trim() || "ball valve"; render(); }
  if (t.dataset.fshow) { const F = A.draft.facets, id = t.dataset.fshow; F.hidden = t.checked ? F.hidden.filter((x) => x !== id) : F.hidden.concat([id]).sort(); render(); }
  if (t.dataset.fcol) { const F = A.draft.facets, id = t.dataset.fcol; F.collapsed = t.checked ? F.collapsed.concat([id]).sort() : F.collapsed.filter((x) => x !== id); render(); }
  if (t.dataset.xon) { const c = A.draft.crosses.find((x) => x.id === t.dataset.xon); if (c) { c.enabled = t.checked; render(); } }
  if (t.dataset.bpon) { const b = A.draft.brand_prefs.find((x) => x.id === t.dataset.bpon); if (b) { b.enabled = t.checked; render(); } }
  if (t.id === "who") ls.set("webb_admin_who", t.value.trim());
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeDrawer();
  const t = e.target;
  if (e.key === "Enter" && t.dataset && t.dataset.cbv && !$(".acl div[data-a]", t.parentElement)) { e.preventDefault(); addCond(t.dataset.cbv, t.value); }
});
$("#ascrim").addEventListener("click", closeDrawer);
$("#side").addEventListener("click", (e) => { const a = e.target.closest("a[data-page]"); if (!a) return; e.preventDefault(); go(a.dataset.page); });
window.addEventListener("hashchange", () => { const p = location.hash.slice(1); if (p && p !== A.page && A.live) go(p); });
window.addEventListener("beforeunload", (e) => { if (A.draft && changes().length) { e.preventDefault(); e.returnValue = ""; } });

(async function init() {
  $("#who").value = ls.get("webb_admin_who", "");
  try {
    const [s, users, h] = await Promise.all([adm("/api/admin/settings"), fetch("/api/demo-users").then((r) => r.json()).catch(() => []), fetch("/api/health").then((r) => r.json()).catch(() => ({}))]);
    A.live = s.settings; A.draft = clone(s.settings); A.defaults = s.defaults; A.limits = s.limits; A.users = users; A.meta = s.meta || {};
    A.branches = await fetch("/api/branches").then((r) => r.json()).catch(() => []);
    A.branchName = Object.fromEntries(A.branches.map((b) => [b.branch_id, b.name]));
    if (h.version) $("#ver").textContent = "v" + h.version;
    const kenn = users.find((u) => /kennebec/i.test(u.name)) || users[0];
    if (kenn && !A.pv.who) A.pv.who = `${kenn.account_id}|${kenn.users[0].user_id}`;
    $("#sfoot").innerHTML = `Settings v${A.live.version}${A.live.updated_by ? ` · ${esc(A.live.updated_by)}` : " · defaults"}<br>${users.length ? `${users.length} demo accounts for previews` : "Demo accounts off: previews run as a guest"}`;
  } catch (e) { $("#page").innerHTML = `<div class="explain">The dashboard couldn't load: ${esc(e.message)}. ${e.status === 403 ? "Reload the page (the server restarted and issued a new token)." : ""}</div>`; return; }
  go(location.hash.slice(1) || "bulk");
  refreshFbCount();
})();

// ------------------------------------------------------------------ Knowledge Center review (v2.1)
A.kcv = "review";
const KC_STATUS = ["to review", "reviewed", "needs document", "wrong", "unsafe", "ok"];
const KC_KIND = { answer: "Answer", error_code: "Error code", serial: "Serial", declined: "Declined (tier 3)", emergency: "Emergency",
  webb_only: "Competitor named", out_of_scope: "Off topic" , pricing: "Pricing (sent to branch)", complaint: "Complaint", legal: "Injury or damage", abusive: "Abusive", where_to_buy: "Where to buy"};
function kcRisk(n) { const c = n >= 80 ? "var(--webb-red)" : n >= 60 ? "#e8a33a" : n >= 30 ? "#d9c27a" : "var(--line)";
  return `<span class="kcbar" title="Risk ${n} of 100"><i style="width:${n}%;background:${c}"></i></span><b class="small">${n}</b>`; }
function kcQual(n) { if (n == null) return `<span class="muted small">n/a</span>`; const c = n >= 80 ? "#2d6a33" : n >= 60 ? "#8a6d00" : "var(--webb-red)";
  return `<b class="small" style="color:${c}">${n}</b>`; }
PAGES.knowledge = async (el) => {
  el.innerHTML = `<div class="ph"><div><h1>Knowledge review</h1><p>Every Knowledge Center question and answer, with the customer who asked. Questions are scored for risk (gas, venting and line-voltage work, attempts to bypass safety devices, emergencies, competitor names) and answers for quality (citations, documents that didn't cover it, FlexIQ answers that failed their check). Anything risky or weak is queued here, riskiest first; where more documents would help is grouped under Library gaps. Internal only: it shows account and user details.</p></div></div><div id="kcp">Loading…</div>`;
  await drawKc();
};
async function drawKc() {
  const v = A.kcv;
  const f = A.kcf || "";
  const d = await adm(`/api/admin/knowledge?view=${v}&limit=300${f && v !== "gaps" && v !== "experts" ? `&flag=${encodeURIComponent(f)}` : ""}`);
  if (A.page !== "knowledge") return;
  const s = d.summary || {}, cov = d.coverage;
  $("#nKc").textContent = s.to_review || "";
  const seg = `<div class="seg">${[["review", `To review (${num(s.to_review || 0)})`], ["all", "All questions"], ["gaps", "Library gaps"], ["experts", "Expert questions"]].map(([k, l]) => `<button class="${v === k ? "on" : ""}" data-a="kcv" data-v="${k}">${l}</button>`).join("")}</div>`
    + (v === "review" || v === "all" ? ` <select class="in" id="kcFlag" title="Guardrail flag">${KC_FLAGS.map(([k, l]) => `<option value="${k}" ${k === f ? "selected" : ""}>${l}</option>`).join("")}</select>` : "");
  let body = "";
  if (v === "gaps") {
    body = (d.gaps || []).length ? `<table class="t"><tr><th>Product line</th><th>Topic</th><th>What would help</th><th class="n">Questions</th><th>Examples</th></tr>${d.gaps.map((g) => `<tr><td>${esc(g.line_name || "No model given")}</td><td>${esc(g.topic || "")}</td><td>${esc(g.need)}</td><td class="n">${g.n}</td><td class="small">${g.examples.map((x) => `“${esc(x)}”`).join("<br>")}</td></tr>`).join("")}</table>`
      : `<div class="empty"><b>No gaps yet.</b>Questions the library answers badly (not in the documents, weak matches, error codes or serials it can't read) are grouped here by product line and topic.</div>`;
  } else if (v === "experts") {
    body = (d.cases || []).length ? `<table class="t"><tr><th>Case</th><th>When</th><th>Branch · discipline</th><th>Customer</th><th>Question</th><th>Channel</th><th>Reply</th></tr>${d.cases.map((c) => `<tr><td><b>${esc(c.id)}</b><div class="muted small">${esc(c.status)}${c.tier >= 2 ? ` · tier ${c.tier}` : ""}</div></td><td class="muted small">${esc(new Date(c.opened * 1000).toLocaleString())}</td><td>${esc(c.branch || "")}<div class="muted small">${esc(c.discipline)}</div></td><td class="small">${esc(c.account || "")}<div class="muted">${esc(c.user || "")}</div></td><td>${esc(c.question)}${(c.conversation || []).length > 1 ? `<div class="muted small">${c.conversation.length} turns attached</div>` : ""}</td><td>${esc(c.channel)}</td>
      <td>${c.reply ? `<div class="small">${esc(c.reply)}</div>` : `<div class="row"><input class="in" data-kcreply="${esc(c.id)}" placeholder="Reply to the customer" style="width:220px"><button class="btn sm pri" data-a="kcsend" data-id="${esc(c.id)}">Send</button></div>`}</td></tr>`).join("")}</table>`
      : `<div class="empty"><b>No expert questions yet.</b>Customers send them from the Knowledge Center's expert card; each arrives with the conversation, the product and the account.</div>`;
  } else {
    const items = d.items || [];
    body = items.length ? `<div class="scroll" style="max-height:820px"><table class="t kct"><tr><th>When</th><th>Customer</th><th>Question</th><th>Kind</th><th>Risk</th><th class="n">Answer quality</th><th>Why flagged</th><th>Answer · sources</th><th>Status · note</th></tr>${items.map((r) => `<tr class="${r.risk >= 80 ? "hot" : ""}">
      <td class="muted small">${esc(new Date(r.ts * 1000).toLocaleString())}</td>
      <td class="small"><b>${esc(r.account_name || r.account || "")}</b><div class="muted">${esc(r.user || "")} · ${esc(r.role || "")}<br>${esc(r.branch || "")}</div></td>
      <td>${esc(r.question)}${r.line_name ? `<div class="muted small">${esc(r.line_name)}</div>` : ""}</td>
      <td class="small">${esc(KC_KIND[r.kind] || r.kind)}${r.tier ? `<div class="muted">tier ${r.tier}</div>` : ""}<div class="muted">${esc(r.engine || "")}</div></td>
      <td style="white-space:nowrap">${kcRisk(r.risk)}</td><td class="n">${kcQual(r.quality)}</td>
      <td class="small">${(r.flags || []).map((f) => `<div>${esc(f)}</div>`).join("")}${(r.tags || []).filter((x) => KC_FLAGS.some(([k]) => k && k === x)).map((x) => `<span class="chip">${esc((KC_FLAGS.find(([k]) => k === x) || [x, x])[1])}</span>`).join(" ")}${r.feedback ? `<div class="chip">customer: ${esc(JSON.parse(r.feedback).vote)} ${esc(JSON.parse(r.feedback).reason || "")}</div>` : ""}</td>
      <td class="small" style="max-width:300px">${esc((r.answer || "").slice(0, 260))}${(r.sources || []).length ? `<div class="muted">${r.sources.map((x) => `[${x.n}] ${esc((x.title || "").split(": ").pop())} p.${x.page}`).join("; ")}</div>` : ""}${r.doc_gap ? `<div class="chip">needs ${esc(r.doc_gap.need)}</div>` : ""}</td>
      <td><select class="in" data-kcst="${esc(r.id)}">${KC_STATUS.map((x) => `<option ${x === r.status ? "selected" : ""}>${x}</option>`).join("")}</select>
        <input class="in" data-kcnote="${esc(r.id)}" placeholder="Note" value="${esc(r.note || "")}" style="width:150px;margin-top:4px">${r.reviewer ? `<div class="muted small">${esc(r.reviewer)}</div>` : ""}</td></tr>`).join("")}</table></div>`
      : `<div class="empty"><b>${v === "review" ? "Nothing to review." : "No questions yet."}</b>Questions asked in the storefront's Knowledge tab land here within a second.</div>`;
  }
  $("#kcp").innerHTML = `<div class="kpis">${kpi("Questions", num(s.questions || 0))}${kpi("Flagged", num(s.flagged || 0))}${kpi("To review", num(s.to_review || 0))}
      ${kpi("Avg answer quality", s.avg_quality == null ? "—" : String(s.avg_quality))}${kpi("Gas, venting or electrical", num(s.high_hazard || 0))}
      ${cov ? kpi("Library", `${num(cov.documents)} docs`) : ""}</div>
    ${cov ? `<div class="explain mt">Library: ${num(cov.lines)} product lines, ${num(cov.documents)} documents, ${num(cov.pages)} pages, ${num(cov.codes)} error codes. ${cov.families.map((f) => `${esc(f.family)} ${num(f.with_documents)} of ${num(f.items)} items covered`).join(" · ")}. ${cov.synthetic ? "<b>Synthetic demo library</b>: replaced by Webb's real documents." : ""}</div>` : ""}
    <div class="card mt"><h3>${seg}</h3>${body}</div>`;
}
// the guardrail flags a question or answer can carry (v2.2)
const KC_FLAGS = [["", "Every flag"], ["warranty", "Warranty"], ["repair", "Repair"], ["lifecycle", "Lifecycle"],
  ["complaint", "Complaint about Webb"], ["legal", "Injury, damage or claim"], ["pricing", "Pricing talk"],
  ["recall", "Recall"], ["code", "Code question"], ["profanity", "Profanity"], ["abusive", "Abusive"],
  ["injection", "Prompt injection"], ["pii", "Personal data (redacted)"], ["where_to_buy", "Where to buy"],
  ["flexiq_rejected", "FlexIQ answer replaced"], ["flexiq_budget", "FlexIQ hourly limit"]];
function kcSave(id, body) {
  return adm(`/api/admin/knowledge/${encodeURIComponent(id)}`, { method: "POST", body: { ...body, reviewer: who() } })
    .then(() => toast(body.status ? `Marked ${body.status}` : "Note saved")).catch((err) => toast(err.message, true));
}
document.addEventListener("change", (e) => {
  const t = e.target;
  if (t.dataset.kcst) { const note = ($(`[data-kcnote="${t.dataset.kcst}"]`) || {}).value || ""; kcSave(t.dataset.kcst, { status: t.value, note }); }
  if (t.id === "kcFlag") { A.kcf = t.value; drawKc(); }
  if (t.dataset.kcnote) { const st = ($(`[data-kcst="${t.dataset.kcnote}"]`) || {}).value || "reviewed"; kcSave(t.dataset.kcnote, { status: st, note: t.value }); }
});
document.addEventListener("click", async (e) => {
  const t = e.target.closest("[data-a]"); if (!t) return;
  if (t.dataset.a === "kcv") { A.kcv = t.dataset.v; drawKc(); }
  if (t.dataset.a === "kcsend") {
    const inp = $(`[data-kcreply="${t.dataset.id}"]`); if (!inp || !inp.value.trim()) return;
    try { await adm(`/api/admin/experts/${encodeURIComponent(t.dataset.id)}`, { method: "POST", body: { reply: inp.value.trim() } }); toast("Reply sent"); drawKc(); } catch (err) { toast(err.message, true); }
  }
});

