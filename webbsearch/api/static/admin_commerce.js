/* v2.2: Commerce (from search or question to order) and Connections (analytics tools, checkout, the export).
   Loaded after admin.js; uses its helpers (adm, esc, num, pct, kpi, chart, hbars, periodSeg, PAGES, A). */
const cmUsd = (v) => (v == null ? "—" : "$" + Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
const SRC_NAME = { search: "Search results", compare: "Compare", ask: "Ask Webb", knowledge: "Knowledge Center", kc_code: "Error-code lookup",
  kc_diagram: "Parts diagram", product_page: "Product page", quick_view: "Quick view", home: "Start page", recs: "Recommendations",
  bulk: "Bulk order", kit: "Job kits", xref: "Cross-reference", reorder: "Reorder", list: "Purchase lists", advisor: "HVAC advisor",
  account: "Account pages", photo: "Photo search", cart: "Cart", other: "Other" };

function funnelHTML(stages) {
  const top = Math.max(1, stages[0] ? stages[0][1] : 1);
  return `<div class="funnel">${stages.map(([label, n, note], k) => {
    const prev = k ? stages[k - 1][1] : null;
    return `<div class="fstage"><span class="fl">${esc(label)}</span><span class="fb"><i style="width:${Math.max(1.5, 100 * (n || 0) / top).toFixed(1)}%"></i></span>
      <span class="fv"><b>${typeof n === "number" ? num(n) : esc(String(n))}</b>${k && prev ? `<small>${pct((n || 0) / prev, 1)} of the step above</small>` : note ? `<small>${esc(note)}</small>` : ""}</span></div>`;
  }).join("")}</div>`;
}

PAGES.commerce = async (el) => {
  el.innerHTML = `<div class="ph"><div><h1>Commerce</h1><p>From a search or a Knowledge Center question to an order. Every add to cart records where it came from (search results, the Knowledge Center, Ask Webb, compare, Bulk order, kits, the product page); the cart carries that to checkout, and the order the storefront reports back credits revenue to it. Revenue is the order's line value, credited to the place each item was first added from.</p></div><span class="sp"></span>${periodSeg()}</div><div id="cm">Loading…</div>`;
  let d;
  try { d = await adm(`/api/admin/commerce?days=${A.days}`); } catch (err) { $("#cm").innerHTML = `<div class="explain">${esc(err.message)}</div>`; return; }
  if (A.page !== "commerce") return;
  if (d.off) { $("#cm").innerHTML = `<div class="explain">Analytics are off on this server (WEBB_ANALYTICS=0).</div>`; return; }
  const f = d.flexsearch, k = d.knowledge, c = d.cart;
  const srcRows = (d.sources || []).map((s) => `<tr><td>${esc(SRC_NAME[s.source] || s.source)}</td><td class="n">${num(s.adds)}</td><td class="n">${cmUsd(s.add_value)}</td><td class="n">${num(s.lines_bought)}</td><td class="n"><b>${cmUsd(s.revenue)}</b></td></tr>`).join("");
  const kinds = Object.entries(k.by_kind || {}).sort((a, b) => b[1] - a[1]).map(([n, v]) => ({ name: ({ answer: "Answered from documents", error_code: "Error code", serial: "Serial number", declined: "Declined (safety)", emergency: "Emergency", webb_only: "Competitor named", out_of_scope: "Off topic", pricing: "Pricing (to branch)", complaint: "Complaint", legal: "Injury or damage", where_to_buy: "Where to buy" })[n] || n, n: v }));
  const flags = Object.entries(k.flags || {}).sort((a, b) => b[1] - a[1]).slice(0, 10).map(([n, v]) => ({ name: n.replace(/_/g, " "), n: v }));
  $("#cm").innerHTML = `
    <div class="kpis">${kpi("Revenue", cmUsd(c.revenue))}${kpi("Orders", num(c.orders))}${kpi("Avg order", cmUsd(c.avg_order))}${kpi("Checkouts", num(c.checkouts))}
      ${kpi("Checkouts not ordered", pct(c.abandon_rate, 0))}${kpi("Adds to cart", num(c.adds))}</div>
    <div class="grid g2 mt">
      <div class="card"><h3>FlexSearch <span class="sub">search to order</span></h3>
        ${funnelHTML([["Searches", f.searches], ["Clicked a result", f.with_click], ["Viewed a product", f.with_view], ["Added to cart", f.with_add], ["Ordered", f.orders]])}
        <div class="kpis sm mt">${kpi("Click-through", pct(f.click_rate, 1))}${kpi("Add-to-cart rate", pct(f.add_rate, 1))}${kpi("Search to order", pct(f.conversion, 2))}${kpi("Revenue", cmUsd(f.revenue))}${kpi("Revenue per search", cmUsd(f.revenue_per_search))}${kpi("No results", num(f.zero))}</div></div>
      <div class="card"><h3>Knowledge Center <span class="sub">question to order</span></h3>
        ${funnelHTML([["Questions", k.questions], ["Answered", k.answered], ["Opened a cited page", k.source_opens], ["Added a part", k.adds], ["Ordered", k.orders]])}
        <div class="kpis sm mt">${kpi("Answered", pct(k.answer_rate, 0))}${kpi("FlexIQ answers", pct(k.flexiq_share, 0))}${kpi("Parts per question", k.adds_per_question == null ? "—" : k.adds_per_question.toFixed(2))}${kpi("Revenue", cmUsd(k.revenue))}${kpi("Expert hand-offs", num(k.experts))}${kpi("Submittals", num(k.submittals))}</div>
        <div class="muted small mt">Error codes ${num(k.codes)} · serials ${num(k.serials)} · diagrams ${num(k.diagrams)} · documents opened ${num(k.doc_opens)} · thumbs ${num((k.feedback || {}).up || 0)} up, ${num((k.feedback || {}).down || 0)} down</div></div>
    </div>
    <div class="card mt"><h3>Per day <span class="sp"></span><span class="legend"><span><i style="background:var(--bar)"></i>adds to cart</span><span><i style="background:var(--webb-red)"></i>orders per checkout</span></span></h3>
      ${chart((d.series || []).map((x) => ({ ...x, rate: x.checkouts ? Math.min(1, x.orders / x.checkouts) : 0 })), "day", "adds", "rate", { label: "Adds to cart per day" })}</div>
    <div class="grid g2 mt">
      <div class="card"><h3>Where adds and revenue come from</h3>${srcRows ? `<table class="t"><tr><th>Source</th><th class="n">Adds</th><th class="n">Value added</th><th class="n">Lines ordered</th><th class="n">Revenue</th></tr>${srcRows}</table>` : `<div class="empty"><b>No adds yet.</b>Adds to cart from anywhere on the storefront appear here with where they came from.</div>`}</div>
      <div class="card"><h3>Searches that sell <span class="sub">revenue · adds</span></h3>${(f.top_queries || []).length ? `<table class="t"><tr><th>Search</th><th class="n">Searches</th><th class="n">Adds</th><th class="n">Orders</th><th class="n">Revenue</th></tr>${f.top_queries.map((q) => `<tr class="click" data-a="query" data-q="${esc(q.q)}"><td class="q clip">${esc(q.q)}</td><td class="n">${num(q.searches)}</td><td class="n">${num(q.adds)}</td><td class="n">${num(q.orders)}</td><td class="n"><b>${cmUsd(q.revenue)}</b></td></tr>`).join("")}</table>` : `<div class="empty">No searches with adds or orders yet.</div>`}</div>
    </div>
    <div class="grid g2 mt">
      <div class="card"><h3>Knowledge Center questions <span class="sub">what they were</span></h3>${kinds.length ? hbars(kinds, k.questions) : `<div class="empty">No questions yet.</div>`}</div>
      <div class="card"><h3>Guardrail flags <span class="sub">questions and answers</span><span class="sp"></span><span class="link small" data-a="go" data-p="knowledge">Review →</span></h3>${flags.length ? hbars(flags, k.questions, true) : `<div class="empty">No flags yet.</div>`}</div>
    </div>`;
};

PAGES.connect = async (el) => {
  el.innerHTML = `<div class="ph"><div><h1>Connections</h1><p>Where FlexSearch's events and orders go besides this dashboard. Each tool is off until its keys are set on the server (environment variables, never in the browser). Third parties get item numbers, quantities, values and where an add came from, with sessions and accounts hashed; never IP addresses, names, e-mail or Knowledge Center question text.</p></div></div><div id="cn">Loading…</div>`;
  const d = await adm("/api/admin/connectors");
  if (A.page !== "connect") return;
  const NAMES = { ga4: ["Google Analytics 4", "Server-side (Measurement Protocol), GA4 e-commerce events: search, select_item, view_item, add_to_cart, remove_from_cart, begin_checkout, purchase; Knowledge Center events as kc_*. The page also pushes them to window.dataLayer when the storefront has Google Tag Manager.", "WEBB_GA4_MEASUREMENT_ID, WEBB_GA4_API_SECRET"],
    datadog: ["Datadog", "Event counts by kind and source as the metric webb.flexsearch.events, for dashboards and alerts (for example: Knowledge Center emergencies, checkout errors).", "WEBB_DATADOG_API_KEY, WEBB_DATADOG_SITE"],
    webhook: ["Webhook", "JSON batches to any URL (an Azure Function, a queue, Segment's HTTP source), signed with HMAC-SHA256 in X-Webb-Signature.", "WEBB_EVENTS_WEBHOOK_URL, WEBB_EVENTS_WEBHOOK_SECRET"] };
  const rows = d.connectors.map((c) => `<tr><td><b>${esc(NAMES[c.name][0])}</b><div class="muted small">${esc(NAMES[c.name][1])}</div></td>
    <td>${c.enabled ? `<span class="chip ok">On</span>` : `<span class="chip">Off</span>`}<div class="muted small">${esc(c.measurement_id || c.site || c.url || "")}</div></td>
    <td class="n">${num(c.sent || 0)}</td><td class="n">${num(c.pending || 0)}</td><td class="small">${c.error ? `<span class="chip red">${esc(c.error)}</span>` : c.at ? esc(new Date(c.at * 1000).toLocaleString()) : "—"}</td>
    <td class="small mono">${esc(NAMES[c.name][2])}</td></tr>`).join("");
  const co = d.checkout;
  $("#cn").innerHTML = `
    <div class="card"><h3>Analytics tools <span class="sp"></span><button class="btn sm" data-a="cnflush">Send queued events now</button></h3>
      <table class="t"><tr><th>Tool</th><th>Status</th><th class="n">Sent</th><th class="n">Queued</th><th>Last send</th><th>Server settings</th></tr>${rows}</table></div>
    <div class="grid g2 mt">
      <div class="card"><h3>Checkout</h3>
        <p class="small">The cart's Checkout button sends the cart here; the server prices it for the signed-in account, keeps where each line came from, and hands it to the storefront's checkout with a signed token (JWT, HS256, 30 minutes). The storefront reports each order to <span class="mono">${esc(co.webhook)}</span>, signed with the same secret in X-Webb-Signature; that's what credits revenue on the Commerce page.</p>
        <table class="t"><tr><td>Storefront checkout URL</td><td>${co.url ? `<span class="mono">${esc(co.url)}</span>` : `<span class="chip">Not set: the demo checkout is used</span>`}<div class="muted small mono">WEBB_CHECKOUT_URL</div></td></tr>
          <tr><td>Shared secret</td><td>${co.secret_set ? `<span class="chip ok">Set</span>` : `<span class="chip warn">Not set: no token, webhook refused</span>`}<div class="muted small mono">WEBB_CHECKOUT_SECRET</div></td></tr></table></div>
      <div class="card"><h3>Snowflake</h3>
        <p class="small">Each table exports as NDJSON (one JSON object per line). Stage the file and <span class="mono">COPY INTO</span>, or point Snowpipe at the stage; <span class="mono">since</span> (a Unix time) takes only newer rows. Sign-ins (IP addresses) are never exported.</p>
        <div class="row" style="flex-wrap:wrap;gap:6px">${d.export.map((t) => `<button class="btn sm" data-a="cnexport" data-t="${esc(t)}">${esc(t)}.ndjson</button>`).join("")}</div></div>
    </div>`;
};

document.addEventListener("click", async (e) => {
  const t = e.target.closest("[data-a]"); if (!t) return;
  if (t.dataset.a === "cnflush") {
    try { const r = await adm("/api/admin/connectors/flush", { method: "POST" }); toast(r.off ? "No tool is configured" : `Sent ${num(r.sent)}; ${num(r.pending)} queued${r.errors.length ? "; " + r.errors[0] : ""}`, !!r.errors.length); PAGES.connect($("#page")); } catch (err) { toast(err.message, true); }
  }
  if (t.dataset.a === "cnexport") {
    try {
      const r = await fetch(`/api/admin/analytics/export?table=${encodeURIComponent(t.dataset.t)}`, { headers: { "X-Admin-Token": TOKEN } });
      if (!r.ok) throw new Error(r.status);
      const b = await r.blob(), u = URL.createObjectURL(b), a = document.createElement("a");
      a.href = u; a.download = `flexsearch-${t.dataset.t}.ndjson`; document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(u), 2000);
    } catch (err) { toast("Export failed: " + err.message, true); }
  }
});
