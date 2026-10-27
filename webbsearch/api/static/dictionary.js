"use strict";
// Search dictionary page (v1.7): the trade vocabulary the eCommerce team maintains. Uses admin.js's helpers.
// Every change saves immediately and is logged per entry (who, when, before, after); only entries marked Live affect
// search. Lint runs as you type; Measure searches the typed words and the target; Try shows the results page both ways.
const DX = { f: { status: "", kind: "", dept: "", flag: "", source: "", q: "", sort: "uses" }, page: 0, size: 50, data: null, sel: new Set(), ed: null, lintT: null };
const DK = { synonym: "Synonym", spelling: "Spelling", brand: "Brand alias", block: "Don't rewrite", glossary: "Glossary" };
const DS = { live: "Live", review: "In review", off: "Off" };
const DS_CHIP = { live: "ok", review: "warn", off: "" };
const DKIND_HELP = {
  synonym: "People type the words on the left; search the words on the right instead (whole words, any case; hyphens, spaces and plurals don't matter).",
  spelling: "A misspelling. The typed word must not be a word the catalog uses.",
  brand: "Another name for a brand the catalog carries (B&G → Bell & Gossett). Keep the product words out of it: “B&G pump” then searches “Bell & Gossett pump”.",
  block: "Never rewrite this word on its own (it means several things). Longer phrases that contain it can still be rules (“run cap”).",
  glossary: "A definition for the team; no effect on search.",
};
const FLAG_LABEL = { bare_number: "bare number", short: "two letters", blocked: "blocked word", conflict: "conflict", same: "same words",
  real_word: "real word", part_number: "looks like a part #", state: "state code", brand_word: "brand word", builtin: "built-in rule",
  unknown_brand: "unknown brand", adds_words: "adds words", target_missing: "target not in catalog", backwards: "maybe backwards",
  duplicate: "duplicate", no_definition: "no definition" };
const FLAG_ERR = new Set(["bare_number", "short", "blocked", "conflict", "same", "real_word"]);

function dxQuery() {
  const p = new URLSearchParams({ ...DX.f, page: DX.page, size: DX.size });
  return "/api/admin/dictionary?" + p.toString();
}
async function dxLoad() { DX.data = await adm(dxQuery()); if ($("#nDict")) $("#nDict").textContent = DX.data.counts.status.review || ""; return DX.data; }

PAGES.dictionary = async (el) => {
  el.innerHTML = `<div class="ph"><div><h1>Search dictionary</h1><p>The trade words customers type and what the catalog calls them: synonyms, misspellings, brand aliases, words never to rewrite, and a glossary. Only <b>Live</b> entries change search; every change is saved as you make it and logged with your name. The results page says when it searched other words and offers the words as typed.</p></div>
    <span class="sp"></span><button class="btn" data-dx="langs">Languages</button><button class="btn" data-dx="builtin">Built-in rules</button><button class="btn" data-dx="log">Change history</button><button class="btn" data-dx="import">Import</button><a class="btn" id="dxExport" href="#">Export CSV</a><button class="btn pri" data-dx="new">New entry</button></div>
    <div id="dxk" class="kpis dxk"></div>
    <div class="card mt"><div class="row dxbar">
      <input class="in" data-dxf="q" placeholder="Find words… e.g. cleanout" style="flex:1;min-width:180px" value="${esc(DX.f.q)}">
      <div class="seg" role="group" aria-label="Status">${[["", "All"], ["live", "Live"], ["review", "In review"], ["off", "Off"]].map(([v, l]) => `<button data-dx="st" data-v="${v}" class="${DX.f.status === v ? "on" : ""}">${l}</button>`).join("")}</div>
      <select class="in" data-dxf="kind" aria-label="Kind"><option value="">Every kind</option>${Object.entries(DK).map(([k, l]) => `<option value="${k}" ${DX.f.kind === k ? "selected" : ""}>${l}</option>`).join("")}</select>
      <select class="in" data-dxf="dept" aria-label="Department" id="dxDept"><option value="">Every department</option></select>
      <select class="in" data-dxf="flag" aria-label="Check" id="dxFlag"><option value="">Any check</option></select>
      <select class="in" data-dxf="source" aria-label="Source" id="dxSrc"><option value="">Any source</option></select>
      <select class="in" data-dxf="sort" aria-label="Sort"><option value="uses">Most searched</option><option value="term" ${DX.f.sort === "term" ? "selected" : ""}>A–Z</option><option value="updated" ${DX.f.sort === "updated" ? "selected" : ""}>Recently changed</option></select>
    </div><div id="dxsel" class="dxsel" hidden></div><div id="dxt">Loading…</div></div>
    <div class="card mt" id="dxcand"></div>`;
  const d = await dxLoad();
  dxFilters(d); dxKpis(d); dxTable(d); dxCandidates();
};

// v2.43: searches people fixed themselves (no results, then a similar search that got a click, in the same session).
// Each pair seen in two or more sessions can be added in review; a second person makes it live, as for any entry
async function dxCandidates() {
  const box = $("#dxcand"); if (!box) return;
  let r; try { r = await adm("/api/admin/dictionary/candidates?days=90&min_sessions=2"); } catch (e) { box.hidden = true; return; }
  const c = r.candidates || [];
  box.innerHTML = `<h3>Suggested from searches <span class="sub">people who found nothing, then searched again and clicked, in the last 90 days</span></h3>`
    + (c.length ? `<table class="t"><tr><th>Typed</th><th>Then searched</th><th>Sessions</th><th>Kind</th><th></th></tr>${c.map((x, i) => `<tr>
      <td class="q"><b>${esc(x.from)}</b></td><td>${esc(x.to)}</td><td class="n">${num(x.sessions)}</td><td><span class="chip">${DK[x.kind] || x.kind}</span></td>
      <td><button class="btn sm" data-dx="cand" data-i="${i}">Add in review</button></td></tr>`).join("")}</table>`
      : `<div class="pvnote">No repeated fixes yet. A pair appears here once two sessions make the same correction.</div>`);
  DX.cand = c;
}

function dxFilters(d) {
  const fill = (id, opts, cur, lab) => { const s = $(id); if (!s) return; s.innerHTML = `<option value="">${lab}</option>` + opts.map(([v, l]) => `<option value="${esc(v)}" ${cur === v ? "selected" : ""}>${esc(l)}</option>`).join(""); };
  fill("#dxDept", d.depts.map((x) => [x, x]), DX.f.dept, "Every department");
  fill("#dxFlag", Object.entries(d.flags).map(([k, n]) => [k, `${FLAG_LABEL[k] || k} (${n})`]), DX.f.flag, "Any check");
  fill("#dxSrc", d.sources.map((x) => [x, x.length > 48 ? x.slice(0, 46) + "…" : x]), DX.f.source, "Any source");
  const ex = $("#dxExport"); if (ex) ex.href = `/api/admin/dictionary/export.csv?status=${encodeURIComponent(DX.f.status)}&kind=${encodeURIComponent(DX.f.kind)}`;
}
function dxKpis(d) {
  const c = d.counts, k = (l, v, sub) => `<div class="kpi"><small>${l}</small><b>${num(v)}</b>${sub ? `<span class="d muted">${sub}</span>` : ""}</div>`;
  $("#dxk").innerHTML = k("Live rules", c.live_rules, "change search now") + k("In review", c.status.review, "waiting for a person") + k("Off", c.status.off, "kept with the reason")
    + k("Don't rewrite", c.kind.block, "ambiguous words") + k("Glossary", c.kind.glossary, "definitions") + k("Entries", c.total, `version ${d.version}`);
}
function dxRow(e) {
  const ctx = (e.any || []).length ? `<div class="muted small">only with: ${esc(e.any.join(", "))}</div>` : "";
  const nctx = (e.none || []).length ? `<div class="muted small">not with: ${esc(e.none.join(", "))}</div>` : "";
  const flags = (e.flags || []).map((f) => `<span class="chip ${FLAG_ERR.has(f) ? "red" : "warn"}" title="${esc(f)}">${esc(FLAG_LABEL[f] || f)}</span>`).join(" ");
  const target = e.kind === "block" ? `<span class="muted">never rewritten alone</span>` : e.kind === "glossary" ? `<span class="muted">${esc(e.definition || e.to || "")}</span>` : esc(e.to);
  return `<tr class="click" data-dx="edit" data-id="${esc(e.id)}">
    <td data-dx="noop"><input type="checkbox" data-dxsel="${esc(e.id)}" ${DX.sel.has(e.id) ? "checked" : ""} aria-label="Select ${esc(e.term)}"></td>
    <td class="q"><b>${esc(e.term)}</b>${ctx}${nctx}</td><td>${target}</td>
    <td><span class="chip">${DK[e.kind]}</span></td><td><span class="chip ${DS_CHIP[e.status]}">${DS[e.status]}</span></td>
    <td class="n">${e.uses ? num(e.uses) : `<span class="muted">0</span>`}</td>
    <td class="dxqc"><div>${esc(e.qc || e.note || "")}</div>${flags ? `<div class="mt4">${flags}</div>` : ""}</td>
    <td class="muted small">${esc(e.updated_by || "")}<br>${esc((e.updated_at || "").slice(0, 10))}</td></tr>`;
}
function dxTable(d) {
  const pages = Math.max(1, Math.ceil(d.total / d.size));
  $("#dxt").innerHTML = d.entries.length ? `<table class="t dxt"><tr><th style="width:26px"><input type="checkbox" data-dxall aria-label="Select all on this page"></th><th>People type</th><th>Searched as</th><th>Kind</th><th>Status</th><th class="n" title="Logged searches containing these words (live log + historical log)">Searches</th><th>Quality check</th><th>Changed</th></tr>
      ${d.entries.map(dxRow).join("")}</table>
      <div class="row mt"><span class="muted small">${num(d.total)} entries${d.total > d.size ? ` · page ${d.page + 1} of ${pages}` : ""}</span><span class="sp" style="flex:1"></span>
      <button class="btn sm" data-dx="pg" data-d="-1" ${d.page ? "" : "disabled"}>Previous</button><button class="btn sm" data-dx="pg" data-d="1" ${d.page + 1 < pages ? "" : "disabled"}>Next</button></div>`
    : `<div class="empty"><b>No entries match.</b>Clear a filter, or add one with New entry.</div>`;
  dxSelBar();
}
function dxSelBar() {
  const b = $("#dxsel"); if (!b) return;
  b.hidden = !DX.sel.size;
  b.innerHTML = `<b>${DX.sel.size} selected</b><button class="btn sm pri" data-dx="bulk" data-v="live">Make live</button><button class="btn sm" data-dx="bulk" data-v="review">Send to review</button><button class="btn sm" data-dx="bulk" data-v="off">Switch off</button><button class="btn sm ghost" data-dx="clearsel">Clear</button>`;
}
async function dxRefresh() { const d = await dxLoad(); if (!$("#dxt")) return; dxFilters(d); dxKpis(d); dxTable(d); }

// ------------------------------------------------------------------ editor
function dxBlank(term) { return { id: "", kind: "synonym", term: term || "", to: "", any: [], none: [], dept: "", status: "review", note: "", definition: "", qc: "", source: "dashboard" }; }
function dxOpen(e, isNew) {
  DX.ed = { e: JSON.parse(JSON.stringify(e)), isNew, lint: null, measure: null, tryQ: e.term ? e.term + (e.any && e.any.length ? " " + e.any[0] : "") : "" };
  openDrawer(isNew ? "New dictionary entry" : `Entry: ${e.term}`, `<div id="dxed"></div>`);
  dxDraw(); dxLintSoon(0);
  if (!isNew) adm(`/api/admin/dictionary/history?id=${encodeURIComponent(e.id)}`).then((h) => dxDrawHist(h.history)).catch(() => {});
}
function dxDraw() {
  const E = DX.ed.e, box = $("#dxed"); if (!box) return;
  const tgt = E.kind === "glossary" ? `<label class="dxl">Definition<textarea class="in" rows="2" data-dxe="definition" maxlength="300">${esc(E.definition || "")}</textarea></label>`
    : E.kind === "block" ? "" : `<label class="dxl">${E.kind === "brand" ? "Brand (as the catalog names it)" : "Search for"}<input class="in" data-dxe="to" value="${esc(E.to)}" maxlength="120" placeholder="${E.kind === "brand" ? "e.g. Bell & Gossett" : "e.g. lavatory faucet"}"></label>`;
  box.innerHTML = `<div class="dxform">
    <div class="seg" role="group" aria-label="Kind">${Object.entries(DK).map(([k, l]) => `<button data-dx="kind" data-v="${k}" class="${E.kind === k ? "on" : ""}">${l}</button>`).join("")}</div>
    <p class="muted small">${DKIND_HELP[E.kind]}</p>
    <div class="grid g2"><label class="dxl">People type<input class="in" data-dxe="term" value="${esc(E.term)}" maxlength="80" placeholder="e.g. bathroom faucet"></label>${tgt}</div>
    ${["synonym", "spelling", "brand"].includes(E.kind) ? `<div class="grid g2"><label class="dxl">Only when the search also has any of<input class="in" data-dxe="any" value="${esc((E.any || []).join(", "))}" placeholder="optional, comma-separated: plug, tee, pvc"></label>
      <label class="dxl">…and none of<input class="in" data-dxe="none" value="${esc((E.none || []).join(", "))}" placeholder="optional: detector, alarm"></label></div>` : ""}
    <div class="grid g2"><label class="dxl">Department<input class="in" data-dxe="dept" list="dxDepts" value="${esc(E.dept || "")}" maxlength="40"><datalist id="dxDepts">${((DX.data && DX.data.depts) || []).map((x) => `<option value="${esc(x)}">`).join("")}</datalist></label>
      <label class="dxl">Why (for the next reviewer)<input class="in" data-dxe="note" value="${esc(E.note || "")}" maxlength="500"></label></div>
    <div class="row"><span class="dxlab">Status</span><div class="seg" role="group" aria-label="Status">${Object.entries(DS).map(([k, l]) => `<button data-dx="est" data-v="${k}" class="${E.status === k ? "on" : ""}">${l}</button>`).join("")}</div>
      <span class="muted small">${E.status === "live" ? "Changes every search as soon as you save." : E.status === "review" ? "Saved, no effect on search until someone makes it live." : "Kept with the reason; no effect on search."}</span></div>
    <div id="dxlint" class="dxlint"></div>
    ${E.qc ? `<div class="explain small"><b>Quality check:</b> ${esc(E.qc)}${E.source ? `<br><span class="muted">Source: ${esc(E.source)}</span>` : ""}</div>` : ""}
    <div class="row"><button class="btn pri" data-dx="save">Save</button>${["synonym", "spelling", "brand"].includes(E.kind) ? `<button class="btn" data-dx="measure">Measure</button>` : ""}
      <span class="sp" style="flex:1"></span>${DX.ed.isNew ? "" : `<button class="btn ghost danger" data-dx="del">Delete…</button>`}</div>
    <div id="dxmeasure"></div>
    ${["synonym", "spelling", "brand"].includes(E.kind) ? `<div class="card mt"><h3>Try it <span class="sub">the results page without this entry, and with it as it is in the form (everything else as live)</span></h3>
      <form class="row" data-dxform="try"><input class="in" id="dxTryQ" value="${esc(DX.ed.tryQ)}" style="flex:1" placeholder="A search to try"><button class="btn">Try</button></form><div id="dxtry" class="mt"></div></div>` : ""}
    ${DX.ed.isNew ? "" : `<div class="card mt"><h3>History <span class="sub">every change to this entry</span></h3><div id="dxhist">Loading…</div></div>`}
  </div>`;
  dxDrawLint();
}
function dxRead() {
  const E = DX.ed.e;
  $$("[data-dxe]").forEach((i) => { const k = i.dataset.dxe; E[k] = (k === "any" || k === "none") ? i.value.split(",").map((x) => x.trim()).filter(Boolean) : i.value; });
  const tq = $("#dxTryQ"); if (tq) DX.ed.tryQ = tq.value;
  return E;
}
function dxLintSoon(ms = 350) {
  clearTimeout(DX.lintT);
  DX.lintT = setTimeout(async () => {
    if (!DX.ed) return;
    try { DX.ed.lint = await adm("/api/admin/dictionary/lint", { method: "POST", body: { entry: dxRead() } }); } catch (err) { DX.ed.lint = { shape: err.message, checks: [] }; }
    dxDrawLint();
  }, ms);
}
function dxDrawLint() {
  const box = $("#dxlint"); if (!box || !DX.ed) return;
  const L = DX.ed.lint;
  if (!L) { box.innerHTML = ""; return; }
  if (L.shape) { box.innerHTML = `<div class="chk err">${esc(L.shape)}</div>`; return; }
  const errs = L.checks.filter((c) => c.level === "error");
  box.innerHTML = (L.checks.length ? L.checks.map((c) => `<div class="chk ${c.level === "error" ? "err" : "warn"}"><b>${c.level === "error" ? "Can't go live" : "Check"}:</b> ${esc(c.text)}</div>`).join("")
    : `<div class="chk ok">No problems found.</div>`) + `<div class="muted small">${L.uses ? `${num(L.uses)} logged searches contain these words.` : "No logged search contains these words yet."}</div>`;
  const sv = $('[data-dx="save"]'); if (sv) { const blocked = DX.ed.e.status === "live" && errs.length; sv.disabled = !!blocked; sv.title = blocked ? "Fix the problems above, or save it In review" : ""; }
}
function dxDrawHist(h) {
  const box = $("#dxhist"); if (!box) return;
  const say = { add: "added", edit: "changed", status: "status", delete: "deleted", import: "imported" };
  box.innerHTML = h.length ? `<table class="t">${h.map((r) => `<tr><td class="muted small" style="white-space:nowrap">${esc(r.at)}</td><td>${esc(r.who)}</td><td>${esc(say[r.action] || r.action)}${r.after && r.before && r.after.status !== r.before.status ? `: ${DS[r.before.status]} → ${DS[r.after.status]}` : ""}${r.note ? ` <span class="muted">· ${esc(r.note)}</span>` : ""}
      <div class="muted small">${r.after ? `${esc(r.after.term)} → ${esc(r.after.to || r.after.definition || "")}` : ""}</div></td>
      <td>${r.after || r.before ? `<button class="btn sm ghost" data-dx="restore" data-at="${esc(r.at)}" title="Put the entry back the way it was after this change">Restore</button>` : ""}</td></tr>`).join("")}</table>` : `<div class="muted">No changes yet.</div>`;
}
async function dxSave() {
  const E = dxRead();
  try {
    const r = await adm("/api/admin/dictionary", { method: "POST", body: { entry: E, who: who(), base_version: DX.data ? DX.data.version : null } });
    toast(r.held_for_review ? r.message : (E.status === "live" ? "Saved: live (no change to what it does)" : "Saved"));
    DX.ed.e = r.entry; DX.ed.isNew = false; await dxRefresh(); closeDrawer();
  } catch (err) { toast(err.status === 409 ? "Someone changed the dictionary since this page loaded; reloading" : err.message, true); if (err.status === 409) dxRefresh(); }
}
async function dxMeasure() {
  const box = $("#dxmeasure"); box.innerHTML = `<div class="muted mt">Searching both ways…</div>`;
  try {
    const m = await adm("/api/admin/dictionary/measure", { method: "POST", body: { entry: dxRead() } });
    DX.ed.measure = m;
    const x = m.measure;
    box.innerHTML = `<div class="card mt"><h3>Measured <span class="sub">first 10 results, anonymous, merchandising off</span></h3>
      ${x ? `<table class="t"><tr><th></th><th class="n">Results</th><th class="n">On target</th></tr><tr><td>As typed: “${esc(DX.ed.e.term)}”</td><td class="n">${num(x.typed_total)}${x.typed_fallback ? " <span class=\"chip warn\">no full match</span>" : ""}</td><td class="n">${pct(x.typed_on, 0)}</td></tr>
        <tr><td>As the target: “${esc(DX.ed.e.to)}”</td><td class="n">${num(x.target_total)}${x.target_fallback ? " <span class=\"chip warn\">no full match</span>" : ""}</td><td class="n">${pct(x.target_on, 0)}</td></tr></table>
        <p class="small">${Math.round(x.overlap * 10)} of the first 10 are the same products. “On target” is the share of the target's words in each result's title.</p>` : ""}
      <div class="row"><span class="chip ${DS_CHIP[m.suggest]}">Suggests: ${DS[m.suggest]}</span><span class="small">${esc(m.why)}</span><span class="sp" style="flex:1"></span>
        ${m.suggest !== DX.ed.e.status ? `<button class="btn sm" data-dx="usesug" data-v="${m.suggest}">Use ${DS[m.suggest]}</button>` : ""}</div></div>`;
    DX.ed.e.qc = m.text;
  } catch (err) { box.innerHTML = `<div class="explain">${esc(err.message)}</div>`; }
}
async function dxTry() {
  const E = dxRead(), box = $("#dxtry"); const q = (DX.ed.tryQ || E.term).trim(); if (!q) return;
  box.innerHTML = `<div class="muted">Searching…</div>`;
  const [acct, user] = (A.pv.who || "").split("|");
  try {
    const r = await adm("/api/admin/dictionary/try", { method: "POST", body: { q, entry: { ...E, status: "live" }, account: acct || null, user: user || null, n: 10 } });
    const col = (side, title) => `<div><h4 class="dxh4">${title} <span class="muted small">${num(side.total)} results${side.searched !== q ? ` · searched “${esc(side.searched)}”` : ""}</span></h4>
      ${side.vocab.length ? `<div class="small muted">${side.vocab.map((v) => `“${esc(v.from)}” → “${esc(v.to)}”`).join(", ")}</div>` : ""}
      <ol class="dxres">${side.items.map((it) => `<li>${esc(it.title)} <span class="muted small">${esc(it.brand)}</span>${it.move != null && it.move !== 0 ? ` <span class="chip ${it.move > 0 ? "ok" : "red"}">${it.move > 0 ? "▲" : "▼"}${Math.abs(it.move)}</span>` : it.was == null && side === r.draft ? ` <span class="chip info">new</span>` : ""}</li>`).join("")}</ol></div>`;
    box.innerHTML = `<div class="grid g2">${col(r.live, "Without this entry")}${col(r.draft, "With this entry")}</div>`;
  } catch (err) { box.innerHTML = `<div class="explain">${esc(err.message)}</div>`; }
}

// ------------------------------------------------------------------ import, built-in, history
function dxImport() {
  openDrawer("Import dictionary entries", `<p>A CSV or Excel file: this page's export (id, kind, term, to, any, none, dept, status, definition, note), or a workbook with <b>Search Term</b>, <b>Synonym</b>, <b>Department</b>, <b>Handling</b>, <b>Notes</b> columns. Rows with an id update that entry; others are added. Every row is linted; a row with a problem never arrives live.</p>
    <div class="row"><input type="file" id="dxFile" accept=".csv,.xlsx,.xlsm,text/csv"></div>
    <label class="row mt"><input type="checkbox" id="dxKeep"> Keep the file's status column (otherwise every row arrives <b>In review</b>)</label>
    <div class="row mt"><button class="btn pri" data-dx="doimport">Import</button></div><div id="dximp" class="mt"></div>`);
}
async function dxDoImport() {
  const f = $("#dxFile").files[0]; if (!f) { toast("Choose a file first", true); return; }
  if (f.size > 10e6) { toast("Files up to 10 MB", true); return; }
  const data = await new Promise((ok, no) => { const rd = new FileReader(); rd.onload = () => ok(rd.result); rd.onerror = no; rd.readAsDataURL(f); });
  $("#dximp").innerHTML = `<div class="muted">Importing…</div>`;
  try {
    const r = await adm("/api/admin/dictionary/import", { method: "POST", body: { name: f.name, data, who: who(), keep_status: $("#dxKeep").checked } });
    $("#dximp").innerHTML = `<div class="explain"><b>${num(r.imported)} entries imported</b> from ${num(r.rows)} rows (source “${esc(r.source)}”).${r.demoted_to_review ? ` ${num(r.demoted_to_review)} arrived In review because of a problem the check found.` : ""}${r.held_for_approval ? ` ${num(r.held_for_approval)} marked live in the file wait In review for someone else to make them live.` : ""}${r.bad_rows.length ? ` ${r.bad_rows.length} rows skipped: ${esc(r.bad_rows.slice(0, 3).map((b) => `row ${b.row}: ${b.why}`).join("; "))}.` : ""}</div>`;
    dxRefresh();
  } catch (err) { $("#dximp").innerHTML = `<div class="explain">${esc(err.message)}</div>`; }
}
async function dxBuiltin() {
  const r = await adm("/api/admin/dictionary/builtin");
  const flagged = { stat: "Also aquastat, humidistat, pressure stat.", ac: "Also alternating current (24 VAC).", prv: "Also pressure relief valve.", lp: "Also low pressure (LP switch).",
    condenser: "Also a condenser coil.", lf: "Also linear feet.", ng: "“ng furnace” also matches model numbers like NG-261; see the NG entry (in review)." };
  openDrawer("Built-in rules", `<p>These rules are part of the search engine itself. They apply to the catalog as well as to searches, so changing one means rebuilding the index: ask engineering. Rules flagged by the review are marked.</p>
    <table class="t"><tr><th>Word</th><th>Read as</th><th>Where</th><th></th></tr>${r.rules.map((x) => `<tr><td class="q"><b>${esc(x.term)}</b></td><td>${esc(x.to)}</td><td class="muted small">${esc(x.where)}</td><td class="small">${flagged[x.term] ? `<span class="chip warn">review</span> ${esc(flagged[x.term])}` : ""}</td></tr>`).join("")}</table>`);
}
// v2.4: trade words in 12 languages, read before the dictionary
async function dxLangs() {
  const r = await adm("/api/admin/lexicon");
  const pct = (x) => `${Math.round(x * 100)}%`;
  openDrawer("Languages", `<p>Searches typed in these languages are read into the catalog's English before the dictionary: the results page says “Read as Portuguese: Searched as …”. ${r.concepts} trade concepts; Spanish, Portuguese, Polish, Russian and Chinese cover all of them, the others the ${r.core} core ones. Chosen from Census data on the languages Webb's trades speak (construction and repair workers with limited English, weighted by branches). The word lists are files in the code (webbsearch/retrieval/lexicon/terms), reviewed before each release; ask engineering to change one.</p>
    <div class="row" style="gap:6px;margin:8px 0"><input class="in" id="dxLangQ" placeholder="Try a search, e.g. registro de esfera 3/4, шаровой кран, 6分球阀" style="flex:1;min-width:200px"><button class="btn" data-dx="langtry">Try</button></div><div id="dxLangOut" class="small"></div>
    <table class="t mt"><tr><th>Language</th><th>Depth</th><th class="r">Terms</th><th class="r">All concepts</th><th class="r">Core</th><th></th></tr>${r.languages.map((x) => `<tr><td><b>${esc(x.name)}</b></td><td>${x.depth === "full" ? "Full" : "Core"}</td><td class="r">${x.terms}</td><td class="r">${pct(x.full)}</td><td class="r">${pct(x.core)}</td><td><button class="linkbtn" data-dx="langshow" data-l="${esc(x.code)}">Terms</button></td></tr>`).join("")}</table><div id="dxLangTerms" class="mt"></div>`);
  const q = $("#dxLangQ"); if (q) q.addEventListener("keydown", (e) => { if (e.key === "Enter") dxLangTry(); });
}
async function dxLangTry() {
  const q = ($("#dxLangQ") || {}).value || ""; if (!q.trim()) return;
  const r = await adm(`/api/admin/lexicon?q=${encodeURIComponent(q)}`);
  const t = r.try;
  $("#dxLangOut").innerHTML = t.lang ? `Read as <b>${esc(t.name)}</b>: searched as “<b>${esc(t.to)}</b>”` : `English (or not recognised): searched as typed.`;
}
async function dxLangShow(code) {
  const r = await adm(`/api/admin/lexicon?lang=${encodeURIComponent(code)}`);
  const name = (r.languages.find((x) => x.code === code) || {}).name || code;
  $("#dxLangTerms").innerHTML = `<h3>${esc(name)}</h3><table class="t"><tr><th>Catalog words</th><th>${esc(name)}</th></tr>${r.entries.map((x) => `<tr><td class="q"><b>${esc(x.english)}</b></td><td>${x.terms.map(esc).join(" · ")}</td></tr>`).join("")}</table>`;
}
async function dxLog() {
  const h = await adm("/api/admin/dictionary/history?limit=200");
  const say = { add: "added", edit: "changed", status: "status", delete: "deleted", import: "imported" };
  const rows = h.history;
  openDrawer("Dictionary change history", rows.length ? `<table class="t"><tr><th>When</th><th>Who</th><th>What</th></tr>${rows.map((r) => `<tr><td class="muted small" style="white-space:nowrap">${esc(r.at)}</td><td>${esc(r.who)}</td><td>${esc(say[r.action] || r.action)} <b>${esc((r.after || r.before || {}).term || "")}</b>${r.after && r.before && r.after.status !== r.before.status ? ` · ${DS[r.before.status]} → ${DS[r.after.status]}` : ""}${r.note ? ` <span class="muted">· ${esc(r.note)}</span>` : ""}</td></tr>`).join("")}</table>` : `<div class="empty">No changes yet.</div>`);
}

// ------------------------------------------------------------------ events
document.addEventListener("click", async (ev) => {
  const t = ev.target.closest("[data-dx]"); if (!t) return;
  const a = t.dataset.dx;
  if (a === "noop") { ev.stopPropagation(); return; }
  switch (a) {
    case "st": DX.f.status = t.dataset.v; DX.page = 0; $$('[data-dx="st"]').forEach((b) => b.classList.toggle("on", b === t)); dxRefresh(); break;
    case "pg": DX.page = Math.max(0, DX.page + +t.dataset.d); dxRefresh(); break;
    case "new": dxOpen(dxBlank(DX.f.q), true); break;
    case "cand": { const x = (DX.cand || [])[+t.dataset.i]; if (!x) break;
      dxOpen({ ...dxBlank(x.from), kind: x.kind, term: x.from, to: x.to, status: "review", source: "search reformulations",
        note: `${x.sessions} sessions searched “${x.from}”, found nothing, then searched “${x.to}” and clicked (last ${x.last}).` }, true); break; }
    case "edit": { if (ev.target.closest('input[type="checkbox"]')) break; const e = DX.data.entries.find((x) => x.id === t.dataset.id); if (e) dxOpen(e, false); break; }
    case "kind": dxRead(); DX.ed.e.kind = t.dataset.v; dxDraw(); dxLintSoon(0); break;
    case "est": dxRead(); DX.ed.e.status = t.dataset.v; dxDraw(); dxLintSoon(0); break;
    case "usesug": dxRead(); DX.ed.e.status = t.dataset.v; dxDraw(); dxLintSoon(0); break;
    case "save": dxSave(); break;
    case "measure": dxMeasure(); break;
    case "del": if (t.dataset.sure !== "1") { t.dataset.sure = "1"; t.textContent = "Confirm delete"; break; }
      try { await adm("/api/admin/dictionary/delete", { method: "POST", body: { id: DX.ed.e.id, who: who(), base_version: DX.data.version } }); toast("Deleted (History can restore it)"); closeDrawer(); dxRefresh(); } catch (err) { toast(err.message, true); }
      break;
    case "restore": try { const r = await adm("/api/admin/dictionary/restore", { method: "POST", body: { id: DX.ed.e.id, at: t.dataset.at, who: who() } }); toast("Restored"); dxRefresh(); dxOpen(r.entry, false); } catch (err) { toast(err.message, true); } break;
    case "bulk": {
      try {
        const r = await adm("/api/admin/dictionary/status", { method: "POST", body: { ids: [...DX.sel], status: t.dataset.v, who: who(), base_version: DX.data.version } });
        toast(`${r.changed} changed${r.refused.length ? ` · ${r.refused.length} not changed (${r.refused.slice(0, 2).map((x) => `${x.term}: ${x.why}`).join("; ")}${r.refused.length > 2 ? "…" : ""})` : ""}`, !!r.refused.length);
        DX.sel.clear(); dxRefresh();
      } catch (err) { toast(err.status === 409 ? "The dictionary changed since this page loaded; reloaded" : err.message, true); dxRefresh(); }
      break; }
    case "clearsel": DX.sel.clear(); $$("[data-dxsel]").forEach((c) => (c.checked = false)); dxSelBar(); break;
    case "import": dxImport(); break;
    case "doimport": dxDoImport(); break;
    case "builtin": dxBuiltin(); break;
    case "langs": dxLangs(); break;
    case "langtry": dxLangTry(); break;
    case "langshow": dxLangShow(t.dataset.l); break;
    case "log": dxLog(); break;
  }
});
document.addEventListener("change", (ev) => {
  const t = ev.target;
  if (t.dataset && t.dataset.dxsel) { t.checked ? DX.sel.add(t.dataset.dxsel) : DX.sel.delete(t.dataset.dxsel); dxSelBar(); return; }
  if (t.hasAttribute && t.hasAttribute("data-dxall")) { $$("[data-dxsel]").forEach((c) => { c.checked = t.checked; t.checked ? DX.sel.add(c.dataset.dxsel) : DX.sel.delete(c.dataset.dxsel); }); dxSelBar(); return; }
  if (t.dataset && t.dataset.dxf && t.tagName === "SELECT") { DX.f[t.dataset.dxf] = t.value; DX.page = 0; dxRefresh(); }
});
let dxQT;
document.addEventListener("input", (ev) => {
  const t = ev.target;
  if (t.dataset && t.dataset.dxf === "q") { clearTimeout(dxQT); dxQT = setTimeout(() => { DX.f.q = t.value; DX.page = 0; dxRefresh(); }, 250); }
  if (t.dataset && t.dataset.dxe) dxLintSoon();
});
document.addEventListener("submit", (ev) => {
  const f = ev.target.closest("[data-dxform]"); if (!f) return;
  ev.preventDefault(); dxRead(); dxTry();
});
// the count of entries waiting for review, in the side menu
adm("/api/admin/dictionary?size=10").then((d) => { if ($("#nDict")) $("#nDict").textContent = d.counts.status.review || ""; }).catch(() => {});
