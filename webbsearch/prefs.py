"""Customer preferences (v2.46): how FlexSearch looks and works for one signed-in person.

Look and feel (theme, style, density, text size), the start page, the type-ahead (what it shows) and search results
(view, page size, sort, part numbers, in-stock only, list price). Saved to the person's sign-in, so they follow them to
any device; an account owner can make theirs the default for everyone on the account (each person can still change
their own). What a person sees = Webb defaults, then the account's defaults, then their own choices.

Guests have no preferences: they get the defaults (theme follows their device).

Stored in data/prefs/prefs.json (one small file; production keeps the same shape in PostgreSQL with the dashboard's
settings). Values are checked against the allowed lists below; anything else is dropped.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

from webbsearch.config import settings

SCOPES = ["categories", "kits", "orders", "events", "content"]
CHOICES: dict[str, list] = {
    "theme": ["device", "light", "dark"],
    "look": ["charcoal", "minimal", "cards"],
    "density": ["comfortable", "compact", "dense"],
    "text": ["standard", "large"],
    "start": ["dashboard", "search", "bulk", "kits", "list"],
    "ta_n": [4, 6, 8],
    "res_view": ["cards", "list", "table"],
    "res_per_page": [24, 48, 96],
    "res_sort": ["best", "bought", "avail", "price_asc"],
    "res_part": ["both", "webb", "mfr"],
}
FLAGS = ["ta_bought", "ta_part", "ta_price", "ta_suppliers", "ta_ask", "res_instock", "res_listprice"]
DEFAULTS: dict = {
    "theme": "device", "look": "charcoal", "density": "comfortable", "text": "standard", "start": "dashboard",
    "ta_bought": True, "ta_part": True, "ta_price": False, "ta_suppliers": True, "ta_ask": True, "ta_n": 6,
    "ta_scopes": list(SCOPES),
    "res_view": "cards", "res_per_page": 48, "res_sort": "best", "res_part": "both",
    "res_instock": False, "res_listprice": False,
}


def clean(p: dict | None) -> dict:
    """Only known keys with allowed values."""
    out: dict = {}
    for k, v in (p or {}).items():
        if k in CHOICES and v in CHOICES[k]:
            out[k] = v
        elif k in FLAGS and isinstance(v, bool):
            out[k] = v
        elif k == "ta_scopes" and isinstance(v, list):
            out[k] = [s for s in SCOPES if s in v]
    return out


class PrefStore:
    def __init__(self, path: Path | None = None):
        self.path = Path(path or (settings.data_dir / "prefs" / "prefs.json"))
        self._lock = threading.Lock()

    def _read(self) -> dict:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            return {"users": d.get("users", {}), "accounts": d.get("accounts", {})}
        except (OSError, ValueError):
            return {"users": {}, "accounts": {}}

    def _write(self, d: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=1, sort_keys=True)
        os.replace(tmp, self.path)

    @staticmethod
    def user_key(p) -> str | None:
        if not p.is_authenticated:
            return None
        return f"user:{p.user_id}" if p.user_id else f"acct:{p.account_id}"

    def get(self, p) -> dict:
        """What this person sees, and where each part came from."""
        d = self._read()
        company = clean(d["accounts"].get(str(p.account_id))) if p.is_authenticated else {}
        mine = clean(d["users"].get(self.user_key(p))) if p.is_authenticated else {}
        eff = {**DEFAULTS, **company, **mine}
        return {"prefs": eff, "mine": mine, "company": company, "company_on": bool(company),
                "can_set_company": bool(p.is_authenticated and p.role == "owner"),
                "signed_in": p.is_authenticated, "defaults": DEFAULTS}

    def put(self, p, prefs: dict, company: bool | None = None) -> dict:
        if not p.is_authenticated:
            raise PermissionError("sign in to save preferences")
        new = clean(prefs)
        with self._lock:
            d = self._read()
            key = self.user_key(p)
            d["users"][key] = {**clean(d["users"].get(key)), **new}
            if company is not None:
                if p.role != "owner":
                    raise PermissionError("only an account owner can set the account's defaults")
                if company:
                    d["accounts"][str(p.account_id)] = {**DEFAULTS, **d["users"][key]}
                else:
                    d["accounts"].pop(str(p.account_id), None)
            self._write(d)
        return self.get(p)

    def reset(self, p) -> dict:
        if not p.is_authenticated:
            raise PermissionError("sign in to save preferences")
        with self._lock:
            d = self._read()
            d["users"].pop(self.user_key(p), None)
            self._write(d)
        return self.get(p)


_store: PrefStore | None = None


def store() -> PrefStore:
    global _store
    if _store is None:
        _store = PrefStore()
    return _store
