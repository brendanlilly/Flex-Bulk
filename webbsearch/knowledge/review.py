"""The Knowledge Center's review log: every question and answer, with the customer, scored for how risky the
question is and how good the answer is, so staff review the ones that matter and see where more documents would help.
Internal only: it holds account and user details."""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from webbsearch.llm.guardrails import redact
from webbsearch.retrieval.analyzer import normalize

STOP = {"what", "is", "the", "a", "an", "how", "do", "i", "it", "this", "of", "to", "for", "my", "does", "can", "should",
        "on", "in", "and", "or", "with", "be", "are", "you", "me", "we", "our", "there", "that", "when", "where", "why",
        "which", "need", "much", "many", "often", "come", "will", "would", "could", "your", "if", "at", "from", "as",
        "in", "it's", "its", "get", "have", "has", "there's", "what's"}


REVIEW_TOPICS = {"warranty", "repair", "lifecycle", "recall", "code"}


def risk_score(a: dict) -> tuple[int, list[str]]:
    kind, lvl = a.get("kind"), (a.get("tier") or {}).get("level", 0)
    flags = []
    if kind == "emergency":
        return 100, ["emergency: gas smell, CO or fire"]
    if kind == "declined":
        return 90, ["tier 3: asks to change or bypass a safety device"]
    tags = set(a.get("flags") or [])
    if kind == "legal" or "legal" in tags:
        return 90, ["injury, damage or a claim: route to the branch manager"]
    if kind == "personal_safety":
        return 90, ["personal safety: crisis or threat; 988 and 911 given"]
    if kind == "complaint" or "complaint" in tags:
        return 85, ["a complaint about F.W. Webb or its people"]
    base = {0: 10, 1: 30, 2: 60}.get(lvl, 10)
    if lvl == 2:
        flags.append("tier 2: gas, venting, line voltage or pressure work")
    elif lvl == 1:
        flags.append("tier 1: caution")
    if kind == "webb_only":
        base = max(base, 70); flags.append("names a competitor")
    if kind == "out_of_scope":
        base = max(base, 40); flags.append("off topic")
    if kind == "off_topic":
        base = max(base, 20); flags.append("off topic: a light reply steered it back")
    for t, pts, text in (("injection", 70, "tries to change FlexIQ's instructions"), ("abusive", 60, "abusive"),
                         ("pricing", 40, "pricing talk: sent to the branch"), ("recall", 60, "asks about a recall"),
                         ("pii", 40, "personal data in the question (redacted)"), ("profanity", 30, "profanity"),
                         ("scrubbed", 50, "sentences removed from the answer by the guardrails")):
        if t in tags or kind == t:
            base = max(base, pts); flags.append(text)
    for t, text in (("warranty", "warranty question"), ("repair", "repair question"),
                    ("lifecycle", "lifecycle question (how long it lasts, when to replace)"), ("code", "code question")):
        if t in tags:
            flags.append(text)
    if lvl >= 2 and not (a.get("scope") or {}).get("line"):
        base += 10; flags.append("high-hazard question with no model")
    return min(base, 100), flags


def quality_score(a: dict) -> tuple[int | None, list[str]]:
    kind = a.get("kind")
    if kind in ("emergency", "declined", "webb_only", "out_of_scope", "pricing", "complaint", "legal", "injection",
                "abusive", "where_to_buy", "off_topic", "personal_safety"):
        return None, []
    if kind == "error_code":
        c = a.get("code") or {}
        return (95, []) if c.get("found") else (40, ["error code not in the library" if not c.get("ask_model")
                                                     else "error code asked without a model"])
    if kind == "serial":
        s = a.get("serial") or {}
        return (95, []) if s.get("decoded") else (40, ["serial not decoded"])
    q, why = 100, []
    if a.get("not_in_docs") and not a.get("statements"):
        q -= 55; why.append("not in the documents")
    elif a.get("not_in_docs"):
        q -= 25; why.append("partly not in the documents")
    if not any(s.get("cites") for s in a.get("statements") or []) and a.get("statements"):
        q -= 40; why.append("no citations")
    for f, pts, text in (("flexiq_rejected", 25, "FlexIQ's answer failed its check; the documents' words were shown"),
                         ("low_match", 20, "weak match to the documents"),
                         ("unscoped", 15, "no model: answered from several product lines")):
        if f in (a.get("flags") or []):
            q -= pts; why.append(text)
    return max(q, 0), why


def topic(question: str) -> str:
    """The question's own words, less the filler: "does it come in pink" -> "pink"."""
    toks = [t for t in re.findall(r"[a-z0-9&/-]+", (question or "").lower()) if t not in STOP and not t.isdigit() and len(t) > 1]
    return " ".join(toks[:6])


class ReviewLog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._db() as c:
            c.execute("""create table if not exists qa (id text primary key, ts real, session text, account text,
                         account_name text, user text, role text, segment text, branch text, question text, kind text,
                         line text, line_name text, sku text, tier int, engine text, risk int, quality int, review int,
                         flags text, doc_gap text, answer text, sources text, status text, note text, reviewer text,
                         reviewed real, feedback text)""")
            cols = {r[1] for r in c.execute("pragma table_info(qa)")}
            if "tags" not in cols:                 # v2.2: the guardrail flags, for the review filter
                c.execute("alter table qa add column tags text")

    def _db(self):
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        return c

    def record(self, a: dict, principal=None, branch: str | None = None, session: str = "") -> str:
        risk, rflags = risk_score(a)
        quality, qwhy = quality_score(a)
        tags = [t for t in (a.get("flags") or []) if isinstance(t, str)]
        review = risk >= 60 or (quality is not None and quality < 60) or \
            bool(set(tags) & REVIEW_TOPICS)          # warranty, repair and lifecycle questions are always reviewed
        scope = a.get("scope") or {}
        gap = None
        code = a.get("code") or {}
        library_miss = (a.get("kind") == "answer") or (a.get("kind") == "serial") or \
            (a.get("kind") == "error_code" and not code.get("ask_model"))     # no model given isn't a library gap
        if quality is not None and quality < 70 and library_miss:
            gap = {"line": scope.get("line"), "line_name": scope.get("name"), "topic": topic(a.get("question", "")),
                   "need": "error-code table" if a.get("kind") == "error_code" else
                           "serial rule" if a.get("kind") == "serial" else "a document that covers this"}
        text = " ".join(s.get("text", "") for s in a.get("statements") or []) or a.get("message") or \
            (a.get("code") or {}).get("meaning") or (a.get("code") or {}).get("message") or ""
        row = {"id": uuid.uuid4().hex[:12], "ts": time.time(), "session": (session or "")[:40],
               "account": getattr(principal, "account_id", None), "account_name": getattr(principal, "account_name", None),
               "user": getattr(principal, "user_id", None), "role": getattr(principal, "role", None),
               "segment": getattr(principal, "segment", None), "branch": branch, "question": redact(a.get("question") or "")[:2000],
               "kind": a.get("kind"), "line": scope.get("line"), "line_name": scope.get("name"), "sku": scope.get("sku"),
               "tier": (a.get("tier") or {}).get("level", 0), "engine": a.get("engine"), "risk": risk, "quality": quality,
               "review": int(review), "flags": json.dumps(rflags + qwhy), "doc_gap": json.dumps(gap) if gap else None,
               "answer": text[:4000], "sources": json.dumps([{k: s.get(k) for k in ("n", "doc_id", "title", "page")}
                                                             for s in a.get("sources") or []]),
               "status": "to review" if review else "ok", "note": None, "reviewer": None, "reviewed": None, "feedback": None,
               "tags": json.dumps(tags)}
        with self._lock, self._db() as c:
            c.execute(f"insert into qa ({','.join(row)}) values ({','.join('?' * len(row))})", list(row.values()))
        return row["id"]

    def list(self, review: bool | None = None, limit: int = 200, kind: str | None = None, status: str | None = None,
             flag: str | None = None) -> list[dict]:
        q, args = "select * from qa where 1=1", []
        if flag:
            q += " and tags like ?"; args.append(f'%"{re.sub(r"[^a-z_]", "", flag)}"%')
        if review is not None:
            q += " and review = ?"; args.append(int(review))
        if kind:
            q += " and kind = ?"; args.append(kind)
        if status:
            q += " and status = ?"; args.append(status)
        q += " order by risk desc, coalesce(quality, 100) asc, ts desc" if review else " order by ts desc"
        with self._db() as c:
            rows = c.execute(q + " limit ?", args + [limit]).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["flags"] = json.loads(d["flags"] or "[]")
            d["doc_gap"] = json.loads(d["doc_gap"]) if d["doc_gap"] else None
            d["sources"] = json.loads(d["sources"] or "[]")
            d["review"] = bool(d["review"])
            d["tags"] = json.loads(d.get("tags") or "[]")
            out.append(d)
        return out

    def gaps(self, limit: int = 50) -> list[dict]:
        """Where more documents would help: questions the library answered badly, grouped by product line and topic."""
        groups: dict[tuple, dict] = {}
        for r in self.list(limit=5000):
            g = r["doc_gap"]
            if not g:
                continue
            k = (g.get("line"), g.get("topic"), g.get("need"))
            e = groups.setdefault(k, {"line": g.get("line"), "line_name": g.get("line_name"), "topic": g.get("topic"),
                                      "need": g.get("need"), "n": 0, "examples": [], "last": 0})
            e["n"] += 1
            e["last"] = max(e["last"], r["ts"])
            if len(e["examples"]) < 3:
                e["examples"].append(r["question"])
        return sorted(groups.values(), key=lambda e: (-e["n"], -e["last"]))[:limit]

    def mark(self, qa_id: str, status: str, note: str = "", reviewer: str = "") -> bool:
        with self._lock, self._db() as c:
            n = c.execute("update qa set status = ?, note = ?, reviewer = ?, reviewed = ? where id = ?",
                          (status[:30], (note or "")[:2000], (reviewer or "")[:80], time.time(), qa_id)).rowcount
        return n == 1

    def feedback(self, qa_id: str, vote: str, reason: str = "", account: str | None = None) -> bool:
        """A thumbs-down (or an 'unsafe' reason) puts the answer in review. With `account`, only that account's own
        answers take feedback."""
        bad = vote == "down"
        with self._lock, self._db() as c:
            if account is not None:
                row = c.execute("select account from qa where id = ?", (qa_id,)).fetchone()
                if row is None or str(row["account"] or "") != str(account):
                    return False
            n = c.execute("update qa set feedback = ?, review = case when ? then 1 else review end, "
                          "status = case when ? then 'to review' else status end, "
                          "risk = case when ? then max(risk, 80) else risk end where id = ?",
                          (json.dumps({"vote": vote, "reason": reason[:200]}), bad, bad,
                           bad and "unsafe" in (reason or "").lower(), qa_id)).rowcount
        return n == 1

    def summary(self) -> dict:
        with self._db() as c:
            r = c.execute("select count(*) n, sum(review) rv, sum(case when status='to review' then 1 else 0 end) open, "
                          "avg(quality) q, sum(case when tier>=2 then 1 else 0 end) hi from qa").fetchone()
        return {"questions": r["n"] or 0, "flagged": r["rv"] or 0, "to_review": r["open"] or 0,
                "avg_quality": round(r["q"], 1) if r["q"] is not None else None, "high_hazard": r["hi"] or 0}
