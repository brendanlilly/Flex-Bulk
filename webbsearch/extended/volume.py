"""Volume quotes for Extended Catalog items (v2.3.2).

A signed-in customer buying $2,500 or more of one brand's -EC items asks inside sales for a project price from the
item's details drawer. The form: quantity, needed by, job name, delivery location (pick up at the branch, or a job site
with an optional liftgate), other items of the same brand in the cart, PO or job number, contact, comments.

The server checks everything again: the items are -EC items this account may see, all of one brand; at account prices
the request comes to at least $2,500. A role that doesn't see prices (a tech), or an item without a confirmed price,
skips the dollar check and inside sales confirms the total. The request becomes a quote case on the expert desk
(answer in 2 business days) and an e-mail to the quote desk, WEBB_QUOTE_EMAIL (for now brendan.lilly@fwwebb.com). The
desk's address never reaches the page."""
from __future__ import annotations

import html as _h
import os
import re
from datetime import date, timedelta

from webbsearch.extended.service import sees_prices
from webbsearch.extended.tiers import add_business_days
from webbsearch.notify import mail

THRESHOLD = 2500.0
SLA_DAYS = 2
MAX_QTY = 100_000
MAX_LINES = 20
DEFAULT_DESK = "brendan.lilly@fwwebb.com"


class VolumeError(ValueError):
    pass


def today(e) -> date:
    """The quote calendar: the real date, or the demo data's date if that is later. The demo's data is dated Sep 25,
    and a quote sent later must be due after the day it was sent."""
    return max(e.today, date.today())


def desk_email() -> str:
    return (os.environ.get("WEBB_QUOTE_EMAIL") or DEFAULT_DESK).strip()


def _money(x: float) -> str:
    return f"${x:,.2f}"


def _item(xt, p, sku: str, branch=None) -> tuple[dict, float | None, str]:
    """(item, price this person sees or None, brand) for an -EC item the account may see, else VolumeError."""
    sku = str(sku or "").strip()[:80]
    if not sku.upper().endswith("-EC"):
        raise LookupError(sku)
    it = xt.product(sku, p, branch, 1)
    if it is None:
        raise LookupError(sku)
    price, row, _ = xt.lookup(sku, p)
    if row is None:
        raise LookupError(sku)
    return it, price, row["brand_name"]


def recent_jobs(e, p, n: int = 8) -> list[dict]:
    """The account's recent job names (and ship-to, when it isn't will call), newest first."""
    try:
        o = e.accounts.orders
        o = o[o["account_id"] == p.account_id].sort_values("order_date", ascending=False)
    except Exception:  # noqa: BLE001
        return []
    out, seen = [], set()
    for job, ship in zip(o["job_name"], o["ship_to"]):
        j = str(job or "").strip()
        if not j or j.lower() in seen:
            continue
        seen.add(j.lower())
        s = str(ship or "").strip()
        out.append({"job": j[:120], "site": "" if s.lower() in ("", "will call") else s[:200]})
        if len(out) >= n:
            break
    return out


def contact_of(e, p) -> dict:
    try:
        u = e.accounts.users.loc[p.user_id]
        return {"name": str(u.get("first_name") or ""), "email": str(u.get("email") or ""), "phone": ""}
    except Exception:  # noqa: BLE001
        return {"name": "", "email": "", "phone": ""}


def context(e, p, sku: str, branch=None, cart: list[str] | None = None) -> dict:
    """What the form starts with. LookupError when the item isn't an -EC item this account sees."""
    xt = e.extended
    it, price, brand = _item(xt, p, sku, branch)
    bi = e.availability.branch_index(branch or p.home_branch)
    same = []
    for s in (cart or [])[:60]:
        if str(s).strip().upper() == it["sku"].upper():
            continue
        try:
            o, op, ob = _item(xt, p, s, branch)
        except LookupError:
            continue
        if ob == brand:
            same.append({"sku": o["sku"], "title": o["title"], "price": op})
    return {"sku": it["sku"], "title": it["title"], "brand": brand, "threshold": int(THRESHOLD), "price": price,
            "priced": price is not None, "sees_prices": sees_prices(p), "branch_name": str(e.availability.names[bi]).split(",")[0],
            "min_date": str(today(e) + timedelta(days=1)), "max_date": str(today(e) + timedelta(days=730)),
            "contact": contact_of(e, p), "jobs": recent_jobs(e, p), "cart_same_brand": same, "answer_days": SLA_DAYS}


def _text(v, n: int) -> str:
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(v or "")).strip()[:n]


def check(e, p, body: dict, branch=None) -> dict:
    """The request, validated and priced. VolumeError with a message for the form; LookupError for an unknown item."""
    xt = e.extended
    now: date = today(e)

    def qty_of(v, what="Quantity"):
        try:
            q = int(v)
        except (TypeError, ValueError):
            q = 0
        if not (1 <= q <= MAX_QTY) or str(v).strip() not in (str(q), f"{q}.0"):
            raise VolumeError(f"{what}: a whole number from 1 to {MAX_QTY:,}.")
        return q

    q = qty_of(body.get("qty"))
    it, price, brand = _item(xt, p, body.get("sku"), branch)
    lines = [{"sku": it["sku"], "qty": q, "price": price, "title": it["title"]}]
    for ln in (body.get("lines") or [])[:MAX_LINES]:
        if not isinstance(ln, dict):
            continue
        try:
            o, op, ob = _item(xt, p, ln.get("sku"), branch)
        except LookupError:
            raise VolumeError("Other items: one of them isn't in the Extended Catalog for this account.")
        if ob != brand:
            raise VolumeError(f"Other items: only items of the same brand ({brand}) count toward a volume quote.")
        if any(x["sku"] == o["sku"] for x in lines):
            continue
        lines.append({"sku": o["sku"], "qty": qty_of(ln.get("qty"), f"Quantity of {o['sku']}"), "price": op,
                      "title": o["title"]})
    try:
        need = date.fromisoformat(str(body.get("needed_by") or "")[:10])
    except ValueError:
        raise VolumeError("Needed by: a date after today.")
    if not (now < need <= now + timedelta(days=730)):
        raise VolumeError("Needed by: a date after today and within two years.")
    job = _text(body.get("job_name"), 120)
    if not job:
        raise VolumeError("Job name: required.")
    d = body.get("delivery") if isinstance(body.get("delivery"), dict) else {}
    mode = d.get("mode")
    if mode == "pickup":
        delivery = {"mode": "pickup"}
    elif mode == "jobsite":
        addr = _text(d.get("address"), 300)
        if len(addr) < 5:
            raise VolumeError("Delivery location: enter the job site address.")
        delivery = {"mode": "jobsite", "address": addr, "liftgate": bool(d.get("liftgate"))}
    else:
        raise VolumeError("Delivery location: pick up at the branch, or a job site.")
    c = body.get("contact") if isinstance(body.get("contact"), dict) else {}
    contact = {"name": _text(c.get("name"), 80), "email": _text(c.get("email"), 120), "phone": _text(c.get("phone"), 40)}
    if not contact["email"] and not contact["phone"]:
        raise VolumeError("Contact: an e-mail or phone number so inside sales can reach you.")
    if contact["email"] and not mail.EMAIL.match(contact["email"]):
        raise VolumeError("Contact: that e-mail address doesn't look right.")
    if contact["phone"] and len(re.sub(r"\D", "", contact["phone"])) < 7:
        raise VolumeError("Contact: that phone number is too short.")
    # the dollar check runs whenever this person sees the main item's price; items without a confirmed price add
    # nothing toward it (one unpriced line never waives the minimum), and then inside sales confirms the total
    checked = sees_prices(p) and lines[0]["price"] is not None
    subtotal = round(sum(x["qty"] * x["price"] for x in lines if x["price"] is not None), 2)
    if checked and subtotal < THRESHOLD:
        raise VolumeError(f"Volume quotes start at {_money(THRESHOLD)[:-3]} of {brand}. This request comes to "
                          f"{_money(subtotal)}: add quantity or other {brand} items.")
    priced = checked and all(x["price"] is not None for x in lines)
    total = subtotal if priced else None
    if not sees_prices(p):
        for x in lines:
            x["price"] = None
    return {"brand": brand, "lines": lines, "total": total, "priced": priced, "needed_by": str(need), "job_name": job,
            "delivery": delivery, "contact": contact, "po": _text(body.get("po"), 40),
            "comments": _text(body.get("comments"), 2000), "supplier": (it.get("supplier") or {}).get("name") or brand,
            "vendor_code": (it.get("erp") or {}).get("vendor_code")}


def _message(case: dict, req: dict, p, branch_name: str, due: date):
    acct = f"{p.account_name or 'Account'} (#{p.account_id})"
    where = (f"Pick up at {branch_name}" if req["delivery"]["mode"] == "pickup" else
             f"Deliver to job site: {req['delivery']['address']}"
             + (" · needs a liftgate" if req["delivery"].get("liftgate") else ""))
    tot = _money(req["total"]) + " at account prices" if req["total"] is not None else "to be confirmed by inside sales"
    c = req["contact"]
    who = ", ".join(x for x in (c["name"], c["email"], c["phone"]) if x)
    rows = [(f"{x['qty']:,} × {x['sku']}", x["title"], _money(x["price"]) if x["price"] is not None else "price to confirm",
             _money(x["qty"] * x["price"]) if x["price"] is not None else "") for x in req["lines"]]
    text = "\n".join([
        f"Volume quote request {case['id']} (Extended Catalog)", "",
        f"Account: {acct}", f"Requested by: {who} ({p.role})", f"Branch: {branch_name}",
        f"Brand: {req['brand']} (vendor {req['vendor_code'] or 'n/a'})", f"Estimated total: {tot}",
        f"Needed by: {req['needed_by']}", f"Job name: {req['job_name']}",
        *( [f"PO or job number: {req['po']}"] if req["po"] else [] ),
        f"Delivery location: {where}", "", "Items:",
        *[f"  {a}  {b}  @ {pr}{('  = ' + ext) if ext else ''}" for a, b, pr, ext in rows], "",
        "Comments:", req["comments"] or "(none)", "",
        f"Answer due: {due} ({SLA_DAYS} business days). Reply to this e-mail to reach the customer.",
        "Sent from FlexSearch (prototype)."])
    esc = _h.escape
    trs = "".join(f"<tr><td>{esc(a)}</td><td>{esc(b)}</td><td style='text-align:right'>{esc(pr)}</td>"
                  f"<td style='text-align:right'>{esc(ext)}</td></tr>" for a, b, pr, ext in rows)
    facts = [("Account", acct), ("Requested by", f"{who} ({p.role})"), ("Branch", branch_name), ("Brand", req["brand"]),
             ("Estimated total", tot), ("Needed by", req["needed_by"]), ("Job name", req["job_name"])] + \
        ([("PO or job number", req["po"])] if req["po"] else []) + [("Delivery location", where)]
    html = (f"<div style='font-family:Arial,sans-serif;font-size:14px;color:#222'>"
            f"<h2 style='margin:0 0 8px'>Volume quote request {esc(case['id'])}</h2>"
            f"<table cellpadding='4' style='border-collapse:collapse'>"
            + "".join(f"<tr><td style='color:#666'>{esc(k)}</td><td><b>{esc(v)}</b></td></tr>" for k, v in facts) +
            f"</table><h3>Items</h3><table cellpadding='4' border='1' style='border-collapse:collapse'>{trs}</table>"
            f"<h3>Comments</h3><p>{esc(req['comments'] or '(none)')}</p>"
            f"<p style='color:#666'>Answer due {esc(str(due))} ({SLA_DAYS} business days). Reply to reach the customer.</p></div>")
    total_s = f", {_money(req['total'])[:-3]} est." if req["total"] is not None else ""
    subject = f"Volume quote {case['id']}: {req['brand']}{total_s} · {p.account_name or p.account_id} · {req['job_name']}"
    return mail.compose(to=desk_email(), subject=subject, text=text, html=html, reply_to=c["email"] or None)


def open_quote(desk, e, p, req: dict, branch_id: str, branch_name: str) -> dict:
    due = add_business_days(today(e), SLA_DAYS)
    n = sum(x["qty"] for x in req["lines"])
    q = (f"Volume quote: {req['brand']}, {len(req['lines'])} items ({n:,} units)"
         + (f", {_money(req['total'])} at account prices" if req["total"] is not None else "")
         + f"; job {req['job_name']}; needed by {req['needed_by']}")
    case = desk.open(question=q[:2000], conversation=[], sku=req["lines"][0]["sku"], line=None, tier=4, channel="quote",
                     principal=p, branch=branch_id, draft={"kind": "volume", **req, "due": str(due)})
    mail.queue(case["id"], _message(case, req, p, branch_name, due))
    return {"id": case["id"], "due": str(due), "branch_name": branch_name, "brand": req["brand"], "total": req["total"],
            "priced": req["priced"]}
