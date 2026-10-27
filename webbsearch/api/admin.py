"""Merchandising & analytics dashboard for Webb employees: /admin and /api/admin/*.

What it changes applies to every search, so access is narrow:
  * served only to this computer (loopback), like demo impersonation;
  * every /api/admin call needs the X-Admin-Token header. The token is random per server start and written
    into the /admin page, so another web page open in the same browser can't call the API (a custom header
    can't be sent cross-site without CORS, which this server doesn't allow);
  * WEBB_ADMIN=0 turns the dashboard off. On a shared server it would sit behind employee sign-in (SSO)
    with a merchandiser role; that isn't built in the prototype.

Analytics (searches, clicks, add-to-cart, Bulk order lines, Ask questions) are written to
data/merch/analytics.sqlite unless WEBB_ANALYTICS=0.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import secrets
import time
from collections import Counter
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

from webbsearch.merch.store import Conflict, LIMITS, norm_q

STATIC = Path(__file__).parent / "static"
ENABLED = os.environ.get("WEBB_ADMIN", "1") == "1"
TOKEN = os.environ.get("WEBB_ADMIN_TOKEN") or secrets.token_urlsafe(24)
LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}
router = APIRouter()

_an = {"db": None, "history": None, "audit": None}


def engine():
    from webbsearch.api.server import engine as _e
    return _e()


def analytics():
    """The analytics store, or None when WEBB_ANALYTICS=0."""
    if os.environ.get("WEBB_ANALYTICS", "1") != "1":
        return None
    if _an["db"] is None:
        from webbsearch.config import settings
        from webbsearch.merch.analytics import Analytics
        _an["db"] = Analytics(os.environ.get("WEBB_ANALYTICS_DB") or settings.data_dir / "merch" / "analytics.sqlite")
        try:                                 # GA4, Datadog, a webhook: on when configured (v2.2)
            from webbsearch.merch.connectors import Forwarder, config_from_env, start_background
            f = Forwarder(_an["db"], config_from_env())
            if f.enabled:
                _an["db"].forwarder = f
                start_background(f)
        except Exception:
            pass
    return _an["db"]


def history():
    if _an["history"] is None:
        from webbsearch.config import settings
        from webbsearch.merch.history import Audit, History
        e = engine()
        _an["history"] = History(e, settings.canonical_dir / "search_log.parquet")
        _an["audit"] = Audit(e, _an["history"], e.merch.root / "audit.json")
    return _an["history"]


def audit():
    history()
    return _an["audit"]


def _local(request: Request) -> bool:
    """This computer, or (on the public demo) a visitor past the site and admin logins (api/gate.py)."""
    from webbsearch.api.gate import admin_ok
    return admin_ok(request)


def require(request: Request) -> None:
    if not ENABLED:
        raise HTTPException(404, "the dashboard is turned off (WEBB_ADMIN=0)")
    if not _local(request):
        raise HTTPException(403, "the dashboard is only served to this computer")
    if not secrets.compare_digest(request.headers.get("x-admin-token", ""), TOKEN):
        raise HTTPException(403, "missing or stale dashboard token: reload /admin")


# ------------------------------------------------------------------ page
@router.get("/admin", include_in_schema=False)
def admin_page(request: Request):
    if not ENABLED:
        raise HTTPException(404, "the dashboard is turned off (WEBB_ADMIN=0)")
    if not _local(request):
        from webbsearch.api.gate import admin_password
        if admin_password():
            from fastapi.responses import RedirectResponse
            return RedirectResponse("/login?admin=1&next=/admin", 303)
        return HTMLResponse("<p>The FlexSearch dashboard is only available on the computer running the server.</p>", 403)
    from webbsearch.api.assets import versioned
    html = versioned((STATIC / "admin.html").read_text(), STATIC).replace("__ADMIN_TOKEN__", TOKEN)
    from webbsearch.api.gate import is_local
    if not is_local(request):            # the public demo: reached through both sign-ins, not from this computer
        html = html.replace(">This computer only</span>", ">Demo · signed in</span>")
    return HTMLResponse(html, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY",
                                       "Referrer-Policy": "no-referrer"})


# ------------------------------------------------------------------ settings
class SaveBody(BaseModel):
    patch: dict
    note: str = ""
    who: str = ""
    base_version: int | None = None


class WhoBody(BaseModel):
    who: str = ""


@router.get("/api/admin/settings")
def get_settings(request: Request):
    require(request)
    m = engine().merch
    a = analytics()
    from webbsearch.merch.store import (FACET_LABELS, FIELD_HELP, MINIMAL_LABELS, RESULTS_MODES, ROLES, SEGMENTS,
                                        STATUS_VALUES, STOCK_VALUES)
    return {"settings": m.get(), "defaults": m.defaults(), "limits": LIMITS, "analytics": a.counts() if a else None,
            "history": history().ok, "meta": {"segments": SEGMENTS, "roles": ROLES, "facets": FACET_LABELS,
                                              "fields": FIELD_HELP, "stock_values": STOCK_VALUES,
                                              "status_values": list(STATUS_VALUES),
                                              "minimal_rail": MINIMAL_LABELS, "results_modes": list(RESULTS_MODES)}}


@router.get("/api/admin/relevance/fields")
def relevance_fields(request: Request):
    """v2.44: the fields and the catalog's attributes the Relevance page weights (how many items have each, examples)."""
    require(request)
    from webbsearch.index.local_backend import FIELD_BOOSTS
    from webbsearch.retrieval.fieldrel import FIELD_HELP as RF_HELP, FIELD_LABELS, POST_FIELDS
    fr = getattr(engine(), "fieldrel", None)
    return {"fields": [{"key": f, "label": FIELD_LABELS[f], "help": RF_HELP[f], "builtin": FIELD_BOOSTS[f],
                        "final_order": f in POST_FIELDS} for f in FIELD_LABELS],
            "attrs": fr.keys() if fr is not None else []}


@router.put("/api/admin/settings")
def save_settings(request: Request, body: SaveBody):
    require(request)
    try:
        s = engine().merch.update(body.patch, who=body.who, note=body.note, base_version=body.base_version)
    except Conflict as ex:
        raise HTTPException(409, str(ex))
    except (ValueError, TypeError, KeyError) as ex:
        raise HTTPException(422, str(ex))
    return {"settings": s}


@router.post("/api/admin/reset/{section}")
def reset(request: Request, section: str, body: WhoBody):
    require(request)
    try:
        return {"settings": engine().merch.reset(section, who=body.who)}
    except ValueError as ex:
        raise HTTPException(422, str(ex))


@router.post("/api/admin/revert/{version}")
def revert(request: Request, version: int, body: WhoBody):
    require(request)
    try:
        return {"settings": engine().merch.revert(version, who=body.who)}
    except ValueError as ex:
        raise HTTPException(404, str(ex))


@router.get("/api/admin/changes")
def changes(request: Request, limit: int = 100):
    require(request)
    return engine().merch.changes(max(1, min(limit, 500)))


# ------------------------------------------------------------------ preview: live vs draft
class PreviewBody(BaseModel):
    q: str
    account: str | None = None
    user: str | None = None
    branch: str | None = None
    patch: dict = {}
    n: int = 24


def _principal(account, user):
    from webbsearch.retrieval.entitlements import ANONYMOUS
    if not account:
        return ANONYMOUS
    try:
        return engine().accounts.principal(str(account), str(user) if user else None)
    except Exception:
        raise HTTPException(404, "unknown account/user")


def _slim(r: dict, n: int) -> list[dict]:
    return [{"sku": it["sku"], "title": it["title"], "brand": it["brand"], "family": it.get("cat3") or it["family"],
             "history": it.get("history"), "pinned": it.get("pinned", False), "at_branch": (it.get("avail") or {}).get("at_branch"),
             "stock_tier": it.get("stock_tier"), "exact": it.get("exact"), "badge": it.get("badge"),
             "adj": it.get("adj") or {}, "tier": (it.get("tier") or {}).get("tier"),
             "tier_text": (it.get("tier") or {}).get("detail")} for it in (r.get("items") or [])[:n]]


@router.post("/api/admin/preview")
def preview(request: Request, body: PreviewBody):
    """The same search with the live settings and with the draft (live + patch), side by side."""
    require(request)
    e = engine()
    p = _principal(body.account, body.user)
    try:
        draft, changed = e.merch.merged(body.patch or {})
    except (ValueError, TypeError, KeyError) as ex:
        raise HTTPException(422, str(ex))
    n = max(5, min(body.n, 48))
    q = body.q[:300]
    live = e.results.results(q, p, branch=body.branch, llm="off", settings=e.merch.get(), explain=True)
    new = e.results.results(q, p, branch=body.branch, llm="off", settings=draft, explain=True) if changed else live
    L, D = _slim(live, n), _slim(new, n)
    lpos = {it["sku"]: k + 1 for k, it in enumerate(_slim(live, 300))}
    for k, it in enumerate(D):
        was = lpos.get(it["sku"])
        it["was"] = was
        it["move"] = None if was is None else was - (k + 1)
    return {"q": q, "changed": changed, "principal": {"account": p.account_id, "name": p.account_name, "role": p.role,
                                                     "segment": p.segment},
            "live": {"items": L, "total": live.get("total", 0), "merch": live.get("merch")},
            "draft": {"items": D, "total": new.get("total", 0), "merch": new.get("merch")}}


# ------------------------------------------------------------------ analytics
@router.get("/api/admin/insights")
def insights(request: Request, days: int = 30):
    require(request)
    a = analytics()
    if a is None:
        return {"disabled": True}
    return a.insights(max(1, min(days, 365)))


@router.get("/api/admin/recent")
def recent(request: Request, limit: int = 50):
    require(request)
    a = analytics()
    return [] if a is None else a.recent(max(1, min(limit, 500)))


@router.get("/api/admin/query")
def query_detail(request: Request, q: str, days: int = 90):
    require(request)
    a = analytics()
    return {"q": q, "live": a.query_detail(q, days) if a else None, "history": history().query(q) if history().ok else None}


@router.get("/api/admin/history")
def history_summary(request: Request):
    require(request)
    h = history()
    if not h.ok:
        return {"available": False}
    return {"available": True, **h.summary()}


@router.post("/api/admin/analytics/clear")
def clear_analytics(request: Request):
    """Empty the live log (e.g. after testing, before a demo). The historical search log is not touched."""
    require(request)
    a = analytics()
    if a is None:
        raise HTTPException(404, "analytics are off")
    return {"cleared": a.clear()}


@router.get("/api/admin/export.csv")
def export_csv(request: Request, table: str = "searches", days: int = 30):
    require(request)
    a = analytics()
    if a is None or table not in ("searches", "events", "feedback"):
        raise HTTPException(404, "nothing to export")
    since = time.strftime("%Y-%m-%d", time.localtime(time.time() - max(1, min(days, 3650)) * 86400))
    cur = a.db.execute(f"SELECT * FROM {table} WHERE day>=? ORDER BY ts", (since,))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([c[0] for c in cur.description])
    from webbsearch.csv_safe import csv_safe_row
    w.writerows(csv_safe_row(r) for r in cur.fetchall())     # search words and comments can't run as formulas
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="webb-search-{table}-{since}.csv"'})


# ------------------------------------------------------------------ Claude usage and sign-ins (v1.10.2)
@router.get("/api/admin/usage")
def claude_usage(request: Request, days: int = 30):
    """Claude API calls recorded by this server: today against the daily cap, and by day, model and feature.
    Costs are estimates from the list prices in llm/client.py; Anthropic's console has the bill."""
    require(request)
    a = analytics()
    if a is None:
        raise HTTPException(404, "analytics are off (WEBB_ANALYTICS=0)")
    out = a.llm_usage(days=max(1, min(days, 365)))
    from webbsearch.config import settings
    out["cap"] = int(os.environ.get("WEBB_CLAUDE_DAILY_CALLS", "0") or 0)
    out["key_set"] = bool(settings.anthropic_api_key) and not settings.llm_disabled
    from webbsearch.api import server
    out["since_start"] = server._engine.claude.stats() if server._engine is not None else None
    return out


@router.get("/api/admin/signins")
def signins(request: Request, days: int = 90, failed: bool = False):
    require(request)
    a = analytics()
    if a is None:
        raise HTTPException(404, "analytics are off (WEBB_ANALYTICS=0)")
    return a.signins(days=max(1, min(days, 365)), failed=failed)


@router.get("/api/admin/signins.csv")
def signins_csv(request: Request, days: int = 90):
    require(request)
    a = analytics()
    if a is None:
        raise HTTPException(404, "analytics are off (WEBB_ANALYTICS=0)")
    from webbsearch.csv_safe import csv_safe_row
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["time_utc", "ip", "login", "ok", "browser"])
    w.writerows(csv_safe_row([time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(r["ts"])), r["ip"], r["kind"],
                              "yes" if r["ok"] else "no", r["ua"]])
                for r in a.signins(days=max(1, min(days, 365)), limit=100000)["rows"])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="flexsearch-signins.csv"'})


# ------------------------------------------------------------------ customer feedback (v1.9)
def _titles(skus: list) -> list[dict]:
    c = engine().catalog
    out = []
    for sku in (skus or [])[:3]:
        i = c.sku_to_idx.get(str(sku))
        out.append({"sku": str(sku), "title": str(c.title[i]) if i is not None else ""})
    return out


@router.get("/api/admin/feedback")
def feedback_admin(request: Request, days: int = 30, vote: int | None = None, kind: str | None = None,
                   status: str | None = None, has_comment: bool = False, q: str = "", limit: int = 200):
    """Thumbs up and down from results pages and Ask answers, with the search, reasons, comment, who and what they saw."""
    require(request)
    a = analytics()
    if a is None:
        return {"disabled": True}
    d = max(1, min(days, 365))
    items = a.feedback_list(d, vote=vote, kind=kind, status=status, has_comment=has_comment, q=q[:100], limit=limit)
    for it in items:
        it["top_items"] = _titles(it["context"].get("top") or it["context"].get("items") or [])
    return {"summary": a.feedback_summary(d), "items": items}


class FeedbackTriage(BaseModel):
    status: str | None = None
    note: str | None = None
    who: str = ""


@router.put("/api/admin/feedback/{fid}")
def feedback_triage(request: Request, fid: str, body: FeedbackTriage):
    require(request)
    a = analytics()
    if a is None:
        raise HTTPException(404, "analytics are off")
    try:
        return a.set_feedback(fid[:16], status=body.status, note=body.note, who=body.who)
    except KeyError:
        raise HTTPException(404, "no such feedback")
    except ValueError as e:
        raise HTTPException(422, str(e))


# ------------------------------------------------------------------ relevance audit
class AuditBody(BaseModel):
    n: int = 150
    mode: str = "top"
    patch: dict | None = None
    who: str = ""


@router.post("/api/admin/audit")
def audit_start(request: Request, body: AuditBody):
    require(request)
    if not history().ok:
        raise HTTPException(404, "no search log on this computer")
    draft = None
    if body.patch:
        try:
            draft, changed = engine().merch.merged(body.patch)
        except (ValueError, TypeError, KeyError) as ex:
            raise HTTPException(422, str(ex))
        draft = draft if changed else None
    started = audit().start(max(10, min(body.n, 500)), "recent" if body.mode == "recent" else "top", draft, body.who)
    return {"started": started, **audit().status()}


@router.get("/api/admin/audit")
def audit_status(request: Request):
    require(request)
    if not history().ok:
        return {"running": False, "result": None}
    return audit().status()


# ------------------------------------------------------------------ rules, A/B tests, Bulk order
@router.get("/api/admin/rules/report")
def rules_report(request: Request, days: int = 30):
    """Per rule: searches it applied to, click-through and add-to-cart, against all searches."""
    require(request)
    a = analytics()
    return {"all": {}, "rules": {}} if a is None else a.rule_report(max(1, min(days, 365)))


@router.get("/api/admin/experiments")
def experiments(request: Request):
    """Every A/B test with its results so far (control A = live settings, B = live + the test's changes)."""
    require(request)
    a = analytics()
    out = []
    for x in engine().merch.get().get("experiments") or []:
        out.append({**x, "report": a.experiment_report(x) if a else None})
    return out[::-1]


def bulk_learned():
    """The learned-choices table (what people pick for a Bulk order line), refreshed from the log."""
    from webbsearch.api.server import bom
    e = engine()
    bom()                                  # attaches e.learned
    lr = getattr(e, "learned", None)
    if lr is None or lr.a is None:
        return None
    lr.refresh()
    return lr


@router.get("/api/admin/bulk")
def bulk_admin(request: Request, days: int = 30):
    """Bulk order analytics, and the crosses people taught it (learned from their choices)."""
    require(request)
    a = analytics()
    lr = bulk_learned()
    s = engine().merch.get()
    off = set(s.get("learned_off") or [])
    tbl = lr.table(engine().catalog) if lr else {"items": [], "brands": []}
    crossed = {(c.get("from_text"), c.get("to_sku")) for c in s.get("crosses") or [] if c.get("kind") == "item"}
    for r in tbl["items"]:
        r["off"] = f"{r['key']}::{r['sku']}" in off
        r["promoted"] = (r["key"], r["sku"]) in crossed
    for r in tbl["brands"]:
        r["off"] = f"brand::{r['from']}::{r['to'].lower()}" in off
    return {"report": a.bulk_report(max(1, min(days, 365))) if a else None, "learned": tbl,
            "weights": s["bulk"], "learning_on": bool(s["personalization"].get("learned_crosses"))}


class BulkTryBody(BaseModel):
    text: str
    account: str | None = None
    user: str | None = None
    branch: str | None = None
    patch: dict = {}


def _bulk_top(x: dict) -> dict:
    o = (x.get("options") or [None])[0] or (x.get("closest") or [None])[0]
    return {"status": x["status"], "note": x.get("note") or "", "brand_cross": x.get("brand_cross"), "cross": x.get("cross"),
            "top": {"sku": o["sku"], "title": o["title"], "brand": o.get("brand", ""), "why": [w["t"] for w in o.get("why") or []]}
            if o else None, "options": [{"sku": c["sku"], "brand": c.get("brand", ""), "title": c["title"]} for c in (x.get("options") or [])[:4]]}


@router.post("/api/admin/bulk/try")
def bulk_try(request: Request, body: BulkTryBody):
    """Match a few Bulk order lines with the live settings and with the draft (crosses, brand order, weights)."""
    require(request)
    from webbsearch.api.server import bom
    from webbsearch.retrieval.bom import parse_request
    e = engine()
    p = _principal(body.account, body.user)
    try:
        draft, changed = e.merch.merged(body.patch or {})
    except (ValueError, TypeError, KeyError) as ex:
        raise HTTPException(422, str(ex))
    lines = parse_request(body.text[:4000], None, None, None, None, None)["lines"][:12]
    b = bom()
    live = b.match_all(lines, p, body.branch or None, settings=e.merch.get())
    new = b.match_all(lines, p, body.branch or None, settings=draft) if changed else live
    return {"changed": changed, "lines": [{"text": ln.get("raw") or ln.get("text"), "qty": ln.get("qty"), "key": x.get("key"),
                                           "live": _bulk_top(x), "draft": _bulk_top(y)}
                                          for ln, x, y in zip(lines, live["lines"], new["lines"])]}


# ------------------------------------------------------------------ insights and recommendations
def recs_engine():
    """The Recommendations page's engine (merch/insights.py), one per server."""
    if _an.get("insights") is None:
        from webbsearch.merch.insights import Insights
        h = history()
        _an["insights"] = Insights(engine(), analytics(), h, audit(), engine().merch.root, learned=bulk_learned)
    return _an["insights"]


@router.get("/api/admin/recs")
def recs(request: Request, days: int = 30):
    """Trends, gaps, opportunities, data issues and cleanup, each with evidence and draft-ready actions."""
    require(request)
    return recs_engine().report(max(1, min(days, 365)))


class RecRunBody(BaseModel):
    who: str = ""
    n: int = 150


@router.post("/api/admin/recs/run")
def recs_run(request: Request, body: RecRunBody):
    """Start the replay analysis (weights, brand cap, brand affinity, head-search lifts, rule impact) in the background."""
    require(request)
    if not history().ok:
        raise HTTPException(404, "no search log on this computer")
    started = recs_engine().start_deep(body.who, max(50, min(body.n, 300)))
    return {**recs_engine()._deep_summary(engine().merch.get()), "started": started}


@router.get("/api/admin/recs/run")
def recs_status(request: Request):
    require(request)
    return recs_engine()._deep_summary(engine().merch.get())


class DismissBody(BaseModel):
    id: str
    who: str = ""
    undo: bool = False


@router.post("/api/admin/recs/dismiss")
def recs_dismiss(request: Request, body: DismissBody):
    require(request)
    if not re.fullmatch(r"[0-9a-f]{12}", body.id):
        raise HTTPException(422, "unknown recommendation")
    return {"dismissed": recs_engine().dismiss(body.id, body.who, body.undo)}


# ------------------------------------------------------------------ lookups for the rule editor
@router.get("/api/admin/lookup")
def lookup(request: Request, kind: str, q: str = "", limit: int = 12):
    require(request)
    e = engine()
    qn = norm_q(q)
    limit = max(1, min(limit, 30))
    if kind == "brand":
        c = _counts("brand", e.catalog.brand)
        hits = [b for b in c if qn in b.lower()] if qn else list(c)
        hits.sort(key=lambda b: (not b.lower().startswith(qn), -c[b]))
        return [{"value": b, "label": b, "n": c[b]} for b in hits[:limit]]
    if kind == "family":
        c = _counts("cat3", e.catalog.cat3)
        hits = [f for f in c if qn in f.lower()] if qn else list(c)
        hits.sort(key=lambda f: (not f.lower().startswith(qn), -c[f]))
        return [{"value": f, "label": f, "n": c[f]} for f in hits[:limit]]
    if kind == "sku":
        i = e.catalog.sku_to_idx.get(q.strip())
        if i is not None:
            return [_sku_row(e, i)]
        if len(qn) < 2:
            return []
        from webbsearch.retrieval.entitlements import ANONYMOUS
        r = e.results.results(q[:120], ANONYMOUS, llm="off", merch=False)
        return [_sku_row(e, e.catalog.sku_to_idx[it["sku"]]) for it in (r.get("items") or [])[:limit]]
    raise HTTPException(422, "kind is brand, family or sku")


_COUNTS: dict = {}


def _counts(key, arr) -> Counter:
    if key not in _COUNTS:
        _COUNTS[key] = Counter(str(x) for x in arr)
    return _COUNTS[key]


def _sku_row(e, i: int) -> dict:
    c = e.catalog
    return {"value": str(c.sku[i]), "label": str(c.title[i]), "brand": str(c.brand[i]), "family": str(c.cat3[i])}


# ------------------------------------------------------------------ what to fix next
@router.get("/api/admin/suggestions")
def suggestions(request: Request, days: int = 30):
    """Concrete next actions from the live log, Bulk order gaps, the audit and the historical log."""
    require(request)
    out = []
    s = engine().merch.get()
    have_rw = {r["from"] for r in s.get("rewrites") or []}
    have_rule = {r["match"]["q"] for r in s.get("rules") or []}
    a = analytics()
    if a is not None:
        ins = a.insights(max(1, min(days, 365)))
        for z in ins["zero_results"][:8]:
            if z["qn"] not in have_rw:
                out.append({"kind": "zero", "score": 100 + z["zero"], "title": f"“{z['q']}” found nothing {z['zero']}×",
                            "detail": "Add a rewrite to words the catalog uses, or a rule that pins the right item.",
                            "action": {"type": "rewrite", "from": z["qn"]}})
        for z in ins["low_ctr"][:6]:
            if z["qn"] not in have_rule:
                out.append({"kind": "low_ctr", "score": 60 + z["n"], "title": f"“{z['q']}”: {round(z['ctr'] * 100)}% of {z['n']} searches clicked",
                            "detail": "People search it and leave. Check the top results in Preview; pin or boost what they want.",
                            "action": {"type": "preview", "q": z["q"]}})
        br = a.bulk_report(max(1, min(days, 365)))
        crossed_brands = {c["from_brand"].lower() for c in s.get("crosses") or [] if c.get("kind") == "brand"}
        for b in br["brands_asked"][:6]:
            if b["brand"].lower() not in crossed_brands:
                out.append({"kind": "brand_cross", "score": 55 + b["n"], "title": f"Bulk order: {b['n']} lines ask for {b['brand']}",
                            "detail": f"Webb doesn't carry {b['brand']}. Add a brand cross so those lines match the brand Webb sells.",
                            "action": {"type": "cross", "from_brand": b["brand"]}})
        lr = bulk_learned()
        if lr is not None:
            have = {(c.get("from_text"), c.get("to_sku")) for c in s.get("crosses") or [] if c.get("kind") == "item"}
            off = set(s.get("learned_off") or [])
            for r in lr.table(engine().catalog, limit=40)["items"]:
                if r["n"] >= 5 and r["share"] >= 0.7 and (r["key"], r["sku"]) not in have and f"{r['key']}::{r['sku']}" not in off:
                    out.append({"kind": "learned", "score": 30 + r["n"], "title": f"“{r['key']}” is matched to one item {r['n']}× ({round(r['share'] * 100)}%)",
                                "detail": f"{r['title'][:80]} (#{r['sku']}). Make it a permanent cross so every account gets it first.",
                                "action": {"type": "promote", "key": r["key"], "sku": r["sku"]}})
        for g in a.feedback_summary(max(1, min(days, 365)))["top_down"][:6]:
            if g["down"] >= 2 or g["comments"]:
                last = next((r for r in a.feedback_list(max(1, min(days, 365)), vote=-1, q=g["qn"], limit=20)
                             if r["qn"] == g["qn"] and r["comment"]), None)
                out.append({"kind": "feedback", "score": 70 + 5 * g["down"], "title": f"Customers marked “{g['q']}” not helpful {g['down']}×",
                            "detail": (f"“{last['comment'][:90]}”" if last else "No comment left.") + " See Feedback for every vote on it.",
                            "action": {"type": "preview", "q": g["q"]}})
        for g in ins["bulk"]["not_in_catalog"][:6]:
            out.append({"kind": "gap", "score": 50 + g["n"], "title": f"Bulk order: no {g['what']} in the catalog ({g['n']} lines)",
                        "detail": f"Customers asked for it (e.g. “{g['example'][:60]}”). A catalog or sourcing gap, not a ranking fix.",
                        "action": None})
    au = audit().status().get("result") if history().ok else None
    if au:
        for m in [m for m in au["misses"] if m["searches"] >= 5][:8]:
            if m["qn"] in have_rule:
                continue
            where = f"now at #{m['pos']}" if m["pos"] else "not on the page"
            out.append({"kind": "audit", "score": 40 + min(m["searches"], 50),
                        "title": f"“{m['q']}” ({m['searches']} searches): the item customers pick is {where}",
                        "detail": f"{m['title'][:80]} (#{m['sku']}). Pin it for this query, or check why it ranks low.",
                        "action": {"type": "pin", "q": m["qn"], "sku": m["sku"]}})
    if history().ok:
        for t in history().summary()["top_queries"][:40]:
            if t["top_share"] >= 0.6 and t["n"] >= 30 and t["qn"] not in have_rule and len(out) < 30:
                out.append({"kind": "head", "score": 20 + t["top_share"] * 10,
                            "title": f"“{t['q']}”: {round(t['top_share'] * 100)}% of {t['n']} logged searches pick one item",
                            "detail": f"{t['top_title'][:80]} (#{t['top_sku']}). Worth checking it's first; pin it if not.",
                            "action": {"type": "preview", "q": t["q"], "sku": t["top_sku"]}})
    return sorted(out, key=lambda x: -x["score"])[:30]


# ------------------------------------------------------------------ helpers the main server calls
_EXP: dict = {}


def experiment_arm(sid: str) -> tuple[dict | None, str]:
    """The running A/B test's arm for this browser session: (settings to search with, "test id:A|B").
    A session stays in one arm for the whole test (hash of test id + session). No session, no test: (None, "")."""
    try:
        m = engine().merch
        s = m.get()
        x = next((x for x in s.get("experiments") or [] if x.get("status") == "running"), None)
        if x is None or not sid:
            return None, ""
        import hashlib
        b = int(hashlib.md5(f"{x['id']}:{sid}".encode()).hexdigest()[:8], 16) % 100 < int(x.get("split", 50))
        if not b:
            return None, f"{x['id']}:A"
        ck = (s.get("version"), x["id"])
        if ck not in _EXP:
            _EXP.clear()
            _EXP[ck] = m.merged(x.get("patch") or {})[0]
        return _EXP[ck], f"{x['id']}:B"
    except Exception:
        return None, ""                # a broken test never breaks search


def _outcome(r: dict) -> str:
    if (r.get("notice") or {}).get("kind") == "fallback":
        return "fallback"
    if not r.get("total"):
        return "answered" if (r.get("webb_items") or r.get("xref")) else "zero"
    return ""


def log_results(r: dict, p, q: str, sid: str, search_id: str | None, personalized: bool, variant: str = "") -> str | None:
    a = analytics()
    if a is None:
        return None
    try:
        if search_id and re.fullmatch(r"[0-9a-f]{16}", search_id) and a.same_search(search_id, q):
            return search_id          # a filter change on the same search: not a new search
        m = r.get("merch") or {}
        return a.log_search(q=q, principal=p, total=int(r.get("total") or 0),
                            latency_ms=float((r.get("timings") or {}).get("total_ms") or 0),
                            personalized=personalized, session=sid, rewritten_from=m.get("rewritten_from"),
                            rules=m.get("rules"), top=[it["sku"] for it in (r.get("items") or [])[:24]],
                            branch=(r.get("branch") or {}).get("branch_id"), outcome=_outcome(r), variant=variant)
    except Exception:
        return None                   # analytics never breaks search


# ------------------------------------------------------------------ search dictionary (v1.7)
class DictSaveBody(BaseModel):
    entry: dict
    who: str = ""
    note: str = ""
    base_version: int | None = None


class DictStatusBody(BaseModel):
    ids: list[str]
    status: str
    who: str = ""
    note: str = ""
    base_version: int | None = None


class DictDeleteBody(BaseModel):
    id: str
    who: str = ""
    note: str = ""
    base_version: int | None = None


class DictRestoreBody(BaseModel):
    id: str
    at: str
    who: str = ""


class DictTryBody(BaseModel):
    q: str
    entry: dict | None = None        # a draft entry, tried as if live
    remove: str | None = None        # or an entry id, tried as if switched off
    account: str | None = None
    user: str | None = None
    branch: str | None = None
    n: int = 12


class DictImportBody(BaseModel):
    name: str
    data: str                        # base64 of the file
    who: str = ""
    keep_status: bool = False        # use the file's status column; otherwise every row arrives in review
    replace_source: bool = False


_USE: dict = {}


def _usage() -> dict:
    """How often each logged search appears (the live log + the historical log), indexed by word, cached a minute."""
    import time as _t
    from webbsearch.merch.dictionary import key as _key
    if _USE.get("at", 0) > _t.time() - 60:
        return _USE
    counts: Counter = Counter()
    a = analytics()
    if a is not None:
        try:
            for qn, n in a._q("select qn, count(*) from searches group by qn"):
                counts[qn] += n
        except Exception:
            pass
    try:
        h = history()
        if h.ok:
            for qn, n in h.df()["qn"].value_counts().items():
                counts[qn] += int(n)
    except Exception:
        pass
    keys, by_tok = [], {}
    for qn, n in counts.items():
        k = _key(qn)
        keys.append((k, n))
        for t in set(k):
            by_tok.setdefault(t, []).append(len(keys) - 1)
    _USE.update(at=_t.time(), keys=keys, by_tok=by_tok, total=sum(counts.values()))
    return _USE


def _uses(term: str) -> int:
    from webbsearch.merch.dictionary import key as _key
    u = _usage()
    k = _key(term)
    if not k:
        return 0
    cands = min((u["by_tok"].get(t, []) for t in set(k)), key=len)
    n = 0
    for i in cands:
        qk, c = u["keys"][i]
        if len(k) == 1 or any(qk[j:j + len(k)] == k for j in range(len(qk) - len(k) + 1)):
            n += c
    return n


def _vocab():
    return engine().vocab


@router.get("/api/admin/dictionary")
def dict_list(request: Request, status: str = "", kind: str = "", dept: str = "", q: str = "", flag: str = "",
              source: str = "", sort: str = "term", page: int = 0, size: int = 50):
    """Entries with filters and paging, the counts, and each entry's lint and how often its words were searched."""
    require(request)
    from webbsearch.merch.dictionary import DEPTS, KIND_LABELS, STATUSES, key as _key
    v = _vocab()
    es = v.entries()
    if status:
        es = [e for e in es if e["status"] == status]
    if kind:
        es = [e for e in es if e["kind"] == kind]
    if dept:
        es = [e for e in es if dept.lower() in (e.get("dept") or "").lower()]
    if source:
        es = [e for e in es if source.lower() in (e.get("source") or "").lower()]
    if flag:
        es = [e for e in es if flag in (e.get("flags") or [])]
    if q.strip():
        qq = q.strip().lower()
        qk = _key(qq)
        es = [e for e in es if qq in e["term"].lower() or qq in (e.get("to") or "").lower()
              or (qk and all(t in _key(e["term"]) + _key(e.get("to") or "") for t in qk))]
    uses = {e["id"]: _uses(e["term"]) for e in es}
    order = {"term": lambda e: e["term"].lower(), "uses": lambda e: -uses[e["id"]],
             "updated": lambda e: e.get("updated_at") or "", "status": lambda e: (e["status"], e["term"].lower())}
    es = sorted(es, key=order.get(sort, order["term"]), reverse=(sort == "updated"))
    size = max(10, min(size, 200))
    out = [{**e, "uses": uses[e["id"]]} for e in es[page * size:(page + 1) * size]]
    allv = v.entries()
    flags = Counter(f for e in allv for f in e.get("flags") or [])
    return {"version": v.version, "counts": v.counts(), "total": len(es), "page": page, "size": size, "entries": out,
            "kinds": KIND_LABELS, "statuses": STATUSES, "depts": sorted({e.get("dept") for e in allv if e.get("dept")} | set(DEPTS)),
            "sources": sorted({e.get("source") for e in allv if e.get("source")}), "flags": dict(flags.most_common()),
            "searches": _usage().get("total", 0)}


@router.post("/api/admin/dictionary/lint")
def dict_lint(request: Request, body: DictSaveBody):
    """Check an entry while it's being edited (nothing is saved)."""
    require(request)
    from webbsearch.merch.dictionary import clean
    try:
        e = clean(body.entry)
    except ValueError as ex:
        return {"shape": str(ex), "checks": []}
    v = _vocab()
    checks = v.lint.check(e, [o for o in v.entries() if o["id"] != e["id"]])
    return {"entry": e, "checks": checks, "uses": _uses(e["term"])}


@router.post("/api/admin/dictionary/measure")
def dict_measure(request: Request, body: DictSaveBody):
    """The quality check for one entry: the typed words and the target searched, the suggested status and why."""
    require(request)
    from webbsearch.merch.dictionary import clean
    from webbsearch.merch.vocab_qc import Measurer, describe, verdict
    try:
        e = clean(body.entry)
    except ValueError as ex:
        raise HTTPException(422, str(ex))
    v = _vocab()
    checks = v.lint.check(e, [o for o in v.entries() if o["id"] != e["id"]])
    if "m" not in _an:
        _an["m"] = Measurer(engine())
    m = _an["m"].measure(e)
    st, why = verdict(e, checks, m)
    return {"measure": m, "suggest": st, "why": why, "text": f"{why} {describe(m)}".strip(), "checks": checks}


# v2.4 (review G14): one release policy for the search dictionary, the same idea as ranking's draft and Publish. A change
# that would put an entry live (a new live entry, an edit to what a live entry does, an import's live rows) is saved
# in review; a second person makes it live with Make live, and nobody approves their own change. Switching an entry
# off, sending it to review and Restore (recovery) act at once and are logged. Releases shipped in code and the
# Dictionary store itself are unchanged.
_SUBSTANCE = ("kind", "term", "to", "any", "none", "dept")


def _who(x: str) -> str:
    return (x or "").strip().lower()


@router.post("/api/admin/dictionary")
def dict_save(request: Request, body: DictSaveBody):
    require(request)
    from webbsearch.merch.dictionary import Conflict as DConflict, clean
    v = _vocab()
    held = False
    entry = dict(body.entry)
    try:
        if entry.get("status") == "live":
            c = clean(entry)
            old = v.get(c["id"]) if hasattr(v, "get") else None
            same = old is not None and old.get("status") == "live" and all(old.get(k) == c.get(k) for k in _SUBSTANCE)
            if not same:
                v._flags({**c})                    # an entry with errors can't go live: refused, as before
                entry["status"], held = "review", True
        e = v.save(entry, who=body.who, note=body.note or ("held for approval" if held else ""),
                   base_version=body.base_version)
    except DConflict as ex:
        raise HTTPException(409, str(ex))
    except (ValueError, TypeError) as ex:
        raise HTTPException(422, str(ex))
    out = {"entry": e, "version": v.version}
    if held:
        out["held_for_review"] = True
        out["message"] = "Saved in review. Someone other than you makes it live with Make live, after trying it."
    return out


@router.post("/api/admin/dictionary/status")
def dict_status(request: Request, body: DictStatusBody):
    require(request)
    from webbsearch.merch.dictionary import Conflict as DConflict
    ids, own = list(body.ids), []
    if body.status == "live":          # v2.4 (G14): approving is someone else's job, and it takes a name
        if not _who(body.who):
            raise HTTPException(422, "Type your name to make entries live.")
        v = _vocab()
        for eid in list(ids):
            e = v.get(eid) if hasattr(v, "get") else None
            if e and e.get("status") != "live" and _who(e.get("updated_by")) == _who(body.who):
                own.append({"id": eid, "term": e.get("term"), "why": "your own change: someone else approves it"})
                ids.remove(eid)
    try:
        r = _vocab().set_status(ids, body.status, who=body.who, note=body.note, base_version=body.base_version)
        if own:
            r = {**r, "refused": (r.get("refused") or []) + own}
    except DConflict as ex:
        raise HTTPException(409, str(ex))
    except ValueError as ex:
        raise HTTPException(422, str(ex))
    return {**r, "version": _vocab().version}


@router.post("/api/admin/dictionary/delete")
def dict_delete(request: Request, body: DictDeleteBody):
    require(request)
    from webbsearch.merch.dictionary import Conflict as DConflict
    try:
        _vocab().delete(body.id, who=body.who, note=body.note, base_version=body.base_version)
    except DConflict as ex:
        raise HTTPException(409, str(ex))
    except KeyError:
        raise HTTPException(404, "no such entry")
    return {"version": _vocab().version}


@router.get("/api/admin/dictionary/candidates")
def dict_candidates(request: Request, days: int = 90, min_sessions: int = 2):
    """Searches people fixed themselves (v2.43): a search with no results, then a similar one in the same session that
    got a click. Pairs the dictionary doesn't already have, for someone to add in review."""
    require(request)
    from webbsearch.merch.dictionary import key as _key
    an = analytics()
    if an is None:
        return {"candidates": []}
    have = {(_key(e["term"]), _key(e.get("to") or "")) for e in _vocab().entries()}
    have_terms = {_key(e["term"]) for e in _vocab().entries()}
    rows = [r for r in an.reformulations(days=max(1, min(days, 365)), min_sessions=max(1, min(min_sessions, 20)))
            if (_key(r["from"]), _key(r["to"])) not in have and _key(r["from"]) not in have_terms]
    return {"candidates": rows, "days": days, "min_sessions": min_sessions}


@router.get("/api/admin/dictionary/history")
def dict_history(request: Request, id: str = "", limit: int = 100):
    require(request)
    return {"history": _vocab().history(id or None, max(1, min(limit, 1000)))}


@router.post("/api/admin/dictionary/restore")
def dict_restore(request: Request, body: DictRestoreBody):
    require(request)
    try:
        e = _vocab().restore(body.id, body.at, who=body.who)
    except (KeyError, ValueError) as ex:
        raise HTTPException(404, str(ex))
    return {"entry": e, "version": _vocab().version}


@router.post("/api/admin/dictionary/try")
def dict_try(request: Request, body: DictTryBody):
    """A search without one entry and with it (as the form has it), everything else as live, side by side."""
    require(request)
    e = engine()
    v = e.vocab
    p = _principal(body.account, body.user)
    from webbsearch.merch.dictionary import clean
    try:
        eid = clean(body.entry)["id"] if body.entry else body.remove     # the store's id: an unsaved copy of a live rule
    except ValueError as ex:
        raise HTTPException(422, str(ex))
    try:
        entries = v.with_draft(body.entry, remove=body.remove) if body.entry else v.with_draft(None, remove=body.remove)
        without = v.with_draft(None, remove=eid)
    except ValueError as ex:
        raise HTTPException(422, str(ex))
    n = max(5, min(body.n, 24))
    q = body.q[:300]
    live = e.results.results(q, p, branch=body.branch, llm="off", vocab=without)
    new = e.results.results(q, p, branch=body.branch, llm="off", vocab=entries)
    lpos = {it["sku"]: k + 1 for k, it in enumerate((live.get("items") or [])[:300])}
    D = _slim(new, n)
    for k, it in enumerate(D):
        was = lpos.get(it["sku"])
        it["was"], it["move"] = was, (None if was is None else was - (k + 1))
    return {"q": q, "live": {"items": _slim(live, n), "total": live.get("total", 0), "searched": live.get("query"),
                             "vocab": (live.get("merch") or {}).get("vocab") or []},
            "draft": {"items": D, "total": new.get("total", 0), "searched": new.get("query"),
                      "vocab": (new.get("merch") or {}).get("vocab") or []}}


@router.get("/api/admin/dictionary/export.csv")
def dict_export(request: Request, status: str = "", kind: str = ""):
    require(request)
    v = _vocab()
    es = [e for e in v.entries() if (not status or e["status"] == status) and (not kind or e["kind"] == kind)]
    return Response(v.to_csv(es), media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=webb-search-dictionary-v{v.version}.csv"})


@router.post("/api/admin/dictionary/import")
def dict_import(request: Request, body: DictImportBody):
    """Add or update entries from a CSV or Excel file (this page's export, or a workbook with Search Term / Synonym /
    Handling columns). Rows arrive in review unless keep_status is set; rows with lint errors never arrive live."""
    require(request)
    import base64 as _b64
    from webbsearch.merch.dictionary import Dictionary
    try:
        raw = _b64.b64decode(body.data.split(",", 1)[-1])
    except Exception:
        raise HTTPException(422, "couldn't read the file")
    if len(raw) > 10_000_000:
        raise HTTPException(413, "files up to 10 MB")
    try:
        rows = Dictionary.rows_from_file(body.name, raw)
    except Exception as ex:
        raise HTTPException(422, f"couldn't read {body.name}: {ex}")
    if not body.keep_status:
        rows = [{**r, "status": "review"} for r in rows]
    # v2.4 (G14): a file can keep off and review, never put rows live on its own: live rows wait for approval
    held = sum(1 for r in rows if str(r.get("status") or "").lower() == "live")
    rows = [{**r, "status": "review"} if str(r.get("status") or "").lower() == "live" else r for r in rows]
    src = f"import {body.name[:60]} ({time.strftime('%b %d %Y')})"
    r = _vocab().import_rows(rows, who=body.who, source=src, replace_source=body.replace_source)
    return {**r, "rows": len(rows), "source": src, "version": _vocab().version, "held_for_approval": held}


@router.get("/api/admin/dictionary/builtin")
def dict_builtin(request: Request):
    """The analyzer's built-in vocabulary (read-only: it is applied to the catalog too, so a change needs a rebuild)."""
    require(request)
    from webbsearch.merch.dictionary import builtin_vocabulary
    return {"rules": builtin_vocabulary()}


@router.get("/api/admin/lexicon")
def lexicon_admin(request: Request, q: str = "", lang: str = ""):
    """Trade words in 12 languages (v2.4): each language's terms and coverage; `q` tries a search; `lang` lists its
    terms by concept. Read-only here: the files are reviewed by native speakers and shipped with a release."""
    require(request)
    from webbsearch.retrieval import lexicon as L
    top = {"es", "pt", "pl", "ru", "zh"}
    out = {"concepts": len(L.concepts()), "core": len(L.core_concepts()),
           "languages": [{"code": k, "name": v["name"], "depth": "full" if k in top else "core", **L.coverage(k)}
                         for k, v in L.LANGS.items()]}
    if q:
        to, got = L.translate(q[:200])
        out["try"] = {"q": q[:200], "to": to, "lang": got, "name": L.name(got) if got else None}
    if lang in L.LANGS:
        out["entries"] = [{"english": en, "terms": ts} for en, ts in L.entries(lang).items()]
    return out


# ---------------------------------------------------------------- the Knowledge Center review (v2.1)
@router.get("/api/admin/knowledge")
def knowledge_admin(request: Request, view: str = "review", limit: int = 200, flag: str = ""):
    """Knowledge questions and answers with the customer: the review queue (riskiest first, then the weakest
    answers), everything, the library gaps, or the expert queue."""
    require(request)
    from webbsearch.api.server import kc_desk, kc_log
    log = kc_log()
    out = {"summary": log.summary()}
    if view == "gaps":
        out["gaps"] = log.gaps()
    elif view == "experts":
        out["cases"] = kc_desk().queue(status=None, limit=max(1, min(limit, 1000)))
    else:
        out["items"] = log.list(review=True if view == "review" else None, limit=max(1, min(limit, 1000)),
                                status="to review" if view == "review" else None, flag=flag[:30] or None)
    try:
        out["coverage"] = engine().knowledge.coverage()
    except Exception:
        pass
    return out


class KnowledgeMark(BaseModel):
    status: str
    note: str = ""
    reviewer: str = ""


@router.post("/api/admin/knowledge/{qa_id}")
def knowledge_mark(request: Request, qa_id: str, body: KnowledgeMark):
    require(request)
    from webbsearch.api.server import kc_log
    if body.status not in ("reviewed", "needs document", "wrong", "unsafe", "to review", "ok"):
        raise HTTPException(422, "status")
    return {"ok": kc_log().mark(qa_id, body.status, body.note, body.reviewer)}


class ExpertReply(BaseModel):
    reply: str
    status: str = "answered"


@router.post("/api/admin/experts/{case_id}")
def expert_reply(request: Request, case_id: str, body: ExpertReply):
    require(request)
    from webbsearch.api.server import kc_desk
    return {"ok": kc_desk().answer(case_id, body.reply, body.status if body.status in ("answered", "closed", "open") else "answered")}


# ---------------------------------------------------------------- commerce, connectors and the export (v2.2)
@router.get("/api/admin/commerce")
def commerce_report(request: Request, days: int = 30):
    """FlexSearch's and the Knowledge Center's funnels from search or question to order, adds and revenue by source."""
    require(request)
    from webbsearch.merch.commerce import Commerce
    a = analytics()
    if a is None:
        return {"off": True}
    return Commerce(a).report(days=max(1, min(days, 365)))


@router.get("/api/admin/connectors")
def connectors_status(request: Request):
    """Which analytics tools get FlexSearch's events, what's queued and the last error. Secrets are never shown."""
    require(request)
    from webbsearch.merch.connectors import config_from_env
    a = analytics()
    cfg = config_from_env()
    f = getattr(a, "forwarder", None) if a is not None else None
    st = f.status() if f is not None else {}
    shown = {"ga4": ("measurement_id",), "datadog": ("site",), "webhook": ("url",)}
    return {"connectors": [{"name": k, "enabled": c["enabled"], **{x: c.get(x) for x in shown[k]},
                            **{x: v for x, v in (st.get(k) or {}).items() if x != "enabled"}} for k, c in cfg.items()],
            "checkout": {"url": os.environ.get("WEBB_CHECKOUT_URL") or None,
                         "secret_set": bool(os.environ.get("WEBB_CHECKOUT_SECRET")),
                         "webhook": "/api/checkout/webhook"},
            "export": ["searches", "events", "feedback", "checkouts", "orders"]}


@router.post("/api/admin/connectors/flush")
def connectors_flush(request: Request):
    require(request)
    a = analytics()
    f = getattr(a, "forwarder", None) if a is not None else None
    return f.flush() if f is not None else {"sent": 0, "errors": [], "pending": 0, "off": True}


EXPORT_TABLES = {"searches": "ts", "events": "ts", "feedback": "ts", "checkouts": "ts", "orders": "ts"}


@router.get("/api/admin/analytics/export")
def analytics_export(request: Request, table: str = "events", since: float = 0, limit: int = 100000):
    """One table as NDJSON (one JSON object per line) for Snowflake: stage the file and COPY INTO, or point Snowpipe at
    the stage. Sign-ins (IP addresses) are never exported."""
    require(request)
    if table not in EXPORT_TABLES:
        raise HTTPException(400, f"export one of: {', '.join(EXPORT_TABLES)}")
    a = analytics()
    if a is None:
        raise HTTPException(503, "analytics is off")
    if table in ("checkouts", "orders"):
        from webbsearch.merch.commerce import Commerce
        Commerce(a)
    cur = a.db.execute(f"SELECT * FROM {table} WHERE {EXPORT_TABLES[table]} >= ? ORDER BY {EXPORT_TABLES[table]} LIMIT ?",
                       (float(since), max(1, min(limit, 1_000_000))))
    cols = [c[0] for c in cur.description]
    from webbsearch.llm.guardrails import redact

    def row(r):
        d = dict(zip(cols, r))
        for k in ("q", "qn", "comment"):            # search words and comments without card, SSN, e-mail or phone
            if isinstance(d.get(k), str):
                d[k] = redact(d[k])
        return json.dumps(d, default=str) + "\n"
    body = "".join(row(r) for r in cur.fetchall())
    return Response(body, media_type="application/x-ndjson",
                    headers={"Content-Disposition": f'attachment; filename="flexsearch-{table}.ndjson"'})
