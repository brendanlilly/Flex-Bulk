/* Flex Bulk dashboard: Brand names & shorthand. Brand names map the manufacturer a customer's line or a vendor
   export names to the manufacturers on the item list (flexbulk/brands.py); shorthand turns ERP abbreviations into
   the item list's words (flexbulk/shorthand.py). Match reports suggest both. Loaded after admin.js. */
"use strict";
PAGES.names = async (el) => {
  el.innerHTML = `<div class="ph"><div><h1>Brand names &amp; shorthand</h1><p>Lines name manufacturers the way the customer's or vendor's system does ("Mueller Industries", "Resideo (Honeywell Home)") and describe items in ERP shorthand ("MXG VLV ADJ 3W THM"). Brand names tell the matcher which manufacturers on the item list a name means, so a part # is checked against the right brand and that brand's items rank first. Shorthand is searched as words. Match reports on customer files suggest both from the lines whose part # found their item.</p></div></div><div id="nm">Loading…</div>`;
  await nmLoad();
};
async function nmLoad() {
  let d; try { d = await adm("/api/admin/names"); } catch (e) { $("#nm").textContent = e.message; return; }
  const B = d.brands, S = d.shorthand;
  const brands = await adm("/api/admin/lookup?kind=brand&q=&limit=30").catch(() => []);
  $("#nm").innerHTML = `<div class="grid g2">
    <div><div class="card"><h3>Brand names <span class="sub">${B.names.length} set</span></h3>
      <div class="xform"><label class="fl">Name on lines<input class="in" id="nmName" placeholder="e.g. Mueller Industries" maxlength="120"></label><span class="muted arr">→</span>
        <label class="fl">Manufacturer on the item list<input class="in" id="nmBrand" list="nmBrands" placeholder="e.g. Mueller" autocomplete="off"></label><datalist id="nmBrands">${brands.map((b) => `<option value="${esc(b.value)}">${num(b.n)} items</option>`).join("")}</datalist>
        <button class="btn pri" data-a="nmadd">Add</button></div>
      ${B.names.length ? `<table class="t"><tr><th>Name on lines</th><th>Means</th><th></th></tr>${B.names.map((r) => `<tr><td><b>${esc(r.name)}</b></td><td>${r.brands.map((b) => `<span class="chip">${esc(b)}</span>`).join(" ")}</td><td><button class="btn sm ghost danger" data-a="nmrm" data-n="${esc(r.name)}">Remove</button></td></tr>`).join("")}</table>`
        : `<div class="empty">None set. Names the list spells the same way, or that start the same ("Legend Valve" → Legend), work without one.</div>`}</div>
      <div class="card mt"><h3>Suggested <span class="sub">part #s that found another manufacturer's item, 2+ times</span></h3>
      ${B.suggested.length ? `<table class="t"><tr><th>Name on lines</th><th>Item list's manufacturer</th><th class="n">Lines</th><th></th></tr>${B.suggested.map((r) => `<tr><td>${esc(r.name)}</td><td>${esc(r.brand)}</td><td class="n">${num(r.n)}</td>
        <td style="white-space:nowrap"><button class="btn sm" data-a="nmok" data-n="${esc(r.name)}" data-b="${esc(r.brand)}">Same company</button> <button class="btn sm ghost" data-a="nmno" data-n="${esc(r.name)}" data-b="${esc(r.brand)}">Not the same</button></td></tr>`).join("")}</table>`
        : `<div class="empty">None yet. Run a match report on a customer's file with manufacturer and part # columns.</div>`}
      <div class="pvnote">"Not the same" is for part #s that two companies happen to share (Hydrolevel 45-026 is also a Whitehall weathervane): it stops the suggestion; the line still offers the item, flagged.</div></div></div>
    <div><div class="card"><h3>Shorthand to review <span class="sub">${S.review.length}</span></h3>
      ${S.review.length ? `<table class="t"><tr><th>Shorthand</th><th>Means</th><th class="n">Lines</th><th>Seen in</th><th></th></tr>${S.review.map((r) => `<tr><td><b>${esc(r.abbr.toUpperCase())}</b></td><td><input class="in" data-w="${esc(r.abbr)}" value="${esc(r.word)}" style="width:130px"></td><td class="n">${num(r.n)}</td><td class="small muted">${(r.examples || []).map(esc).join("<br>")}</td>
        <td style="white-space:nowrap"><button class="btn sm" data-a="shok" data-s="${esc(r.abbr)}">Use it</button> <button class="btn sm ghost" data-a="shoff" data-s="${esc(r.abbr)}">Not shorthand</button></td></tr>`).join("")}</table>`
        : `<div class="empty">Nothing to review. Learned shorthand seen on 3+ lines is used without review; the rest waits here.</div>`}</div>
      <div class="card mt"><h3>Shorthand in use</h3>
        <div class="xform"><label class="fl">Shorthand<input class="in" id="shA" placeholder="e.g. MXG" maxlength="20" style="width:110px"></label><span class="muted arr">→</span><label class="fl">Means<input class="in" id="shW" placeholder="e.g. mixing" maxlength="60"></label><button class="btn pri" data-a="shadd">Add</button></div>
        <div class="small" style="line-height:1.9">${Object.entries({ ...S.starter, ...S.on }).filter(([a]) => !S.off.includes(a)).sort().map(([a, w]) => `<span class="chip" title="${S.on[a] ? "learned or added" : "starting list"}">${esc(a.toUpperCase())} → ${esc(w)} <span class="link" data-a="shoff" data-s="${esc(a)}" title="Stop using">×</span></span>`).join(" ")}</div>
        ${S.off.length ? `<div class="pvnote">Not used: ${S.off.map((a) => `<span class="link" data-a="shon" data-s="${esc(a)}" title="Use again">${esc(a.toUpperCase())}</span>`).join(", ")}</div>` : ""}</div></div></div>`;
}
document.addEventListener("click", async (e) => {
  const t = e.target.closest("[data-a]"); if (!t || A.page !== "names") return;
  const go = async (path, body, msg) => { try { await adm(path, { method: "POST", body }); toast(msg); nmLoad(); } catch (err) { toast(err.message, true); } };
  switch (t.dataset.a) {
    case "nmadd": go("/api/admin/names/brand", { name: $("#nmName").value, brands: [$("#nmBrand").value] }, "Brand name added"); break;
    case "nmrm": go("/api/admin/names/brand/remove", { name: t.dataset.n }, "Removed"); break;
    case "nmok": go("/api/admin/names/brand", { name: t.dataset.n, brands: [t.dataset.b] }, `${t.dataset.n} → ${t.dataset.b}`); break;
    case "nmno": go("/api/admin/names/brand/dismiss", { name: t.dataset.n, brand: t.dataset.b }, "Won't suggest it again"); break;
    case "shok": { const w = $(`[data-w="${t.dataset.s}"]`); go("/api/admin/names/shorthand", { abbr: t.dataset.s, word: w ? w.value : "", action: "accept" }, "Shorthand in use"); break; }
    case "shoff": go("/api/admin/names/shorthand", { abbr: t.dataset.s, action: "off" }, "Not used"); break;
    case "shon": { const w = prompt(`What does ${t.dataset.s.toUpperCase()} mean?`); if (w) go("/api/admin/names/shorthand", { abbr: t.dataset.s, word: w, action: "accept" }, "Shorthand in use"); break; }
    case "shadd": go("/api/admin/names/shorthand", { abbr: $("#shA").value, word: $("#shW").value, action: "accept" }, "Shorthand added"); break;
  }
});
