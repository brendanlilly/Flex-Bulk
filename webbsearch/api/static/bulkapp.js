/* Flex Bulk: the page around the bulk-order matcher (bulk.js).
   Pages: #new (the order being worked: who it's for, then the lines), #order/<id> (a saved one, reopened),
   #orders (saved orders). The header holds the pickup branch, the price level and the name of whoever is preparing
   the order. No build step, no framework. */
"use strict";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (v) => v == null ? "" : "$" + Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const G = window.Glyphs;
const I = {
  x: '<path d="M6 6l12 12M18 6L6 18"/>', check: '<path d="M5 12l5 5 9-10"/>', search: '<circle cx="11" cy="11" r="7"/><path d="M20 20l-4-4"/>',
  save: '<path d="M5 3h11l3 3v15H5z"/><path d="M8 3v5h7V3M8 21v-7h8v7"/>', down: '<path d="M12 4v11M7 10l5 5 5-5M5 20h14"/>',
  doc: '<path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4M9 12h6M9 16h6"/>', list: '<path d="M8 6h13M8 12h13M8 18h13"/><circle cx="4" cy="6" r="1"/><circle cx="4" cy="12" r="1"/><circle cx="4" cy="18" r="1"/>',
  plus: '<path d="M12 5v14M5 12h14"/>', cart: '<path d="M5 3h11l3 3v15H5z"/><path d="M8 3v5h7V3M8 21v-7h8v7"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>', trash: '<path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/>',
};
const ic = (n, cls = "") => `<svg class="${cls}" viewBox="0 0 24 24">${I[n] || ""}</svg>`;
const S = { setup: null, order: blankOrder(), dirty: false, saving: false };
let seq = 0;
const store = { get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }, set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private window */ } } };
const SID = (() => { const mk = () => Math.random().toString(36).slice(2, 12); try { let v = sessionStorage.getItem("fb_sid"); if (!v) { v = mk(); sessionStorage.setItem("fb_sid", v); } return v; } catch (e) { return mk(); } })();

function blankOrder() { return { id: null, status: "draft", customer_no: "", customer_name: "", job: "", po: "", contact: "", notes: "", updated: null }; }
function qs(o) { const p = new URLSearchParams(); for (const [k, v] of Object.entries(o || {})) if (v !== undefined && v !== null && v !== "") p.append(k, v); return p.toString(); }
async function api(path, params) { const r = await fetch(path + (params ? "?" + qs(params) : "")); if (!r.ok) throw new Error(r.status); return r.json(); }
async function post(path, body, method = "POST") {
  const r = await fetch(path, { method, headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
  const j = await r.json().catch(() => ({})); if (!r.ok) throw new Error(j.detail || j.error || r.status); return j;
}
let toastT;
function toast(m) { const t = $("#toast"); t.textContent = m; t.classList.add("on"); clearTimeout(toastT); toastT = setTimeout(() => t.classList.remove("on"), 2200); }

// who the lines are matched for: the customer (their own past picks count more), the member of staff, the price level
function custKey() { return (S.order.customer_no || S.order.customer_name || "").trim(); }
function staffName() { return store.get("fb_staff") || ""; }
function who() { return { customer: custKey(), staff: staffName(), level: $("#level").value || "" }; }
function branchName() { const o = $("#branch").selectedOptions[0]; return o ? o.dataset.name || o.textContent : ""; }
function track(kind, sku, extra = {}) {
  try { fetch("/api/track", { method: "POST", headers: { "content-type": "application/json" }, keepalive: true,
    body: JSON.stringify({ kind, sku, sid: SID, meta: extra.meta || {}, customer: custKey(), staff: staffName() }) }).catch(() => {}); } catch (e) { /* never blocks the order */ }
}
// items from FlexSearch's Webb cross-reference (only when FLEXBULK_XREF=1)
const BAND_CLASS = { Exact: "b-exact", Equivalent: "b-eq", Compatible: "b-comp", Upgrade: "b-up" };
const bandPill = (band, score) => `<span class="xband ${BAND_CLASS[band] || ""}">${esc(band)}${score != null ? " · " + score : ""}</span>`;
const webbLink = (c, label) => `<span>${label}</span>`;
function markDirty() { S.dirty = true; paintStatus(); }

// ------------------------------------------------------------------ header
async function boot() {
  try { S.setup = await api("/api/setup"); } catch (e) { $("#main").innerHTML = `<div class="fb-empty">The server isn't answering. Is it still starting? Reload in a moment.</div>`; return; }
  const st = S.setup, bsel = $("#branch"), saved = store.get("fb_branch");
  bsel.innerHTML = st.branches.map((b) => `<option value="${esc(b.branch_id)}" data-name="${esc(b.name)}">${esc(b.name)}${b.state ? ", " + esc(b.state) : ""}</option>`).join("");
  bsel.value = st.branches.some((b) => b.branch_id === saved) ? saved : st.default_branch;
  const lsel = $("#level");
  lsel.innerHTML = `<option value="">List</option>` + st.levels.map((l) => `<option>${esc(l)}</option>`).join("");
  lsel.value = st.levels.includes(store.get("fb_level")) ? store.get("fb_level") : "";
  paintHeader();
  const ld = st.loaded || {};
  $("#foot").innerHTML = `Flex Bulk v${esc(st.version)} · ${st.items.toLocaleString()} items${ld.files && ld.files.items ? ` from ${esc(ld.files.items)}` : ""}${ld.at ? `, loaded ${esc(ld.at)}` : ""}${st.stock ? "" : " · no stock file loaded"}${(ld.warnings || []).length ? ` · <span class="link" data-act="loadnotes">${ld.warnings.length} note${ld.warnings.length > 1 ? "s" : ""} from loading</span>` : ""}`;
  window.addEventListener("hashchange", route);
  route();
}
function paintHeader() {
  $("#branchLbl").textContent = branchName().split(",")[0] || "…";
  $("#levelLbl").textContent = $("#level").value || "List";
  const n = staffName();
  $("#staffLbl").textContent = n || "Add your name";
  $("#avatar").textContent = n ? n.split(/\s+/).map((w) => w[0]).join("").slice(0, 2).toUpperCase() : "?";
}
$("#branch").addEventListener("change", () => { store.set("fb_branch", $("#branch").value); paintHeader(); if (S.page === "new") { markDirty(); bulkContextChanged(); } });
$("#level").addEventListener("change", () => { store.set("fb_level", $("#level").value); paintHeader(); if (S.page === "new") { markDirty(); bulkContextChanged(); } });
$("#staffBtn").addEventListener("click", () => askStaff());
function askStaff(then) {
  $("#modal").innerHTML = `<div class="box fb-small"><h3>Your name</h3><p class="muted">It goes on the orders you save, and the dashboard counts your picks under it. Kept on this computer.</p>
    <input class="fb-in" id="staffIn" value="${esc(staffName())}" maxlength="60" placeholder="First and last name" autocomplete="name">
    <div class="qofoot"><span style="flex:1"></span><button class="btn" data-act="modalclose">Cancel</button><button class="btn primary" id="staffOk">Save</button></div></div>`;
  $("#modal").classList.add("on"); $("#staffIn").focus();
  const ok = () => { store.set("fb_staff", $("#staffIn").value.trim()); paintHeader(); $("#modal").classList.remove("on"); if (then) then(); };
  $("#staffOk").onclick = ok; $("#staffIn").onkeydown = (e) => { if (e.key === "Enter") ok(); };
}

// ------------------------------------------------------------------ pages
async function route() {
  const h = location.hash.replace(/^#/, "") || "new", my = ++seq;
  $$(".fb-nav a").forEach((a) => a.classList.toggle("on", h.startsWith(a.dataset.nav) || (a.dataset.nav === "new" && h.startsWith("order/"))));
  if (h === "orders") { S.page = "orders"; return pageOrders(my); }
  if (h.startsWith("order/")) {
    const id = decodeURIComponent(h.slice(6));
    if (S.order.id !== id) {
      if (S.dirty && !confirm("This order has unsaved changes. Open the other one anyway?")) { history.replaceState(null, "", S.order.id ? "#order/" + encodeURIComponent(S.order.id) : "#new"); return; }
      try { loadOrder(await api("/api/orders/" + encodeURIComponent(id))); } catch (e) { toast("That order isn't here."); location.hash = "#orders"; return; }
    }
  } else if (h === "new" && S.order.id) {
    if (S.dirty && !confirm("This order has unsaved changes. Start a new one anyway?")) { history.replaceState(null, "", "#order/" + encodeURIComponent(S.order.id)); return; }
    newOrder(false);
  }
  S.page = "new";
  pageOrder(my);
}
function newOrder(go = true) {
  S.order = blankOrder(); S.dirty = false;
  if (typeof bkReset === "function") bkReset();
  if (go) { if (location.hash !== "#new") location.hash = "#new"; else route(); }
}
function loadOrder(o) {
  S.order = { ...blankOrder(), ...o }; S.dirty = false;
  if (o.branch && [...$("#branch").options].some((x) => x.value === o.branch)) $("#branch").value = o.branch;
  $("#level").value = S.setup.levels.includes(o.level) ? o.level : "";
  paintHeader();
  bkLoad(o.rows || [], o.docs || []);
}

function pageOrder(my) {
  const o = S.order;
  $("#main").innerHTML = `<section class="fb-order">
    <div class="fb-ohead"><div class="fb-otitle"><h1>${o.id ? `Order <span class="mono">${esc(o.id)}</span>` : "New order"}</h1><span id="fbStatus" class="muted"></span></div>
      <div class="fb-oact"><button class="btn" data-act="neworder">${ic("plus")}New order</button>
        <button class="btn" data-act="saveorder">${ic("save")}Save</button>
        <div class="fb-menu"><button class="btn" data-act="exportmenu" aria-haspopup="true">${ic("down")}Export</button>
          <div class="fb-pop" id="exportPop" hidden><button data-act="export" data-f="xlsx">Excel workbook<small>header, every line, an order-entry sheet</small></button>
            <button data-act="export" data-f="csv">CSV<small>header and every line</small></button>
            <button data-act="export" data-f="entry">Order entry file<small>confirmed lines: item # and quantity</small></button></div></div></div></div>
    <div class="fb-cust">
      <label class="fl w1">Customer #<input class="fb-in" data-o="customer_no" value="${esc(o.customer_no)}" maxlength="40" list="custList" autocomplete="off" placeholder="Account number"></label>
      <label class="fl w2">Customer name<input class="fb-in" data-o="customer_name" value="${esc(o.customer_name)}" maxlength="120" autocomplete="off" placeholder="Company"></label>
      <label class="fl w2">Job<input class="fb-in" data-o="job" value="${esc(o.job)}" maxlength="120" placeholder="Job name or address"></label>
      <label class="fl w1">PO<input class="fb-in" data-o="po" value="${esc(o.po)}" maxlength="60"></label>
      <label class="fl w2">Contact<input class="fb-in" data-o="contact" value="${esc(o.contact)}" maxlength="120" placeholder="Name, phone or email"></label>
      <label class="fl wfull">Notes<input class="fb-in" data-o="notes" value="${esc(o.notes)}" maxlength="2000" placeholder="Delivery, timing, anything the counter should know"></label>
      <datalist id="custList"></datalist>
      <div class="fb-custhint muted" id="custHint"></div>
    </div>
    <div id="bulkHost"></div></section>`;
  paintStatus();
  runBulk(my);
}
function paintStatus() {
  const el = $("#fbStatus"); if (!el) return;
  const o = S.order;
  el.innerHTML = S.saving ? "Saving…" : o.id ? `${S.dirty ? `<b class="fb-dirty">Unsaved changes</b> · ` : ""}saved ${esc(when(o.updated))}${o.staff ? ` by ${esc(o.staff)}` : ""}` : S.dirty ? `<b class="fb-dirty">Not saved yet</b>` : "";
}
function when(t) { if (!t) return ""; const d = new Date(t * 1000), now = new Date(); return d.toDateString() === now.toDateString() ? d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : d.toLocaleDateString([], { month: "short", day: "numeric", year: d.getFullYear() === now.getFullYear() ? undefined : "numeric" }); }

// header facts a document stated (an email's job name, PO...) fill empty boxes; staff can change them
function fillFromDocs(meta) {
  // an email's original sender ("Jane Doe (acmeplumbing.com)") is the customer's contact; whoever forwarded it is
  // usually Webb's own rep, so it isn't used
  const person = meta.customer && /\([^)]*\.[a-z]{2,}\)\s*$/i.test(meta.customer);
  const map = { customer: person ? "contact" : "customer_name", job: "job", project: "job", po: "po", fulfil: "notes" }; let n = 0;
  for (const [k, f] of Object.entries(map)) if (meta[k] && !S.order[f]) { S.order[f] = String(meta[k]).slice(0, 120); const el = $(`[data-o="${f}"]`); if (el) el.value = S.order[f]; n++; }
  if (n) { markDirty(); toast(`Filled ${n} order detail${n > 1 ? "s" : ""} from the document`); }
}

async function saveOrder(quiet) {
  if (!staffName()) { askStaff(() => saveOrder(quiet)); return null; }
  if (!bkRowsForSave().length && !custKey()) { toast("Add a customer or some lines first."); return null; }
  S.saving = true; paintStatus();
  try {
    const o = S.order;
    const r = await post("/api/orders", { id: o.id, status: o.status, customer_no: o.customer_no, customer_name: o.customer_name, job: o.job, po: o.po,
      contact: o.contact, notes: o.notes, branch: $("#branch").value, branch_name: branchName(), level: $("#level").value, staff: staffName(),
      rows: bkRowsForSave(), docs: bkDocsForSave() });
    const first = !o.id;
    Object.assign(S.order, r); S.dirty = false;
    if (first) history.replaceState(null, "", "#order/" + encodeURIComponent(r.id));
    const h = $(".fb-otitle h1"); if (h) h.innerHTML = `Order <span class="mono">${esc(r.id)}</span>`;
    if (!quiet) toast(`Saved ${r.id}: ${r.n_lines} line${r.n_lines === 1 ? "" : "s"}, ${r.n_confirmed} confirmed`);
    return r;
  } catch (e) { toast("Couldn't save: " + e.message); return null; }
  finally { S.saving = false; paintStatus(); }
}
async function exportOrder(fmt) {
  const r = S.dirty || !S.order.id ? await saveOrder(true) : S.order;
  if (!r || !r.id) return;
  const a = document.createElement("a"); a.href = `/api/orders/${encodeURIComponent(r.id)}/export?fmt=${fmt}`; a.click();
}

async function pageOrders(my) {
  $("#main").innerHTML = `<section class="fb-orders"><div class="fb-ohead"><h1>Orders</h1><div class="fb-oact"><input class="fb-in" id="ordQ" placeholder="Customer, job, PO, order # or who prepared it" style="width:340px"><button class="btn primary" data-act="neworder">${ic("plus")}New order</button></div></div><div id="ordList" class="muted">Loading…</div></section>`;
  const load = async () => {
    let L; try { L = await api("/api/orders", { q: $("#ordQ") ? $("#ordQ").value : "" }); } catch (e) { L = null; }
    if (my !== seq) return;
    const el = $("#ordList"); if (!el) return;
    if (!L) { el.textContent = "Couldn't load the orders."; return; }
    el.innerHTML = L.length ? `<div class="bkscroll"><table class="bkt fb-ot"><thead><tr><th>Order</th><th>Customer</th><th>Job</th><th>PO</th><th class="n">Lines</th><th class="n">Confirmed</th><th class="n">Total</th><th>Branch</th><th>Prepared by</th><th>Saved</th><th></th></tr></thead><tbody>
      ${L.map((o) => `<tr data-act="openorder" data-id="${esc(o.id)}"><td class="mono"><b>${esc(o.id)}</b></td><td>${esc([o.customer_no, o.customer_name].filter(Boolean).join(" · ")) || `<span class="muted">none</span>`}</td><td>${esc(o.job)}</td><td>${esc(o.po)}</td>
        <td class="n">${o.n_lines}</td><td class="n">${o.n_confirmed}</td><td class="n">${o.total ? money(o.total) : ""}</td><td>${esc((o.branch_name || "").split(",")[0])}</td><td>${esc(o.staff)}</td><td class="muted">${esc(when(o.updated))}</td>
        <td class="fb-ex"><a class="linkbtn" href="/api/orders/${encodeURIComponent(o.id)}/export?fmt=xlsx" data-stop="1">Excel</a> <button class="linkbtn" data-act="delorder" data-id="${esc(o.id)}">Delete</button></td></tr>`).join("")}</tbody></table></div>`
      : `<div class="fb-empty">${$("#ordQ").value ? "No saved orders match." : "No saved orders yet. Start one under New order and Save it."}</div>`;
  };
  let t; $("#ordQ").addEventListener("input", () => { clearTimeout(t); t = setTimeout(load, 250); });
  load();
}

// ------------------------------------------------------------------ one item: details, and finding an item by hand
async function openSku(sku) {
  let d; try { d = await api("/api/product/" + encodeURIComponent(sku), { branch: $("#branch").value, level: $("#level").value }); } catch (e) { toast("Not on the item list."); return; }
  const attrs = Object.entries(d.attrs || {});
  $("#modal").innerHTML = `<div class="box fb-pdp"><div class="fb-mh"><div><h3>${esc(d.title)}</h3><div class="muted">Item ${esc(d.sku)}${d.brand ? " · " + esc(d.brand) : ""}${d.mfr_part ? " · " + esc(d.mfr_part) : ""} · ${esc(d.uom)}${d.status !== "A" ? ` · <b>${d.status === "D" ? "Discontinued" : "Special order"}</b>` : ""}</div></div><button class="btn sm" data-act="modalclose">Close</button></div>
    <div class="fb-pdpg"><div><div class="fb-k">Category</div><div>${esc([d.cat1, d.cat2, d.cat3].filter((x, i, a) => x && a.indexOf(x) === i).join(" › "))}</div>
      ${attrs.length ? `<div class="fb-k">Read from the item</div><div>${attrs.map(([k, v]) => `<span class="tag">${esc(k)}: ${esc(v)}</span>`).join(" ")}</div>` : ""}
      <div class="fb-k">Prices</div><table class="fb-mini"><tr><td>List</td><td class="n">${d.list_price ? money(d.list_price) : "none"}</td></tr>${Object.entries(d.levels || {}).map(([k, v]) => `<tr><td>${esc(k)}</td><td class="n">${v != null ? money(v) : "list"}</td></tr>`).join("")}</table></div>
      <div><div class="fb-k">On hand, nearest first (pickup ${esc(d.branch.name.split(",")[0])})</div><table class="fb-mini">${d.branches.map((b) => `<tr class="${b.home ? "fb-home" : ""}"><td>${esc(b.name.split(",")[0])}</td><td class="muted n">${b.home ? "pickup" : b.miles + " mi"}</td><td class="n"><b>${b.qty}</b></td></tr>`).join("")}</table></div></div></div>`;
  $("#modal").classList.add("on");
}
async function findItem(rowId, q) {
  const r = bkRow(rowId); if (!r) return;
  $("#modal").innerHTML = `<div class="box"><div class="fb-mh"><div><h3>Find an item for line ${esc(r.ln.n || "")}</h3><div class="muted">Asked for: <b>${esc(r.ln.text)}</b>${r.ln.desc && r.ln.desc !== r.ln.text ? " · " + esc(r.ln.desc) : ""} · qty ${r.ln.qty}${r.ln.uom ? " " + esc(r.ln.uom) : ""}</div></div><button class="btn sm" data-act="modalclose">Close</button></div>
    <form id="findF" class="fb-find"><input class="fb-in" id="findQ" value="${esc(q || r.ln.desc || r.ln.text)}" placeholder="Item #, part #, or words"><button class="btn primary">${ic("search")}Search</button></form>
    <div id="findR" class="muted">Searching…</div></div>`;
  $("#modal").classList.add("on");
  const go = async () => {
    const el = $("#findR"); el.textContent = "Searching…";
    let d; try { d = await api("/api/lookup", { q: $("#findQ").value, branch: $("#branch").value, level: $("#level").value, qty: r.ln.qty, uom: r.ln.uom || "" }); } catch (e) { el.textContent = "Search failed."; return; }
    S.found = d.items;
    el.innerHTML = d.items.length ? `${d.notice ? `<div class="qnote">${esc(d.notice)}</div>` : ""}<div class="bkcards fb-found">${d.items.map((c, i) => `<div class="bkcard"><div class="bkcg">${G.forItem(c, 56)}</div>
        <div class="bkct"><span class="link" data-act="open-sku" data-sku="${esc(c.sku)}">${esc(c.title)}</span></div><div class="muted" style="font-size:11.5px">Item ${esc(c.sku)}${c.mfr_part ? " · " + esc(c.mfr_part) : ""}${c.conv ? " · " + esc(c.conv) : ""}</div>
        <div class="bkcp">${c.price != null ? `<b>${money(c.price)}</b>` : `<span class="muted">no price</span>`}</div>${fulfilHTML(c.fulfil, true)}
        <button class="btn sm primary" data-act="usefound" data-id="${r.id}" data-i="${i}">Use for this line</button></div>`).join("")}</div>` : `<div class="fb-empty">Nothing on the item list matches. Try fewer words, or a part number.</div>`;
  };
  $("#findF").addEventListener("submit", (e) => { e.preventDefault(); go(); });
  $("#findQ").focus(); $("#findQ").select();
  go();
}

// ------------------------------------------------------------------ events
document.addEventListener("click", async (e) => {
  if (e.target.closest("[data-stop]")) return;
  const t = e.target.closest("[data-act]");
  if (!t) { if (!e.target.closest(".fb-menu")) $$(".fb-pop").forEach((p) => (p.hidden = true)); if (e.target === $("#modal")) $("#modal").classList.remove("on"); return; }
  switch (t.dataset.act) {
    case "modalclose": $("#modal").classList.remove("on"); break;
    case "neworder": if (!S.dirty || confirm("This order has unsaved changes. Start a new one anyway?")) { S.dirty = false; newOrder(); } break;
    case "saveorder": saveOrder(); break;
    case "exportmenu": { const p = $("#exportPop"); p.hidden = !p.hidden; break; }
    case "export": $("#exportPop").hidden = true; exportOrder(t.dataset.f); break;
    case "openorder": location.hash = "#order/" + encodeURIComponent(t.dataset.id); break;
    case "delorder": e.stopPropagation(); if (confirm(`Delete order ${t.dataset.id}? This can't be undone.`)) { try { await fetch("/api/orders/" + encodeURIComponent(t.dataset.id), { method: "DELETE" }); if (S.order.id === t.dataset.id) { S.order = blankOrder(); bkReset(); } toast("Deleted"); route(); } catch (x) { toast("Couldn't delete"); } } break;
    case "open-sku": openSku(t.dataset.sku); break;
    case "loadnotes": $("#modal").innerHTML = `<div class="box fb-small"><h3>From loading the item list</h3><ul>${(S.setup.loaded.warnings || []).map((w) => `<li>${esc(w)}</li>`).join("")}</ul><div class="qofoot"><span style="flex:1"></span><button class="btn" data-act="modalclose">Close</button></div></div>`; $("#modal").classList.add("on"); break;
  }
});
document.addEventListener("input", (e) => {
  const f = e.target.dataset && e.target.dataset.o; if (!f) return;
  const before = custKey();
  S.order[f] = e.target.value; markDirty();
  if (f === "customer_no" || f === "customer_name") { custSuggest(e.target.value); if (custKey() !== before) custChanged(); }
});
document.addEventListener("change", (e) => {
  const f = e.target.dataset && e.target.dataset.o;
  if (f === "customer_no") { const m = (S.custs || []).find((c) => c.customer_no === e.target.value); if (m && !S.order.customer_name) { S.order.customer_name = m.customer_name; const n = $('[data-o="customer_name"]'); if (n) n.value = m.customer_name; } }
});
let custT, ctxT;
function custSuggest(q) {
  clearTimeout(custT); if (q.length < 2) return;
  custT = setTimeout(async () => { try { S.custs = await api("/api/customers", { q }); const dl = $("#custList"); if (dl) dl.innerHTML = S.custs.map((c) => `<option value="${esc(c.customer_no)}">${esc(c.customer_name)} · ${c.orders} order${c.orders > 1 ? "s" : ""}</option>`).join(""); } catch (x) { /* suggestions only */ } }, 200);
}
// a different customer: their own past picks change the ranking, so lines already matched are matched again
function custChanged() { clearTimeout(ctxT); ctxT = setTimeout(() => bulkContextChanged(), 700); }
document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("#modal").classList.remove("on"); if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s" && S.page === "new") { e.preventDefault(); saveOrder(); } });
window.addEventListener("beforeunload", (e) => { if (S.dirty) { e.preventDefault(); e.returnValue = ""; } });
document.addEventListener("DOMContentLoaded", boot);
