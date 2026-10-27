"""Search analytics: what people searched, what they clicked and added, what Bulk order couldn't find.

One SQLite file (WAL). The server writes a `searches` row for every results page and `events` rows for
clicks, quick views, expands, add-to-cart, add-to-list (sent by the page with the search id and position),
Bulk order lines and Ask questions, and a `feedback` row for each thumbs up or down on a results page or an Ask answer
(v1.9: the vote, the reasons and comment that may follow, the search it was about, a triage status for the eCommerce
team). Insights are plain SQL over a date window.
"""
from __future__ import annotations

import contextvars
import json
import re
import sqlite3
import threading
import time
import uuid
from collections import Counter
from pathlib import Path

from webbsearch.merch.store import norm_q

CLICKS = ("click", "quick_view", "expand")
# v2.4 (review G13): one definition of click-through everywhere (Overview, Commerce, rules, A/B tests): a search counts as
# clicked when its results got a click, a quick view, an expand or an add to cart. Test traffic is never logged.
ENGAGED = CLICKS + ("add_to_cart",)
METRIC_DEFINITIONS = {
    "searches": "Searches typed on the results page, once each; test traffic (QA suites, automated browsers, a browser "
                "marked with ?test=1) is not counted.",
    "ctr": "Click-through: searches whose results got a click, quick view, expand or add to cart, divided by searches.",
    "atc_rate": "Add to cart: searches with an add to cart from their results, divided by searches.",
    "zero_rate": "No results: searches that matched nothing, or nothing with every word (the nearest were shown).",
    "avg_pos": "Average clicked position: the best position clicked or added per search, over searches with one.",
}
# requests that are tests, not customers: the server marks them (header, cookie, automated browser) and nothing is logged
test_traffic: contextvars.ContextVar[bool] = contextvars.ContextVar("test_traffic", default=False)
_AUTOMATION = re.compile(r"HeadlessChrome|Playwright|Puppeteer|Selenium|webdriver|PhantomJS|python-requests|curl/|"
                         r"\bbot\b|crawler|spider|Lighthouse", re.I)


def is_test_request(headers, cookies) -> bool:
    """X-Webb-QA: 1 (the QA suites), the webb_test cookie (/?test=1 sets it), or an automated browser's user agent."""
    if headers.get("x-webb-qa") == "1" or cookies.get("webb_test") == "1":
        return True
    return bool(_AUTOMATION.search(headers.get("user-agent") or ""))
SCHEMA = """
CREATE TABLE IF NOT EXISTS searches (
  id TEXT PRIMARY KEY, ts REAL, day TEXT, session TEXT, account TEXT, user TEXT, role TEXT, segment TEXT,
  q TEXT, qn TEXT, total INTEGER, latency REAL, personalized INTEGER, rewritten_from TEXT, rules TEXT,
  top TEXT, branch TEXT, outcome TEXT);
CREATE INDEX IF NOT EXISTS s_day ON searches(day);
CREATE INDEX IF NOT EXISTS s_qn ON searches(qn);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, day TEXT, search_id TEXT, session TEXT, account TEXT,
  role TEXT, kind TEXT, sku TEXT, pos INTEGER, qty INTEGER, value REAL, meta TEXT);
CREATE INDEX IF NOT EXISTS e_day ON events(day);
CREATE INDEX IF NOT EXISTS e_search ON events(search_id);
CREATE INDEX IF NOT EXISTS e_kind ON events(kind);
CREATE TABLE IF NOT EXISTS feedback (
  id TEXT PRIMARY KEY, ts REAL, day TEXT, updated REAL, kind TEXT, search_id TEXT, session TEXT, account TEXT,
  user TEXT, role TEXT, segment TEXT, q TEXT, qn TEXT, vote INTEGER, reasons TEXT, comment TEXT, context TEXT,
  status TEXT, note TEXT, handled_by TEXT, handled_at REAL);
CREATE INDEX IF NOT EXISTS f_day ON feedback(day);
CREATE INDEX IF NOT EXISTS f_qn ON feedback(qn);
CREATE INDEX IF NOT EXISTS f_search ON feedback(search_id);
CREATE TABLE IF NOT EXISTS llm_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, day TEXT, model TEXT, purpose TEXT, status TEXT,
  input INTEGER, output INTEGER, cache_read INTEGER, cache_write INTEGER, ms REAL, cost REAL);
CREATE INDEX IF NOT EXISTS l_day ON llm_calls(day);
CREATE TABLE IF NOT EXISTS signins (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, day TEXT, ip TEXT, kind TEXT, ok INTEGER, ua TEXT);
CREATE INDEX IF NOT EXISTS si_ts ON signins(ts);
"""
SIGNIN_KEEP_DAYS = 90                      # sign-in records (with IP addresses) are deleted after this
EVENT_KINDS = ("click", "quick_view", "expand", "add_to_cart", "add_to_list", "bulk_line", "bulk_choice", "bulk_upload",
               "ask", "zero_suggest", "photo_search", "start_page", "results_mode", "fits", "compare", "kc_expert", "kc_submittal",
               "volume_quote",  # v2.3.2
               "filter",        # v2.42: a filter turned on or off on the results page
               # v2.2: views, the cart, checkout and orders; the Knowledge Center's own steps
               "view_item", "remove_from_cart", "begin_checkout", "purchase", "kc_ask", "kc_code", "kc_serial", "kc_diagram",
               "kc_doc", "kc_source", "kc_feedback")
# feedback on a results page or an Ask answer (v1.9)
FEEDBACK_KINDS = ("search", "ask")
FEEDBACK_REASONS = {"wrong": "Wrong products or answer", "missing": "Didn't find what I needed", "price_stock": "Price or stock looks wrong",
                    "slow": "Too slow", "other": "Something else"}
FEEDBACK_STATUS = ("new", "reviewed", "done")
FEEDBACK_PER_SESSION_DAY = 200             # a browser session can't flood the table
CONTEXT_KEYS = ("tab", "sort", "view", "filters", "total", "top", "notice", "photo", "answer", "engine", "ask_kind", "items",
                "branch", "page")


def _day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


class Analytics:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        if "detail" not in {r[1] for r in self.db.execute("PRAGMA table_info(llm_calls)")}:
            self.db.execute("ALTER TABLE llm_calls ADD COLUMN detail TEXT")
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(searches)")}
        for c in ("outcome", "variant"):
            if c not in cols:
                self.db.execute(f"ALTER TABLE searches ADD COLUMN {c} TEXT")
        self.forwarder = None          # v2.2: connectors.Forwarder when GA4, Datadog or a webhook is configured

    # ------------------------------------------------------------------ write
    def log_search(self, *, q: str, principal, total: int, latency_ms: float, personalized: bool, session: str = "",
                   rewritten_from: str | None = None, rules: list | None = None, top: list | None = None,
                   branch: str | None = None, ts: float | None = None, outcome: str = "", variant: str = "") -> str:
        """outcome: "" (products matched), "zero" (nothing), "fallback" (nothing matched every word; nearest shown),
        "answered" (no products, but an exact Webb item or cross-reference answered it)."""
        sid = uuid.uuid4().hex[:16]
        ts = ts or time.time()
        if test_traffic.get():
            return sid                   # v2.4: a test search gets an id for the page, and no row
        with self.lock:
            self.db.execute("INSERT INTO searches (id, ts, day, session, account, user, role, segment, q, qn, total, latency, "
                            "personalized, rewritten_from, rules, top, branch, outcome, variant) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                sid, ts, _day(ts), (session or "")[:40], str(principal.account_id or ""), str(principal.user_id or ""),
                principal.role or "anonymous", principal.segment or "", q[:300], norm_q(q)[:300], int(total),
                round(float(latency_ms), 1), int(bool(personalized)), rewritten_from, json.dumps(rules or []),
                json.dumps((top or [])[:24]), branch or "", outcome or ("zero" if not total else ""), variant or ""))
        self._forward({"kind": "search", "ts": ts, "search_id": sid, "session": (session or "")[:40],
                       "account": str(principal.account_id or ""), "role": principal.role or "anonymous", "q": q[:300],
                       "total": int(total)})
        return sid

    def _forward(self, event: dict) -> None:
        f = self.forwarder
        if f is not None:
            try:
                f.enqueue(event)
            except Exception:
                pass                     # forwarding never breaks the page or the log

    def log_event(self, kind: str, *, search_id: str | None = None, session: str = "", principal=None,
                  sku: str | None = None, pos: int | None = None, qty: int | None = None, value: float | None = None,
                  meta: dict | None = None, ts: float | None = None) -> None:
        if kind not in EVENT_KINDS:
            raise ValueError(f"unknown event kind {kind}")
        if test_traffic.get():
            return
        ts = ts or time.time()
        with self.lock:
            self.db.execute("INSERT INTO events (ts, day, search_id, session, account, role, kind, sku, pos, qty, value, meta) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
                                ts, _day(ts), search_id, (session or "")[:40],
                                str(getattr(principal, "account_id", "") or ""), getattr(principal, "role", None) or "anonymous",
                                kind, sku, pos, qty, value, json.dumps(meta or {})[:4000]))
        self._forward({"kind": kind, "ts": ts, "search_id": search_id, "session": (session or "")[:40],
                       "account": str(getattr(principal, "account_id", "") or ""),
                       "role": getattr(principal, "role", None) or "anonymous", "sku": sku, "pos": pos, "qty": qty,
                       "value": value, "meta": meta or {}})

    def search_exists(self, sid: str) -> bool:
        return bool(self._q("SELECT 1 FROM searches WHERE id=?", (sid,)))

    def same_search(self, sid: str, q: str) -> bool:
        """`sid` is a logged search for the same words (the page refetching with a filter)."""
        return bool(self._q("SELECT 1 FROM searches WHERE id=? AND qn=?", (sid, norm_q(q)[:300])))

    def clear(self) -> dict:
        with self.lock:
            n = self.db.execute("SELECT (SELECT COUNT(*) FROM searches), (SELECT COUNT(*) FROM events), "
                                "(SELECT COUNT(*) FROM feedback)").fetchone()
            self.db.execute("DELETE FROM events")
            self.db.execute("DELETE FROM searches")
            self.db.execute("DELETE FROM feedback")
        return {"searches": n[0], "events": n[1], "feedback": n[2]}

    # ------------------------------------------------------------------ feedback (v1.9)
    def save_feedback(self, *, kind: str = "search", fid: str | None = None, search_id: str | None = None, session: str = "",
                      principal=None, q: str = "", vote: int | None = None, reasons: list | None = None,
                      comment: str | None = None, context: dict | None = None, ts: float | None = None) -> str:
        """A thumbs up (+1) or down (-1) on a results page or an Ask answer, then optionally reasons and a comment.
        One row per search (or answer) per browser session: a changed vote or a later comment updates it. The query,
        result count and top items come from the logged search when there is one, not from the page."""
        if kind not in FEEDBACK_KINDS:
            raise ValueError("feedback is for a search or an Ask answer")
        if vote is not None and vote not in (1, -1):
            raise ValueError("vote is 1 (helpful) or -1 (not helpful)")
        rs = None if reasons is None else list(dict.fromkeys(r for r in reasons if r in FEEDBACK_REASONS))[:5]
        if reasons and not rs:
            raise ValueError("unknown reason")
        cm = None if comment is None else " ".join(str(comment).split())[:1000]
        ctx = {k: v for k, v in (context or {}).items() if k in CONTEXT_KEYS}
        if len(json.dumps(ctx)) > 6000:
            raise ValueError("context too large")
        ts = ts or time.time()
        sess = (session or "")[:40]
        sid = search_id if search_id and len(search_id) == 16 and all(c in "0123456789abcdef" for c in search_id) else None
        with self.lock:
            row = None
            if fid:
                row = self.db.execute("SELECT id, context FROM feedback WHERE id=? AND session=?", (fid, sess)).fetchone()
                if row is None:
                    raise KeyError("no such feedback in this session")
            elif kind == "search" and sid:
                row = self.db.execute("SELECT id, context FROM feedback WHERE kind='search' AND search_id=? AND session=?",
                                      (sid, sess)).fetchone()
            logged = self.db.execute("SELECT q, total, top, outcome, variant, branch FROM searches WHERE id=?",
                                     (sid,)).fetchone() if sid else None
            if logged:                     # the server's record of the search wins over what the page sent
                q = logged[0]
                ctx.update(total=logged[1], top=json.loads(logged[2] or "[]")[:10], outcome=logged[3] or "",
                           variant=logged[4] or "", branch=logged[5] or "")
            q = " ".join(str(q or "").split())[:500]
            if row:
                merged = {**json.loads(row[1] or "{}"), **ctx}
                self.db.execute("UPDATE feedback SET updated=?, vote=COALESCE(?, vote), reasons=COALESCE(?, reasons), "
                                "comment=COALESCE(?, comment), context=?, q=CASE WHEN ?<>'' THEN ? ELSE q END, "
                                "qn=CASE WHEN ?<>'' THEN ? ELSE qn END WHERE id=?",
                                (ts, vote, None if rs is None else json.dumps(rs), cm, json.dumps(merged), q, q, q,
                                 norm_q(q)[:300], row[0]))
                return row[0]
            if vote is None:
                raise ValueError("vote first, then add a comment")
            n = self.db.execute("SELECT COUNT(*) FROM feedback WHERE session=? AND day=?", (sess, _day(ts))).fetchone()[0]
            if sess and n >= FEEDBACK_PER_SESSION_DAY:
                raise PermissionError("too much feedback from this session today")
            new = uuid.uuid4().hex[:16]
            self.db.execute("INSERT INTO feedback (id, ts, day, updated, kind, search_id, session, account, user, role, segment, "
                            "q, qn, vote, reasons, comment, context, status, note, handled_by, handled_at) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                                new, ts, _day(ts), ts, kind, sid, sess, str(getattr(principal, "account_id", "") or ""),
                                str(getattr(principal, "user_id", "") or ""), getattr(principal, "role", None) or "anonymous",
                                getattr(principal, "segment", None) or "", q, norm_q(q)[:300], vote,
                                json.dumps(rs or []), cm or "", json.dumps(ctx), "new", "", "", None))
            return new

    @staticmethod
    def _fb_row(r) -> dict:
        (fid, ts, updated, kind, search_id, account, role, segment, q, qn, vote, reasons, comment, context, status, note,
         by, at) = r
        return {"id": fid, "at": time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)), "ts": ts,
                "updated": time.strftime("%Y-%m-%d %H:%M", time.localtime(updated or ts)), "kind": kind,
                "search_id": search_id, "account": account, "role": role, "segment": segment, "q": q, "qn": qn,
                "vote": vote, "reasons": json.loads(reasons or "[]"), "comment": comment or "",
                "context": json.loads(context or "{}"), "status": status or "new", "note": note or "",
                "handled_by": by or "", "handled_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(at)) if at else ""}

    _FB_COLS = ("id, ts, updated, kind, search_id, account, role, segment, q, qn, vote, reasons, comment, context, status, "
                "note, handled_by, handled_at")

    def feedback_list(self, days: int = 30, vote: int | None = None, kind: str | None = None, status: str | None = None,
                      has_comment: bool = False, q: str = "", limit: int = 200, now: float | None = None) -> list[dict]:
        since = _day((now or time.time()) - days * 86400)
        where, args = ["day>=?"], [since]
        if vote in (1, -1):
            where.append("vote=?"); args.append(vote)
        if kind in FEEDBACK_KINDS:
            where.append("kind=?"); args.append(kind)
        if status in FEEDBACK_STATUS:
            where.append("status=?"); args.append(status)
        if has_comment:
            where.append("comment<>''")
        if q.strip():
            where.append("(qn LIKE ? OR comment LIKE ?)"); args += [f"%{norm_q(q)}%", f"%{q.strip()}%"]
        rows = self._q(f"SELECT {self._FB_COLS} FROM feedback WHERE {' AND '.join(where)} ORDER BY ts DESC LIMIT ?",
                       tuple(args) + (max(1, min(limit, 1000)),))
        return [self._fb_row(r) for r in rows]

    def feedback_summary(self, days: int = 30, now: float | None = None) -> dict:
        now = now or time.time()
        since, prev = _day(now - days * 86400), _day(now - 2 * days * 86400)
        rows = self.feedback_list(days=days, limit=100000, now=now)
        prev_rows = [r for r in self.feedback_list(days=2 * days, limit=100000, now=now) if r["at"][:10] < since]

        def rate(rs):
            return round(sum(r["vote"] == 1 for r in rs) / len(rs), 3) if rs else None
        by_kind = {k: {"n": sum(r["kind"] == k for r in rows), "up": sum(r["kind"] == k and r["vote"] == 1 for r in rows),
                       "down": sum(r["kind"] == k and r["vote"] == -1 for r in rows)} for k in FEEDBACK_KINDS}
        days_ = {}
        for r in rows:
            d = days_.setdefault(r["at"][:10], {"day": r["at"][:10], "up": 0, "down": 0})
            d["up" if r["vote"] == 1 else "down"] += 1
        reasons = Counter(x for r in rows if r["vote"] == -1 for x in r["reasons"])
        groups = {}
        for r in rows:
            if r["kind"] != "search" or not r["qn"]:
                continue
            g = groups.setdefault(r["qn"], {"q": r["q"], "qn": r["qn"], "up": 0, "down": 0, "comments": 0, "last": r["at"]})
            g["up" if r["vote"] == 1 else "down"] += 1
            g["comments"] += bool(r["comment"])
        roles = {}
        for r in rows:
            x = roles.setdefault(r["role"], {"role": r["role"], "n": 0, "up": 0})
            x["n"] += 1; x["up"] += r["vote"] == 1
        return {"days": days, "since": since, "n": len(rows), "up": sum(r["vote"] == 1 for r in rows),
                "down": sum(r["vote"] == -1 for r in rows), "helpful_rate": rate(rows), "prev_helpful_rate": rate(prev_rows),
                "prev_n": len(prev_rows), "with_comment": sum(bool(r["comment"]) for r in rows),
                "new": sum(r["status"] == "new" for r in rows), "by_kind": by_kind,
                "series": sorted(days_.values(), key=lambda d: d["day"]),
                "reasons": [{"reason": k, "label": FEEDBACK_REASONS[k], "n": v} for k, v in reasons.most_common()],
                "top_down": sorted([g for g in groups.values() if g["down"]], key=lambda g: (-g["down"], g["last"]))[:15],
                "roles": sorted(({**x, "helpful_rate": round(x["up"] / x["n"], 3)} for x in roles.values()), key=lambda x: -x["n"])}

    def set_feedback(self, fid: str, *, status: str | None = None, note: str | None = None, who: str = "") -> dict:
        if status is not None and status not in FEEDBACK_STATUS:
            raise ValueError("status is new, reviewed or done")
        with self.lock:
            if not self.db.execute("SELECT 1 FROM feedback WHERE id=?", (fid,)).fetchone():
                raise KeyError("no such feedback")
            self.db.execute("UPDATE feedback SET status=COALESCE(?, status), note=COALESCE(?, note), handled_by=?, handled_at=? "
                            "WHERE id=?", (status, None if note is None else str(note)[:500], (who or "")[:60], time.time(), fid))
        return self._fb_row(self._q(f"SELECT {self._FB_COLS} FROM feedback WHERE id=?", (fid,))[0])

    # ------------------------------------------------------------------ Claude usage (v1.10.2)
    def record_llm(self, *, model: str, purpose: str, status: str, usage=None, ms: float = 0.0,
                   ts: float | None = None, detail: str = "") -> None:
        """One Claude API call: status ok, error, timeout, or capped (refused by the daily cap, never sent)."""
        from webbsearch.llm.client import call_cost
        ts = ts or time.time()
        g = lambda k: int(getattr(usage, k, 0) or 0) if usage is not None else 0      # noqa: E731
        with self.lock:
            self.db.execute("INSERT INTO llm_calls (ts, day, model, purpose, status, input, output, cache_read, "
                            "cache_write, ms, cost, detail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                            (ts, _day(ts), model, purpose, status, g("input_tokens"), g("output_tokens"),
                             g("cache_read_input_tokens"), g("cache_creation_input_tokens"), float(ms),
                             call_cost(model, usage) if status == "ok" else 0.0, (detail or "")[:300]))

    def llm_usage(self, days: int = 30, now: float | None = None) -> dict:
        now = now or time.time()
        since, today = _day(now - (days - 1) * 86400), _day(now)
        # timeouts apart from errors: the quick search-reading step stops waiting on purpose and finishes in the
        # background, so its timeouts are routine; an error is a call Anthropic refused (v2.01)
        agg = ("SUM(status='ok'), SUM(status='error'), SUM(status='timeout'), SUM(status='capped'), SUM(input), "
               "SUM(output), SUM(cache_read), SUM(cache_write), SUM(cost), AVG(CASE WHEN status='ok' THEN ms END)")
        keys = ("calls", "errors", "timeouts", "capped", "input_tokens", "output_tokens", "cache_read_tokens",
                "cache_write_tokens", "cost_usd", "avg_ms")

        def row(r):
            d = {k: (v or 0) for k, v in zip(keys, r)}
            d["cost_usd"] = round(float(d["cost_usd"]), 6)
            d["avg_ms"] = round(float(d["avg_ms"]))
            return d
        t = row(self._q(f"SELECT {agg} FROM llm_calls WHERE day = ?", (today,))[0])
        span = row(self._q(f"SELECT {agg} FROM llm_calls WHERE day >= ?", (since,))[0])
        by_day = [{"day": r[0], **row(r[1:])} for r in
                  self._q(f"SELECT day, {agg} FROM llm_calls WHERE day >= ? GROUP BY day ORDER BY day DESC", (since,))]
        models = [{"model": r[0], **row(r[1:])} for r in self._q(
            f"SELECT model, {agg} FROM llm_calls WHERE day >= ? AND status = 'ok' GROUP BY model ORDER BY SUM(cost) DESC", (since,))]
        purposes = [{"purpose": r[0], **row(r[1:])} for r in self._q(
            f"SELECT purpose, {agg} FROM llm_calls WHERE day >= ? AND status = 'ok' GROUP BY purpose ORDER BY SUM(cost) DESC", (since,))]
        errs = [{"at": time.strftime("%Y-%m-%d %H:%M", time.localtime(r[0])), "ts": r[0], "model": r[1], "purpose": r[2],
                 "status": r[3], "detail": r[4] or ""}
                for r in self._q("SELECT ts, model, purpose, status, detail FROM llm_calls WHERE status != 'ok' "
                                 "ORDER BY ts DESC LIMIT 20")]
        first = self._q("SELECT MIN(day) FROM llm_calls")[0][0]
        return {"days_back": days, "since": since, "first_day": first, "today": t, "span": span, "days": by_day,
                "models": models, "purposes": purposes, "recent_errors": errs}

    # ------------------------------------------------------------------ sign-ins (v1.10.2)
    def record_signin(self, *, ip: str, kind: str, ok: bool, ua: str = "", ts: float | None = None) -> None:
        ts = ts or time.time()
        with self.lock:
            self.db.execute("INSERT INTO signins (ts, day, ip, kind, ok, ua) VALUES (?,?,?,?,?,?)",
                            (ts, _day(ts), (ip or "")[:64], kind, 1 if ok else 0, (ua or "")[:300]))
            self.db.execute("DELETE FROM signins WHERE ts < ?", (ts - SIGNIN_KEEP_DAYS * 86400,))

    def signins(self, days: int = 90, failed: bool = False, limit: int = 1000, now: float | None = None) -> dict:
        now = now or time.time()
        cut = now - days * 86400
        where, args = "WHERE ts >= ?" + (" AND ok = 0" if failed else ""), (cut,)
        rows = [{"at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r[0])), "ts": r[0], "ip": r[1], "kind": r[2],
                 "ok": bool(r[3]), "ua": r[4]}
                for r in self._q(f"SELECT ts, ip, kind, ok, ua FROM signins {where} ORDER BY ts DESC, id DESC LIMIT ?",
                                 (*args, limit))]
        n = self._q("SELECT COUNT(*), SUM(ok), COUNT(DISTINCT ip) FROM signins WHERE ts >= ?", (cut,))[0]
        by_ip = [{"ip": r[0], "attempts": r[1], "ok": r[2] or 0, "last": time.strftime("%Y-%m-%d %H:%M", time.localtime(r[3])),
                  "ts": r[3]}
                 for r in self._q("SELECT ip, COUNT(*), SUM(ok), MAX(ts) FROM signins WHERE ts >= ? GROUP BY ip "
                                  "ORDER BY MAX(ts) DESC LIMIT 200", (cut,))]
        return {"days": days, "keep_days": SIGNIN_KEEP_DAYS, "rows": rows, "by_ip": by_ip,
                "summary": {"attempts": n[0], "ok": n[1] or 0, "failed": n[0] - (n[1] or 0), "addresses": n[2]}}

    def counts(self) -> dict:
        n = self._q("SELECT (SELECT COUNT(*) FROM searches), (SELECT COUNT(*) FROM events), (SELECT MIN(day) FROM searches), "
                    "(SELECT COUNT(*) FROM feedback)")[0]
        return {"searches": n[0], "events": n[1], "since": n[2], "feedback": n[3]}

    # ------------------------------------------------------------------ read
    def _q(self, sql: str, args=()) -> list[tuple]:
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def insights(self, days: int = 30, now: float | None = None) -> dict:
        now = now or time.time()
        since = _day(now - days * 86400)
        prev_since = _day(now - 2 * days * 86400)
        per = self._per_search(since)
        prev = self._per_search(prev_since, until=since)
        out = {"days": days, "since": since, "kpis": self._kpis(per), "prev_kpis": self._kpis(prev)}
        out["series"] = self._series(per)
        out["top_queries"] = self._by_query(per, min_n=1)[:40]
        out["zero_results"] = [r for r in self._by_query(per, min_n=1, key="zero") if r["zero"] > 0][:30]
        out["low_ctr"] = sorted([r for r in self._by_query(per, min_n=3) if r["zero"] == 0 and r["ctr"] < 0.25],
                                key=lambda r: (r["ctr"], -r["n"]))[:20]
        out["personalization"] = self._lift(per)
        out["roles"] = self._roles(per)
        out["clicked_positions"] = self._positions(since)
        out["bulk"] = self._bulk(since)
        out["ask"] = [{"at": time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)), "q": json.loads(m).get("q", ""),
                       "role": role} for ts, m, role in
                      self._q("SELECT ts, meta, role FROM events WHERE kind='ask' AND day>=? ORDER BY ts DESC LIMIT 25", (since,))]
        out["rules"] = self._rules(per, since)
        out["latency"] = self._latency(per)
        out["start_page"] = self._start_page(since)
        out["results_mode"] = self._results_mode(since)
        out["filters"] = self._filters(since, len(per))
        fb = self.feedback_summary(days, now=now)
        out["feedback"] = {k: fb[k] for k in ("n", "up", "down", "helpful_rate", "prev_helpful_rate", "with_comment", "new")}
        out["feedback"]["latest_down"] = self.feedback_list(days=days, vote=-1, limit=5, now=now)
        return out

    def _start_page(self, since: str) -> dict:
        """Switches between the simple and the full start page (the storefront logs one per click)."""
        n = {"simple": 0, "full": 0}
        sessions = set()
        for m, sess in self._q("SELECT meta, session FROM events WHERE kind='start_page' AND day>=?", (since,)):
            mode = (json.loads(m or "{}") or {}).get("mode")
            if mode in n:
                n[mode] += 1
                sessions.add(sess)
        return {"to_simple": n["simple"], "to_full": n["full"], "sessions": len(sessions)}

    def _results_mode(self, since: str) -> dict:
        """Switches between Minimal, Normal and Advanced on the results page (one event per click)."""
        n = {"minimal": 0, "normal": 0, "advanced": 0}
        sessions = set()
        for m, sess in self._q("SELECT meta, session FROM events WHERE kind='results_mode' AND day>=?", (since,)):
            to = (json.loads(m or "{}") or {}).get("to")
            if to in n:
                n[to] += 1
                sessions.add(sess)
        return {"to_minimal": n["minimal"], "to_normal": n["normal"], "to_advanced": n["advanced"], "sessions": len(sessions)}

    def _filters(self, since: str, searches: int = 0) -> dict:
        """Filters people use on the results page (v2.42): which ones, how often, which values, and how many searches
        had one. A requirement unchecked in the rail ("relax") counts as a filter turned off."""
        by: dict[str, dict] = {}
        sids, sessions, n = set(), set(), 0
        for m, sid, sess in self._q("SELECT meta, search_id, session FROM events WHERE kind='filter' AND day>=?", (since,)):
            d = json.loads(m or "{}") or {}
            f = str(d.get("facet") or "other")[:30]
            e = by.setdefault(f, {"facet": f, "on": 0, "off": 0, "values": {}, "how": {}})
            e["on" if d.get("on", True) else "off"] += 1
            v = str(d.get("value") or "")[:60]
            if v:
                e["values"][v] = e["values"].get(v, 0) + 1
            h = str(d.get("how") or "")[:20]
            if h:
                e["how"][h] = e["how"].get(h, 0) + 1
            n += 1
            if sid:
                sids.add(sid)
            sessions.add(sess)
        rows = sorted(by.values(), key=lambda e: -(e["on"] + e["off"]))
        for e in rows:
            e["values"] = sorted(({"value": k, "n": c} for k, c in e["values"].items()), key=lambda x: -x["n"])[:8]
        return {"events": n, "sessions": len(sessions), "searches_with_filter": len(sids),
                "share_of_searches": round(len(sids) / searches, 4) if searches else None, "facets": rows}

    def reformulations(self, days: int = 90, min_sessions: int = 2, window_s: float = 180.0, now: float | None = None,
                       limit: int = 60) -> list[dict]:
        """Searches people fixed themselves (v2.43): a search that found nothing (or only the closest), followed within
        `window_s` in the same session by a similar search that got a click or an add. Pairs seen in `min_sessions`
        sessions or more are candidates for the search dictionary (a misspelling or a synonym), added in review."""
        import difflib
        since = time.strftime("%Y-%m-%d", time.localtime((now or time.time()) - days * 86400))
        rows = self._q("SELECT id, ts, session, q, qn, total, outcome FROM searches WHERE day>=? AND session<>'' "
                       "ORDER BY session, ts", (since,))
        engaged = {sid for (sid,) in self._q(
            "SELECT DISTINCT search_id FROM events WHERE day>=? AND search_id IS NOT NULL AND kind IN (%s)"
            % ",".join("?" * len(ENGAGED)), (since, *ENGAGED))}
        pairs: dict[tuple, dict] = {}
        for a, b in zip(rows, rows[1:]):
            sid_a, ts_a, sess_a, q_a, qn_a, tot_a, out_a = a
            sid_b, ts_b, sess_b, q_b, qn_b, _tot_b, _out_b = b
            if sess_a != sess_b or ts_b - ts_a > window_s or not qn_a or not qn_b or qn_a == qn_b:
                continue
            if not (int(tot_a or 0) == 0 or out_a in ("zero", "fallback")) or sid_b not in engaged:
                continue
            ta, tb = set(qn_a.split()), set(qn_b.split())
            if difflib.SequenceMatcher(None, qn_a, qn_b).ratio() < 0.6 and not (ta & tb):
                continue                        # a different search, not the same one fixed
            e = pairs.setdefault((qn_a, qn_b), {"from": q_a, "to": q_b, "sessions": set(), "n": 0, "last": ts_b})
            e["sessions"].add(sess_a)
            e["n"] += 1
            e["last"] = max(e["last"], ts_b)
        out = []
        for (fa, fb), e in pairs.items():
            if len(e["sessions"]) < min_sessions:
                continue
            one_word = len(fa.split()) == len(fb.split()) == 1 or fa.replace(" ", "") == fb.replace(" ", "")
            out.append({"from": e["from"], "to": e["to"], "sessions": len(e["sessions"]), "searches": e["n"],
                        "kind": "spelling" if one_word or difflib.SequenceMatcher(None, fa, fb).ratio() >= 0.8 else "synonym",
                        "last": time.strftime("%Y-%m-%d", time.localtime(e["last"]))})
        out.sort(key=lambda r: (-r["sessions"], -r["searches"], r["from"]))
        return out[:limit]

    def _per_search(self, since: str, until: str | None = None) -> list[dict]:
        rows = self._q("SELECT id, day, q, qn, total, latency, personalized, role, rules, rewritten_from, session, outcome, variant FROM searches "
                       "WHERE day>=?" + (" AND day<?" if until else ""), (since, until) if until else (since,))
        ev = {}
        for sid, kind, pos in self._q("SELECT search_id, kind, pos FROM events WHERE day>=? AND search_id IS NOT NULL"
                                      + (" AND day<?" if until else ""), (since, until) if until else (since,)):
            e = ev.setdefault(sid, {"clicks": 0, "atc": 0, "first_pos": None})
            if kind in CLICKS:           # "clicked" below is any of ENGAGED: a click-type event or an add to cart
                e["clicks"] += 1
                if pos and (e["first_pos"] is None or pos < e["first_pos"]):
                    e["first_pos"] = pos
            elif kind in ("add_to_cart", "add_to_list"):
                e["atc"] += kind == "add_to_cart"
                if kind == "add_to_cart" and pos and e["first_pos"] is None:
                    e["first_pos"] = pos
        out = []
        for sid, day, q, qn, total, lat, pers, role, rules, rw, sess, oc, var in rows:
            e = ev.get(sid, {})
            out.append({"id": sid, "day": day, "q": q, "qn": qn, "total": total, "latency": lat, "personalized": pers,
                        "zero": oc in ("zero", "fallback") or (not total and oc != "answered"), "outcome": oc or "", "variant": var or "",
                        "role": role, "rules": json.loads(rules or "[]"), "rewritten": rw, "session": sess,
                        "clicked": e.get("clicks", 0) > 0 or e.get("atc", 0) > 0, "atc": e.get("atc", 0) > 0,
                        "pos": e.get("first_pos")})
        return out

    @staticmethod
    def _kpis(per: list[dict]) -> dict:
        n = len(per)
        if not n:
            return {"searches": 0, "sessions": 0, "zero_rate": None, "ctr": None, "atc_rate": None, "avg_pos": None,
                    "personalized_share": None, "p95_ms": None}
        pos = [r["pos"] for r in per if r["pos"]]
        lat = sorted(r["latency"] for r in per)
        return {"searches": n, "sessions": len({r["session"] for r in per if r["session"]}),
                "zero_rate": round(sum(r["zero"] for r in per) / n, 4),
                "ctr": round(sum(r["clicked"] for r in per) / n, 4),
                "atc_rate": round(sum(r["atc"] for r in per) / n, 4),
                "avg_pos": round(sum(pos) / len(pos), 1) if pos else None,
                "personalized_share": round(sum(r["personalized"] for r in per) / n, 4),
                "p95_ms": lat[min(len(lat) - 1, int(0.95 * len(lat)))]}

    @staticmethod
    def _series(per: list[dict]) -> list[dict]:
        by = {}
        for r in per:
            d = by.setdefault(r["day"], {"day": r["day"], "searches": 0, "zero": 0, "clicked": 0, "atc": 0})
            d["searches"] += 1
            d["zero"] += r["zero"]
            d["clicked"] += r["clicked"]
            d["atc"] += r["atc"]
        return [by[k] for k in sorted(by)]

    @staticmethod
    def _by_query(per: list[dict], min_n: int = 1, key: str = "n") -> list[dict]:
        g = {}
        for r in per:
            d = g.setdefault(r["qn"], {"q": r["q"], "n": 0, "zero": 0, "clicked": 0, "atc": 0, "pos": [], "last": r["day"],
                                       "rewritten": r["rewritten"], "outcome": r["outcome"]})
            d["n"] += 1
            d["zero"] += r["zero"]
            d["outcome"] = r["outcome"] or d["outcome"]
            d["clicked"] += r["clicked"]
            d["atc"] += r["atc"]
            if r["pos"]:
                d["pos"].append(r["pos"])
            d["last"] = max(d["last"], r["day"])
        rows = []
        for qn, d in g.items():
            if d["n"] < min_n:
                continue
            rows.append({"q": d["q"], "qn": qn, "n": d["n"], "zero": d["zero"], "ctr": round(d["clicked"] / d["n"], 3),
                         "atc": round(d["atc"] / d["n"], 3), "avg_pos": round(sum(d["pos"]) / len(d["pos"]), 1) if d["pos"] else None,
                         "last": d["last"], "rewritten": d["rewritten"], "outcome": d["outcome"]})
        return sorted(rows, key=lambda r: (-r[key], -r["n"], r["q"]))

    def _lift(self, per: list[dict]) -> dict:
        def agg(rows):
            n = len(rows)
            pos = [r["pos"] for r in rows if r["pos"]]
            return {"n": n, "ctr": round(sum(r["clicked"] for r in rows) / n, 3) if n else None,
                    "atc": round(sum(r["atc"] for r in rows) / n, 3) if n else None,
                    "avg_pos": round(sum(pos) / len(pos), 1) if pos else None}
        return {"personalized": agg([r for r in per if r["personalized"]]), "anonymous": agg([r for r in per if not r["personalized"]])}

    @staticmethod
    def _roles(per: list[dict]) -> list[dict]:
        c = Counter(r["role"] for r in per)
        return [{"role": k, "n": v} for k, v in c.most_common()]

    def _positions(self, since: str) -> list[dict]:
        rows = self._q("SELECT pos, COUNT(*) FROM events WHERE day>=? AND kind IN ('click','quick_view','expand','add_to_cart') "
                       "AND pos IS NOT NULL GROUP BY pos ORDER BY pos", (since,))
        buckets = Counter()
        for pos, n in rows:
            b = "1" if pos == 1 else "2-3" if pos <= 3 else "4-6" if pos <= 6 else "7-12" if pos <= 12 else "13-24" if pos <= 24 else "25+"
            buckets[b] += n
        return [{"bucket": b, "n": buckets.get(b, 0)} for b in ("1", "2-3", "4-6", "7-12", "13-24", "25+")]

    def _bulk(self, since: str) -> dict:
        rows = self._q("SELECT meta FROM events WHERE kind='bulk_line' AND day>=?", (since,))
        st, missing, sources = Counter(), Counter(), Counter()
        examples = {}
        for (m,) in rows:
            d = json.loads(m or "{}")
            st[d.get("status", "?")] += 1
            if d.get("source"):
                sources[d["source"]] += 1
            if d.get("missing"):
                missing[d["missing"]] += 1
                examples.setdefault(d["missing"], d.get("text", ""))
        n = sum(st.values())
        return {"lines": n, "status": dict(st), "sources": dict(sources),
                "matched_rate": round((n - st.get("none", 0) - st.get("skip", 0)) / n, 3) if n else None,
                "not_in_catalog": [{"what": w, "n": c, "example": examples.get(w, "")} for w, c in missing.most_common(25)]}

    def _rules(self, per: list[dict], since: str) -> list[dict]:
        seen = Counter()
        for r in per:
            for x in r["rules"]:
                seen[x.get("id")] += 1
        pinned = Counter()
        for (m,) in self._q("SELECT meta FROM events WHERE day>=? AND kind IN ('click','quick_view','expand','add_to_cart')", (since,)):
            d = json.loads(m or "{}")
            if d.get("pinned"):
                pinned[d.get("rule") or "pinned"] += 1
        return [{"id": k, "searches": v} for k, v in seen.most_common()] + \
               [{"id": "_pinned_clicks", "searches": sum(pinned.values())}]

    @staticmethod
    def _latency(per: list[dict]) -> dict:
        lat = sorted(r["latency"] for r in per)
        if not lat:
            return {}
        q = lambda f: lat[min(len(lat) - 1, int(f * len(lat)))]
        return {"p50": q(0.5), "p90": q(0.9), "p95": q(0.95), "max": lat[-1]}

    def query_detail(self, q: str, days: int = 90) -> dict:
        qn = norm_q(q)
        since = _day(time.time() - days * 86400)
        per = [r for r in self._per_search(since) if r["qn"] == qn]
        clicks = Counter()
        for sku, pos, kind in self._q("SELECT e.sku, e.pos, e.kind FROM events e JOIN searches s ON s.id=e.search_id "
                                      "WHERE s.qn=? AND s.day>=? AND e.sku IS NOT NULL", (qn, since)):
            clicks[(sku, kind)] += 1
        by_sku = {}
        for (sku, kind), n in clicks.items():
            d = by_sku.setdefault(sku, {"sku": sku, "clicks": 0, "atc": 0})
            d["clicks" if kind in CLICKS else "atc"] += n
        fb = [self._fb_row(r) for r in self._q(f"SELECT {self._FB_COLS} FROM feedback WHERE qn=? AND day>=? ORDER BY ts DESC LIMIT 30",
                                               (qn, since))]
        return {"q": q, "kpis": self._kpis(per), "series": self._series(per),
                "skus": sorted(by_sku.values(), key=lambda d: -(d["clicks"] + 2 * d["atc"]))[:20],
                "feedback": {"up": sum(r["vote"] == 1 for r in fb), "down": sum(r["vote"] == -1 for r in fb), "items": fb}}

    def recent(self, limit: int = 50) -> list[dict]:
        rows = self._q("SELECT s.ts, s.q, s.role, s.total, s.latency, s.personalized, s.rewritten_from, s.rules, s.outcome, "
                       "(SELECT COUNT(*) FROM events e WHERE e.search_id=s.id AND e.kind IN ('click','quick_view','expand')), "
                       "(SELECT COUNT(*) FROM events e WHERE e.search_id=s.id AND e.kind='add_to_cart') "
                       "FROM searches s ORDER BY s.ts DESC LIMIT ?", (limit,))
        return [{"at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)), "q": q, "role": role, "total": total,
                 "ms": lat, "personalized": bool(pers), "rewritten_from": rw, "rules": [x.get("name") for x in json.loads(rules or "[]")],
                 "outcome": oc or "", "clicks": c, "atc": a} for ts, q, role, total, lat, pers, rw, rules, oc, c, a in rows]

    # ------------------------------------------------------------------ rules, experiments, Bulk order
    def rule_report(self, days: int = 30) -> dict:
        """Per rule: searches it applied to, and their click-through and add-to-cart against all searches."""
        since = _day(time.time() - days * 86400)
        per = self._per_search(since)
        allk = self._kpis(per)
        by = {}
        for r in per:
            for x in r["rules"]:
                by.setdefault(x.get("id"), []).append(r)
        return {"all": {"ctr": allk["ctr"], "atc_rate": allk["atc_rate"]},
                "rules": {rid: {"searches": len(rs), **{k: v for k, v in self._kpis(rs).items() if k in ("ctr", "atc_rate", "avg_pos")}}
                          for rid, rs in by.items()}}

    def experiment_report(self, exp: dict) -> dict:
        """Control (A) vs variant (B) for one A/B test: searches, sessions, click-through, add-to-cart, no-result rate,
        average clicked position, with a two-proportion z-test on click-through and add-to-cart."""
        since = (exp.get("started") or "2000-01-01")[:10]
        per = [r for r in self._per_search(since) if (r.get("variant") or "").startswith(exp["id"] + ":")]
        arms = {}
        for arm in ("A", "B"):
            rs = [r for r in per if r["variant"].endswith(":" + arm)]
            arms[arm] = {**self._kpis(rs), "clicked": sum(r["clicked"] for r in rs), "carted": sum(r["atc"] for r in rs)}
        out = {"arms": arms}
        for m, k in (("ctr", "clicked"), ("atc_rate", "carted")):
            a, b = arms["A"], arms["B"]
            out[m] = _ztest(a[k], a["searches"], b[k], b["searches"])
        days = sorted({r["day"] for r in per})
        out["series"] = [{"day": d, **{arm: round(sum(r["clicked"] for r in per if r["day"] == d and r["variant"].endswith(":" + arm)) /
                                           max(1, sum(1 for r in per if r["day"] == d and r["variant"].endswith(":" + arm))), 3)
                                       for arm in ("A", "B")}} for d in days]
        return out

    def bulk_report(self, days: int = 30) -> dict:
        since = _day(time.time() - days * 86400)
        up = [json.loads(m or "{}") for (m,) in self._q("SELECT meta FROM events WHERE kind='bulk_upload' AND day>=?", (since,))]
        lines = self._q("SELECT day, meta FROM events WHERE kind='bulk_line' AND day>=?", (since,))
        ch = [json.loads(m or "{}") for (m,) in self._q("SELECT meta FROM events WHERE kind='bulk_choice' AND day>=?", (since,))]
        st, src, gaps, asked, crosses, series = Counter(), Counter(), Counter(), Counter(), Counter(), Counter()
        examples = {}
        for day, m in lines:
            d = json.loads(m or "{}")
            st[d.get("status", "?")] += 1
            src[d.get("source", "?")] += 1
            series[day] += 1
            if d.get("missing"):
                gaps[d["missing"]] += 1
                examples.setdefault(d["missing"], d.get("text", ""))
            if d.get("req_brand") and (d.get("uncarried") or d.get("brand_cross") or d.get("status") == "none"):
                asked[(d["req_brand"], d.get("brand_cross") or "")] += 1
            if d.get("cross"):
                crosses["item cross"] += 1
            if d.get("brand_cross"):
                crosses["brand cross: " + d["brand_cross"]] += 1
        n = sum(st.values())
        hows = Counter(c.get("how", "?") for c in ch)
        top = sum(1 for c in ch if c.get("rank") == 0)
        return {"uploads": len(up), "upload_kinds": dict(Counter(u.get("kind", "?") for u in up)),
                "upload_lines": sum(int(u.get("lines") or 0) for u in up), "set_aside": sum(int(u.get("set_aside") or 0) for u in up),
                "lines": n, "status": dict(st), "sources": dict(src),
                "matched_rate": round((n - st.get("none", 0) - st.get("skip", 0)) / n, 3) if n else None,
                "choices": len(ch), "choice_how": dict(hows), "top_pick_rate": round(top / len(ch), 3) if ch else None,
                "override_rate": round(sum(1 for c in ch if (c.get("rank") or 0) > 0) / len(ch), 3) if ch else None,
                "not_in_catalog": [{"what": w, "n": c, "example": examples.get(w, "")} for w, c in gaps.most_common(30)],
                "brands_asked": [{"brand": b, "crossed_to": x.split(" → ")[-1] if x else None, "n": c}
                                 for (b, x), c in asked.most_common(20)],
                "crosses_used": [{"what": k, "n": c} for k, c in crosses.most_common(20)],
                "series": [{"day": d, "lines": series[d]} for d in sorted(series)]}


def _ztest(x1: int, n1: int, x2: int, n2: int) -> dict:
    """Two-proportion z-test: rate A, rate B, relative lift, two-sided p."""
    import math
    if not n1 or not n2:
        return {"a": None, "b": None, "lift": None, "p": None}
    p1, p2 = x1 / n1, x2 / n2
    pp = (x1 + x2) / (n1 + n2)
    se = math.sqrt(pp * (1 - pp) * (1 / n1 + 1 / n2)) or 1e-9
    z = (p2 - p1) / se
    p = math.erfc(abs(z) / math.sqrt(2))
    return {"a": round(p1, 4), "b": round(p2, 4), "lift": round((p2 - p1) / p1, 4) if p1 else None, "z": round(z, 2), "p": round(p, 4)}
