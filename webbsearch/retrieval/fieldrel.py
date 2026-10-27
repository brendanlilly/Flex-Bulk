"""Relevance by field and by attribute (v2.44): the merchandising team's weights on where a search word is found.

Field weights for title, brand, category and specs act on the final order: an item gets (weight - 1) x the share of
the search's words found in that field, in ranker points, and the preview shows it as a chip. Title x1.5 on "brass ball
valve" puts +0.5 on an item whose title has all three words and +0.17 on one whose title has one. A field set to 0 is
also left out of what's gathered. Synonyms, description and part numbers multiply the search index's built-in weight
for that field (synonyms 0.9, description 0.35, part numbers 8.0): they change which items are gathered and the
text-match score the learned ranker reads, so their effect on the order is the ranker's, not a fixed number.

Attribute weights are points added when a search word matches that spec on the item: material +0.5 lifts the brass
items on "brass ball valve"; port +1.0 lifts the full-port ones on "full port ball valve" (not the standard-port ones:
words most values of an attribute share, like "port" or "gpm", don't count as a match on their own; see matches()).
"""
from __future__ import annotations

import threading
from collections import Counter, defaultdict

import numpy as np

from webbsearch.retrieval.analyzer import normalize

FIELDS = ("title", "brand", "cat", "attrs", "syn", "desc", "ids")
FIELD_LABELS = {"title": "Title", "brand": "Brand", "cat": "Category", "attrs": "Specs (all attributes)",
                "syn": "Synonyms and trade terms", "desc": "Description", "ids": "Part numbers"}
FIELD_HELP = {
    "title": "Words in the product title.",
    "brand": "The brand name. Only counts for words of a brand the search names (\"valve\" in Legend Valve doesn't).",
    "cat": "The category path and product family (Valves > Ball Valves).",
    "attrs": "Any spec value: material, connection, size, finish, voltage and the rest. Set single specs below.",
    "syn": "Trade terms and synonyms added to items at indexing (sweat for solder, FIP for female thread).",
    "desc": "The long description. Low by default: descriptions mention many things an item isn't.",
    "ids": "Manufacturer, Webb and UPC numbers. Exact part numbers stay first whatever this is set to.",
}
POST_FIELDS = ("title", "brand", "cat", "attrs")       # the fields whose words are checked again on the final order
FIELD_SCALE = 1.0
NOISE = frozenset({"in", "x", "ft", "and", "for", "with", "the", "a", "of", "to", "or", "w", "inch"})
GENERIC_SHARE = 0.4          # a word in more than 40% of an attribute's distinct values is that attribute's own word
_CACHE_MAX = 200_000


def matches(value: tuple[frozenset, frozenset], q: frozenset) -> bool:
    """A spec value matches a search when all its words are in the search ("Full Port", "Direct Vent"), or at least
    half are and one of those tells this attribute's values apart ("Stainless Steel" on "stainless ball valve";
    "Standard Port" doesn't match "full port ball valve")."""
    allw, distinct = value
    if allw <= q:
        return True
    hit = allw & q
    return bool(hit & distinct) and 2 * len(hit) >= len(allw)


class FieldRel:
    def __init__(self, catalog):
        self.cat = catalog
        self._lock = threading.Lock()
        self._fields: dict[str, dict[int, frozenset]] = {f: {} for f in POST_FIELDS}
        self._attrs: dict[int, dict[str, frozenset]] = {}
        self._generic: dict[str, frozenset] | None = None
        self._keys: list[dict] | None = None

    # ------------------------------------------------------------------ catalog side
    def _survey(self):
        """Per attribute: how many items have it, example values, and the words most of its values share."""
        if self._generic is not None:
            return
        c = self.cat
        n = len(c.sku)
        step = max(1, n // 60_000)
        items, vals = Counter(), defaultdict(set)
        for i in range(0, n, step):
            for k, v in c.attrs(i).items():
                v = str(v).strip()
                if v:
                    items[k] += 1
                    if len(vals[k]) < 400:
                        vals[k].add(v)
        generic = {}
        for k, vs in vals.items():
            df = Counter(t for v in vs for t in set(normalize(v)))
            generic[k] = frozenset(t for t, m in df.items() if m >= 2 and m / len(vs) > GENERIC_SHARE) | NOISE
        self._keys = [{"key": k, "items": int(m * step), "examples": sorted(vals[k])[:6]}
                      for k, m in items.most_common()]
        self._generic = generic

    def keys(self) -> list[dict]:
        with self._lock:
            self._survey()
        return self._keys or []

    def warm(self):
        self.keys()

    def field_tokens(self, f: str, i: int) -> frozenset:
        d = self._fields[f]
        t = d.get(i)
        if t is None:
            c = self.cat
            if f == "title":
                t = frozenset(normalize(str(c.title[i])))
            elif f == "brand":
                t = frozenset(normalize(str(c.brand[i])))
            elif f == "cat":
                t = frozenset(normalize(" ".join(str(x) for x in (c.cat1[i], c.cat2[i], c.cat3[i], c.family[i]))))
            else:
                t = frozenset(x for v in c.attrs(i).values() for x in normalize(str(v)))
            if len(d) > _CACHE_MAX:
                d.clear()
            d[i] = t
        return t

    def attr_tokens(self, i: int) -> dict[str, tuple[frozenset, frozenset]]:
        """attribute -> (the value's words, the ones that tell its values apart)."""
        a = self._attrs.get(i)
        if a is None:
            with self._lock:
                self._survey()
            g = self._generic or {}
            a = {}
            for k, v in self.cat.attrs(i).items():
                allw = frozenset(normalize(str(v))) - NOISE
                if allw:
                    a[k] = (allw, allw - g.get(k, NOISE))
            if len(self._attrs) > _CACHE_MAX:
                self._attrs.clear()
            self._attrs[i] = a
        return a

    # ------------------------------------------------------------------ query side
    @staticmethod
    def query_tokens(tokens, sizes=()) -> frozenset:
        q = set(tokens)
        for s in sizes or ():
            q.update(normalize(str(s)))
        return frozenset(t for t in q if t not in NOISE)

    @staticmethod
    def active(rel: dict | None) -> bool:
        if not rel:
            return False
        return any(abs(float(v) - 1.0) > 1e-9 for v in (rel.get("fields") or {}).values()) or \
            any(abs(float(v)) > 1e-9 for v in (rel.get("attrs") or {}).values())

    def adjust(self, idx: np.ndarray, qtoks: frozenset, rel: dict | None) -> dict[str, np.ndarray]:
        """name -> points per candidate (same order as idx), for the fields and attributes whose weight is set."""
        out: dict[str, np.ndarray] = {}
        if not rel or not len(idx) or not qtoks:
            return out
        nq = len(qtoks)
        for f in POST_FIELDS:
            m = float((rel.get("fields") or {}).get(f, 1.0)) - 1.0
            if abs(m) < 1e-9:
                continue
            share = np.fromiter((len(qtoks & self.field_tokens(f, int(i))) / nq for i in idx), np.float64, len(idx))
            if share.any():
                out[f"{FIELD_LABELS[f].split(' (')[0].lower()} ×{m + 1:g}"] = m * FIELD_SCALE * share
        aw = {k: float(v) for k, v in (rel.get("attrs") or {}).items() if abs(float(v)) > 1e-9}
        if aw:
            arrs = {k: np.zeros(len(idx)) for k in aw}
            for r, i in enumerate(idx):
                at = self.attr_tokens(int(i))
                for k, w in aw.items():
                    t = at.get(k)
                    if t and matches(t, qtoks):
                        arrs[k][r] = w
            for k, a in arrs.items():
                if a.any():
                    out[f"spec: {k}"] = a
        return out
