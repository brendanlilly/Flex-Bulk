"""From search or question to order (v2.2): checkout handoff, orders, and the funnels on the dashboard.

  start()         the cart, priced on the server for the signed-in account, becomes a checkout: a row here, a
                  begin_checkout event, and a signed token (JWT, HS256) the storefront's checkout imports. Every line
                  keeps where it came from: the source (search, knowledge, ask, compare, bulk...), the search id or the
                  Knowledge Center answer id, and its position.
  record_order()  an order placed in checkout (the storefront's signed webhook, or the demo checkout) becomes one
                  purchase event per line, credited to the line's source. The same order twice is counted once.
  report()        FlexSearch's funnel (searches, clicks, views, adds, orders, revenue), the Knowledge Center's
                  (questions, answers, sources opened, parts added, orders, revenue, expert hand-offs), adds and
                  revenue by source, the cart (checkouts, orders, abandonment) and a daily series.

Revenue is credited to the place the item was added from (last touch per line); an item added twice keeps the first
place it was added. Prices in the token and the order are the account's, from the server, never the page's."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
import uuid
from collections import Counter, defaultdict

KC_SOURCES = {"knowledge", "kc_code", "kc_diagram", "kc_submittal"}
# what FlexSearch found for the customer; reorders, lists, account pages and orders with no checkout aren't search's
FS_SOURCES = {"search", "compare", "ask", "quick_view", "product_page", "home", "recs", "photo", "bulk", "kit", "xref",
              "advisor"}
SOURCES = ("search", "compare", "ask", "knowledge", "kc_code", "kc_diagram", "product_page", "quick_view", "home", "recs",
           "bulk", "kit", "xref", "reorder", "list", "advisor", "account", "photo", "cart", "other")
TOKEN_TTL = 1800                       # the storefront has 30 minutes to import a checkout
SCHEMA = """
CREATE TABLE IF NOT EXISTS checkouts (
  id TEXT PRIMARY KEY, ts REAL, day TEXT, session TEXT, account TEXT, user TEXT, branch TEXT, lines TEXT, value REAL,
  status TEXT, order_id TEXT);
CREATE INDEX IF NOT EXISTS co_day ON checkouts(day);
CREATE TABLE IF NOT EXISTS orders (
  order_id TEXT PRIMARY KEY, ts REAL, day TEXT, checkout_id TEXT, account TEXT, lines TEXT, total REAL, via TEXT);
CREATE INDEX IF NOT EXISTS o_day ON orders(day);
CREATE TABLE IF NOT EXISTS checkout_plans (id TEXT PRIMARY KEY, plan TEXT, returns_ack INTEGER DEFAULT 0);
"""


def _day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def clean_attr(attr) -> dict:
    """Where a cart line came from, as the page reports it: only known keys, short values."""
    a = attr if isinstance(attr, dict) else {}
    out = {}
    src = str(a.get("source") or "").strip().lower()[:30]
    out["source"] = src if src in SOURCES else "other"
    if isinstance(a.get("search_id"), str) and re.fullmatch(r"[0-9a-f]{16}", a["search_id"]):
        out["search_id"] = a["search_id"]
    if isinstance(a.get("qa_id"), str) and re.fullmatch(r"[0-9A-Za-z]{1,24}", a["qa_id"]):
        out["qa_id"] = a["qa_id"]
    if isinstance(a.get("pos"), int) and 0 <= a["pos"] <= 10_000:
        out["pos"] = a["pos"]
    return out


# ------------------------------------------------------------------ the handoff token (JWT, HS256)
def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_token(payload: dict, secret: str) -> str:
    head = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(secret.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest()
    return f"{head}.{body}.{_b64(sig)}"


def verify_token(token: str, secret: str, now: float | None = None) -> dict | None:
    try:
        head, body, sig = token.split(".")
        want = hmac.new(secret.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(want, _unb64(sig)):
            return None
        payload = json.loads(_unb64(body))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("exp") and (now or time.time()) > payload["exp"]:
        return None
    return payload


def sign_body(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_body(body: bytes, signature: str, secret: str) -> bool:
    return bool(secret) and hmac.compare_digest(sign_body(body, secret), signature or "")


class _Who:
    """The account an order belongs to, for the purchase events (role is unknown to the webhook)."""

    def __init__(self, account: str | None):
        self.account_id, self.role = account or None, "customer"


class Commerce:
    def __init__(self, analytics):
        self.a = analytics
        with self.a.lock:
            self.a.db.executescript(SCHEMA)

    # ------------------------------------------------------------------ checkout
    def start(self, *, lines: list[dict], principal, session: str = "", branch: str | None = None,
              secret: str = "", checkout_url: str = "", plan: dict | None = None) -> dict:
        """`lines`: [{sku, qty, price, title, attr}] already priced by the server. `plan`: the cart grouped by how it
        arrives (v2.3, extended/cart.py): groups, freight, return terms, routing."""
        cid = uuid.uuid4().hex[:12]
        ts = time.time()
        value = round(sum((ln.get("price") or 0) * ln["qty"] for ln in lines), 2)
        with self.a.lock:
            self.a.db.execute("INSERT INTO checkouts (id, ts, day, session, account, user, branch, lines, value, status) "
                              "VALUES (?,?,?,?,?,?,?,?,?,?)",
                              (cid, ts, _day(ts), (session or "")[:40], str(getattr(principal, "account_id", "") or ""),
                               str(getattr(principal, "user_id", "") or ""), branch or "", json.dumps(lines), value, "started"))
            if plan is not None:
                self.a.db.execute("INSERT OR REPLACE INTO checkout_plans (id, plan) VALUES (?,?)",
                                  (cid, json.dumps(plan)))
        srcs = Counter(ln["attr"].get("source", "other") for ln in lines)
        self.a.log_event("begin_checkout", session=session, principal=principal, qty=sum(ln["qty"] for ln in lines),
                         value=value, meta={"checkout_id": cid, "lines": len(lines), "sources": dict(srcs)})
        payload = {"iss": "flexsearch", "checkout_id": cid, "account": getattr(principal, "account_id", None),
                   "branch": branch, "iat": int(ts), "exp": int(ts) + TOKEN_TTL,
                   "lines": [{"sku": ln["sku"], "qty": ln["qty"], "price": ln.get("price")} for ln in lines]}
        if plan is not None:     # what the storefront needs to route the order: groups, ship-tos, freight, POs
            payload["plan"] = {"groups": [{"key": g["key"], "ship_to": g["ship_to"],
                                           "freight": (g.get("freight") or {}).get("amount")} for g in plan["groups"]],
                               "returns_ack_required": plan["returns_ack_required"],
                               "erp_lines": plan["routing"]["erp_lines"], "vendor_pos": plan["routing"]["vendor_pos"]}
        token = make_token(payload, secret) if secret else ""
        redirect = (f"{checkout_url}{'&' if '?' in checkout_url else '?'}token={token}" if checkout_url and token
                    else f"/checkout/demo/{cid}")
        return {"checkout_id": cid, "lines": lines, "value": value, "token": token, "redirect": redirect}

    def checkout(self, cid: str) -> dict | None:
        r = self.a._q("SELECT id, ts, account, user, branch, lines, value, status, order_id FROM checkouts WHERE id=?",
                      (cid,))
        if not r:
            return None
        x = r[0]
        pl = self.a._q("SELECT plan, returns_ack FROM checkout_plans WHERE id=?", (cid,))
        return {"checkout_id": x[0], "ts": x[1], "account": x[2], "user": x[3], "branch": x[4],
                "lines": json.loads(x[5] or "[]"), "value": x[6], "status": x[7], "order_id": x[8],
                "plan": json.loads(pl[0][0]) if pl and pl[0][0] else None,
                "returns_ack": bool(pl[0][1]) if pl else False}

    def acknowledge_returns(self, cid: str) -> None:
        with self.a.lock:
            self.a.db.execute("UPDATE checkout_plans SET returns_ack = 1 WHERE id=?", (cid,))

    # ------------------------------------------------------------------ orders
    def record_order(self, order: dict, via: str = "webhook") -> dict:
        if not isinstance(order, dict):
            raise ValueError("an order is a JSON object")
        oid = str(order.get("order_id") or "").strip()[:40]
        if not oid:
            raise ValueError("an order needs its order_id")
        if self.a._q("SELECT 1 FROM orders WHERE order_id=?", (oid,)):
            return {"ok": True, "duplicate": True, "order_id": oid}
        co = self.checkout(str(order.get("checkout_id") or "")) if order.get("checkout_id") else None
        if co and co["status"] == "ordered":          # one checkout, one order: the demo and a webhook can't both credit it
            return {"ok": True, "duplicate": True, "order_id": co["order_id"]}
        attr = {}
        for ln in (co or {}).get("lines", []):
            attr.setdefault(str(ln["sku"]), ln.get("attr") or {})
        lines = []
        for ln in order.get("lines") if isinstance(order.get("lines"), list) else []:
            if not isinstance(ln, dict):
                continue
            try:
                sku, qty, price = str(ln["sku"])[:40], int(ln.get("qty") or 0), float(ln.get("price") or 0)
            except (KeyError, TypeError, ValueError):
                continue
            if qty <= 0 or price < 0 or price > 1e6:
                continue
            lines.append({"sku": sku, "qty": qty, "price": price, "attr": attr.get(sku, {"source": "other"})})
        total = round(sum(x["qty"] * x["price"] for x in lines), 2)
        account = str(order.get("account") or (co or {}).get("account") or "")
        ts = time.time()
        with self.a.lock:
            n = self.a.db.execute("INSERT OR IGNORE INTO orders (order_id, ts, day, checkout_id, account, lines, total, via) "
                                  "VALUES (?,?,?,?,?,?,?,?)", (oid, ts, _day(ts), (co or {}).get("checkout_id"), account,
                                                               json.dumps(lines), total, via)).rowcount
            if not n:                                   # the same order arriving twice at once
                return {"ok": True, "duplicate": True, "order_id": oid}
            if co:
                self.a.db.execute("UPDATE checkouts SET status='ordered', order_id=? WHERE id=?", (oid, co["checkout_id"]))
        who = _Who(account)
        for x in lines:
            self.a.log_event("purchase", search_id=x["attr"].get("search_id"), principal=who, sku=x["sku"], qty=x["qty"],
                             value=round(x["qty"] * x["price"], 2),
                             meta={"order_id": oid, "checkout_id": (co or {}).get("checkout_id"), **x["attr"]})
        # one event for the whole order, for tools that want the order (GA4's purchase), with the checkout's session
        sess = self.a._q("SELECT session FROM checkouts WHERE id=?", (co["checkout_id"],))[0][0] if co else ""
        self.a._forward({"kind": "order", "ts": ts, "session": sess or f"order:{oid}", "account": account, "value": total,
                         "meta": {"order_id": oid, "checkout_id": (co or {}).get("checkout_id")},
                         "items": [{"sku": x["sku"], "qty": x["qty"], "price": x["price"],
                                    "source": x["attr"].get("source", "other")} for x in lines]})
        return {"ok": True, "duplicate": False, "order_id": oid, "lines": len(lines), "total": total}

    # ------------------------------------------------------------------ the report
    def report(self, days: int = 30, now: float | None = None) -> dict:
        now = now or time.time()
        since = _day(now - days * 86400)
        searches = self.a._q("SELECT id, q, outcome, day FROM searches WHERE day >= ?", (since,))
        ev = [(k, sid, sku, qty, val, json.loads(m or "{}"), day) for k, sid, sku, qty, val, m, day in
              self.a._q("SELECT kind, search_id, sku, qty, value, meta, day FROM events WHERE day >= ?", (since,))]
        by_kind = defaultdict(list)
        for e in ev:
            by_kind[e[0]].append(e)

        def is_kc(meta):
            return meta.get("source") in KC_SOURCES or bool(meta.get("qa_id"))

        def is_fs(meta):
            return not is_kc(meta) and (meta.get("source") in FS_SOURCES)

        def sids(*kinds):
            return {e[1] for k in kinds for e in by_kind[k] if e[1]}

        n_s = len(searches)
        q_of = {s[0]: s[1] for s in searches}
        fs_buy = [e for e in by_kind["purchase"] if is_fs(e[5])]
        kc_buy = [e for e in by_kind["purchase"] if is_kc(e[5])]
        fs_add = [e for e in by_kind["add_to_cart"] if is_fs(e[5])]
        kc_add = [e for e in by_kind["add_to_cart"] if is_kc(e[5])]
        from webbsearch.merch.analytics import ENGAGED
        # click-through has one definition (analytics.ENGAGED), so Overview and Commerce agree on the same window
        with_click, with_view, with_add = sids(*ENGAGED), sids("view_item", "quick_view"), \
            sids("add_to_cart")

        def rate(a, b):
            return round(a / b, 4) if b else None

        rev_q = defaultdict(lambda: {"revenue": 0.0, "orders": set(), "adds": 0})
        for e in fs_buy:
            if e[1] in q_of:
                d = rev_q[q_of[e[1]]]
                d["revenue"] += e[4] or 0
                d["orders"].add(e[5].get("order_id"))
        for e in by_kind["add_to_cart"]:
            if e[1] in q_of:
                rev_q[q_of[e[1]]]["adds"] += 1
        n_q = Counter(s[1] for s in searches)
        top_q = sorted(({"q": q, "searches": n_q[q], "adds": d["adds"], "orders": len(d["orders"]),
                         "revenue": round(d["revenue"], 2)} for q, d in rev_q.items()),
                       key=lambda x: (-x["revenue"], -x["adds"]))[:15]
        fs_orders = {e[5].get("order_id") for e in fs_buy}
        flexsearch = {
            "searches": n_s, "zero": sum(1 for s in searches if s[2] == "zero"),
            "with_click": len(with_click & set(q_of)), "with_view": len(with_view & set(q_of)),
            "with_add": len(with_add & set(q_of)), "adds": len(fs_add),
            "add_value": round(sum(e[4] or 0 for e in fs_add), 2), "orders": len(fs_orders),
            "revenue": round(sum(e[4] or 0 for e in fs_buy), 2), "top_queries": top_q}
        flexsearch.update({"click_rate": rate(flexsearch["with_click"], n_s), "add_rate": rate(flexsearch["with_add"], n_s),
                           "conversion": rate(len(fs_orders), n_s),
                           "revenue_per_search": round(flexsearch["revenue"] / n_s, 2) if n_s else None})

        asks = by_kind["kc_ask"]
        kinds = Counter(e[5].get("kind") or "answer" for e in asks)
        kc_orders = {e[5].get("order_id") for e in kc_buy}
        answered = sum(n for k, n in kinds.items() if k in ("answer", "error_code", "serial"))
        knowledge = {
            "questions": len(asks), "answered": answered, "answer_rate": rate(answered, len(asks)),
            "flexiq_share": rate(sum(1 for e in asks if e[5].get("engine") == "flexiq"), len(asks)),
            "by_kind": dict(kinds), "by_tier": dict(Counter(str(e[5].get("tier", 0)) for e in asks)),
            "flags": dict(Counter(f for e in asks for f in (e[5].get("flags") or []))),
            "codes": len(by_kind["kc_code"]), "serials": len(by_kind["kc_serial"]), "diagrams": len(by_kind["kc_diagram"]),
            "doc_opens": len(by_kind["kc_doc"]), "source_opens": len(by_kind["kc_source"]),
            "experts": len(by_kind["kc_expert"]), "submittals": len(by_kind["kc_submittal"]),
            "feedback": dict(Counter(e[5].get("vote") for e in by_kind["kc_feedback"])),
            "adds": len(kc_add), "add_value": round(sum(e[4] or 0 for e in kc_add), 2),
            "orders": len(kc_orders), "revenue": round(sum(e[4] or 0 for e in kc_buy), 2)}
        knowledge["adds_per_question"] = rate(len(kc_add), len(asks))
        knowledge["conversion"] = rate(len(kc_orders), len(asks))

        src = defaultdict(lambda: {"adds": 0, "add_value": 0.0, "lines_bought": 0, "revenue": 0.0})
        for e in by_kind["add_to_cart"]:
            d = src[e[5].get("source") or "other"]
            d["adds"] += 1
            d["add_value"] += e[4] or 0
        for e in by_kind["purchase"]:
            d = src[e[5].get("source") or "other"]
            d["lines_bought"] += 1
            d["revenue"] += e[4] or 0
        sources = sorted(({"source": k, "adds": v["adds"], "add_value": round(v["add_value"], 2),
                           "lines_bought": v["lines_bought"], "revenue": round(v["revenue"], 2)} for k, v in src.items()),
                         key=lambda x: (-x["revenue"], -x["adds"]))

        cos = self.a._q("SELECT id, status, value FROM checkouts WHERE day >= ?", (since,))
        orders = self.a._q("SELECT order_id, total, checkout_id FROM orders WHERE day >= ?", (since,))
        ordered_cos = {o[2] for o in orders if o[2]}
        cart = {"adds": len(by_kind["add_to_cart"]), "removes": len(by_kind["remove_from_cart"]), "checkouts": len(cos),
                "checkout_value": round(sum(c[2] or 0 for c in cos), 2), "orders": len(orders),
                "revenue": round(sum(o[1] or 0 for o in orders), 2),
                "abandon_rate": rate(len([c for c in cos if c[0] not in ordered_cos]), len(cos)),
                "avg_order": round(sum(o[1] or 0 for o in orders) / len(orders), 2) if orders else None}

        days_ = defaultdict(lambda: {"searches": 0, "questions": 0, "adds": 0, "checkouts": 0, "orders": set(), "revenue": 0.0})
        for s in searches:
            days_[s[3]]["searches"] += 1
        for e in ev:
            d = days_[e[6]]
            if e[0] == "kc_ask":
                d["questions"] += 1
            elif e[0] == "add_to_cart":
                d["adds"] += 1
            elif e[0] == "begin_checkout":
                d["checkouts"] += 1
            elif e[0] == "purchase":
                d["orders"].add(e[5].get("order_id"))
                d["revenue"] += e[4] or 0
        series = [{"day": k, **{x: v[x] for x in ("searches", "questions", "adds", "checkouts")},
                   "orders": len(v["orders"]), "revenue": round(v["revenue"], 2)} for k, v in sorted(days_.items())]
        return {"days": days, "since": since, "flexsearch": flexsearch, "knowledge": knowledge, "sources": sources,
                "cart": cart, "series": series}
