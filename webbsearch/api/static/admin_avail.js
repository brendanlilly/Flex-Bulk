/* v2.3: Availability ranking (tiers, delivery, data quality, the Extended Catalog section, manufacturer rules), the
   Extended Catalog build, suppliers and special-order quote requests. Loaded after admin.js; uses its helpers
   (adm, esc, num, pct, kpi, PAGES, A, previewCard, runPreview, previewSoon, renderDraftBar, toast). */
const AV_TIERS = [["1a", "In stock in the same-day zone"], ["1b", "In stock in the region or its CD (next day)"], ["1c", "In stock elsewhere at Webb (2-3 day transfer)"],
  ["2a", "Coded, not stocked: in stock at the factory"], ["2b", "Coded, not stocked: manufacturer's lead time"], ["2c", "Coded, not stocked: lead time unknown"],
  ["3a", "Extended Catalog: in stock at the factory"], ["3b", "Extended Catalog: manufacturer's lead time"], ["3c", "Extended Catalog: lead time unknown"]];
const AV_SEG = { plumbing_contractor: "Plumbing contractors", hvac_contractor: "HVAC contractors", mechanical_contractor: "Mechanical contractors",
  builder_remodeler: "Builders and remodelers", facilities_mro: "Facilities / MRO", industrial: "Industrial", water_works: "Water works" };
const avD = () => A.draft.availability, avL = () => A.live.availability;
const avGet = (o, path) => path.split(".").reduce((x, k) => (x == null ? x : x[k]), o);
function avSet(path, v) {
  const ks = path.split("."); let o = avD();
  for (const k of ks.slice(0, -1)) o = o[k];
  o[ks[ks.length - 1]] = v;
  renderDraftBar(); previewSoon();
  const lab = document.querySelector(`[data-avl="${path}"]`);
  if (lab) lab.classList.toggle("chg", JSON.stringify(avGet(avD(), path)) !== JSON.stringify(avGet(avL(), path)));
}
function avNum(path, label, desc, lo, hi, step) {
  const v = avGet(avD(), path), ch = v !== avGet(avL(), path);
  return `<div class="wrow"><div><b class="${ch ? "chg" : ""}" data-avl="${path}">${esc(label)}</b>${desc ? `<div class="desc">${esc(desc)}</div>` : ""}</div>
    <input type="range" min="${lo}" max="${hi}" step="${step}" value="${v}" data-av="${path}" aria-label="${esc(label)}">
    <input class="in num" value="${v}" data-avn="${path}" inputmode="decimal" aria-label="${esc(label)} value"></div>`;
}

PAGES.availability = async (el) => {
  el.innerHTML = `<div class="ph"><div><h1>Availability ranking</h1><p>How availability moves results. Coded items always come first; within them, in-stock-nearby rises and long lead times sink. Extended Catalog (-EC) items are a separate section after every relevant coded item, from their own index and their own request, so they never slow the coded results.</p></div><span class="sp"></span><button class="btn" data-a="reset" data-s="availability">Reset this page to defaults</button></div><div id="avpg">Loading…</div>`;
  let info;
  try { info = await adm("/api/admin/availability"); } catch (e) { $("#avpg").innerHTML = `<div class="explain">${esc(e.message)}</div>`; return; }
  if (A.page !== "availability") return;
  A.avInfo = info;
  if (!A.draft.availability) { A.draft.availability = JSON.parse(JSON.stringify(info.settings)); A.live.availability = JSON.parse(JSON.stringify(info.settings)); }
  const D = avD(), st = info.status;
  const ec = D.ec;
  $("#avpg").innerHTML = `
    <div class="kpis">${kpi("Extended Catalog items", st.ready ? num(st.items) : st.building ? "building…" : "not built")}${kpi("Held back (missing core fields)", num(st.held_back))}
      ${kpi("Index shards", num(st.shards))}${kpi("Built", st.built ? esc(st.built.replace("T", " ")) : "—")}${kpi("Suppliers with stock feeds", `${num(info.suppliers.stock_feed)} of ${num(info.suppliers.count)}`)}${kpi("Free-freight terms", num(info.suppliers.ffa))}</div>
    <div class="grid g12 mt"><div>
      <div class="card"><h3>Coded results <label class="sw" style="float:right"><input type="checkbox" data-avt="enabled" ${D.enabled ? "checked" : ""} aria-label="Availability moves coded results"><span></span></label></h3>
        <div class="scale">Score = relevance + availability. Availability is scaled to the relevance spread of the best ${D.relevance_window} matches: a tier weight of 0.30 is 30% of the gap between the best and the ${D.relevance_window}th best match. Exact part numbers stay first. Off: availability only labels the cards.</div>
        ${AV_TIERS.slice(0, 6).map(([t, l]) => avNum(`tiers.${t}`, `Tier ${t}`, l, -0.5, 0.5, 0.01)).join("")}
        ${avNum("per_day", "Per business day of delivery", "Subtracted for each day of the delivery estimate (lead time × the manufacturer's lateness + transit + handling).", 0, 0.05, 0.001)}
        ${avNum("day_cap", "Delivery penalty cap", "At most this much for a slow delivery.", 0, 0.5, 0.01)}
        ${avNum("stale_hours", "Factory stock older than (hours)", "Counts as unknown stock: tier a falls to b.", 1, 240, 1)}
        ${avNum("zone_minutes", "Same-day zone (drive minutes)", "Counters within this drive of the pickup branch count as local (tier 1a).", 10, 120, 5)}
        ${avNum("relevance_window", "Relevance window", "How many of the best matches set the scale.", 10, 600, 10)}</div>
      <div class="card mt"><h3>Extended Catalog section <label class="sw" style="float:right"><input type="checkbox" data-avt="ec.enabled" ${ec.enabled ? "checked" : ""} aria-label="Show the Extended Catalog section"><span></span></label></h3>
        <div class="scale">Shown after the coded results to signed-in accounts of the segments below. Never interleaved with coded items.</div>
        <div class="row" style="flex-wrap:wrap;gap:10px;margin:8px 0">${Object.entries(AV_SEG).map(([k, l]) => `<label class="chk"><input type="checkbox" data-avseg="${k}" ${ec.segments.includes(k) ? "checked" : ""}> ${esc(l)}</label>`).join("")}</div>
        ${AV_TIERS.slice(6).map(([t, l]) => avNum(`tiers.${t}`, `Tier ${t}`, l, -0.5, 0.5, 0.01)).join("")}
        ${avNum("data.ship_complete", "Weight and dimensions known", "", -0.2, 0.2, 0.01)}
        ${avNum("data.ship_missing", "Shipping data missing", "", -0.2, 0.2, 0.01)}
        ${avNum("data.no_price", "No confirmed price", "", -0.2, 0.2, 0.01)}
        ${avNum("ec.min_coverage", "Words matched", "Share of the searched words an -EC item must carry.", 0.3, 1, 0.05)}
        ${avNum("ec.max_items", "Items in the section", "", 0, 60, 1)}
        ${avNum("ec.price_confidence", "Pricing-group confidence to show a price", "Below it: “Price confirmed at order” (Cortex pricing groups go to pricing review).", 50, 100, 1)}
        ${avNum("ec.budget_ms", "Time budget (ms)", "The section gives up and retries after this long; the coded results never wait.", 100, 3000, 50)}</div>
      <div class="card mt"><h3>Manufacturers</h3><div class="scale">Boost or bury a manufacturer in availability ranking; block removes it from the Extended Catalog (coded items are never hidden here).</div>
        <div id="avbr">${brandRows()}</div>
        <form class="row" data-f="avbrand" style="gap:6px;margin-top:8px"><input class="in" id="avbn" placeholder="Brand" list="avbl"><datalist id="avbl">${info.suppliers.rows.map((s) => `<option value="${esc(s.brand)}">`).join("")}</datalist>
          <select class="in" id="avbm"><option value="boost">Boost</option><option value="bury">Bury</option><option value="block">Block</option></select><input class="in num" id="avbv" value="0.05" inputmode="decimal"><button class="btn">Add</button></form></div>
      <div class="card mt"><h3>Replay the most-searched queries</h3><div class="scale">The logged searches with the live settings and with your draft: how many of the first 12 are in stock at Webb, the average delivery estimate, and how far items moved.</div>
        <button class="btn dark" data-a="avreplay">Replay 30 searches</button><div id="avrep" class="mt"></div></div>
    </div><div>${previewCard()}
      <div class="card mt"><h3>Extended Catalog build</h3><div class="scale">Production: Snowflake publishes the -EC items that pass the core-field check (brand, part, title, category, pricing group, image, compliance for potable water and gas) and an index job loads them. Here: a synthetic build in a separate, low-priority process; search keeps running.</div>
        <div class="small">${st.ready ? `Live build <b>${esc(st.build)}</b>: ${num(st.items)} items in ${st.shards} shard${st.shards > 1 ? "s" : ""}, built in ${num(st.build_seconds)} s.` : st.building ? "A build is running." : "No build yet."}</div>
        <div class="row mt" style="gap:6px"><input class="in num" id="avbuildn" value="1000000"><button class="btn" data-a="avbuild">Start a build</button></div>
        <div class="small mt">Regional CDs (demo designation): ${Object.values(info.cds).map(esc).join(" · ")}</div></div>
      <div class="card mt"><h3>Quote requests: special orders and volume</h3><div id="avq" class="small">Loading…</div></div>
      <div class="card mt"><h3>Suppliers <span class="sub">${num(info.suppliers.count)}</span></h3><div class="tblwrap" style="max-height:360px;overflow:auto"><table class="tbl"><tr><th>Brand</th><th>Code</th><th>Stock feed</th><th>Lead (days)</th><th>Late</th><th>Free freight over</th><th>Ships from</th></tr>
        ${info.suppliers.rows.map((s) => `<tr><td>${esc(s.brand)}</td><td>${esc(s.vendor_code)}</td><td>${s.stock_feed ? `yes, ~${Math.round(s.feed_age_hours)} h old` : "no"}</td><td class="n">${s.lead_feed ? s.lead_days_typical : "unknown"}</td><td class="n">${pct(s.on_time_lateness, 0)}</td><td class="n">${s.ffa_min ? "$" + num(s.ffa_min) : "freight added"}</td><td>${esc(s.ships_from.join("; "))}</td></tr>`).join("")}</table></div></div>
    </div></div>`;
  runPreview(); loadQuotes();
};
function brandRows() {
  const b = avD().brands || {};
  const rows = Object.entries(b);
  return rows.length ? rows.map(([n, r]) => `<div class="row small" style="gap:8px;margin:3px 0"><b>${esc(n)}</b><span class="chip">${esc(r.mode)}${r.mode === "block" ? "" : ` ${r.value}`}</span><button class="linkbtn" data-a="avbrm" data-b="${esc(n)}">remove</button></div>`).join("") : `<div class="muted small">No manufacturer rules.</div>`;
}
async function loadQuotes() {
  const el = $("#avq"); if (!el) return;
  let d; try { d = await adm("/api/admin/quotes"); } catch (e) { el.textContent = e.message; return; }
  const mailLine = (m) => !m ? "" : `<div class="small">E-mail to ${esc(m.to)}: <b>${esc(m.status)}</b>${m.error ? ` · ${esc(m.error)}` : ""} · <button class="linkbtn" data-a="avqeml" data-q="${esc(m.key)}">open the e-mail (.eml)</button></div>`;
  el.innerHTML = d.quotes.length ? d.quotes.slice(0, 20).map((q) => `<div style="margin:6px 0"><b>${esc(q.id)}</b> · ${(q.draft || {}).kind === "volume" ? "<span class=\"chip\">Volume</span> · " : ""}branch ${esc(q.branch)} · due ${esc((q.draft || {}).due || "")}<div class="muted">${esc(q.question)}</div>${mailLine(q.mail)}</div>`).join("") : "No open quote requests.";
}
async function quoteEml(id) {
  try { const t = await adm(`/api/admin/quotes/${encodeURIComponent(id)}.eml`); const u = URL.createObjectURL(new Blob([t], { type: "message/rfc822" }));
    const a = document.createElement("a"); a.href = u; a.download = `${id}.eml`; document.body.appendChild(a); a.click(); setTimeout(() => { URL.revokeObjectURL(u); a.remove(); }, 500);
  } catch (e) { toast(e.message, true); }
}
async function avReplay(btn) {
  btn.disabled = true; $("#avrep").innerHTML = `<div class="muted small">Running…</div>`;
  let r; try { r = await adm("/api/admin/availability/replay", { method: "POST", body: { patch: avD(), n: 30 } }); }
  catch (e) { $("#avrep").innerHTML = `<div class="explain">${esc(e.message)}</div>`; btn.disabled = false; return; }
  btn.disabled = false;
  const s = r.summary, mix = (m) => Object.entries(m.tiers).sort().map(([t, n]) => `${t}:${n}`).join(" ");
  $("#avrep").innerHTML = `<div class="kpis sm">${kpi("In stock at Webb, first 12 (live)", pct(s.live_in_stock_share, 0))}${kpi("… with your draft", pct(s.draft_in_stock_share, 0))}${kpi("Positions moved per search", s.avg_positions_moved)}</div>
    <div class="tblwrap mt" style="max-height:300px;overflow:auto"><table class="tbl"><tr><th>Search</th><th>Live tiers (first 12)</th><th>Draft tiers</th><th>Avg days live → draft</th><th>Moved</th></tr>
    ${r.rows.map((x) => `<tr><td>${esc(x.q)}</td><td class="small">${mix(x.live)}</td><td class="small">${mix(x.draft)}</td><td class="n">${x.live.avg_days ?? "—"} → ${x.draft.avg_days ?? "—"}</td><td class="n">${x.moved}</td></tr>`).join("")}</table></div>`;
}

document.addEventListener("input", (e) => {
  const t = e.target;
  if (t.dataset.av) { const v = parseFloat(t.value); avSet(t.dataset.av, v); const n = document.querySelector(`[data-avn="${t.dataset.av}"]`); if (n) n.value = v; }
  if (t.dataset.avn && t.value.trim() !== "" && !Number.isNaN(parseFloat(t.value))) { const v = parseFloat(t.value); avSet(t.dataset.avn, v); const r = document.querySelector(`[data-av="${t.dataset.avn}"]`); if (r) r.value = v; }
});
document.addEventListener("change", (e) => {
  const t = e.target;
  if (t.dataset.avt) avSet(t.dataset.avt, t.checked);
  if (t.dataset.avseg) { const s = new Set(avD().ec.segments); t.checked ? s.add(t.dataset.avseg) : s.delete(t.dataset.avseg); avSet("ec.segments", Object.keys(AV_SEG).filter((k) => s.has(k))); }
});
document.addEventListener("click", async (e) => {
  const t = e.target.closest("[data-a]"); if (!t) return;
  if (t.dataset.a === "avreplay") avReplay(t);
  if (t.dataset.a === "avbrm") { const b = { ...avD().brands }; delete b[t.dataset.b]; avSet("brands", b); $("#avbr").innerHTML = brandRows(); }
  if (t.dataset.a === "avqeml") quoteEml(t.dataset.q);
  if (t.dataset.a === "avbuild") {
    try { const r = await adm("/api/admin/extended/build", { method: "POST", body: { n: parseInt($("#avbuildn").value) || 1000000 } }); toast(r.building ? "Build started in the background" : "Build not started"); }
    catch (err) { toast(err.message, true); }
  }
});
document.addEventListener("submit", (e) => {
  const f = e.target; if (!f.dataset || f.dataset.f !== "avbrand") return;
  e.preventDefault();
  const n = $("#avbn").value.trim(); if (!n) return;
  const v = Math.max(0, Math.min(1, parseFloat($("#avbv").value) || 0.05));
  avSet("brands", { ...avD().brands, [n]: { mode: $("#avbm").value, value: v } });
  $("#avbr").innerHTML = brandRows(); $("#avbn").value = "";
});
