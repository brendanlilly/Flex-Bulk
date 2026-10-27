"""Part numbers typed close to one we have (v2.43).

A part number lookup ends in one of four states, and only the first is ever called a match:

    exact       the key (case, spaces, dashes, dots and slashes ignored) is a catalog part number, item or UPC
    suggest     no exact key, but one or a few keys are one slip away: a character added, dropped, swapped or
                wrong, a leading zero dropped ("7LF" for "07LF"), a suffix cut short or run on ("07L", "07LFF").
                The page says "Did you mean ...?"; it never puts the neighbor on the page as if it were asked for,
                because a changed digit is usually a different size or model (the Oct 24 review's rule)
    contains    the typed digits are part of longer part numbers ("36142-07"): those are listed, labeled
    unresolved  none of the above

Neighbors are found by generating every one-slip variant of the typed key and looking each up in the catalog's
part-number map (a few hundred dictionary lookups, no extra index). Letter-suffix differences rank first, then a
dropped zero, a swap, a wrong letter, and last a changed digit. All-digit keys (Webb item numbers, UPCs) only get a
suggestion when there are at most three neighbors: in a dense number space a random slip lands on a real item.
"""
from __future__ import annotations

import re

import numpy as np

from webbsearch.retrieval.analyzer import compact_id

ALPHA = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
MIN_KEY = 5                 # shorter keys are too short to correct or search inside


def _variants(k: str):
    """(variant, kind, cost) for every one-slip change of k."""
    n = len(k)
    for i in range(n):                                   # a character typed that shouldn't be there
        v = k[:i] + k[i + 1:]
        yield v, "extra", 2.0 if k[i].isdigit() else 1.6
    for i in range(n - 1):                               # two characters swapped
        if k[i] != k[i + 1]:
            yield k[:i] + k[i + 1] + k[i] + k[i + 2:], "swap", 1.5
    for i in range(n):                                   # one wrong character
        for c in ALPHA:
            if c != k[i]:
                yield k[:i] + c + k[i + 1:], "wrong", 3.0 if (k[i].isdigit() or c.isdigit()) else 2.0
    for i in range(n + 1):                               # a character left out
        for c in ALPHA:
            yield k[:i] + c + k[i:], "missing", 2.6 if c.isdigit() else 1.8
    # letters and digits that look alike: O/0, I/1, L/1, S/5, B/8, Z/2
    for i, ch in enumerate(k):
        for a, b in LOOKALIKE:
            if ch == a:
                yield k[:i] + b + k[i + 1:], "lookalike", 1.0


LOOKALIKE = [("O", "0"), ("0", "O"), ("I", "1"), ("1", "I"), ("L", "1"), ("S", "5"), ("5", "S"), ("B", "8"), ("8", "B"),
             ("Z", "2"), ("2", "Z")]


def _zero_variants(typed: str):
    """A leading zero dropped or added in a group of digits as typed ("-7LF" for "-07LF"); separators matter here."""
    t = typed.upper()
    for m in re.finditer(r"\d+", t):
        s, e = m.span()
        yield compact_id(t[:s] + "0" + t[s:])
        if t[s] == "0" and e - s > 1:
            yield compact_id(t[:s] + t[s + 1:])


WHY = {"suffix": "the end of the number differs", "extra": "one character too many", "swap": "two characters swapped",
       "wrong": "one character different", "missing": "one character missing", "zero": "a leading zero",
       "lookalike": "a letter for a look-alike digit"}


class IdMatch:
    def __init__(self, catalog):
        self.cat = catalog
        self._keys = None

    def _sorted(self) -> np.ndarray:
        if self._keys is None:
            self._keys = self.cat._sorted_ids.astype("U")
        return self._keys

    def exact(self, key: str) -> list[int]:
        return self.cat.id_lookup(key) if len(key) >= 4 else []

    def suggest(self, typed: str, limit: int = 3) -> list[dict]:
        """Part numbers one slip from what was typed, best first: [{idx, key, kind, why, cost}]."""
        k = compact_id(typed)
        if len(k) < MIN_KEY or self.exact(k):
            return []
        found: dict[int, tuple] = {}

        def add(i, key, kind, cost):
            if i not in found or cost < found[i][2]:
                found[i] = (key, kind, cost)
        # a suffix cut short ("07L" for "07LF") or run on ("07LFF"), up to two characters
        for j in self.cat.id_prefix(k, 8):
            key = compact_id(self.cat.mfr_part[j])
            extra = key[len(k):]
            if 0 < len(extra) <= 2:
                add(j, key, "suffix", 0.8 if extra.isalpha() else 2.4)
        for cut in (1, 2):
            if len(k) - cut >= MIN_KEY:
                tail = k[-cut:]
                for j in self.exact(k[:-cut]):
                    add(j, k[:-cut], "suffix", 0.9 if tail.isalpha() else 2.4)
        seen = set()
        for v in _zero_variants(typed):
            seen.add(v)
            for j in self.exact(v):
                add(j, v, "zero", 1.0)
        best: dict[str, tuple] = {}
        for v, kind, cost in _variants(k):               # the cheapest explanation of each variant
            if len(v) >= 4 and (v not in best or cost < best[v][1]):
                best[v] = (kind, cost)
        for v, (kind, cost) in best.items():
            if v in seen:
                continue
            for j in self.exact(v):
                add(j, v, kind, cost)
        if k.isdigit() and len(found) > 3:
            return []                                    # a slip in a number lands on some real item: no guess
        out = sorted(found.items(), key=lambda kv: (kv[1][2], kv[1][0]))
        res, keys = [], set()
        for i, (key, kind, cost) in out:
            if key in keys and len(res) >= 1:
                continue
            keys.add(key)
            res.append({"idx": int(i), "key": key, "kind": kind, "why": WHY[kind], "cost": cost})
            if len(res) >= limit:
                break
        return res

    def contains(self, typed: str, limit: int = 60) -> tuple[list[int], int]:
        """Catalog products whose part number contains what was typed (5+ characters): (indexes, how many)."""
        k = compact_id(typed)
        if len(k) < MIN_KEY or self.exact(k):
            return [], 0
        keys = self._sorted()
        hit = keys[np.char.find(keys, k) >= 0]
        out: list[int] = []
        for key in hit[: limit * 2]:
            out.extend(self.exact(str(key)))
            if len(out) >= limit:
                break
        return out[:limit], int(len(hit))
