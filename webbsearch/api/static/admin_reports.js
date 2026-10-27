/* Flex Bulk dashboard: Match reports. Lines from saved orders, the app's log or a test file are matched with today's
   settings and written to one Excel workbook (flexbulk/report.py): how often the item staff confirmed comes first,
   what wasn't found, which brands aren't carried, what changed. Loaded after admin.js. */
"use strict";
const RPT = { hide: false, source: "tests", days: 90, branch: "", perCust: false, file: null };
PAGES.reports = async (el) => {
  el.innerHTML = `<div class="ph"><div><h1>Match reports</h1><p>Match a set of lines again with today's settings and get one Excel workbook: every line with its top matches and their reasons, where the item it should have matched ranked, what wasn't found, which brands aren't on the list, and what changed since staff saw it. Run one, change a setting, run it again: the Summary sheet compares the two. The workbook is also the thing to hand to Claude when working on the matching.</p></div></div>
    <div class="grid g12"><div>
      <div class="card"><h3>New report</h3>
        <div class="row" style="gap:14px;align-items:flex-end">
          <label class="fl">Lines from<select class="in" id="rpSrc">
            <option value="tests" ${RPT.source === "tests" ? "selected" : ""}>Test cases (customers' documents read again, confirmed items are the answers)</option>
            <option value="documents" ${RPT.source === "documents" ? "selected" : ""}>Customers' documents (read and matched; no answers)</option>
            <option value="orders" ${RPT.source === "orders" ? "selected" : ""}>Saved orders (confirmed lines are the answers)</option>
            <option value="log" ${RPT.source === "log" ? "selected" : ""}>Lines matched in the app (staff choices are the answers)</option>
            <option value="file" ${RPT.source === "file" ? "selected" : ""}>A test file (lines, and the item each should match)</option></select></label>
          <label class="fl" id="rpDocsL" ${RPT.source === "documents" ? "" : "hidden"}>Documents<input type="file" class="in" id="rpDocs" multiple accept=".xlsx,.xlsm,.csv,.tsv,.txt,.pdf,.docx,.eml,.jpg,.jpeg,.png,.heic,.heif,.tif,.tiff,.webp,image/*" style="height:auto;padding:5px"></label>
          <label class="fl" id="rpDaysL" ${["orders", "log"].includes(RPT.source) ? "" : "hidden"}>Last<select class="in" id="rpDays">${[7, 30, 90, 365, 3650].map((d) => `<option value="${d}" ${RPT.days === d ? "selected" : ""}>${d === 3650 ? "all time" : d + " days"}</option>`).join("")}</select></label>
          <label class="fl" id="rpFileL" ${RPT.source === "file" ? "" : "hidden"}>Test file<input type="file" class="in" id="rpFile" accept=".csv,.txt,.tsv,.xlsx,.parquet" style="height:auto;padding:5px"></label>
          <label class="fl">Pickup branch<select class="in" id="rpBranch"><option value="">Default</option>${A.branches.map((b) => `<option value="${esc(b.branch_id)}" ${RPT.branch === b.branch_id ? "selected" : ""}>${esc(b.name)}</option>`).join("")}</select></label>
          <label class="row small" style="gap:6px"><input type="checkbox" id="rpCust" ${RPT.perCust ? "checked" : ""}>Count each customer's own picks</label>
          <label class="row small" style="gap:6px" title="Lines whose manufacturer and part # find their item become test cases: the part # is taken off and the description has to find the item"><input type="checkbox" id="rpHide" ${RPT.hide ? "checked" : ""}>Hide part #s (score the descriptions)</label>
          <button class="btn pri" data-a="rprun">Run report</button></div>
        <div id="rpJob" class="mt small"></div>
        <div class="pvnote"><b>Test cases</b> are orders saved in the staff app with <b>Test case</b> ticked: their documents are read again (so reading is checked too) and each confirmed item is the right answer. <b>Bundle</b> downloads the workbook with the matching code, the settings and the documents, everything needed to work on the matching from the report. A test file needs a column of lines (<b>Line</b>, Requested or Description) and, to be scored, the item # each should match (<b>Expected</b>). Quantity and unit are optional. <span class="link" data-a="rptemplate">Download a template</span>. Saved orders score every confirmed line against the item staff confirmed; lines matched in the app score the ones staff made a choice on.</div></div>
</div>
      <div><div class="card"><h3>Reading a report</h3><div class="small" style="line-height:1.6"><b>Top-1</b>: of lines with a known answer, the share where it came first. <b>Top-3</b>: in the first three. <b>Matched</b>: lines with any match.<br><br><b>Misses</b> is the sheet to work from: each row is a line, the item it should have matched, what came first instead and why. The usual fixes, cheapest first: a dictionary entry (the line's words), a cross (a part # or brand Webb doesn't carry), a brand order for the category, then a weight.</div></div></div></div>
    <div class="card mt"><h3>Reports</h3><div id="rpList">Loading…</div></div>`;
  $("#rpSrc").onchange = (e) => { RPT.source = e.target.value; $("#rpDaysL").hidden = !["orders", "log"].includes(RPT.source); $("#rpFileL").hidden = RPT.source !== "file"; $("#rpDocsL").hidden = RPT.source !== "documents"; };
  $("#rpDays").onchange = (e) => { RPT.days = +e.target.value; };
  $("#rpBranch").onchange = (e) => { RPT.branch = e.target.value; };
  $("#rpCust").onchange = (e) => { RPT.perCust = e.target.checked; };
  $("#rpHide").onchange = (e) => { RPT.hide = e.target.checked; };
  await rpLoad();
};
async function rpLoad() {
  let d; try { d = await adm("/api/admin/reports"); } catch (e) { if ($("#rpList")) $("#rpList").textContent = e.message; return; }
  const j = d.job || {}, el = $("#rpJob");
  if (el) el.innerHTML = j.running ? `<b>Running…</b> ${j.total ? `${num(j.done)} of ${num(j.total)} lines` : "collecting lines"}`
    : j.error ? `<span class="chip red">Didn't run</span> ${esc(j.error)}` : j.result ? `<span class="chip ok">Done</span> ${esc(j.result.meta.id)}: ${num(j.result.summary.lines)} lines in ${j.result.meta.secs}s${j.result.summary.top1 != null ? `, top-1 ${pct(j.result.summary.top1, 1)}` : ""}` : "";
  const L = $("#rpList");
  if (L) L.innerHTML = d.reports.length ? `<table class="t"><tr><th>Report</th><th>When</th><th>Lines from</th><th class="n">Lines</th><th class="n">With answer</th><th class="n">Top-1</th><th class="n">Top-3</th><th class="n">Matched</th><th></th></tr>
    ${d.reports.map((r) => `<tr><td class="num" style="white-space:nowrap">${esc(r.id)}</td><td class="muted small" style="white-space:nowrap">${esc(r.at)}</td><td class="small">${esc(r.source)}</td><td class="n">${num(r.lines)}</td><td class="n">${num(r.with_expected)}</td>
      <td class="n"><b>${pct(r.top1, 1)}</b></td><td class="n">${pct(r.top3, 1)}</td><td class="n">${pct(r.matched_rate, 1)}</td><td style="white-space:nowrap"><button class="btn sm" data-a="rpget" data-id="${esc(r.id)}" data-x="xlsx" title="The Excel workbook">Excel</button> <button class="btn sm" data-a="rpget" data-id="${esc(r.id)}" data-x="zip" title="The workbook, the matching and page code, the settings and the documents: everything to work on the matching from this report">Bundle</button></td></tr>`).join("")}</table>`
    : `<div class="empty"><b>No reports yet.</b>Run one above.</div>`;
  if (j.running && !A.rpPoll) {
    A.rpPoll = setInterval(async () => { const s = await adm("/api/admin/reports").catch(() => null); if (!s || !s.job.running) { clearInterval(A.rpPoll); A.rpPoll = null; } if (A.page === "reports") rpLoad(); }, 1500);
  }
}
const rpB64 = (f) => new Promise((ok, bad) => { const r = new FileReader(); r.onload = () => ok(String(r.result)); r.onerror = bad; r.readAsDataURL(f); });
document.addEventListener("click", async (e) => {
  const t = e.target.closest("[data-a]"); if (!t) return;
  if (t.dataset.a === "rprun") {
    const body = { source: RPT.source, days: RPT.days, branch: RPT.branch, per_customer: RPT.perCust, hide: RPT.hide };
    if (RPT.source === "documents") {
      const fs = [...$("#rpDocs").files]; if (!fs.length) { toast("Choose one or more documents", true); return; }
      if (fs.some((f) => f.size > 15e6)) { toast("Each document must be under 15 MB", true); return; }
      body.files = await Promise.all(fs.map(async (f) => ({ name: f.name, b64: await rpB64(f) })));
    }
    if (RPT.source === "file") {
      const f = $("#rpFile").files[0]; if (!f) { toast("Choose a test file", true); return; }
      body.file_name = f.name; body.file_b64 = await rpB64(f);
    }
    try { await adm("/api/admin/reports", { method: "POST", body }); toast("Report started"); rpLoad(); } catch (err) { toast(err.message, true); }
  } else if (t.dataset.a === "rpget") {
    try {
      const x = t.dataset.x || "xlsx";
      const r = await fetch(`/api/admin/reports/${encodeURIComponent(t.dataset.id)}.${x}`, { headers: { "X-Admin-Token": TOKEN } });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const a = document.createElement("a"); a.href = URL.createObjectURL(await r.blob()); a.download = `flex-bulk-match-report-${t.dataset.id}.${x}`; a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 2000);
    } catch (err) { toast(err.message, true); }
  } else if (t.dataset.a === "rptemplate") {
    const csv = "Line,Expected,Qty\n3/4 ball valve sweat,,12\n1/2 copper 90 elbow,,40\nWebstone 51414,611304,2\n";
    const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" })); a.download = "match-test-template.csv"; a.click();
  }
});
