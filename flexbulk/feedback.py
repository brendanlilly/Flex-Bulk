"""Line feedback: staff say, line by line, whether the match was right, what was wrong, and (when they know it) the
right item. Each piece of feedback keeps the line as asked, what was shown, and the matcher's steps at that moment
(the same trace as "Why?"), so it can be worked on without reproducing it.

One SQLite file (data/bulk/feedback.sqlite). The dashboard's Line feedback page lists it; match reports can score
the matcher on it (source "feedback": a line marked right, or given its right item, has an answer).
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from webbsearch.config import settings

REASONS = {
    "wrong_item": "Wrong item",
    "wrong_size": "Wrong size",
    "wrong_brand": "Wrong brand",
    "wrong_spec": "Wrong material, joint or spec",
    "ranking": "Right item is in the options, not first",
    "should_find": "It's on the list: should have found it",
    "not_carried": "We don't carry it",
    "misread": "Line read wrong (text, qty, unit)",
    "other": "Other",
}
VERDICTS = ("good", "bad")
STATUSES = ("open", "resolved", "wontfix")


def _clean(v, n: int = 300) -> str:
    return re.sub(r"[\x00-\x08\x0b-\x1f]", " ", str(v or "")).strip()[:n]


class Feedback:
    def __init__(self, path: Path | None = None):
        self.path = Path(path or os.environ.get("FLEXBULK_FEEDBACK_DB") or settings.data_dir / "bulk" / "feedback.sqlite")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS feedback (
            id TEXT PRIMARY KEY, created REAL, updated REAL, staff TEXT, customer TEXT, order_id TEXT, branch TEXT,
            verdict TEXT, reasons TEXT, note TEXT, right_sku TEXT, right_title TEXT, line TEXT, shown TEXT,
            now TEXT, trace TEXT, status TEXT, resolution TEXT, version TEXT)""")
        self.db.commit()

    # ------------------------------------------------------------------ write
    def save(self, body: dict) -> dict:
        verdict = body.get("verdict")
        if verdict not in VERDICTS:
            raise ValueError("verdict is good or bad")
        reasons = [r for r in (body.get("reasons") or []) if r in REASONS][:8]
        line = body.get("line") or {}
        line = {k: (_clean(v, 400) if isinstance(v, str) else v) for k, v in line.items()
                if k in ("text", "desc", "brand", "part", "qty", "uom", "src_label", "n", "row", "from_ocr", "raw")}
        if not (line.get("text") or line.get("desc")):
            raise ValueError("the line's text is needed")
        shown = body.get("shown") or {}
        shown = {"sku": _clean(shown.get("sku"), 60), "title": _clean(shown.get("title"), 300), "brand": _clean(shown.get("brand"), 80),
                 "rank": shown.get("rank") if isinstance(shown.get("rank"), int) else None, "status": _clean(shown.get("status"), 20),
                 "via": _clean(shown.get("via"), 60), "note": _clean(shown.get("note"), 300),
                 "options": [_clean(s, 60) for s in (shown.get("options") or [])][:8]}
        now = time.time()
        fid = _clean(body.get("id"), 20) or None
        row = {"staff": _clean(body.get("staff"), 60), "customer": _clean(body.get("customer"), 80),
               "order_id": _clean(body.get("order_id"), 40), "branch": _clean(body.get("branch"), 20), "verdict": verdict,
               "reasons": json.dumps(reasons), "note": _clean(body.get("note"), 1000), "right_sku": _clean(body.get("right_sku"), 60),
               "right_title": _clean(body.get("right_title"), 300), "line": json.dumps(line), "shown": json.dumps(shown),
               "now": json.dumps(body.get("now") or [])[:20000], "trace": json.dumps(body.get("trace") or [], default=str)[:400_000],
               "version": _clean(body.get("version"), 20)}
        with self.lock:
            old = self.db.execute("SELECT id, created, status FROM feedback WHERE id=?", (fid,)).fetchone() if fid else None
            if old:
                self.db.execute("UPDATE feedback SET updated=?, " + ", ".join(f"{k}=?" for k in row) + " WHERE id=?",
                                (now, *row.values(), fid))
            else:
                fid = time.strftime("F%y%m%d-") + secrets.token_hex(3).upper()
                self.db.execute("INSERT INTO feedback (id, created, updated, status, resolution, " + ", ".join(row) + ") VALUES (?, ?, ?, 'open', '', "
                                + ", ".join("?" * len(row)) + ")", (fid, now, now, *row.values()))
            self.db.commit()
        return self.get(fid, trace=False)

    def delete(self, fid: str) -> bool:
        with self.lock:
            n = self.db.execute("DELETE FROM feedback WHERE id=?", (fid,)).rowcount
            self.db.commit()
        return n > 0

    def resolve(self, fid: str, status: str, resolution: str = "") -> dict:
        if status not in STATUSES:
            raise ValueError(f"status is one of {', '.join(STATUSES)}")
        with self.lock:
            n = self.db.execute("UPDATE feedback SET status=?, resolution=?, updated=? WHERE id=?",
                                (status, _clean(resolution, 1000), time.time(), fid)).rowcount
            self.db.commit()
        if not n:
            raise KeyError(fid)
        return self.get(fid, trace=False)

    # ------------------------------------------------------------------ read
    @staticmethod
    def _out(r: sqlite3.Row, trace: bool) -> dict:
        d = dict(r)
        for k in ("reasons", "line", "shown", "now", "trace"):
            d[k] = json.loads(d.get(k) or "null")
        d["reason_labels"] = [REASONS[x] for x in d["reasons"] or [] if x in REASONS]
        if not trace:
            d.pop("trace", None)
        return d

    def get(self, fid: str, trace: bool = True) -> dict | None:
        r = self.db.execute("SELECT * FROM feedback WHERE id=?", (fid,)).fetchone()
        return self._out(r, trace) if r else None

    def list(self, status: str = "", verdict: str = "", q: str = "", limit: int = 500, since: float = 0,
             trace: bool = False) -> list[dict]:
        w, a = ["updated>=?"], [since]
        if status in STATUSES:
            w.append("status=?")
            a.append(status)
        if verdict in VERDICTS:
            w.append("verdict=?")
            a.append(verdict)
        if q:
            w.append("(line LIKE ? OR note LIKE ? OR staff LIKE ? OR customer LIKE ? OR right_sku=?)")
            a += [f"%{q}%"] * 4 + [q]
        rows = self.db.execute(f"SELECT * FROM feedback WHERE {' AND '.join(w)} ORDER BY updated DESC LIMIT ?", (*a, limit)).fetchall()
        return [self._out(r, trace) for r in rows]

    def counts(self) -> dict:
        out = {"total": 0, "open": 0, "good": 0, "bad": 0, "with_right_item": 0, "by_reason": {}}
        for r in self.db.execute("SELECT verdict, status, reasons, right_sku FROM feedback"):
            out["total"] += 1
            out["open"] += r["status"] == "open"
            out[r["verdict"]] = out.get(r["verdict"], 0) + 1
            out["with_right_item"] += bool(r["right_sku"])
            for x in json.loads(r["reasons"] or "[]"):
                out["by_reason"][REASONS.get(x, x)] = out["by_reason"].get(REASONS.get(x, x), 0) + 1
        out["by_reason"] = dict(sorted(out["by_reason"].items(), key=lambda kv: -kv[1]))
        return out

    def to_csv(self, rows: list[dict]) -> bytes:
        buf = io.StringIO()
        out = csv.writer(buf)

        class _W:                           # a cell starting = + - @ is text, not a formula, in Excel
            @staticmethod
            def writerow(cells):
                out.writerow([("'" + c) if isinstance(c, str) and c[:1] in ("=", "+", "-", "@") else c for c in cells])
        w = _W()
        w.writerow(["ID", "Date", "Staff", "Customer", "Order", "Line", "Description", "Qty", "Shown item", "Shown description",
                    "Shown status", "Verdict", "What was wrong", "Note", "Right item", "Right description", "Status", "Resolution"])
        for r in rows:
            ln, sh = r["line"] or {}, r["shown"] or {}
            w.writerow([r["id"], time.strftime("%Y-%m-%d %H:%M", time.localtime(r["created"])), r["staff"], r["customer"], r["order_id"],
                        ln.get("text"), ln.get("desc"), ln.get("qty"), sh.get("sku"), sh.get("title"), sh.get("status"),
                        r["verdict"], "; ".join(r["reason_labels"]), r["note"], r["right_sku"], r["right_title"], r["status"], r["resolution"]])
        return ("﻿" + buf.getvalue()).encode()


_fb: Feedback | None = None


def store() -> Feedback:
    global _fb
    if _fb is None:
        _fb = Feedback()
    return _fb
