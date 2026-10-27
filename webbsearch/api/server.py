"""Flex Bulk: the staff app (/) and the settings dashboard (/admin).

    uvicorn flexbulk.server:app --port 8080

Webb staff match a customer's list (a takeoff, BOM, email, Word list, photo of a note, or typed lines) against the
item list loaded from files (flexbulk.loader), enter who it's for and the job, confirm the lines, save the order and
export it (Excel, CSV, or an item/qty file for order entry). There are no customer accounts: matching ranks by the
item list, branch stock, the dashboard's settings (crosses, brand order, weights, dictionary) and what staff have
picked before, for the same line and, more strongly, for the same customer.

Sign-in: WEBB_SITE_PASSWORD puts a login in front of everything (api/gate.py); WEBB_ADMIN_PASSWORD opens the
dashboard to staff away from the server's own computer. Without them the app is open on this computer only.

This file replaces FlexSearch's customer-facing server; the dashboard (api/admin.py) imports engine() and bom() from
here, as it did there.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from webbsearch import __version__ as _VERSION
from webbsearch.api import gate
from webbsearch.api.admin import analytics
from webbsearch.api.admin import router as admin_router
from webbsearch.api.assets import no_cache_static, versioned
from webbsearch.retrieval.entitlements import ANONYMOUS, Principal
from webbsearch.retrieval.service import Engine

STATIC = Path(__file__).parent / "static"
SAMPLES = Path(__file__).resolve().parents[2] / "samples"

app = FastAPI(title="Flex Bulk", version=_VERSION)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
app.middleware("http")(no_cache_static)
app.middleware("http")(gate.middleware)


@app.middleware("http")
async def _test_traffic(request: Request, call_next):
    """QA runs, automated browsers and a browser marked with ?test=1 are test traffic: analytics (and so the learned
    crosses) record none of it. ?test=0 clears the mark."""
    from webbsearch.merch.analytics import is_test_request, test_traffic
    flag = request.query_params.get("test")
    tok = test_traffic.set(flag == "1" or (flag != "0" and is_test_request(request.headers, request.cookies)))
    try:
        resp = await call_next(request)
    finally:
        test_traffic.reset(tok)
    if flag in ("0", "1") and request.url.path in ("/", "/admin"):
        if flag == "1":
            resp.set_cookie("webb_test", "1", max_age=30 * 86400, httponly=True, samesite="lax")
        else:
            resp.delete_cookie("webb_test")
    return resp


app.include_router(admin_router)

_engine: Engine | None = None
_bom = None
_prices = None


def engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = Engine()
        attach_usage(_engine)
    return _engine


def attach_usage(e) -> None:
    """Claude calls (handwriting reading, when an API key is set) go to the dashboard's usage page."""
    an = analytics()
    if an is not None:
        e.claude.recorder = an.record_llm


def bom():
    global _bom
    if _bom is None:
        from webbsearch.merch.bulkprefs import Learned
        from webbsearch.retrieval.bom import Bom
        e = engine()
        e.learned = Learned(analytics())     # what staff chose for a line, weighted into the next match
        _bom = Bom(e)
    return _bom


def prices():
    global _prices
    if _prices is None:
        from flexbulk.prices import Prices
        _prices = Prices()
    return _prices


def orders():
    from flexbulk.orders import store
    return store()


@app.on_event("startup")
def _startup():
    engine()
    bom()


def resolve_principal(request: Request, account: str | None = None, user: str | None = None) -> Principal:
    """No customer accounts here: matching is the same for every member of staff (the dashboard's Try box uses this)."""
    return ANONYMOUS


def customer_key(customer_no: str | None, customer_name: str | None = None) -> str | None:
    """What staff choices for one customer are remembered under (their number, else their name)."""
    k = re.sub(r"\s+", " ", (customer_no or "").strip() or (customer_name or "").strip()).lower()
    return f"cust:{k[:60]}" if k else None


def staff_principal(customer: str | None, staff: str = ""):
    """How analytics records a line or choice: the customer it was for, made by staff."""
    return SimpleNamespace(account_id=customer or "", role="staff", user_id=staff[:60])


def fallback_note(ex: Exception) -> str:
    return "FlexIQ is unavailable right now; this was read with rules."


# ------------------------------------------------------------------ pages
@app.get("/login", include_in_schema=False)
def login_form(request: Request):
    return gate.login_page(request)


@app.post("/login", include_in_schema=False)
async def login_post(request: Request):
    return await gate.login_submit(request)


@app.get("/")
def index():
    return HTMLResponse(versioned((STATIC / "index.html").read_text(), STATIC), headers={"Cache-Control": "no-cache"})


@app.get("/api/health")
def health(request: Request):
    e = engine()
    from webbsearch.api import admin
    return {"ok": True, "app": "flex-bulk", "version": _VERSION, "items": e.catalog.n,
            "stock": e.availability.table is not None, "price_levels": prices().levels,
            "claude": e.claude.enabled, "admin": admin.ENABLED and gate.admin_ok(request)}


@app.get("/api/setup")
def setup():
    """What the page needs first: branches, price levels, the item list loaded and when, how photos are read."""
    import json
    from webbsearch.config import settings
    from webbsearch.retrieval import ocr
    e = engine()
    rep = {}
    try:
        rep = json.loads((settings.canonical_dir / "load_report.json").read_text())
    except (OSError, ValueError):
        pass
    eng = ocr.pick_engine(e.claude)
    return {"version": _VERSION, "branches": e.availability.all_branches(), "default_branch": str(e.availability.default),
            "levels": prices().levels, "items": e.catalog.n, "stock": e.availability.table is not None,
            "loaded": {"at": rep.get("at"), "files": rep.get("files", {}), "counts": rep.get("counts", {}),
                       "warnings": rep.get("warnings", [])},
            "ocr": {"engine": eng, "label": ocr.ENGINE_LABEL.get(eng) if eng else None}}


@app.get("/api/demo-users")
def demo_users():
    """The dashboard asks for demo customer accounts to preview with; there are none here (previews run as staff)."""
    return []


@app.get("/api/branches")
def branches():
    return engine().availability.all_branches()


# ------------------------------------------------------------------ items
@app.get("/api/product/{sku}")
def product(sku: str, branch: str | None = None, level: str | None = None):
    e = engine()
    j = e.catalog.sku_to_idx.get(sku)
    if j is None:
        raise HTTPException(404, "not on the item list")
    cat = e.catalog
    p, basis = prices().price(sku, level)
    lv = {name: prices().price(sku, name)[0] for name in prices().levels}
    return {"sku": sku, "title": cat.title[j], "brand": cat.brand[j], "mfr_part": cat.mfr_part[j], "uom": cat.uom[j],
            "cat1": cat.cat1[j], "cat2": cat.cat2[j], "cat3": cat.cat3[j], "status": cat.status[j],
            "stock_tier": cat.stock_tier[j], "attrs": cat.attrs(int(j)), "price": p, "price_basis": basis,
            "web_url": __import__("flexbulk.prices", fromlist=["product_url"]).product_url(sku),
            "list_price": prices().list.get(sku), "levels": lv,
            "branches": e.availability.by_branch(int(j), branch, k=10), "branch": e.availability.branch_info(branch)}


@app.get("/api/lookup")
def lookup(q: str = "", branch: str | None = None, level: str | None = None, qty: int = 1, uom: str = "", k: int = 12):
    """Search the item list for a line staff want to match by hand ("Find an item")."""
    q = q.strip()[:200]
    if not q:
        return {"items": []}
    e = engine()
    b = bom()
    res = e.results.results(q, ANONYMOUS, branch=branch, llm="off")
    out = []
    for it in (res.get("items") or [])[:max(1, min(k, 30))]:
        j = e.catalog.sku_to_idx.get(it["sku"])
        if j is None:
            continue
        c = b._card(int(j), ANONYMOUS, branch, max(1, qty), {}, {}, uom.upper())
        c.pop("_bonus", None)
        c.pop("_parts", None)
        prices().apply(c, level)
        out.append(c)
    return {"q": q, "items": out, "notice": (res.get("notice") or {}).get("text")}


# ------------------------------------------------------------------ reading documents
class ParseBody(BaseModel):
    sid: str = ""
    text: str | None = None
    filename: str | None = None
    data_b64: str | None = None
    sheet: str | None = None
    roles: dict | None = None
    header_row: int | None = None
    classify: bool = False
    customer: str | None = None
    staff: str = ""


@app.post("/api/bom/parse")
def bom_parse(body: ParseBody):
    """A paste or an uploaded document -> lines to match: Excel, CSV/TSV/TXT, Word, email (.eml with attachments),
    text PDFs, and photos or scans of handwritten notes (OCR). Lines that aren't items come back per source."""
    from webbsearch.retrieval.bom import parse_request
    if body.text and len(body.text) > 400_000:
        raise HTTPException(413, "paste is over 400 KB")
    if body.data_b64 and len(body.data_b64) > 21_000_000:
        raise HTTPException(413, "file is over 15 MB")
    t0 = time.perf_counter()
    try:
        e = engine()
        vocab = getattr(getattr(e.retriever, "b", None), "term_exists", None)
        r = parse_request(body.text, body.filename, body.data_b64, body.sheet, body.roles, body.header_row,
                          vocab=vocab, claude=e.claude, classify=body.classify)
    except ValueError as ex:
        raise HTTPException(413, str(ex))
    except Exception as ex:  # noqa: BLE001  (an unreadable file: say so, don't 500)
        return JSONResponse({"error": f"Couldn't read {body.filename or 'the file'} ({type(ex).__name__}). "
                                      "Save it as .xlsx, .csv or .docx, or take a clearer photo, and try again."}, status_code=422)
    a = analytics()
    if a is not None and not body.roles:
        try:
            fn = (body.filename or "").lower()
            kind = "paste" if not body.data_b64 else (Path(fn).suffix.lstrip(".") or r.get("kind") or "file")
            n = int(r.get("total") or len(r.get("lines") or []))
            a.log_event("bulk_upload", session=body.sid[:40], principal=staff_principal(customer_key(body.customer), body.staff),
                        qty=n, meta={"kind": kind[:12], "lines": n, "set_aside": sum(len(s.get("other") or []) for s in r.get("sources") or []),
                                     "bytes": len(body.data_b64 or body.text or ""), "ms": round((time.perf_counter() - t0) * 1000),
                                     "truncated": bool(r.get("truncated")), "ocr": bool(r.get("pages"))})
        except Exception:  # noqa: BLE001
            pass
    return r


SAMPLE_DOCS = {"order-email.eml": "message/rfc822", "material-list.docx":
               "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "handwritten-note.jpg": "image/jpeg"}


@app.get("/api/bom/samples/{name}")
def bom_sample(name: str):
    if name not in SAMPLE_DOCS or not (SAMPLES / name).exists():
        raise HTTPException(404, "no such sample")
    return FileResponse(SAMPLES / name, media_type=SAMPLE_DOCS[name], filename=name)


@app.get("/api/bom/ocr")
def bom_ocr():
    from webbsearch.retrieval import ocr
    e = engine()
    eng = ocr.pick_engine(e.claude)
    return {"engine": eng, "label": ocr.ENGINE_LABEL.get(eng) if eng else None, "available": ocr.engines(e.claude)}


@app.get("/api/bom/context")
def bom_context(branch: str | None = None, customer: str | None = None):
    e = engine()
    lr = getattr(e, "learned", None)
    ck = customer_key(customer)
    mine = 0
    if lr is not None and ck:
        lr.refresh()
        mine = sum(sum(c.values()) for (a, _), c in lr.by_key_acct.items() if a == ck)
    return {"branch": e.availability.branch_info(branch), "items": e.catalog.n, "levels": prices().levels,
            "stock": e.availability.table is not None, "customer_choices": mine}


# ------------------------------------------------------------------ matching
class MatchBody(BaseModel):
    lines: list[dict]
    branch: str | None = None
    level: str | None = None
    customer: str | None = None
    staff: str = ""
    k: int = 6
    sid: str = ""


def _clean_line(ln: dict) -> dict | None:
    t = str(ln.get("text") or "")[:300]
    if not t.strip() and not str(ln.get("desc") or "").strip():
        return None
    try:
        qty = max(1, min(int(float(ln.get("qty") or 1)), 99_999))
    except (TypeError, ValueError):
        qty = 1
    return {"n": ln.get("n"), "raw": str(ln.get("raw") or t)[:400], "text": t, "desc": str(ln.get("desc") or "")[:300],
            "brand": str(ln.get("brand") or "")[:80], "part": str(ln.get("part") or "")[:80], "qty": qty,
            "uom": str(ln.get("uom") or "")[:6], "qty_how": ln.get("qty_how") or "given", "alt_text": ln.get("alt_text"),
            "from_ocr": bool(ln.get("from_ocr")), "edited": bool(ln.get("edited")),
            "conf": ln.get("conf") if isinstance(ln.get("conf"), (int, float)) else None,
            "src": ln.get("src") if isinstance(ln.get("src"), int) else None,
            "src_label": str(ln.get("src_label") or "")[:120] or None,
            "page": ln.get("page") if isinstance(ln.get("page"), int) else None}


def price_lines(r: dict, level: str | None) -> None:
    pr = prices()
    for x in r["lines"]:
        for key in ("options", "substitutes", "closest"):
            for c in x.get(key) or []:
                pr.apply(c, level)


@app.post("/api/bom/match")
def bom_match(body: MatchBody):
    """Match up to 50 lines: exact item # / mfr part # / UPC, merchandising crosses, else recommendations ranked by
    fit, the dashboard's weights, Webb's brand order, what staff picked before (this customer's picks count more) and
    branch stock; the quantity against on-hand, transfers and on-the-shelf substitutes; priced at the order's level."""
    lines = [x for x in (_clean_line(ln) for ln in body.lines[:50]) if x]
    level = body.level if body.level in prices().levels else None
    ck = customer_key(body.customer)
    r = bom().match_all(lines, ANONYMOUS, body.branch, k=max(1, min(body.k, 8)), acct=ck)
    price_lines(r, level)
    r["level"] = level or "List"
    a = analytics()
    if a is not None:
        try:
            who = staff_principal(ck, body.staff)
            for x in r["lines"]:
                a.log_event("bulk_line", session=body.sid[:40], principal=who, qty=x.get("qty"),
                            sku=(x["options"][0].get("sku") if x["options"] else None),
                            meta={"status": x["status"], "text": (x.get("raw") or x.get("text") or "")[:120],
                                  "missing": x.get("missing_what"), "weak": bool(x.get("weak")), "key": x.get("key"),
                                  "req_brand": x.get("req_brand") or None, "uncarried": bool(x.get("uncarried")),
                                  "cross": (x.get("cross") or {}).get("label"),
                                  "brand_cross": (lambda b: f"{b['from']} → {b['to']}" if b else None)(x.get("brand_cross")),
                                  "source": "handwriting/scan" if x.get("from_ocr") else
                                  (Path(x["src_label"]).suffix.lower().lstrip(".") or "file") if x.get("src_label") and "." in x["src_label"]
                                  else "paste"})
        except Exception:  # noqa: BLE001  (analytics never breaks the order)
            pass
    return r


class ExplainBody(BaseModel):
    line: dict
    branch: str | None = None
    level: str | None = None
    customer: str | None = None


@app.post("/api/bom/explain")
def bom_explain(body: ExplainBody):
    """One line matched again with every step recorded: how it was read, crosses, the exact lookup, what was searched,
    and each candidate's score (search position, fit, bonus and its parts). The staff app's "Why?"."""
    ln = _clean_line(body.line)
    if ln is None:
        raise HTTPException(422, "an empty line")
    level = body.level if body.level in prices().levels else None
    r = bom().match_all([ln], ANONYMOUS, body.branch, k=8, acct=customer_key(body.customer), trace=True)
    price_lines(r, level)
    return r["lines"][0]


class TrackBody(BaseModel):
    kind: str
    sid: str = ""
    sku: str | None = None
    meta: dict | None = None
    customer: str | None = None
    staff: str = ""


CHOICE_META = ("key", "req_brand", "brand", "family", "rank", "how", "status", "branch")


@app.post("/api/track")
def track(body: TrackBody):
    """What staff chose for a line: the learned crosses (dashboard → Learned crosses) are built from these."""
    a = analytics()
    if a is None:
        return {"ok": False, "reason": "analytics off"}
    if body.kind != "bulk_choice":
        raise HTTPException(422, "unknown event")
    meta = {k: (v[:200] if isinstance(v, str) else v) for k, v in (body.meta or {}).items()
            if k in CHOICE_META and isinstance(v, (str, int, float, bool))}
    if not (meta.get("key") and body.sku):
        raise HTTPException(422, "a bulk choice needs the line key and the item chosen")
    a.log_event("bulk_choice", session=body.sid[:40], principal=staff_principal(customer_key(body.customer), body.staff),
                sku=body.sku[:40], meta=meta)
    return {"ok": True}


# ------------------------------------------------------------------ orders
class OrderBody(BaseModel):
    id: str | None = None
    status: str = "draft"
    customer_no: str = ""
    customer_name: str = ""
    job: str = ""
    po: str = ""
    contact: str = ""
    branch: str = ""
    branch_name: str = ""
    level: str = ""
    staff: str = ""
    notes: str = ""
    rows: list[dict] = []
    docs: list[dict] = []
    test: bool = False                 # keep as a test case for match reports
    files: list[dict] = []             # a test case's documents: [{name, b64}], sent once


@app.get("/api/orders")
def order_list(q: str = "", limit: int = 100, customer: str = "", tests: bool = False):
    return orders().list(q[:100], limit, customer[:60], tests)


@app.get("/api/customers")
def customer_list(q: str = ""):
    return orders().customers(q[:60])


@app.post("/api/orders")
def order_save(body: OrderBody):
    if len(body.rows) > 2000:
        raise HTTPException(413, "an order holds up to 2,000 lines")
    return orders().save(body.model_dump())


@app.get("/api/orders/{oid}")
def order_get(oid: str):
    o = orders().get(oid[:40])
    if o is None:
        raise HTTPException(404, "no such order")
    return o


@app.delete("/api/orders/{oid}")
def order_delete(oid: str):
    if not orders().delete(oid[:40]):
        raise HTTPException(404, "no such order")
    return {"ok": True}


@app.get("/api/orders/{oid}/export")
def order_export(oid: str, fmt: str = Query("xlsx", pattern="^(xlsx|csv|entry)$")):
    from flexbulk.orders import to_csv, to_entry_csv, to_xlsx
    o = orders().get(oid[:40])
    if o is None:
        raise HTTPException(404, "no such order")
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", "-".join(x for x in (o["id"], o["customer_name"] or o["customer_no"], o["job"]) if x))[:80]
    if fmt == "xlsx":
        return Response(to_xlsx(o), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="{base}.xlsx"'})
    data = to_entry_csv(o) if fmt == "entry" else to_csv(o)
    name = f"{base}-order-entry.csv" if fmt == "entry" else f"{base}.csv"
    return Response(data, media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'})


if os.environ.get("FLEXBULK_WARM", "1") == "0":       # tests build the engine themselves
    app.router.on_startup.clear()


# ------------------------------------------------------------------ match reports (dashboard)
class ReportBody(BaseModel):
    source: str = "orders"
    days: int = 90
    branch: str | None = None
    level: str | None = None
    per_customer: bool = False
    file_name: str | None = None
    file_b64: str | None = None
    files: list[dict] = []             # source "documents": customers' files [{name, b64}]
    hide: bool = False                 # hide the part #s: score the description matching


_REPORT_JOB: dict = {}


@app.post("/api/admin/reports")
def report_start(request: Request, body: ReportBody):
    """Start a match report (flexbulk/report.py) in the background; the page polls GET /api/admin/reports."""
    import base64
    import threading
    from webbsearch.api.admin import require
    require(request)
    if _REPORT_JOB.get("running"):
        raise HTTPException(409, "a report is already running")
    data = base64.b64decode(body.file_b64.split(",", 1)[-1]) if body.file_b64 else None
    if data and len(data) > 30_000_000:
        raise HTTPException(413, "test file is over 30 MB")
    docs = [(str(f.get("name") or "file"), base64.b64decode(str(f.get("b64") or "").split(",", 1)[-1])) for f in body.files[:30]]
    if any(len(d) > 15_000_000 for _, d in docs):
        raise HTTPException(413, "a document is over 15 MB")
    _REPORT_JOB.clear()
    _REPORT_JOB.update(running=True, done=0, total=0, error=None, result=None, started=time.time())

    def go():
        from flexbulk.report import make
        try:
            _REPORT_JOB["result"] = make(body.source, max(1, min(body.days, 3650)), body.file_name, data, body.branch or None,
                                         body.level or None, body.per_customer,
                                         progress=lambda d, n: _REPORT_JOB.update(done=d, total=n), documents=docs,
                                         hide=body.hide)
        except Exception as ex:  # noqa: BLE001
            _REPORT_JOB["error"] = f"{type(ex).__name__}: {ex}" if not isinstance(ex, ValueError) else str(ex)
        finally:
            _REPORT_JOB["running"] = False
    threading.Thread(target=go, daemon=True).start()
    return {"ok": True}


@app.get("/api/admin/reports")
def report_list(request: Request):
    from flexbulk.report import listing
    from webbsearch.api.admin import require
    require(request)
    return {"job": {k: v for k, v in _REPORT_JOB.items()}, "reports": listing()}


@app.get("/api/admin/reports/{rid}.zip")
def report_bundle(request: Request, rid: str, inputs: bool = True):
    """The report with what's needed to work on the matching without the repo: the workbook, the matching and page
    code, the live settings, the column mapping, and (inputs=1) the documents the lines were read from."""
    from flexbulk.report import REPORTS, bundle
    from webbsearch.api.admin import require
    require(request)
    if not re.fullmatch(r"R[\w-]{6,30}", rid) or not (REPORTS / f"{rid}.xlsx").exists():
        raise HTTPException(404, "no such report")
    return Response(bundle(rid, inputs), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="flex-bulk-match-report-{rid}.zip"'})


@app.get("/api/admin/reports/{rid}.xlsx")
def report_file(request: Request, rid: str):
    from flexbulk.report import REPORTS
    from webbsearch.api.admin import require
    require(request)
    if not re.fullmatch(r"R[\w-]{6,30}", rid) or not (REPORTS / f"{rid}.xlsx").exists():
        raise HTTPException(404, "no such report")
    return FileResponse(REPORTS / f"{rid}.xlsx", filename=f"flex-bulk-match-report-{rid}.xlsx",
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ------------------------------------------------------------------ brand names and shorthand (dashboard)
class BrandNameBody(BaseModel):
    name: str
    brands: list[str] = []
    brand: str = ""


class ShorthandBody(BaseModel):
    abbr: str
    word: str = ""
    action: str = "accept"            # accept | off


def _names_changed():
    if _bom is not None:
        _bom._cache.clear()


@app.get("/api/admin/names")
def names(request: Request):
    from flexbulk.brands import get as bget
    from flexbulk.shorthand import get as sget
    from webbsearch.api.admin import require
    require(request)
    return {"brands": bget(engine()).table(), "shorthand": sget().listing()}


@app.post("/api/admin/names/brand")
def names_brand_add(request: Request, body: BrandNameBody):
    from flexbulk.brands import get as bget
    from webbsearch.api.admin import require
    require(request)
    try:
        t = bget(engine()).add(body.name, body.brands or [body.brand])
    except ValueError as ex:
        raise HTTPException(422, str(ex))
    _names_changed()
    return t


@app.post("/api/admin/names/brand/remove")
def names_brand_remove(request: Request, body: BrandNameBody):
    from flexbulk.brands import get as bget
    from webbsearch.api.admin import require
    require(request)
    _names_changed()
    return bget(engine()).remove(body.name)


@app.post("/api/admin/names/brand/dismiss")
def names_brand_dismiss(request: Request, body: BrandNameBody):
    from flexbulk.brands import get as bget
    from webbsearch.api.admin import require
    require(request)
    return bget(engine()).dismiss(body.name, body.brand)


@app.post("/api/admin/names/shorthand")
def names_shorthand(request: Request, body: ShorthandBody):
    from flexbulk.shorthand import get as sget
    from webbsearch.api.admin import require
    require(request)
    a = body.abbr.strip().lower()[:20]
    if not re.fullmatch(r"[a-z][a-z.-]{0,19}", a):
        raise HTTPException(422, "an abbreviation is letters")
    if body.action == "off":
        sget().switch_off(a)
    else:
        if not body.word.strip():
            raise HTTPException(422, "what it stands for")
        sget().accept(a, body.word[:60])
    _names_changed()
    return sget().listing()
