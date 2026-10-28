"""Brand names: the manufacturer a customer's line or a vendor export names -> the manufacturers on the item list.

Vendor masters and the PIM spell the same company differently ("Mueller Industries" / "Mueller", "Resideo (Honeywell
Home)" / "Honeywell Home", "A. O. Smith / State" / "A.O. Smith" + "State"), and some lines name a parent company
whose products list under a product brand (Diversitech -> Quick-Sling). resolve(name) gives the list's brands for
a name from:
  1. the brand names table (data/bulk/brand_names.json, edited on the dashboard's Brand names page)
  2. the same name, ignoring case and punctuation ("AO Smith" = "A.O. Smith")
  3. each part of a name with / ( ) or , in it, and a list brand that is the start of the name or the name the start
     of a list brand ("Legend Valve" -> Legend), when that is unambiguous enough (4+ letters)

Suggestions: when a line's part # finds an item of a different brand, the pair (line brand -> item brand) is a
candidate name; match reports count them, and two or more agreeing lines show on the dashboard to accept.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path

from webbsearch.config import settings


# a list's "manufacturer" that names no company: a house line, commodity, the rep agency
GENERIC = re.compile(r"commodity|house line|distributor|generic|various|misc|no brand|unbranded|\(rep\)", re.I)


def key(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


class BrandNames:
    def __init__(self, catalog_brands, path: Path | None = None):
        self.path = Path(path or os.environ.get("FLEXBULK_BRAND_NAMES") or settings.data_dir / "bulk" / "brand_names.json")
        self.lock = threading.Lock()
        self.by_key: dict[str, set[str]] = defaultdict(set)
        for b in set(map(str, catalog_brands)):
            if b and b.strip():
                self.by_key[key(b)].add(b)
        self.keys = sorted(k for k in self.by_key if len(k) >= 4)
        self._cache: dict[str, frozenset] = {}
        self.data = self._read()

    def _read(self) -> dict:
        try:
            d = json.loads(self.path.read_text())
            return {"names": d.get("names", {}), "suggested": d.get("suggested", {}), "dismissed": d.get("dismissed", [])}
        except (OSError, ValueError):
            return {"names": {}, "suggested": {}, "dismissed": []}

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, sort_keys=True))
        os.replace(tmp, self.path)
        self._cache.clear()

    # ------------------------------------------------------------------ the lookup
    def resolve(self, name: str) -> frozenset:
        """The list's brands a name means (empty: none of them)."""
        name = (name or "").strip()
        if not name:
            return frozenset()
        c = self._cache.get(name)
        if c is not None:
            return c
        out: set[str] = set()
        table = {key(k): v for k, v in self.data["names"].items()}
        parts = [name] + [x for x in re.split(r"[/(),;&]| and ", name) if x.strip()]
        for part in parts:
            k = key(part)
            if not k:
                continue
            if k in table:
                out |= set(table[k])
            if k in self.by_key:
                out |= self.by_key[k]
        if not out:
            for part in parts:
                k = key(part)
                if len(k) < 4:
                    continue
                hits = [lk for lk in self.keys    # "muellerindustries" starts with "mueller"; "legendvalve" with "legend"
                        if (k.startswith(lk) and (len(lk) >= 6 or len(lk) >= len(k) // 2)) or (lk.startswith(k) and len(k) >= 5)]
                if hits:
                    longest = max(len(h) for h in hits)
                    for lk in hits:
                        if len(lk) == longest or lk.startswith(k):
                            out |= self.by_key[lk]
        r = frozenset(out)
        if len(self._cache) > 20000:
            self._cache.clear()
        self._cache[name] = r
        return r

    def same(self, name: str, brand: str) -> bool | None:
        """Is the item's brand the one the line names? None when the name means nothing on the list."""
        r = self.resolve(name)
        if not r:
            return None
        return str(brand) in r

    # ------------------------------------------------------------------ the table
    def add(self, name: str, brands: list[str], who: str = "") -> dict:
        name = name.strip()[:120]
        brands = [b for b in brands if b in {x for v in self.by_key.values() for x in v}]
        if not name or not brands:
            raise ValueError("a name and at least one manufacturer on the item list")
        with self.lock:
            self.data["names"][name] = sorted(set(self.data["names"].get(name, [])) | set(brands))
            self.data["suggested"].pop(f"{name}=>{brands[0]}", None)
            for b in brands:
                self.data["suggested"].pop(f"{name}=>{b}", None)
            self._write()
        return self.table()

    def remove(self, name: str) -> dict:
        with self.lock:
            self.data["names"].pop(name, None)
            self._write()
        return self.table()

    def suggest(self, pairs: Counter, source: str = "") -> None:
        """Count (line brand, item brand) pairs seen on part # hits (from match reports). Counts are kept per
        source (the report's file or source), so running the same report again doesn't add them twice."""
        if not pairs:
            return
        with self.lock:
            for (a, b), n in pairs.items():
                if not a or not b or GENERIC.search(a) or self.same(a, b) or f"{a}=>{b}" in self.data["dismissed"]:
                    continue
                k = f"{a}=>{b}"
                s = self.data["suggested"].get(k) or {"name": a, "brand": b, "n": 0, "last": ""}
                by = s.setdefault("by", {})
                by[source or "?"] = n
                s["n"] = sum(by.values())
                s["last"] = time.strftime("%Y-%m-%d")
                self.data["suggested"][k] = s
            self._write()

    def dismiss(self, name: str, brand: str) -> dict:
        with self.lock:
            k = f"{name}=>{brand}"
            self.data["suggested"].pop(k, None)
            if k not in self.data["dismissed"]:
                self.data["dismissed"].append(k)
            self._write()
        return self.table()

    def table(self) -> dict:
        sug = sorted((s for s in self.data["suggested"].values() if s["n"] >= 2), key=lambda s: -s["n"])
        return {"names": [{"name": k, "brands": v} for k, v in sorted(self.data["names"].items())], "suggested": sug[:200]}


_bn: BrandNames | None = None


def get(engine) -> BrandNames:
    global _bn
    if _bn is None or getattr(_bn, "_cat", None) is not engine.catalog:
        _bn = BrandNames(engine.catalog.brand)
        _bn._cat = engine.catalog
    return _bn
