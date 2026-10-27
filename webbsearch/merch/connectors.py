"""Sending FlexSearch and Knowledge Center events to other analytics tools (v2.2). All off until configured.

  ga4       Google Analytics 4, server side (Measurement Protocol), in GA4's recommended e-commerce events: search,
            select_item, view_item, add_to_cart, remove_from_cart, begin_checkout, purchase; Knowledge Center events
            as custom ones (kc_question, kc_error_code, ...). The page also pushes the same events to window.dataLayer
            when the storefront has Google Tag Manager, so a GA4 tag can take them client side instead.
  datadog   counts per event kind and source as Datadog metrics (webb.flexsearch.events), for dashboards and alerts.
  webhook   the events as JSON batches to any URL (Segment, an Azure Function, a queue), signed with HMAC-SHA256 in
            X-Webb-Signature.
  snowflake not pushed: the dashboard's export (NDJSON per table) is what Snowpipe or a scheduled COPY loads.

What leaves: the event, item numbers, quantities, values, the source of an add, and a session id and account, both
hashed with a salt. What never leaves: IP addresses, names, e-mail, user ids, Knowledge Center question text; search
terms go with personal data (card, SSN, e-mail, phone) taken out.

Events wait in an outbox table and go out in batches (flush()); a tool that is down keeps its events for the next
flush, up to OUTBOX_MAX."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
import urllib.request

from webbsearch.llm.guardrails import redact

OUTBOX_MAX = 50_000         # per tool
MAX_TRIES = 5               # a batch a tool keeps refusing is dropped after this many tries (and counted)
PUBLIC_META = ("source", "kind", "tier", "engine", "channel", "checkout_id", "lines", "order_id", "found", "decoded",
               "vote", "where", "line")
BATCH = 25                  # GA4 takes 25 events per request
GA4_NAMES = {"search": "search", "click": "select_item", "quick_view": "select_item", "view_item": "view_item",
             "add_to_cart": "add_to_cart", "remove_from_cart": "remove_from_cart", "add_to_list": "add_to_wishlist",
             "begin_checkout": "begin_checkout", "purchase": "purchase", "ask": "ask_webb", "compare": "compare",
             "kc_ask": "kc_question", "kc_code": "kc_error_code", "kc_serial": "kc_serial", "kc_diagram": "kc_diagram",
             "kc_doc": "kc_document", "kc_source": "kc_source_open", "kc_expert": "kc_expert", "kc_submittal": "kc_submittal",
             "kc_feedback": "kc_feedback", "bulk_upload": "bulk_order"}
SCHEMA = """CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, target TEXT, event TEXT, tries INTEGER DEFAULT 0, error TEXT);
CREATE INDEX IF NOT EXISTS ob_target ON outbox(target);
CREATE TABLE IF NOT EXISTS fwd_meta (k TEXT PRIMARY KEY, v TEXT);"""


def config_from_env() -> dict:
    e = os.environ.get
    return {
        "ga4": {"enabled": bool(e("WEBB_GA4_MEASUREMENT_ID") and e("WEBB_GA4_API_SECRET")),
                "measurement_id": e("WEBB_GA4_MEASUREMENT_ID", ""), "api_secret": e("WEBB_GA4_API_SECRET", "")},
        "datadog": {"enabled": bool(e("WEBB_DATADOG_API_KEY")), "api_key": e("WEBB_DATADOG_API_KEY", ""),
                    "site": e("WEBB_DATADOG_SITE", "datadoghq.com")},
        "webhook": {"enabled": bool(e("WEBB_EVENTS_WEBHOOK_URL")), "url": e("WEBB_EVENTS_WEBHOOK_URL", ""),
                    "secret": e("WEBB_EVENTS_WEBHOOK_SECRET", "")},
    }


def _hash(v, salt: str) -> str | None:
    return hashlib.sha256(f"{salt}|{v}".encode()).hexdigest()[:24] if v else None


def _num(v):
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


def public(event: dict, salt: str) -> dict:
    """The event as it may leave Webb: hashed session and account, no IP, no names, no question text."""
    m = {k: v for k, v in (event.get("meta") or {}).items()
         if k in PUBLIC_META and isinstance(v, (str, int, float, bool)) and not (isinstance(v, str) and "@" in v)}
    out = {"kind": event.get("kind"), "ts": event.get("ts"), "session": _hash(event.get("session"), salt),
           "account": _hash(event.get("account"), salt), "role": event.get("role"), "sku": event.get("sku"),
           "qty": event.get("qty"), "value": _num(event.get("value")), "pos": event.get("pos"),
           "search_id": event.get("search_id"), "meta": m}
    if event.get("kind") == "search":
        out["search_term"] = redact(event.get("q") or "")[:100]
        out["results"] = event.get("total")
    if event.get("kind") == "order":
        out["items"] = [{k: x.get(k) for k in ("sku", "qty", "price", "source")} for x in event.get("items") or []]
    return {k: v for k, v in out.items() if v not in (None, "", {})}


def ga4_event(event: dict, salt: str) -> dict:
    p = public(event, salt)
    kind = p.get("kind")
    name = GA4_NAMES.get(kind, kind or "event")
    m = p.get("meta") or {}
    params = {"session_id": p.get("session"), "engagement_time_msec": 1}
    if kind == "order":                      # GA4's purchase: one per order, every line, the order's id
        name = "purchase"
        params.update({"currency": "USD", "value": p.get("value") or 0.0, "transaction_id": m.get("order_id"),
                       "items": [{"item_id": x["sku"], "quantity": x["qty"], "price": x["price"],
                                  "item_list_name": x.get("source") or "other"} for x in p.get("items") or []]})
    elif kind == "search":
        params.update({"search_term": p.get("search_term", ""), "results": p.get("results")})
    elif p.get("sku") or kind in ("begin_checkout", "purchase"):
        qty = p.get("qty") or 1
        item = {"item_id": p.get("sku"), "quantity": qty}
        if p.get("value") is not None and p.get("sku"):
            item["price"] = round(p["value"] / qty, 2)
        if m.get("source"):
            item["item_list_name"] = m["source"]
        if p.get("pos") is not None:
            item["index"] = p["pos"]
        params.update({"currency": "USD", "value": p.get("value") or 0.0, "items": [item] if p.get("sku") else []})
        if kind == "purchase":
            params["transaction_id"] = m.get("order_id")
    for k in ("kind", "tier", "engine", "source", "channel", "where", "checkout_id", "lines"):
        if k in m and k not in params and isinstance(m[k], (str, int, float, bool)):
            params[f"webb_{k}" if k == "kind" else k] = m[k]
    return {"name": name, "params": {k: v for k, v in params.items() if v is not None}}


def _post(url: str, body: bytes, headers: dict) -> int:
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", **headers}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:          # noqa: S310 (the URL is the operator's configuration)
        return r.status


class Forwarder:
    def __init__(self, analytics, config: dict, post=_post, salt: str | None = None):
        self.a, self.cfg, self.post = analytics, config, post
        self.targets = [k for k, c in config.items() if c.get("enabled")]
        self.last = {k: {"sent": 0, "errors": 0, "dropped": 0, "at": None, "error": None} for k in self.targets}
        self._lock = threading.Lock()
        with self.a.lock:
            self.a.db.executescript(SCHEMA)
        self.salt = salt if salt is not None else (os.environ.get("WEBB_ANALYTICS_HASH_SALT") or self._stored_salt())

    def _stored_salt(self) -> str:
        """A random salt kept with the analytics data, so hashed ids can't be reversed from the source code."""
        import secrets
        with self.a.lock:
            r = self.a.db.execute("SELECT v FROM fwd_meta WHERE k='salt'").fetchone()
            if r:
                return r[0]
            v = secrets.token_hex(16)
            self.a.db.execute("INSERT OR IGNORE INTO fwd_meta (k, v) VALUES ('salt', ?)", (v,))
            return self.a.db.execute("SELECT v FROM fwd_meta WHERE k='salt'").fetchone()[0]

    @property
    def enabled(self) -> bool:
        return bool(self.targets)

    def enqueue(self, event: dict) -> None:
        if not self.targets:
            return
        body = json.dumps(event, default=str)
        with self.a.lock:
            for t in self.targets:
                if t == "ga4" and event.get("kind") == "purchase":
                    continue                       # GA4 gets the order as one purchase (the "order" event)
                n = self.a.db.execute("SELECT count(*) FROM outbox WHERE target=?", (t,)).fetchone()[0]
                if n >= OUTBOX_MAX:
                    self.last[t]["dropped"] += 1   # this tool is far behind; the others aren't held up
                    continue
                self.a.db.execute("INSERT INTO outbox (ts, target, event) VALUES (?,?,?)", (time.time(), t, body))

    def pending(self) -> int:
        return self.a._q("SELECT count(*) FROM outbox")[0][0]

    def flush(self, limit: int = 500) -> dict:
        sent, errors = 0, []
        with self._lock:
            for t in self.targets:
                rows = self.a._q("SELECT id, event FROM outbox WHERE target=? ORDER BY id LIMIT ?", (t, limit))
                for k in range(0, len(rows), BATCH):
                    chunk = rows[k:k + BATCH]
                    events = [json.loads(r[1]) for r in chunk]
                    try:
                        self._send(t, events)
                    except Exception as ex:                 # the tool is down: keep the events for the next flush
                        errors.append(f"{t}: {str(ex)[:120]}")
                        with self.a.lock:
                            ids = [r[0] for r in chunk]
                            self.a.db.execute(f"UPDATE outbox SET tries = tries + 1, error = ? WHERE id IN "
                                              f"({','.join('?' * len(ids))})", [str(ex)[:200]] + ids)
                            gone = self.a.db.execute(f"DELETE FROM outbox WHERE tries >= ? AND id IN "
                                                     f"({','.join('?' * len(ids))})", [MAX_TRIES] + ids).rowcount
                        self.last[t]["dropped"] += gone
                        self.last[t].update({"errors": self.last[t]["errors"] + 1, "error": str(ex)[:200], "at": time.time()})
                        break
                    with self.a.lock:
                        self.a.db.execute(f"DELETE FROM outbox WHERE id IN ({','.join('?' * len(chunk))})", [r[0] for r in chunk])
                    sent += len(chunk)
                    self.last[t].update({"sent": self.last[t]["sent"] + len(chunk), "at": time.time(), "error": None})
        return {"sent": sent, "errors": errors, "pending": self.pending()}

    def _send(self, target: str, events: list[dict]) -> None:
        c = self.cfg[target]
        if target == "ga4":
            by_client = {}
            for e in events:
                by_client.setdefault(_hash(e.get("session") or "anonymous", self.salt), []).append(e)
            for cid, evs in by_client.items():
                body = {"client_id": cid, "events": [ga4_event(e, self.salt) for e in evs]}
                acct = next((e.get("account") for e in evs if e.get("account")), None)
                if acct:
                    body["user_id"] = _hash(acct, self.salt)
                self._ok(self.post(f"https://www.google-analytics.com/mp/collect?measurement_id={c['measurement_id']}"
                                   f"&api_secret={c['api_secret']}", json.dumps(body).encode(), {}))
        elif target == "datadog":
            now = int(time.time())
            counts: dict[tuple, int] = {}
            for e in events:
                key = (e.get("kind"), (e.get("meta") or {}).get("source") or "none")
                counts[key] = counts.get(key, 0) + 1
            series = [{"metric": "webb.flexsearch.events", "type": 1, "points": [{"timestamp": now, "value": n}],
                       "tags": [f"kind:{k}", f"source:{s}", "service:flexsearch"]} for (k, s), n in counts.items()]
            self._ok(self.post(f"https://api.{c['site']}/api/v2/series", json.dumps({"series": series}).encode(),
                               {"DD-API-KEY": c["api_key"]}))
        elif target == "webhook":
            body = json.dumps({"source": "flexsearch", "sent": time.time(),
                               "events": [public(e, self.salt) for e in events]}).encode()
            sig = "sha256=" + hmac.new((c.get("secret") or "").encode(), body, hashlib.sha256).hexdigest()
            self._ok(self.post(c["url"], body, {"X-Webb-Signature": sig}))

    @staticmethod
    def _ok(status) -> None:
        if isinstance(status, int) and not 200 <= status < 300:
            raise OSError(f"HTTP {status}")

    def status(self) -> dict:
        out = {}
        for k in ("ga4", "datadog", "webhook"):
            c = self.cfg.get(k) or {}
            out[k] = {"enabled": bool(c.get("enabled")), **self.last.get(k, {}),
                      "pending": self.a._q("SELECT count(*) FROM outbox WHERE target=?", (k,))[0][0]}
        return out


def start_background(forwarder: Forwarder, every: float = 10.0) -> threading.Thread | None:
    """Flush every `every` seconds in a daemon thread (the server starts it when a connector is configured)."""
    if not forwarder.enabled:
        return None

    def loop():
        while True:
            time.sleep(every)
            try:
                forwarder.flush()
            except Exception:
                pass
    t = threading.Thread(target=loop, name="flexsearch-forwarder", daemon=True)
    t.start()
    return t
