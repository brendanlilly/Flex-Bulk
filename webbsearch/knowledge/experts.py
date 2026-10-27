"""Questions handed to a Webb expert: routed to the customer's branch and the right discipline, with the whole
conversation, so nobody asks twice. A small SQLite queue; the staff console reads it."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

DISCIPLINE = {"gas_tank": "Plumbing and water heaters", "elec_tank": "Plumbing and water heaters",
              "tankless": "Plumbing and water heaters"}
CHANNELS = ("message", "call", "callback", "video", "quote")      # quote: special-order requests (v2.3)


def _dumps(v, empty) -> str:
    """JSON for a column, never cut mid-value: kept in its own characters, and the empty value if it's still too long."""
    t = json.dumps(v, ensure_ascii=False)
    return t if len(t) <= 20000 else json.dumps(empty)


def _loads(t, empty):
    """A stored column that can't be read (an older row cut at 20,000 characters) reads as empty, not an error."""
    try:
        return json.loads(t or json.dumps(empty))
    except ValueError:
        return empty


class ExpertDesk:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._db() as c:
            c.execute("""create table if not exists cases (id text primary key, opened real, status text, branch text,
                         discipline text, channel text, account text, user text, question text, sku text, line text,
                         tier int, conversation text, draft text, reply text, replied real)""")

    def _db(self):
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        return c

    def open(self, *, question: str, conversation: list, sku: str | None, line, tier: int, channel: str,
             principal, branch: str, draft: dict | None = None) -> dict:
        case = {"id": uuid.uuid4().hex[:10].upper(), "opened": time.time(), "status": "open", "branch": branch,
                "discipline": "Inside sales: special orders" if channel == "quote" else
                DISCIPLINE.get(getattr(line, "cls", None), "Inside sales"),
                "channel": channel if channel in CHANNELS else "message",
                "account": getattr(principal, "account_id", None), "user": getattr(principal, "user_id", None),
                "question": (question or "")[:2000], "sku": sku, "line": getattr(line, "id", None), "tier": int(tier),
                "conversation": _dumps((conversation or [])[-12:], []), "draft": _dumps(draft or {}, {}),
                "reply": None, "replied": None}
        with self._lock, self._db() as c:
            c.execute(f"insert into cases ({','.join(case)}) values ({','.join('?' * len(case))})", list(case.values()))
        return {k: v for k, v in case.items() if k not in ("conversation", "draft")}

    def queue(self, branch: str | None = None, status: str | None = "open", limit: int = 100) -> list[dict]:
        q, args = "select * from cases where 1=1", []
        if branch:
            q += " and branch = ?"; args.append(branch)
        if status:
            q += " and status = ?"; args.append(status)
        with self._db() as c:
            rows = c.execute(q + " order by opened desc limit ?", args + [limit]).fetchall()
        return [dict(r) | {"conversation": _loads(r["conversation"], []), "draft": _loads(r["draft"], {})} for r in rows]

    def answer(self, case_id: str, reply: str, status: str = "answered") -> bool:
        with self._lock, self._db() as c:
            n = c.execute("update cases set reply = ?, replied = ?, status = ? where id = ?",
                          (reply[:4000], time.time(), status, case_id)).rowcount
        return n == 1
