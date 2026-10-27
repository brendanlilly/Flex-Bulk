/* Bulk order matching (FlexSearch v1.2-v2.46, Flex Bulk 1.0): multi-item search, quick entry and upload (BOMs,
   emails, Word, photos and scans of handwritten notes), one matching table.
   Every line is matched on the server (/api/bom/match): exact item # / part # / UPC, merchandising crosses, else
   recommendations ranked by fit, Webb's brand order, what staff picked before (this customer's picks most) and
   branch stock, with the quantity against on-hand, transfer plans and on-the-shelf substitutes.
   Confirmation: exact matches arrive confirmed; any recommendation or substitute needs a click on its line; any
   line can be matched by hand (Find). The order is saved and exported (bulkapp.js), not sent to a cart. */
"use strict";

S.bk = { mode: "bom", view: "table", filter: "all", limit: 200, rows: [], seq: 0, ctxKey: "", ctx: null, parse: null, file: null, multi: "", paste: "", docs: [] };
let bkId = 0;
const BK_SAMPLE = `Needham Library boiler replacement · BOM rev 2 · 9/20/2026
Line,Qty,U/M,Manufacturer,Mfr Part Number,Description
1,2,EA,Honeywell,V8168A1069,Gas valve 3/4 in
2,6,EA,White-Rodgers,50M56U-843,Universal ignition kit
3,40,EA,,,3/4 cxc 90 elbow
4,1,EA,Taco,007-F5,Circulator
5,8,EA,,,45/5 440v run capacitor
6,12,EA,,,3/4 ball valve sweat
7,2,EA,Acme,ZZ-4471,Expansion tank 4.4 gal
8,200,FT,,,1/2 pex b tubing
9,4,EA,Norton,271W,Hot surface igniter
10,1,EA,,,Honeywell T6 Pro thermostat`;
const BK_MULTI = "3/4 ball valve\n45/5 cap\n1/2 pex b tubing\nhoneywell v8168a1069\n3/4 cxc 90 elbow";
const bkKey = () => `${custKey()}|${$("#branch").value}|${$("#level").value}`;

// ------------------------------------------------------------------ rows
function bkItem(r) { if (!r.m) return null; const L = r.ch.t === "sub" ? r.m.substitutes : r.m.options; return L[r.ch.i] || null; }
function bkState(r) {
  if (r.busy) return "busy";
  if (!r.m) return "new";
  if (r.m.status === "skip") return "skip";
  if (r.excluded) return "out";
  if (!bkItem(r)) return "none";
  return r.confirmed ? "ok" : "confirm";
}
function bkNewRow(ln) { return { id: ++bkId, ln: { ...ln }, m: null, ch: { t: "opt", i: 0 }, confirmed: false, excluded: false, busy: true }; }
// every match request carries the row's sequence number and the quantity it asked about: a slower, older answer
// (typed text still matching when the quantity changed) never overwrites a newer one
function bkApply(r, m, sent) {
  if (sent && (sent.seq !== r.mseq || sent.qty !== r.ln.qty || sent.text !== r.ln.text)) return false;
  r.m = m; r.mq = sent ? sent.qty : r.ln.qty; r.busy = false; r.ch = { t: "opt", i: 0 }; r.confirmed = !!m.confirmed; r.excluded = false;
  return true;
}
function bkSend(r) { r.mseq = (r.mseq || 0) + 1; return { seq: r.mseq, qty: r.ln.qty, text: r.ln.text }; }
async function bkMatch(rows, label) {
  // large takeoffs: three requests in flight, 8 lines each; identical lines are matched once on the server; the
  // table repaints at most every 400 ms while matching
  const my = S.bk.seq, todo = rows.slice(), t0 = performance.now();
  rows.forEach((r) => { r.busy = true; }); bkPaint();
  let done = 0, failed = false, last = 0;
  const prog = () => { const st = $("#bkProg"); if (st) st.innerHTML = `${label || "Matching"} ${done.toLocaleString()} of ${rows.length.toLocaleString()}… <span class="qoprog"><i style="width:${Math.round(100 * done / rows.length)}%"></i></span>`; };
  const paint = () => { const now = performance.now(); if (rows.length <= 80 || now - last > 400) { last = now; bkPaint(); } };
  async function worker() {
    while (todo.length && !failed) {
      const chunk = todo.splice(0, 8);
      const sent = chunk.map(bkSend);
      let res;
      try {
        const rq = await fetch("/api/bom/match", { method: "POST", headers: { "content-type": "application/json" },
          body: JSON.stringify({ lines: chunk.map((r) => ({ ...r.ln, crop: undefined })), ...who(), branch: $("#branch").value, sid: SID }) });
        if (!rq.ok) throw new Error(rq.status); res = await rq.json();
      } catch (e) { failed = true; chunk.forEach((r) => { r.busy = false; }); return; }
      if (my !== S.bk.seq) { failed = true; return; }
      // the server drops empty lines; line up by position among the non-empty ones
      const keep = chunk.filter((r) => (r.ln.text || "").trim() || (r.ln.desc || "").trim());
      keep.forEach((r, k) => res.lines[k] && bkApply(r, res.lines[k], sent[chunk.indexOf(r)]));
      chunk.forEach((r) => { r.busy = false; });
      done += chunk.length; prog(); paint();
    }
  }
  await Promise.all([worker(), worker(), worker()]);
  if (my !== S.bk.seq) { rows.forEach((r) => { r.busy = false; }); return; }
  rows.forEach((r) => { r.busy = false; });
  if (failed) toast("Matching failed for some lines. Try again.");
  bkPaint();
  const st = $("#bkProg"); if (st) st.textContent = rows.length > 80 ? `Matched ${rows.length.toLocaleString()} lines in ${((performance.now() - t0) / 1000).toFixed(1)} s.` : "";
}
// what someone chose for a line: logged so Bulk order learns crosses (merch/bulkprefs.py)
function bkLogChoice(r, how) {
  try {
    const it = bkItem(r); if (!it || !r.m || typeof track !== "function") return;
    const rank = r.ch.t === "opt" ? r.ch.i : -1;
    track("bulk_choice", it.sku, { meta: { key: r.m.key || "", req_brand: r.m.req_brand || "", brand: it.brand || "", family: it.family || "",
      rank, how, status: r.m.status || "", branch: $("#branch").value } });
  } catch (e) { /* logging never blocks the order */ }
}
async function bkParseAndAdd(body, replace) {
  $("#bkProg").textContent = "Reading…";
  let r;
  try {
    const rq = await fetch("/api/bom/parse", { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ ...body, ...who(), sid: SID }) });
    r = await rq.json(); if (!rq.ok) throw new Error(r.error || r.detail || rq.status);
  } catch (e) { $("#bkProg").textContent = String(e.message || e).slice(0, 160); return; }
  S.bk.parse = r;
  if (replace) S.bk.docs = [];
  if (r.sources) S.bk.docs.push(r);
  if (r.meta) fillFromDocs(r.meta);
  markDirty();
  if (!r.lines.length) {
    $("#bkProg").textContent = (r.notes || []).concat(r.note || []).join(" ") || "No item lines found."; bkPaintInput();
    if (replace) { S.bk.rows = []; bkPaint(); }
    return;
  }
  S.bk.seq++;
  const rows = r.lines.map(bkNewRow);
  S.bk.rows = replace ? rows : S.bk.rows.filter((x) => x.m || (x.ln.text || "").trim()).concat(rows);
  bkPaintInput();
  await bkMatch(rows, `Matching ${r.truncated ? "(first 2,000 lines) " : ""}`);
}

// ------------------------------------------------------------------ page
async function runBulk(my) {
  const host = $("#bulkHost"); if (!host) return;
  host.innerHTML = `<div class="bkpage fb-bk" id="bkPage">
    <div class="bkhead"><div><h2>Lines</h2><div class="muted" id="bkCtx">&nbsp;</div></div>
      <div class="seg bkmodes" role="tablist">${[["bom", "Upload or paste"], ["multi", "Multi-search"], ["entry", "Quick entry"]].map(([k, l]) =>
        `<button role="tab" aria-selected="${S.bk.mode === k}" class="${S.bk.mode === k ? "on" : ""}" data-act="bkmode" data-k="${k}">${l}</button>`).join("")}</div></div>
    <div id="bkInput"></div><div class="muted bkprog" id="bkProg" aria-live="polite"></div>
    <div id="bkBody"></div></div>`;
  bkPaintInput();
  bkPaint();
  if (!S.bk.ocrInfo) S.bk.ocrInfo = S.setup.ocr || { engine: null };
  const o = $(".bkocr"); if (o) o.textContent = bkOcrLine();
  // customer, pickup branch or price level changed while away: the same lines, matched again for them
  const key = bkKey();
  if (S.bk.ctxKey && S.bk.ctxKey !== key && S.bk.rows.some((r) => r.m)) { S.bk.seq++; bkMatch(S.bk.rows.filter((r) => (r.ln.text || "").trim()), "Re-matching for this customer, branch and price level:"); }
  S.bk.ctxKey = key;
  await bkLoadCtx(my);
}
async function bkLoadCtx(my) {
  let ctx; try { ctx = await api("/api/bom/context", { branch: $("#branch").value, customer: custKey() }); } catch (e) { ctx = null; }
  if (my != null && my !== seq) return;
  S.bk.ctx = ctx; bkPaintCtx();
}
function bkPaintCtx() {
  const c = S.bk.ctx, el = $("#bkCtx"); if (!el || !c) return;
  el.innerHTML = `Matched against <b>${c.items.toLocaleString()}</b> items · pickup <b>${esc(c.branch.name.split(",")[0])}</b> · <b>${esc($("#level").value || "List")}</b> prices`
    + (custKey() ? ` · ${c.customer_choices ? `this customer's ${c.customer_choices} earlier pick${c.customer_choices > 1 ? "s" : ""} count first` : "no earlier picks for this customer yet"}` : "")
    + (c.stock ? "" : ` · <b>no stock loaded</b>`) + bkTestLine();
}
// a test case: of the lines confirmed so far (the right answers), how many the matcher had as its top pick
function bkTestLine() {
  if (!S.order.test) return "";
  const R = S.bk.rows.filter((r) => bkState(r) === "ok" && !r.excluded && r.m && (r.m.options || []).length);
  const right = R.filter((r) => r.ch.t === "opt" && r.ch.i === 0 && r.m.via !== "Found by hand").length;
  return `<div class="fb-testline"><b>Test case</b> · top pick right on <b>${right} of ${R.length}</b> confirmed line${R.length === 1 ? "" : "s"}${R.length ? ` (${Math.round(100 * right / R.length)}%)` : ""}. Confirm the right item on each line; the order's documents are kept when you save.</div>`;
}
// customer, branch or level changed on this page
function bulkContextChanged() {
  const key = bkKey();
  if (key === S.bk.ctxKey) return;
  S.bk.ctxKey = key;
  // every line with words, including any still matching (their answers were for the old customer, branch or level)
  const rows = S.bk.rows.filter((r) => (r.m || r.busy) && ((r.ln.text || "").trim() || (r.ln.desc || "").trim()));
  if (rows.length) { S.bk.seq++; bkMatch(rows, "Re-matching for this customer, branch and price level:"); }
  bkLoadCtx();
}
// a new order, a saved one opened, an order saved
function bkReset() { S.bk.seq++; S.bk.rows = []; S.bk.parse = null; S.bk.file = null; S.bk.docs = []; S.bk.raw = []; S.bk.filter = "all"; S.bk.limit = 200; S.bk.ctxKey = ""; }
function bkLoad(rows, docs) {
  bkReset();
  S.bk.rows = rows.map((x) => ({ id: ++bkId, ln: { ...(x.ln || { text: x.text, qty: x.qty, uom: x.uom, desc: x.desc, n: x.n }) }, m: x.m || null, mq: (x.ln || {}).qty || x.qty,
    ch: x.ch || { t: "opt", i: 0 }, confirmed: !!x.confirmed, excluded: !!x.excluded, busy: false }));
  S.bk.docs = docs || [];
  S.bk.ctxKey = bkKey();
  if (S.bk.rows.length && !S.bk.rows.some((r) => r.ln.from_ocr) && S.bk.mode === "entry") S.bk.mode = "bom";
}
function bkRowsForSave() {
  return S.bk.rows.filter((r) => r.m || (r.ln.text || "").trim()).map((r) => ({ ln: r.ln, m: r.m, ch: r.ch, confirmed: r.confirmed, excluded: r.excluded,
    state: bkState(r), item: bkState(r) === "out" ? null : bkItem(r) }));
}
function bkDocsForSave() {
  return (S.bk.docs || []).map((d) => ({ filename: d.filename, kind: d.kind, meta: d.meta, notes: d.notes, header: d.header,
    email: d.email ? { from: d.email.from, date: d.email.date, subject: d.email.subject, attachments: d.email.attachments } : null,
    sources: (d.sources || []).map((x) => ({ id: x.id, label: x.label, kind: x.kind, n_items: x.n_items, header: x.header,
      other: (x.other || []).map((o) => ({ n: o.n, raw: o.raw, why: o.why, text: o.text, qty: o.qty, uom: o.uom, desc: o.desc, src: o.src, src_label: o.src_label })) })) }));
}
function bkPaintInput() {
  const el = $("#bkInput"); if (!el) return;
  const m = S.bk.mode;
  if (m === "multi") {
    el.innerHTML = `<div class="bkin"><label class="muted" for="bkMulti">Several products at once: one per line, or separated by semicolons. Add a quantity if you like (“12 x 3/4 ball valve”).</label>
      <textarea id="bkMulti" rows="5" placeholder="${esc(BK_MULTI)}">${esc(S.bk.multi)}</textarea>
      <div class="qobar"><button class="btn primary" data-act="bksearch">Search each</button><button class="linkbtn" data-act="bkmultisample">Try an example</button></div></div>`;
  } else if (m === "entry") {
    el.innerHTML = `<div class="bkin muted">Type a part number, Webb item # or description on each line; it matches as you type. Enter moves to quantity, Enter again adds a line. Paste several lines into a box to add them all.</div>`;
  } else {
    const p = S.bk.parse;
    const roles = [["", "Ignore"], ["qty", "Quantity"], ["part", "Part #"], ["desc", "Description"], ["brand", "Manufacturer"], ["uom", "Unit"], ["size", "Size"], ["material", "Material"], ["notes", "Notes"], ["line", "Line #"]];
    const map = p && p.header ? `<div class="bkmap"><span class="muted">Columns found${p.header_row ? ` (header on row ${p.header_row + 1})` : ""}:</span>
        ${p.header.map((h, k) => { const cur = Object.entries(p.roles || {}).find(([, v]) => v === k); return `<label class="bkcol"><span>${esc(h || "col " + (k + 1))}</span><select data-act="bkrole" data-k="${k}">${roles.map(([v, l]) => `<option value="${v}" ${(cur ? cur[0] : "") === v ? "selected" : ""}>${l}</option>`).join("")}</select></label>`; }).join("")}
        <button class="btn sm" data-act="bkreread">Re-read with these columns</button></div>` : p && p.total ? `<div class="bkmap muted">No header row found: each line was read like a pasted list (quantity before or after the item).</div>` : "";
    el.innerHTML = `<div class="bkin bkdrop" id="bkDrop"><div><b>Upload a bill of materials, an email or a note</b> <span class="muted">Excel, CSV, Word (.docx), email (.eml, with its attachments), PDF, or a photo or scan of a handwritten list. Up to 15 MB and 2,000 lines a file; several files at once.</span></div>
      <div class="muted bkocr">${esc(bkOcrLine())}</div>
      <div class="qobar"><label class="btn primary"><input type="file" id="bkFile" multiple accept="${BK_ACCEPT}" hidden>Choose files…</label>
        <span class="muted">or drop it here, or</span><button class="linkbtn" data-act="bkpastebox">paste rows</button><button class="linkbtn" data-act="bksample">try a sample BOM</button>
        <span class="muted bksamples">· samples: ${[["order-email.eml", "email"], ["material-list.docx", "Word list"], ["handwritten-note.jpg", "handwritten note"]].map(([f, l]) => `<button class="linkbtn" data-act="bksamplefile" data-f="${f}">${l}</button>`).join(" · ")}</span>
        ${p && p.filename ? `<span class="tag">${esc(p.filename)}${p.kind === "pdf" ? (p.pages ? ` · ${p.pages}-page PDF` : " · PDF") : ""}</span>` : ""}
        ${p && p.sheets && p.sheets.length > 1 ? `<label class="muted">Sheet <select data-act="bksheet">${p.sheets.map((s) => `<option ${s === p.sheet ? "selected" : ""}>${esc(s)}</option>`).join("")}</select></label>` : ""}</div>
      <div id="bkPasteBox" ${S.bk.pasteOpen ? "" : "hidden"}><textarea id="bkPaste" rows="6" placeholder="Paste rows from a spreadsheet, takeoff or PO">${esc(S.bk.paste)}</textarea><div class="qobar"><button class="btn primary" data-act="bkreadpaste">Read these rows</button></div></div>
      ${(S.bk.docs || []).length ? "" : map}${p && p.note ? `<div class="qnote">${esc(p.note)}</div>` : ""}</div>${bkDocsHTML(map)}`;
    const fi = $("#bkFile"); if (fi) fi.addEventListener("change", (e) => bkReadFiles([...e.target.files]));
    const dz = $("#bkDrop");
    if (dz) {
      dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("over"); });
      dz.addEventListener("dragleave", () => dz.classList.remove("over"));
      dz.addEventListener("drop", (e) => { e.preventDefault(); dz.classList.remove("over"); bkReadFiles([...e.dataTransfer.files]); });
    }
  }
}
const BK_ACCEPT = ".xlsx,.xlsm,.csv,.tsv,.txt,.pdf,.docx,.eml,.jpg,.jpeg,.png,.heic,.heif,.tif,.tiff,.webp,.gif,.bmp,image/*,text/csv,text/plain,application/pdf,message/rfc822,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,application/vnd.openxmlformats-officedocument.wordprocessingml.document";
const readB64 = (f) => new Promise((ok, bad) => { const rd = new FileReader(); rd.onload = () => ok(String(rd.result)); rd.onerror = bad; rd.readAsDataURL(f); });
async function bkReadFiles(files) {
  files = files.filter(Boolean);
  if (!files.length) return;
  for (const [k, f] of files.entries()) {
    if (f.size > 15e6) { toast(`${f.name} is over 15 MB`); continue; }
    const b64 = await readB64(f);
    S.bk.file = { name: f.name, b64 };
    if (k === 0) S.bk.raw = [];
    S.bk.raw.push({ name: f.name, b64 });        // kept for a test case (bulkapp.js saveOrder)
    $("#bkProg").textContent = `Reading ${f.name}${files.length > 1 ? ` (${k + 1} of ${files.length})` : ""}…`;
    await bkParseAndAdd({ filename: f.name, data_b64: b64 }, k === 0);
  }
}
function bkReadFile(f) { return bkReadFiles([f]); }
function bkOcrLine() {
  const o = S.bk.ocrInfo;
  if (!o) return "";
  return o.engine ? `Photos and scans are read with ${o.label}. Lines read from handwriting always need your confirmation.`
    : "No handwriting reader on this server: photos and scans can't be read here (a Mac, or a Claude API key, can).";
}
const META_LABEL = { customer: "Customer", job: "Job", project: "Project", po: "PO", location: "Location", ship_to: "Ship to", ref: "Ref #", tag: "Tag", date: "Date", fulfil: "Fulfillment", sender: "Sent by" };
function bkMeta() {
  const m = {}; (S.bk.docs || []).forEach((d) => Object.entries(d.meta || {}).forEach(([k, v]) => { if (!(k in m)) m[k] = v; })); return m;
}
function bkMetaHTML(m) {
  m = m || bkMeta();
  const keys = ["customer", "job", "project", "po", "location", "ship_to", "ref", "tag", "date", "fulfil"].filter((k) => m[k]);
  return keys.length ? `<div class="bkmeta">${keys.map((k) => `<span class="bkmk"><small>${META_LABEL[k]}</small>${esc(m[k])}</span>`).join("")}</div>` : "";
}
function bkDocsHTML(map) {
  const docs = S.bk.docs || []; if (!docs.length) return "";
  // one document: its header facts on top; several: each document's own (two jobs must not blend into one header)
  return `<div class="bkdocs">${docs.length === 1 ? bkMetaHTML() : ""}${docs.map((d, di) => `<div class="bkdoc">${docs.length > 1 ? bkMetaHTML(d.meta || {}) : ""}
      <div class="bkdh"><b>${esc(d.filename || "Pasted text")}</b>${d.email ? ` <span class="muted">email from ${esc(d.email.from)}${d.email.date ? " · " + esc(d.email.date.replace(/ [+-]\d{4}.*$/, "")) : ""}${d.email.attachments.length ? ` · ${d.email.attachments.length} attachment${d.email.attachments.length > 1 ? "s" : ""}` : ""}</span>` : ""}</div>
      ${d.sources.map((s) => `<div class="bksrc">
        ${s.ocr && s.ocr.thumbs ? `<div class="bkthumbs">${s.ocr.thumbs.map((t) => `<img src="${t}" alt="${esc(s.label)}">`).join("")}</div>` : ""}
        <div><div>${esc(s.label)}${s.ocr ? ` <span class="tag">${esc(s.ocr.engine ? "read with " + (s.ocr.engine === "vision" ? "Apple handwriting recognition" : s.ocr.engine === "claude" ? "FlexIQ" : "Tesseract") : "not read")}</span>` : ""}</div>
          <div class="muted">${s.n_items} item line${s.n_items === 1 ? "" : "s"}${s.other.length ? ` · <button class="linkbtn" data-act="bkother" data-d="${di}" data-s="${s.id}">${s.other.length} other line${s.other.length === 1 ? "" : "s"} not read as items</button>` : ""}</div>
          <div class="bkothers" id="bkoth-${di}-${s.id}" hidden>${s.other.map((o, oi) => `<div class="bkoth">${o.crop ? `<img src="${o.crop}" alt="">` : ""}<span>${esc(o.raw)}</span><span class="muted">${esc(o.why)}</span><button class="linkbtn" data-act="bkaddother" data-d="${di}" data-s="${s.id}" data-o="${oi}">add as an item</button></div>`).join("")}</div></div></div>`).join("")}
      ${(d.notes || []).map((n) => `<div class="muted bknote">${esc(n)}</div>`).join("")}
      ${docs.length === 1 && d.header ? map : ""}</div>`).join("")}</div>`;
}
function bkSearchEach(text) {
  S.bk.multi = text; S.bk.view = "cards";
  const lines = text.split(/\n|;/).map((x) => x.trim()).filter(Boolean).join("\n");
  if (!lines) return;
  bkParseAndAdd({ text: lines }, true);
}

// ------------------------------------------------------------------ table / cards
const BK_ST = { ok: ["Confirmed", "ok"], confirm: ["Confirm", "maybe"], none: ["Not found", "no"], skip: ["Skipped", ""], out: ["Left out", ""], busy: ["Matching…", ""], new: ["", ""] };
function whyHTML(c) { return (c.why || []).map((w) => `<span class="bkwhy bkw-${w.k}">${esc(w.t)}</span>`).join(""); }
function fulfilHTML(f, short) {
  if (!f) return `<span class="muted">Stock: ask your branch</span>`;
  const cls = f.status === "pickup" ? "ok" : f.status === "partial" || f.status === "transfer" ? "tr" : "no";
  const lab = f.status === "pickup" ? "Pickup today" : f.status === "partial" ? "Part transfer" : f.status === "transfer" ? f.eta : f.eta;
  return `<div class="bkful ${cls}"><b>${esc(lab)}</b>${short ? "" : `<div>${esc(f.text)}</div>`}${f.demo ? ` <span class="tag">demo stock</span>` : ""}</div>`;
}
function itemLink(c) { return c.kind === "webb" ? webbLink(c, esc(c.title)) : `<span class="link" data-act="open-sku" data-sku="${esc(c.sku)}">${esc(c.title)}</span>`; }
function priceOf(c, r) { const q = c.units || r.ln.qty; return c.price == null ? null : c.price * q; }
function bkRowHTML(r, k) {
  const st = bkState(r), it = bkItem(r), m = r.m || {}, entry = S.bk.mode === "entry";
  const opts = m.options || [], subs = m.substitutes || [];
  const sel = (opts.length + subs.length) > 1 ? `<select class="qoalt" data-act="bkpick" data-id="${r.id}" aria-label="Choose a match for line ${k + 1}">
      ${opts.map((o, i) => `<option value="opt:${i}" ${r.ch.t === "opt" && r.ch.i === i ? "selected" : ""}>${i === 0 && m.confirmed ? "Exact: " : i === 0 ? "Top pick: " : ""}${esc(o.title.slice(0, 64))}${o.band ? ` (${o.band} ${o.wmcr})` : ""}${o.fulfil ? ` · ${o.fulfil.status === "pickup" ? o.fulfil.at_branch + " here" : o.fulfil.eta}` : ""}</option>`).join("")}
      ${subs.length ? `<optgroup label="On the shelf instead">${subs.map((o, i) => `<option value="sub:${i}" ${r.ch.t === "sub" && r.ch.i === i ? "selected" : ""}>${esc(o.title.slice(0, 64))}${o.fulfil ? ` · ${o.fulfil.at_branch} here` : ""}</option>`).join("")}</optgroup>` : ""}</select>` : "";
  const f = it && it.fulfil;
  const subHint = it && r.ch.t === "opt" && f && f.status !== "pickup" && subs.length ? `<div class="bksub">On the shelf instead: ${itemLink(subs[0])} <span class="muted">${esc(subs[0].how || "")}</span> <button class="linkbtn" data-act="bkusesub" data-id="${r.id}">Use it</button></div>` : "";
  const multiSrc = new Set(S.bk.rows.map((x) => x.ln.src_label).filter(Boolean)).size > 1;
  const req = entry ? `<input class="bkq" data-act="bktext" data-id="${r.id}" value="${esc(r.ln.text || "")}" placeholder="Part #, Webb item # or description" aria-label="Item for line ${k + 1}">`
    : r.ln.from_ocr ? `${r.ln.crop ? `<img class="bkcrop" src="${r.ln.crop}" alt="What the note says">` : ""}<input class="bkq bkocrq" data-act="bkedit" data-id="${r.id}" value="${esc(r.ln.text || "")}" aria-label="Line ${k + 1} as read from the note; edit to correct">
        <div class="muted bkread">${r.ln.edited ? "corrected by you" : `as read${r.ln.conf != null && r.ln.conf < 0.6 ? " (low confidence)" : ""}: qty ${r.ln.qty}`}</div>`
    : `<div class="bkreq">${esc(r.ln.text || "")}</div>${r.ln.desc && r.ln.desc !== r.ln.text ? `<div class="muted">${esc(r.ln.desc)}</div>` : ""}`;
  const srcChip = multiSrc && r.ln.src_label ? `<div class="bksrcchip" title="${esc(r.ln.src_label)}">${esc(r.ln.src_label.replace(/^Email: /, "✉ ").slice(0, 38))}</div>` : "";
  const ev = st === "ok" ? `<span class="qs ok">${m.confirmed && r.ch.t === "opt" && r.ch.i === 0 ? (m.status === "webb" ? "Webb item" : "Exact") : "Confirmed"} ✓</span>${!(m.confirmed && r.ch.t === "opt" && r.ch.i === 0) ? `<button class="linkbtn bkundo" data-act="bkunconfirm" data-id="${r.id}">undo</button>` : ""}`
    : st === "confirm" ? `<button class="btn sm primary" data-act="bkconfirm" data-id="${r.id}">Confirm</button>${it && it.band && r.ch.t === "opt" ? `<div>${bandPill(it.band, it.wmcr)}</div>` : ""}`
    : st === "out" ? `<span class="qs">Left out</span> <button class="linkbtn" data-act="bkinclude" data-id="${r.id}">include</button>`
    : st === "busy" ? `<span class="muted">Matching…</span>` : st === "none" ? `<span class="qs no">Not found</span>` : st === "skip" ? `<span class="qs">Skipped</span>` : "";
  return `<tr class="bk-${st}" data-id="${r.id}">
    <td class="muted" ${r.ln.row ? `title="Line ${r.ln.row} of ${esc(r.ln.src_label || "the document")}"` : ""}>${r.ln.row ? k + 1 : (r.ln.n || k + 1)}</td>
    <td class="bkreqc">${req}${srcChip}</td>
    <td><input class="qoq" type="number" min="1" value="${r.ln.qty || 1}" data-act="bkqty" data-id="${r.id}" aria-label="Quantity for line ${k + 1}">${r.ln.uom && r.ln.uom !== "EA" ? `<div class="muted">${esc(r.ln.uom)}</div>` : ""}</td>
    <td class="bkm">${it ? `<div class="bkmi"><div class="bkg">${G.forItem(it, 34)}</div><div>${itemLink(it)}
        <div class="muted">${it.kind === "webb" ? `Webb # ${esc(it.webb_item)}` : `Item ${esc(it.sku)}`}${it.mfr_part ? ` · ${esc(it.mfr_part)}` : ""}${it.conv ? ` · <b>${esc(it.conv)}</b>` : ""}${it.web_url ? ` · <a class="fb-web" href="${esc(it.web_url)}" target="_blank" rel="noopener" title="Open the item on fwwebb.com">fwwebb.com ↗</a>` : ""}</div>
        <div class="bkwhys">${whyHTML(it)}${r.ch.t === "sub" && it.how ? `<span class="bkwhy bkw-sub">${esc(it.how)}</span>` : ""}${it.how && r.ch.t === "opt" && it.kind === "webb" ? `<span class="bkwhy bkw-sub">${esc(it.how)}</span>` : ""}</div></div></div>${sel}`
      : st === "none" ? `<div class="muted">${esc(m.note || "No match")}</div>${bkClosestHTML(r)}<button class="linkbtn" data-act="bkfind" data-id="${r.id}">Find an item →</button>` : st === "skip" ? `<div class="muted">${esc(m.note || "")}</div>` : ""}
      ${m.note && it ? `<div class="qnote">${esc(m.note)}</div>` : ""}${m.searched_as ? `<div class="muted bksa">searched as “${esc(m.searched_as)}”</div>` : ""}</td>
    <td class="bkst">${it ? fulfilHTML(f) + subHint : ""}</td>
    <td class="bkpr">${it && it.price != null ? `${money(it.price)}<div class="muted">${money(priceOf(it, r))}</div>` : it && it.kind === "webb" ? `<span class="muted">at branch</span>` : ""}</td>
    <td class="bkev">${ev}${it && st !== "out" ? ` <button class="linkbtn bkx" data-act="bkexclude" data-id="${r.id}" title="Leave this line out" aria-label="Leave line ${k + 1} out">×</button>` : ""}${r.m && st !== "busy" && st !== "skip" && st !== "none" ? `<div><button class="linkbtn fb-find1" data-act="bkfind" data-id="${r.id}" title="Search the item list for this line">Find another</button></div>` : ""}${r.m && st !== "busy" && st !== "skip" ? `<div><button class="linkbtn fb-find1" data-act="bkwhy" data-id="${r.id}" title="How this line was matched, step by step">Why?</button></div>` : ""}</td></tr>`;
}
// the catalog has nothing of the product the line names: the nearest items, used only if the person says so
function bkClosestHTML(r) {
  const cl = (r.m && r.m.closest) || [];
  return cl.length ? `<div class="bkclose"><div class="muted">Nearest in the catalog:</div>${cl.map((c, i) => `<div class="bkcl">${itemLink(c)} <button class="linkbtn" data-act="bkuseclosest" data-id="${r.id}" data-i="${i}">use this</button></div>`).join("")}</div>` : "";
}
function bkUseClosest(r, i, confirmed) {
  r.m = { ...r.m, options: r.m.closest, closest: [], status: "rec", via: "Nearest item", weak: true, note: (r.m.note || "") + " Using the nearest item." };
  r.ch = { t: "opt", i }; r.confirmed = !!confirmed; r.excluded = false;
}
function bkCardHTML(c, r, key, chosen) {
  return `<div class="bkcard ${chosen ? "on" : ""}" data-sku="${esc(c.sku)}"><div class="bkcg">${G.forItem(c, 64)}</div>
    <div class="bkct">${itemLink(c)}</div><div class="muted" style="font-size:11.5px">${esc(c.brand || "")}${c.mfr_part ? " · " + esc(c.mfr_part) : ""}${c.conv ? " · " + esc(c.conv) : ""}${c.web_url ? ` · <a class="fb-web" href="${esc(c.web_url)}" target="_blank" rel="noopener" title="Open the item on fwwebb.com">fwwebb.com ↗</a>` : ""}</div>
    <div class="bkcp">${c.price != null ? `<b>${money(c.price)}</b>` : `<span class="muted">${c.kind === "webb" ? "price at branch" : ""}</span>`}</div>
    ${fulfilHTML(c.fulfil, true)}<div class="bkwhys">${whyHTML(c)}${c.band ? bandPill(c.band, c.wmcr) : ""}${c.how ? `<span class="bkwhy bkw-sub">${esc(c.how)}</span>` : ""}</div>
    <button class="btn sm ${chosen ? "" : "primary"}" data-act="bkchoose" data-id="${r.id}" data-k="${key}">${chosen ? "Chosen ✓" : "Choose"}</button></div>`;
}
function bkStripHTML(r, k) {
  const m = r.m || {}, st = bkState(r);
  const cards = (m.options || []).map((c, i) => bkCardHTML(c, r, "opt:" + i, r.ch.t === "opt" && r.ch.i === i && r.confirmed)).join("") +
    (m.substitutes || []).map((c, i) => bkCardHTML(c, r, "sub:" + i, r.ch.t === "sub" && r.ch.i === i && r.confirmed)).join("") +
    (!(m.options || []).length ? (m.closest || []).map((c, i) => bkCardHTML(c, r, "cls:" + i, false)).join("") : "");
  return `<div class="bkstrip bk-${st}" data-id="${r.id}"><div class="bksh">${r.ln.crop ? `<img class="bkcrop" src="${r.ln.crop}" alt="What the note says">` : ""}<b>${esc(r.ln.text || "")}</b> <label class="muted">qty <input class="qoq" type="number" min="1" value="${r.ln.qty || 1}" data-act="bkqty" data-id="${r.id}"></label>
      <span class="qs ${BK_ST[st][1]}">${st === "confirm" ? "Choose one" : st === "ok" ? "Chosen ✓" : BK_ST[st][0]}</span>${m.note ? `<span class="qnote">${esc(m.note)}</span>` : ""}</div>
    ${st === "busy" ? `<div class="muted">Matching…</div>` : cards ? `<div class="bkcards">${cards}</div>` : `<div class="muted">${esc(m.note || "Nothing found.")}</div>`}
    ${r.m && st !== "busy" ? `<button class="linkbtn" data-act="bkfind" data-id="${r.id}">Find an item →</button>` : ""}</div>`;
}
function bkCounts() {
  const R = S.bk.rows.filter((r) => r.m && r.m.status !== "skip");
  const c = { lines: R.length, ok: 0, confirm: 0, none: 0, out: 0, short: 0, exact: 0 };
  R.forEach((r) => { const s = bkState(r); c[s] = (c[s] || 0) + 1; const it = bkItem(r); if (it && !r.excluded && it.fulfil && it.fulfil.status !== "pickup") c.short++; if (r.m.confirmed && r.ch.t === "opt" && r.ch.i === 0 && !r.excluded) c.exact++; });
  c.skip = S.bk.rows.filter((r) => r.m && r.m.status === "skip").length;
  return c;
}
function bkPaint() {
  if (S.order && S.order.test) { const t = $(".fb-testline"); if (t) t.outerHTML = bkTestLine(); }
  const el = $("#bkBody"); if (!el) return;
  const rows = S.bk.rows, entry = S.bk.mode === "entry";
  if (entry && !rows.some((r) => !(r.ln.text || "").trim())) rows.push({ id: ++bkId, ln: { text: "", qty: 1 }, m: null, ch: { t: "opt", i: 0 }, confirmed: false, excluded: false, busy: false });
  if (!rows.length) { el.innerHTML = `<div class="muted bkempty">${S.bk.mode === "multi" ? "Results for each product appear here." : "Matched lines appear here."}</div>`; return; }
  const c = bkCounts(), f = S.bk.filter;
  const show = rows.map((r, k) => [r, k]).filter(([r]) => f === "all" || (f === "confirm" ? bkState(r) === "confirm" : f === "short" ? (bkItem(r) && bkItem(r).fulfil && bkItem(r).fulfil.status !== "pickup") : f === "none" ? ["none", "out", "skip"].includes(bkState(r)) : true) || (entry && !(r.ln.text || "").trim()));
  const pickable = rows.filter((r) => bkState(r) === "confirm" && !r.m.weak && r.ch.t === "opt" && r.ch.i === 0).length;
  const view = (S.bk.view === "cards" || (innerWidth < 720 && !S.bk.viewPicked)) && !entry ? "cards" : "table";   // phones: cards
  el.innerHTML = `<div class="bkbar"><b>${c.lines} line${c.lines === 1 ? "" : "s"}</b>
      <span class="qs ok">${c.ok || 0} confirmed</span><span class="qs maybe">${c.confirm || 0} to confirm</span><span class="qs x">${c.short} need a transfer or substitute</span><span class="qs no">${c.none || 0} not found</span>${c.out ? `<span class="qs">${c.out} left out</span>` : ""}${c.skip ? `<span class="qs">${c.skip} skipped</span>` : ""}
      <span style="flex:1"></span>
      <div class="seg qof">${[["all", "All"], ["confirm", "To confirm"], ["short", "Transfers"], ["none", "Not found"]].map(([k, l]) => `<button class="${f === k ? "on" : ""}" data-act="bkfilter" data-k="${k}">${l}</button>`).join("")}</div>
      ${entry ? "" : `<div class="seg qof">${[["table", "Table"], ["cards", "Cards"]].map(([k, l]) => `<button class="${view === k ? "on" : ""}" data-act="bkview" data-k="${k}">${l}</button>`).join("")}</div>`}</div>
    ${view === "cards" ? `<div class="bkstrips">${show.slice(0, S.bk.limit).map(([r, k]) => bkStripHTML(r, k)).join("")}</div>` :
    `<div class="bkscroll"><table class="bkt"><thead><tr><th>#</th><th>Requested</th><th>Qty</th><th>Match</th><th>Stock at your branch</th><th>Price</th><th></th></tr></thead><tbody>${show.slice(0, S.bk.limit).map(([r, k]) => bkRowHTML(r, k)).join("")}</tbody></table></div>`}
    ${show.length > S.bk.limit ? `<div class="showmore"><button class="btn" data-act="bkmore">Show ${Math.min(200, show.length - S.bk.limit)} more of ${(show.length - S.bk.limit).toLocaleString()} lines</button> <span class="muted">Totals, filters, Confirm top picks and Review cover all lines.</span></div>` : ""}
    <div class="bkfoot"><button class="btn" data-act="bkclear">Clear lines</button><button class="btn" data-act="saveorder">${ic("save")}Save</button>
      <span style="flex:1"></span>${pickable ? `<button class="btn" data-act="bkconfirmall">Confirm ${pickable} top pick${pickable === 1 ? "" : "s"}</button>` : ""}
      <button class="btn primary" data-act="bkreview" ${c.lines ? "" : "disabled"}>${ic("check")}Review (${c.ok || 0} of ${c.lines} confirmed)</button></div>`;
}

// ------------------------------------------------------------------ review + cart
function bkGroups() {
  const g = { exact: [], rec: [], sub: [], transfer: [], dc: [], out: [] };
  S.bk.rows.forEach((r) => {
    const st = bkState(r), it = bkItem(r);
    if (st === "new" || st === "busy" || st === "skip") return;
    if (st !== "ok") { g.out.push([r, it, st === "confirm" ? "not confirmed" : st === "out" ? "left out" : "not found"]); return; }
    if (r.ch.t === "sub") g.sub.push([r, it]); else if (r.m.confirmed && r.ch.i === 0) g.exact.push([r, it]); else g.rec.push([r, it]);
    const f = it.fulfil;
    if (f && (f.status === "partial" || f.status === "transfer")) g.transfer.push([r, it]);
    else if (f && f.status !== "pickup") g.dc.push([r, it]);
  });
  return g;
}
async function bkReview() {
  // the quantity boxes are the truth (typed, pasted, scanned or filled by a tool); any line matched for another
  // quantity, or still being re-matched, is matched again before units, prices and stock are counted
  $$('[data-act="bkqty"]').forEach((el) => { const r = bkRow(el.dataset.id); if (r) r.ln.qty = Math.max(1, parseInt(el.value) || 1); });
  const stale = S.bk.rows.filter((r) => r.m && r.mq != null && r.mq !== r.ln.qty && (r.ln.text || "").trim());
  if (stale.length) await bkMatch(stale, "Re-checking quantities:");
  const g = bkGroups(), b = branchName().split(",")[0];
  const add = [...g.exact, ...g.rec, ...g.sub];
  const units = add.reduce((s, [r, it]) => s + (it.units || r.ln.qty), 0);
  const tot = add.reduce((s, [r, it]) => s + (priceOf(it, r) || 0), 0);
  const priced = add.some(([, it]) => it.price != null);
  const today = add.filter(([, it]) => !it.fulfil || it.fulfil.status === "pickup").length;
  const li = (r, it, extra) => `<li><span class="muted">${esc(r.ln.text)}</span> → <b>${esc(it.title)}</b> · ${it.units || r.ln.qty}${it.conv ? ` (${esc(it.conv)})` : ""}${extra ? ` <span class="muted">${extra}</span>` : ""}</li>`;
  const sec = (title, arr, fn, open) => arr.length ? `<details class="bkrev" ${open ? "open" : ""}><summary><b>${title}</b> <span class="muted">${arr.length}</span></summary><ul>${arr.map(fn).join("")}</ul></details>` : "";
  const o = S.order, cust = [o.customer_no, o.customer_name].filter(Boolean).join(" · ");
  $("#modal").innerHTML = `<div class="box" style="max-width:860px"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px"><b>Review · ${cust ? esc(cust) : "no customer yet"}${o.job ? " · " + esc(o.job) : ""} · pickup ${esc(b)} · ${esc($("#level").value || "List")} prices</b><button class="btn sm" data-act="modalclose">Close</button></div>${bkMetaHTML()}
    <div class="kpi bkkpi"><div><b>${add.length}</b>confirmed lines</div><div><b>${units.toLocaleString()}</b>units</div><div><b>${today}</b>ready at ${esc(b)} today</div><div><b>${g.transfer.length + g.dc.length}</b>by transfer or CD</div>${priced ? `<div><b>${money(tot)}</b>subtotal</div>` : ""}</div>
    ${sec("Exact matches", g.exact, ([r, it]) => li(r, it), false)}
    ${sec("Recommendations you confirmed", g.rec, ([r, it]) => li(r, it, (it.why || []).map((w) => esc(w.t)).join(" · ") || (it.band ? `${esc(it.band)} ${it.wmcr}` : "")), true)}
    ${sec("Substitutions", g.sub, ([r, it]) => li(r, it, esc(it.how || "")), true)}
    ${sec("Needs a transfer", g.transfer, ([r, it]) => `<li><b>${esc(it.title)}</b>: ${esc(it.fulfil.text)}${it.fulfil.demo ? " (demo stock)" : ""}</li>`, true)}
    ${sec("From the CD or special order", g.dc, ([r, it]) => `<li><b>${esc(it.title)}</b>: ${esc(it.fulfil.text)}</li>`, true)}
    ${sec("Not confirmed or left out", g.out, ([r, it, why]) => `<li><span class="muted">${esc(r.ln.text)}</span> · ${why}</li>`, true)}
    <p class="muted" style="font-size:12px">The order-entry file has the ${add.length} confirmed line${add.length === 1 ? "" : "s"} (item # and quantity). The Excel workbook and CSV have every line, with what was asked, the match, stock and price.${custKey() ? "" : " Add the customer before saving, so their picks are remembered for next time."}</p>
    <div class="qofoot"><button class="btn" data-act="modalclose">Back to the lines</button><button class="btn" data-act="export" data-f="entry">Order entry file</button><button class="btn" data-act="export" data-f="csv">CSV</button><span style="flex:1"></span>
      <button class="btn" data-act="export" data-f="xlsx">${ic("down")}Excel</button><button class="btn primary" data-act="bkcommit">${ic("save")}Save order</button></div></div>`;
  $("#modal").classList.add("on");
}
async function bkCommit() { const r = await saveOrder(); if (r) $("#modal").classList.remove("on"); }
// ------------------------------------------------------------------ quick entry
let bkT = {};
function bkEntryChanged(r, text) {
  r.ln = { ...r.ln, text, n: r.ln.n };
  clearTimeout(bkT[r.id]);
  if (!text.trim()) { r.m = null; r.busy = false; return; }
  bkT[r.id] = setTimeout(async () => {
    const my = S.bk.seq, sent = bkSend(r);
    const rq = await fetch("/api/bom/match", { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ lines: [r.ln], ...who(), branch: $("#branch").value, sid: SID }) }).catch(() => null);
    if (!rq || !rq.ok || my !== S.bk.seq || r.ln.text !== text) return;
    const res = await rq.json(); if (!res.lines[0] || !bkApply(r, res.lines[0], sent)) return;
    const a = document.activeElement, keep = a && a.dataset && a.dataset.id ? [a.dataset.act, a.dataset.id, a.selectionStart] : null;
    bkPaint();
    if (keep) { const el = $(`[data-act="${keep[0]}"][data-id="${keep[1]}"]`); if (el) { el.focus(); try { el.setSelectionRange(keep[2], keep[2]); } catch (e) { /* number inputs */ } } }
  }, 450);
}

// ------------------------------------------------------------------ events
const bkRow = (id) => S.bk.rows.find((r) => r.id === +id);
document.addEventListener("click", (e) => {
  const t = e.target.closest("[data-act]"); if (!t) return;
  const r = t.dataset.id ? bkRow(t.dataset.id) : null;
  switch (t.dataset.act) {
    case "bkmode": S.bk.mode = t.dataset.k; if (t.dataset.k === "multi") S.bk.view = "cards"; else S.bk.view = "table";
      $$(".bkmodes button").forEach((b) => { b.classList.toggle("on", b.dataset.k === S.bk.mode); b.setAttribute("aria-selected", b.dataset.k === S.bk.mode); });
      S.bk.rows = S.bk.rows.filter((x) => (x.ln.text || "").trim()); bkPaintInput(); bkPaint();
      if (S.bk.mode === "entry") setTimeout(() => { const el = $$(".bkq").pop(); if (el) el.focus(); }, 30); break;
    case "bksearch": bkSearchEach($("#bkMulti").value); break;
    case "bkmultisample": $("#bkMulti").value = BK_MULTI; bkSearchEach(BK_MULTI); break;
    case "bksample": S.bk.file = null; bkParseAndAdd({ text: BK_SAMPLE, filename: "sample-bom.csv" }, true); break;
    case "bksamplefile": (async () => {
      try { const rq = await fetch("/api/bom/samples/" + encodeURIComponent(t.dataset.f)); if (!rq.ok) throw new Error(rq.status);
        bkReadFiles([new File([await rq.blob()], t.dataset.f)]); } catch (e) { toast("That sample isn't in this copy."); }
    })(); break;
    case "bkpastebox": S.bk.pasteOpen = true; $("#bkPasteBox").hidden = false; $("#bkPaste").focus(); break;
    case "bkreadpaste": S.bk.paste = $("#bkPaste").value; S.bk.file = null; bkParseAndAdd({ text: S.bk.paste, classify: true, filename: "Pasted text" }, true); break;
    case "bkother": { const el = $(`#bkoth-${t.dataset.d}-${t.dataset.s}`); if (el) el.hidden = !el.hidden; break; }
    case "bkaddother": {
      const d = S.bk.docs[+t.dataset.d], src = d && d.sources.find((x) => x.id === +t.dataset.s), o = src && src.other[+t.dataset.o];
      if (!o || o.added) break;
      o.added = true; t.textContent = "added"; t.disabled = true;
      const row = bkNewRow({ n: o.n, row: o.row, raw: o.raw, text: o.text || o.raw, qty: o.qty || 1, uom: o.uom || "", desc: o.desc || "", src: o.src, src_label: o.src_label, crop: o.crop, from_ocr: o.from_ocr });
      S.bk.rows.push(row); bkMatch([row], "Matching the added line:"); break;
    }
    case "bkreread": {
      const roles = {}; $$("[data-act=bkrole]").forEach((s) => { if (s.value) roles[s.value] = +s.dataset.k; });
      const p = S.bk.parse || {}, body = S.bk.file ? { filename: S.bk.file.name, data_b64: S.bk.file.b64, sheet: p.sheet } : { text: S.bk.paste || BK_SAMPLE };
      bkParseAndAdd({ ...body, roles, header_row: p.header_row }, true); break;
    }
    case "bkconfirm": if (r) { r.confirmed = true; bkLogChoice(r, "confirm"); bkPaint(); } break;
    case "bkunconfirm": if (r) { r.confirmed = false; bkPaint(); } break;
    case "bkconfirmall": S.bk.rows.forEach((x) => { if (bkState(x) === "confirm" && !x.m.weak && x.ch.t === "opt" && x.ch.i === 0) { x.confirmed = true; bkLogChoice(x, "confirm top pick"); } }); bkPaint(); break;
    case "bkexclude": if (r) { r.excluded = true; r.confirmed = false; bkPaint(); } break;
    case "bkinclude": if (r) { r.excluded = false; r.confirmed = !!(r.m.confirmed && r.ch.t === "opt" && r.ch.i === 0); bkPaint(); } break;
    case "bkusesub": if (r) { r.ch = { t: "sub", i: 0 }; r.confirmed = false; bkLogChoice(r, "substitute"); bkPaint(); } break;
    case "bkchoose": if (r) { const [tt, i] = t.dataset.k.split(":"); if (tt === "cls") bkUseClosest(r, +i, true); else { r.ch = { t: tt, i: +i }; r.confirmed = true; r.excluded = false; } bkLogChoice(r, tt === "sub" ? "substitute" : tt === "cls" ? "nearest" : "chosen"); bkPaint(); } break;
    case "bkuseclosest": if (r) { bkUseClosest(r, +t.dataset.i, false); bkLogChoice(r, "nearest"); bkPaint(); } break;
    case "bkfilter": S.bk.filter = t.dataset.k; S.bk.limit = 200; bkPaint(); break;
    case "bkmore": S.bk.limit += 200; bkPaint(); break;
    case "bkview": S.bk.view = t.dataset.k; S.bk.viewPicked = true; bkPaint(); break;
    case "bkclear": S.bk.seq++; S.bk.rows = []; S.bk.parse = null; S.bk.file = null; S.bk.docs = []; bkPaintInput(); bkPaint(); break;
    case "bkreview": bkReview(); break;
    case "bkcommit": bkCommit(); break;
    case "bkfind": if (r) findItem(r.id); break;
    case "bkwhy": if (r) explainLine(r); break;
    case "usefound": if (r && S.found && S.found[+t.dataset.i]) {
      const c = S.found[+t.dataset.i];
      r.m = { ...(r.m || {}), options: [c, ...((r.m && r.m.options) || []).filter((x) => x.sku !== c.sku)].slice(0, 8), closest: [], status: "rec", via: "Found by hand", weak: false,
        note: "Picked by hand", needs_confirm: false, confirmed: false, substitutes: (r.m && r.m.substitutes) || [] };
      if (!r.m.key) r.m.key = "";
      r.ch = { t: "opt", i: 0 }; r.confirmed = true; r.excluded = false; r.busy = false;
      bkLogChoice(r, "search"); $("#modal").classList.remove("on"); bkPaint();
    } break;
  }
  if (/^bk(confirm|unconfirm|confirmall|exclude|include|usesub|choose|useclosest|clear|addother)$|^usefound$/.test(t.dataset.act)) markDirty();
});
document.addEventListener("change", (e) => {
  const t = e.target, act = t.dataset.act; if (!act || !act.startsWith("bk")) return;
  const r = t.dataset.id ? bkRow(t.dataset.id) : null;
  if (act === "bkpick" && r) { const [tt, i] = t.value.split(":"); r.ch = { t: tt, i: +i }; r.confirmed = !!(tt === "opt" && +i === 0 && r.m.confirmed); markDirty(); bkPaint(); }
  if (act === "bkqty" && r) { markDirty(); r.ln.qty = Math.max(1, parseInt(t.value) || 1); if ((r.ln.text || "").trim() && r.mq !== r.ln.qty) bkMatch([r], "Re-checking stock:"); }
  if (act === "bksheet") { const p = S.bk.parse; if (S.bk.file) bkParseAndAdd({ filename: S.bk.file.name, data_b64: S.bk.file.b64, sheet: t.value }, true); }
});
document.addEventListener("change", (e) => {
  const t = e.target; if (t.dataset.act !== "bkedit") return;
  const r = bkRow(t.dataset.id); if (!r || t.value.trim() === (r.ln.text || "").trim()) return;
  const p = t.value.trim().match(/^\s*(\d{1,5})\s*[-–_)x]\s*(.+)$/);    // "2 - 1¼ tee": keep the count in the qty box
  r.ln = { ...r.ln, text: p ? p[2] : t.value.trim(), qty: p ? +p[1] : r.ln.qty, edited: true }; markDirty();
  bkMatch([r], "Re-matching the corrected line:");
});
document.addEventListener("input", (e) => {
  const t = e.target;
  if (t.dataset.act === "bkqty") { const r = bkRow(t.dataset.id); if (r && parseInt(t.value) >= 1) r.ln.qty = parseInt(t.value); return; }
  if (t.dataset.act !== "bktext") return;
  const r = bkRow(t.dataset.id); if (r) { bkEntryChanged(r, t.value); markDirty(); }
});
document.addEventListener("keydown", (e) => {
  const t = e.target; if (!t.dataset || e.key !== "Enter") return;
  if (t.dataset.act === "bktext") { e.preventDefault(); const q = $(`[data-act="bkqty"][data-id="${t.dataset.id}"]`); if (q) { q.focus(); q.select(); } }
  else if (t.dataset.act === "bkqty" && S.bk.mode === "entry") { e.preventDefault(); t.blur(); setTimeout(() => { bkPaint(); const el = $$(".bkq").pop(); if (el) el.focus(); }, 60); }
  else if (t.id === "bkMulti" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); bkSearchEach(t.value); }
});
document.addEventListener("paste", (e) => {
  const t = e.target, txt = (e.clipboardData || window.clipboardData).getData("text");
  const lines = txt.split(/\r?\n/).filter((x) => x.trim());
  if (lines.length < 2) return;
  if (t.dataset && t.dataset.act === "bktext") {           // several lines pasted into one entry box: add them all
    e.preventDefault(); S.bk.rows = S.bk.rows.filter((x) => (x.ln.text || "").trim()); bkParseAndAdd({ text: txt }, false);
  }
});

