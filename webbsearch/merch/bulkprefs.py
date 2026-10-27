"""Bulk order preferences: the crosses and brand orders merchandising sets, and the ones people teach it.

Set by merchandising (settings "crosses" and "brand_prefs", company-wide or for one branch):
  * brand cross   lines naming a brand Webb doesn't carry are matched as another ("Kohler" -> "American Standard")
  * item cross    a part number or a line's words -> one catalog item
  * brand order   for a category, the brands Webb prefers when a line names none (NIBCO > Apollo > Watts)
A branch's own entry wins over the company one for that branch.

Learned from choices (analytics events "bulk_choice", written when someone confirms, changes or swaps a Bulk
order match): for each line key (the line's words after shorthand expansion), which item people chose, company-wide
and per account; and when a line named a brand, which brand they took instead. Those counts add a bonus the next
time the same line is matched. Merchandising can switch a learned cross off, or promote it to an item cross.
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
from collections import Counter, defaultdict
from functools import lru_cache

from webbsearch.merch.store import norm_q


# Well-known brands the demo catalog doesn't carry, so a line naming one is recognized (for learned swaps and the
# "brands asked for" report) even before merchandising sets a cross. Production reads this from the brand master.
OTHER_BRANDS = ("Kohler", "Grohe", "Hansgrohe", "Pfister", "Jacuzzi", "Eljer", "Proflo", "Kingston Brass", "Kraus", "Blanco",
                "Franke", "A.O. Smith", "AO Smith", "State Water Heaters", "Raypak", "Laars", "Viessmann", "Speakman",
                "Bradley", "Acorn", "Haws", "Halsey Taylor", "Bemis", "Myers", "Barnes", "Sta-Rite", "Carrier", "Trane",
                "Lennox", "Goodman", "Daikin", "Mitsubishi")


def other_brand(text: str) -> str:
    """A brand the catalog doesn't carry, named in the line ("" if none)."""
    return next((b for b in OTHER_BRANDS if _word_in(b, text)), "")


def line_key(text: str) -> str:
    """The words of a line, for matching the same request again (count and punctuation dropped)."""
    t = norm_q(re.sub(r"[,;()\"”]+", " ", text or ""))
    return re.sub(r"^\d{1,5}\s*[-x×]?\s+", "", t)[:120]


@lru_cache(maxsize=4096)
def _word_re(word: str) -> re.Pattern:
    return re.compile(rf"(?<![\w-]){re.escape(word)}(?![\w-])", re.I)


def _word_in(word: str, text: str) -> bool:
    return bool(word) and _word_re(word.lower()).search(text or "") is not None


def scoped(items: list[dict], branch: str | None) -> list[dict]:
    """Enabled entries for this branch: the branch's own first, then company-wide."""
    on = [x for x in items or [] if x.get("enabled", True)]
    return [x for x in on if branch and x.get("scope") == str(branch)] + [x for x in on if x.get("scope", "company") == "company"]


def brand_cross(settings: dict, branch: str | None, text: str, brand_col: str = "") -> dict | None:
    """The brand cross for a line that names a brand merchandising crosses to another."""
    for c in scoped([x for x in settings.get("crosses") or [] if x.get("kind") == "brand"], branch):
        fb = c["from_brand"]
        if (brand_col and brand_col.strip().lower() == fb.lower()) or _word_in(fb, text):
            return c
    return None


def item_cross(settings: dict, branch: str | None, key: str, part_keys: set[str]) -> dict | None:
    for c in scoped([x for x in settings.get("crosses") or [] if x.get("kind") == "item"], branch):
        fp = re.sub(r"[^a-z0-9]", "", c.get("from_part", "").lower())
        if (fp and fp in part_keys) or (c.get("from_text") and c["from_text"] == key):
            return c
    return None


def brand_rank(settings: dict, branch: str | None, family: str, cat3: str, brand: str) -> tuple[int, int] | None:
    """(rank, list length) of `brand` in Webb's preference order for the category, or None."""
    f1, f2 = (family or "").lower(), (cat3 or "").lower()
    for b in scoped(settings.get("brand_prefs") or [], branch):
        if b["family"].lower() in (f1, f2):
            low = [x.lower() for x in b["brands"]]
            if brand.lower() in low:
                return low.index(brand.lower()), len(low)
            return None                      # the branch's (or company's) list for this category decides
    return None


def replace_brand(text: str, old: str, new: str) -> str:
    return _word_re(old.lower()).sub(new, text or "")


class Learned:
    """Counts of what people chose, read from the analytics log (refreshed when new choices arrive)."""

    def __init__(self, analytics, days: int = 365):
        self.a = analytics
        self.days = days
        self.lock = threading.Lock()
        self._mark = None
        self.by_key: dict[str, Counter] = defaultdict(Counter)
        self.by_key_acct: dict[tuple[str, str], Counter] = defaultdict(Counter)
        self.accts: dict[tuple[str, str], set] = defaultdict(set)
        self.swaps: dict[tuple[str, str], Counter] = defaultdict(Counter)
        self.last: dict[tuple[str, str], str] = {}
        self.meta: dict[str, dict] = {}

    def refresh(self) -> None:
        if self.a is None:
            return
        mark = self.a._q("SELECT COUNT(*), MAX(id) FROM events WHERE kind='bulk_choice'")[0]
        if mark == self._mark:
            return
        since = time.strftime("%Y-%m-%d", time.localtime(time.time() - self.days * 86400))
        rows = self.a._q("SELECT id, session, account, day, sku, meta FROM events WHERE kind='bulk_choice' AND day>=? ORDER BY id",
                         (since,))
        # one vote per order session per line: the last choice counts (someone changing their mind, or clicking twice)
        final = {}
        for eid, sess, acct, day, sku, m in rows:
            d = json.loads(m or "{}")
            if d.get("key") and sku:
                final[(sess or f"#{eid}", d["key"])] = (acct, day, sku, d)
        by_key, by_acct, accts, swaps, last, meta = defaultdict(Counter), defaultdict(Counter), defaultdict(set), defaultdict(Counter), {}, {}
        for acct, day, sku, d in final.values():
            key = d["key"]
            by_key[key][sku] += 1
            if acct:
                by_acct[(acct, key)][sku] += 1
                accts[(key, sku)].add(acct)
            last[(key, sku)] = day
            meta[sku] = {"brand": d.get("brand", ""), "family": d.get("family", "")}
            rb, br = (d.get("req_brand") or "").lower(), d.get("brand") or ""
            if rb and br and rb != br.lower():
                swaps[(rb, (d.get("family") or "").lower())][br] += 1
        with self.lock:
            self.by_key, self.by_key_acct, self.accts, self.swaps, self.last, self.meta = by_key, by_acct, accts, swaps, last, meta
            self._mark = mark

    def counts(self, key: str, sku: str, account: str | None) -> tuple[int, int]:
        return self.by_key.get(key, {}).get(sku, 0), (self.by_key_acct.get((account, key), {}).get(sku, 0) if account else 0)

    def swap(self, req_brand: str, family: str, brand: str) -> tuple[int, float]:
        c = self.swaps.get(((req_brand or "").lower(), (family or "").lower()))
        if not c:
            return 0, 0.0
        n = c.get(brand, 0)
        return n, n / max(sum(c.values()), 1)

    def bonus(self, bw: dict, off: set, key: str, sku: str, brand: str, family: str, req_brand: str,
              account: str | None) -> tuple[float, str | None]:
        """(bonus, reason) for one candidate on one line."""
        if not bw.get("learned"):
            return 0.0, None
        b, why = 0.0, None
        if f"{key}::{sku}" not in off:
            n, na = self.counts(key, sku, account)
            if n >= bw.get("learned_min", 2) or na >= 1:
                b += bw["learned"] * min(1.0, math.log1p(n) / math.log1p(10)) + 0.5 * bw["learned"] * min(1.0, math.log1p(na) / math.log1p(5))
                why = f"Chosen {n}× for this line" + (f" ({na} by your team)" if na else "")
        if req_brand and brand.lower() != req_brand.lower() and f"brand::{req_brand.lower()}::{brand.lower()}" not in off:
            n, share = self.swap(req_brand, family, brand)
            if n >= bw.get("learned_min", 2):
                b += bw["learned"] * share
                why = why or f"{req_brand} lines are usually matched to {brand} ({n}×)"
        return b, why

    _labels: dict | None = None

    def _family_labels(self, catalog) -> dict:
        """family id -> the category name people see (its most common cat3), computed once"""
        if self._labels is None:
            c = Counter(zip((str(x) for x in catalog.family), (str(x) for x in catalog.cat3)))
            best = {}
            for (f, c3), n in c.most_common():
                best.setdefault(f.lower(), c3)
            self._labels = best
        return self._labels

    def table(self, catalog, limit: int = 200) -> dict:
        """The learned crosses for the dashboard, strongest first."""
        rows = []
        for key, c in self.by_key.items():
            tot = sum(c.values())
            for sku, n in c.most_common(3):
                i = catalog.sku_to_idx.get(sku)
                rows.append({"key": key, "sku": sku, "title": str(catalog.title[i]) if i is not None else sku,
                             "brand": str(catalog.brand[i]) if i is not None else self.meta.get(sku, {}).get("brand", ""),
                             "n": n, "share": round(n / tot, 3), "line_total": tot, "accounts": len(self.accts.get((key, sku), ())),
                             "last": self.last.get((key, sku))})
        rows.sort(key=lambda r: (-r["n"], -r["share"]))
        labels = self._family_labels(catalog)
        swaps = [{"from": rb, "family": fam, "family_label": labels.get(fam, fam), "to": br, "n": n,
                  "share": round(n / max(sum(c.values()), 1), 3)}
                 for (rb, fam), c in self.swaps.items() for br, n in c.most_common(3)]
        swaps.sort(key=lambda r: -r["n"])
        return {"items": rows[:limit], "brands": swaps[:100]}
