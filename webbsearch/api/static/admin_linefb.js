/* Flex Bulk dashboard: Line feedback. What staff said about matches, line by line, in the staff app (right / wrong,
   what was wrong, a note, the right item), each kept with the matcher's steps at the time (flexbulk/feedback.py).
   Loaded after admin.js. */
"use strict";
const LFB = { status: "open", verdict: "bad", q: "", open: null };
PAGES.linefb = async (el) => {
  el.innerHTML = `<div class="ph"><div><h1>Line feedback</h1><p>What staff said about matches in the staff app, line by line: right or wrong, what was wrong, a note, and the right item when they knew it. Each is kept with the matcher's steps at that moment. A match report on <b>staff feedback</b> matches every line again and says which are fixed now.</p></div>
    <div class="acts"><button class="btn" data-a="lfbcsv">Download CSV</button> <button class="btn pri" data-a="lfbreport">Match report on feedback</button></div></div>
    <div class="xform" style="margin-bottom:12px">
      <label class="fl">Status<select class="in" id="lfbS"><option value="open">Open</option><option value="resolved">Resolved</option><option value="wontfix">Won't fix</option><option value="">All</option></select></label>
      <label class="fl">Verdict<select class="in" id="lfbV"><option value="bad">Marked wrong</option><option value="good">Marked right</option><option value="">Both</option></select></label>
      <label class="fl">Search<input class="in" id="lfbQ" placeholder="line, note, staff, customer, item #" value="${esc(LFB.q)}"></label></div>
    <div id="lfb">Loading…</div>`;
  $("#lfbS").value = LFB.status; $("#lfbV").value = LFB.verdict;
  $("#lfbS").onchange = (e) => { LFB.status = e.target.value; lfbLoad(); };
  $("#lfbV").onchange = (e) => { LFB.verdict = e.target.value; lfbLoad(); };
  let t; $("#lfbQ").oninput = (e) => { clearTimeout(t); t = setTimeout(() => { LFB.q = e.target.value.trim(); lfbLoad(); }, 300); };
  await lfbLoad();
};
async function lfbLoad() {
  let d; try { d = await adm(`/api/admin/line-feedback?status=${encodeURIComponent(LFB.status)}&verdict=${encodeURIComponent(LFB.verdict)}&q=${encodeURIComponent(LFB.q)}`); }
  catch (e) { $("#lfb").textContent = e.message; return; }
  const c = d.counts, R = Object.entries(c.by_reason);
  const day = (ts) => new Date(ts * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
  $("#lfb").innerHTML = `<div class="kpis" style="margin-bottom:12px">
      <div class="kpi"><small>Feedback</small><b>${num(c.total)}</b></div>
      <div class="kpi"><small>Open</small><b>${num(c.open)}</b></div>
      <div class="kpi"><small>Marked wrong</small><b>${num(c.bad || 0)}</b></div>
      <div class="kpi"><small>Marked right</small><b>${num(c.good || 0)}</b></div>
      <div class="kpi"><small>With the right item</small><b>${num(c.with_right_item)}</b></div></div>
    ${R.length ? `<div class="card" style="margin-bottom:12px"><h3>What was wrong</h3><div class="small" style="line-height:2">${R.map(([k, n]) => `<span class="chip">${esc(k)} · ${num(n)}</span>`).join(" ")}</div></div>` : ""}
    <div class="card">${d.items.length ? `<table class="t"><tr><th>Date</th><th>Line</th><th>Shown</th><th>What was wrong</th><th>Right item</th><th>Staff</th><th></th></tr>
      ${d.items.map((f) => `<tr><td class="small" style="white-space:nowrap">${day(f.created)}</td>
        <td><b>${esc((f.line || {}).text || "")}</b>${(f.line || {}).desc && f.line.desc !== f.line.text ? `<div class="small muted">${esc(f.line.desc)}</div>` : ""}${f.note ? `<div class="small" style="margin-top:3px">“${esc(f.note)}”</div>` : ""}</td>
        <td class="small">${(f.shown || {}).sku ? `${esc(f.shown.title)}<div class="muted">${esc(f.shown.sku)}${f.shown.via ? " · " + esc(f.shown.via) : ""}</div>` : `<span class="muted">${esc((f.shown || {}).note || "no match")}</span>`}</td>
        <td class="small">${f.verdict === "good" ? `<span class="chip ok">right</span>` : (f.reason_labels || []).map((x) => `<span class="chip">${esc(x)}</span>`).join(" ") || `<span class="chip">wrong</span>`}</td>
        <td class="small">${f.right_sku ? `${esc(f.right_title)}<div class="muted">${esc(f.right_sku)}</div>` : ""}</td>
        <td class="small">${esc(f.staff || "")}${f.customer ? `<div class="muted">${esc(f.customer)}</div>` : ""}</td>
        <td style="white-space:nowrap"><button class="btn sm" data-a="lfbsteps" data-id="${esc(f.id)}">Steps</button>
          ${f.status === "open" ? `<button class="btn sm" data-a="lfbres" data-id="${esc(f.id)}" data-s="resolved">Resolved</button> <button class="btn sm ghost" data-a="lfbres" data-id="${esc(f.id)}" data-s="wontfix">Won't fix</button>`
            : `<span class="small muted" title="${esc(f.resolution || "")}">${f.status === "wontfix" ? "won't fix" : "resolved"}</span> <button class="btn sm ghost" data-a="lfbres" data-id="${esc(f.id)}" data-s="open">Reopen</button>`}</td></tr>
        ${LFB.open === f.id ? `<tr><td></td><td colspan="6" id="lfbSteps-${esc(f.id)}" class="small muted">Loading…</td></tr>` : ""}`).join("")}</table>`
      : `<div class="empty">No feedback here yet. Staff mark lines ✓ or ✗ in the staff app, beside each match.</div>`}</div>`;
  if (LFB.open) lfbSteps(LFB.open);
}
async function lfbSteps(id) {
  const el = $(`#lfbSteps-${CSS.escape(id)}`); if (!el) return;
  try {
    const f = await adm(`/api/admin/line-feedback/${encodeURIComponent(id)}`);
    const tr = f.trace || [], rk = tr.find((t) => t.step === "ranked");
    el.innerHTML = `<ol style="margin:0 0 8px;padding-left:18px">${tr.filter((t) => t.step !== "ranked").map((t) => `<li><b>${esc(t.step)}</b> ${esc(JSON.stringify(Object.fromEntries(Object.entries(t).filter(([k]) => k !== "step"))).slice(0, 400))}</li>`).join("")}</ol>
      ${rk ? `<table class="t"><tr><th>#</th><th>Item</th><th>Product</th><th>Brand</th><th>Size</th><th>Spec</th><th class="n">Total</th></tr>${rk.candidates.slice(0, 10).map((c) => `<tr${c.sku === f.right_sku ? ' style="font-weight:600"' : ""}><td>${c.rank}</td><td>${esc(c.title)} <span class="muted">${esc(c.sku)}${c.sku === f.right_sku ? " · the right item" : c.sku === (f.shown || {}).sku ? " · shown" : ""}</span></td><td>${c.product ? "✓" : "✗"}</td><td>${c.brand_ok === false ? "✗" : "✓"}</td><td>${c.size ? "✓" : "✗"}</td><td>${c.spec_ok ? "✓" : "✗"}</td><td class="n">${c.total}</td></tr>`).join("")}</table>` : ""}
      ${f.right_sku && rk && !rk.candidates.some((c) => c.sku === f.right_sku) ? `<div style="margin-top:6px">The right item wasn't among the candidates: the search didn't find it.</div>` : ""}`;
  } catch (e) { el.textContent = e.message; }
}
document.addEventListener("click", async (e) => {
  const t = e.target.closest("[data-a]"); if (!t || A.page !== "linefb") return;
  switch (t.dataset.a) {
    case "lfbsteps": LFB.open = LFB.open === t.dataset.id ? null : t.dataset.id; lfbLoad(); break;
    case "lfbres": {
      const note = t.dataset.s === "open" ? "" : (prompt(t.dataset.s === "resolved" ? "What fixed it? (optional)" : "Why not? (optional)") ?? null);
      if (note === null) break;
      try { await adm(`/api/admin/line-feedback/${encodeURIComponent(t.dataset.id)}`, { method: "POST", body: { status: t.dataset.s, resolution: note } }); toast("Saved"); lfbLoad(); } catch (err) { toast(err.message, true); }
      break;
    }
    case "lfbcsv": {
      const r = await fetch(`/api/admin/line-feedback.csv?status=${encodeURIComponent(LFB.status)}&verdict=${encodeURIComponent(LFB.verdict)}&q=${encodeURIComponent(LFB.q)}`, { headers: { "X-Admin-Token": TOKEN } });
      if (!r.ok) { toast("Couldn't download", true); break; }
      const a = document.createElement("a"); a.href = URL.createObjectURL(await r.blob()); a.download = "flex-bulk-line-feedback.csv"; a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 2000); break;
    }
    case "lfbreport":
      try { await adm("/api/admin/reports", { method: "POST", body: { source: "feedback", days: 3650 } }); toast("Report started: see Match reports"); go("reports"); }
      catch (err) { toast(err.message, true); }
      break;
  }
});
