"""Brands and product lines in a search (v2.42).

One brand master (config/brands/brands.tsv) says how each brand is typed ("weil mclain", "am std", "b&g"), which catalog
brands a name covers (Honeywell covers Honeywell Home and Resideo; B-Line is listed two ways), which brands need a
choice (Mueller is three companies), when a short alias counts ("bw" is Bradford White next to a heater word) and which
real brands the demo catalog doesn't carry (a search for A. O. Smith says so instead of showing GE heaters).

Product lines come from the catalog's own series attribute (Moen Align, Zurn Dia 600) and from config/brands/lines.tsv,
the lines customers type that the catalog may not list, each with the kind of product it is: "lochinvar knight" is a
Lochinvar boiler, so with no Knight line here the page shows Lochinvar boilers and says so.

With the real catalog, the brand master and the line list belong in the product data (Stibo STEP / Snowflake): one
brand record with its aliases and parent, one line record per brand series. These files are the shape of that data.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from webbsearch.retrieval.analyzer import normalize

CFG = Path(__file__).resolve().parents[2] / "config" / "brands"


@dataclass
class BrandEntry:
    name: str                       # shown on the page
    covers: frozenset               # catalog brands a search for it accepts (empty: not carried)
    carried: bool
    context: re.Pattern | None = None
    choose: tuple = ()              # the companies to pick from ("Mueller")
    same: bool = False              # one company listed under several names: one value in the Brand filter
    cfg: bool = False               # from the brand master (it outranks the catalog's bare name)
    makes: str = ""                 # a brand the demo catalog lacks: what it's known for ("water heater")


@dataclass
class BrandHit:
    name: str
    covers: frozenset
    carried: bool
    span: tuple                     # token positions in normalize(raw)
    choose: tuple = ()
    makes: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "carried": self.carried, "covers": sorted(self.covers),
                **({"choose": list(self.choose)} if self.choose else {})}


@dataclass
class LineHit:
    brand: str
    line: str
    kind: str
    carried: bool                   # the catalog lists this line for the brand
    series: tuple = ()              # the catalog's series values that are this line
    tokens: tuple = ()              # the line's words as typed (normalized)
    alone: bool = False             # found without the brand typed
    generic: bool = False           # the trade uses the line's name for the product type ("propress"): no brand held
    note: str = ""                  # what the page says instead of the standard line notice

    def to_dict(self) -> dict:
        return {"brand": self.brand, "line": self.line, "kind": self.kind, "carried": self.carried,
                "series": list(self.series)[:8], "alone": self.alone, "generic": self.generic,
                **({"note": self.note} if self.note else {})}


def _rows(path: Path) -> list[list[str]]:
    if not path.exists():
        return []
    out = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        if ln.strip() and not ln.startswith("#"):
            out.append([c.strip() for c in ln.split("\t")])
    return out


_MODELISH = re.compile(r"\d")


class Brands:
    def __init__(self, catalog=None, brands: list[str] | None = None, series: dict | None = None):
        """`catalog`: the product catalog (brands and series read from it). Tests may pass `brands` and `series`
        ({brand: [series values]}) instead."""
        if catalog is not None:
            cat_brands = set(map(str, set(catalog.brand)))
        else:
            cat_brands = set(brands or [])
        self.catalog_brands = cat_brands
        self.alias: dict[tuple, list[BrandEntry]] = defaultdict(list)
        self.display: dict[str, str] = {}
        self.entries: dict[str, BrandEntry] = {}
        for b in cat_brands:
            e = BrandEntry(b, frozenset({b}), True)
            self.entries[b] = e
            k = normalize(b)
            if k:
                self.alias[k].append(e)
        for r in _rows(CFG / "brands.tsv"):
            r += [""] * (6 - len(r))
            name, aliases, covers, context, note, makes = r[:6]
            cov = {c.strip() for c in covers.split("|") if c.strip()} or {name}
            carried = bool(cov & cat_brands)
            e = BrandEntry(name, frozenset(cov & cat_brands), carried,
                           re.compile(context, re.I) if context else None,
                           tuple(sorted(cov & cat_brands)) if "choose" in note and len(cov & cat_brands) > 1 else (),
                           "same" in note, True, makes)
            if not context:
                self.entries[name] = e
            if e.same:
                for c in e.covers:
                    self.display[c] = name
            for a in [name] + [x for x in aliases.split("|") if x.strip()]:
                k = normalize(a)
                if not k:
                    continue
                lst = self.alias[k]
                # a brand-master row replaces the catalog's bare entry for the same words (it adds covers or a choice)
                if e.context is None:
                    lst[:] = [x for x in lst if x.cfg or x.context is not None]
                if e.context:
                    lst.insert(0, e)
                else:
                    lst.append(e)
        self.max_len = max((len(k) for k in self.alias), default=1)
        # ---- product lines
        self.lines: dict[tuple, list[tuple]] = defaultdict(list)      # alias tokens -> [(brand, line, kind, alone)]
        self.line_text: dict[tuple, set] = defaultdict(set)            # (brand, line) -> the aliases as typed
        self.line_note: dict[tuple, str] = {}
        for r in _rows(CFG / "lines.tsv"):
            r += [""] * (6 - len(r))
            brand, line, aliases, kind, alone, note = r[:6]
            self.line_note[(brand, line)] = note
            for a in [line] + [x for x in aliases.split("|") if x.strip()]:
                k = normalize(a)
                if k:
                    self.lines[k].append((brand, line, kind, alone.lower()))
                    self.line_text[(brand, line)].add(a.strip())
        self.max_line = max((len(k) for k in self.lines), default=1)
        # the catalog's own series per brand (word-like names only: "KRHL246N" is a model, not a line)
        self.series: dict[str, dict[tuple, set]] = defaultdict(lambda: defaultdict(set))
        src = series
        if src is None and catalog is not None:
            src = defaultdict(set)
            raw_attrs = getattr(catalog, "_attrs_raw", None)
            rx = re.compile(r'"series":\s*"([^"]+)"')
            if raw_attrs is not None:
                for b, a in zip(catalog.brand, raw_attrs):
                    m = rx.search(str(a)) if a else None
                    if m:
                        src[str(b)].add(m.group(1))
        for b, vals in (src or {}).items():
            for s in vals:
                head = s.split()[0]
                if _MODELISH.search(head) or len(head) < 3:
                    continue
                k = normalize(head)
                if k:
                    self.series[b][k].add(s)

    # ------------------------------------------------------------------ brands
    def detect(self, raw: str, toks: tuple | None = None) -> list[BrandHit]:
        """Brands named in a search, longest name first, left to right."""
        toks = tuple(toks if toks is not None else normalize(raw or ""))
        out, i = [], 0
        while i < len(toks):
            hit = None
            for L in range(min(self.max_len, len(toks) - i), 0, -1):
                ents = self.alias.get(tuple(toks[i:i + L]))
                if not ents:
                    continue
                e = next((x for x in ents if x.context is not None and x.context.search(raw or "")), None) or \
                    next((x for x in ents if x.context is None), None)
                if e is None:
                    continue
                hit = BrandHit(e.name, e.covers, e.carried, (i, i + L), e.choose, e.makes)
                break
            if hit:
                if not any(h.name == hit.name for h in out):
                    out.append(hit)
                i = hit.span[1]
            else:
                i += 1
        return out

    def names(self, raw: str, toks: tuple | None = None) -> list[str]:
        return [h.name for h in self.detect(raw, toks)]

    def facet_name(self, brand: str) -> str:
        return self.display.get(brand, brand)

    def covers(self, name: str) -> frozenset:
        e = self.entries.get(name)
        return e.covers if e else frozenset({name})

    # ------------------------------------------------------------------ lines
    def line(self, raw: str, hits: list[BrandHit]) -> LineHit | None:
        """A product line named in the search: after its brand ("lochinvar knight"), or alone when the line names
        its brand by itself ("greenstar", "packout")."""
        toks = tuple(normalize(raw or ""))
        named = {h.name for h in hits}
        covered = set().union(*(h.covers for h in hits)) if hits else set()
        best = None
        for i in range(len(toks)):
            for L in range(min(max(self.max_line, 3), len(toks) - i), 0, -1):
                key = tuple(toks[i:i + L])
                if any(i < h.span[1] and h.span[0] < i + L for h in hits):
                    continue                    # the brand's own words aren't its line
                # the catalog's series for a typed brand ("moen align", "zurn dia")
                for b in sorted(covered):
                    ss = self.series.get(b, {}).get(key)
                    if ss:
                        cand = LineHit(b, sorted(ss)[0].split()[0], "", True, tuple(sorted(ss)), key)
                        if best is None or len(key) > len(best.tokens):
                            best = cand
                for brand, line, kind, alone in self.lines.get(key, []):
                    if brand in named or brand in covered or (alone in ("yes", "generic") and not hits):
                        carried_series = tuple(sorted(set().union(*(self.series.get(b, {}).get(normalize(line), set())
                                                                    for b in (self.covers(brand) or {brand})))))
                        cand = LineHit(brand, line, kind, bool(carried_series), carried_series, key,
                                       alone=brand not in named, generic=alone == "generic" and brand not in named,
                                       note=self.line_note.get((brand, line), ""))
                        if best is None or len(key) > len(best.tokens) or (len(key) == len(best.tokens) and not best.kind):
                            best = cand
        return best


    def without_line(self, raw: str, hit: LineHit) -> str:
        """The search with the line's name (and a model number right after it: "npe 240") taken out."""
        alts = sorted(self.line_text.get((hit.brand, hit.line), {hit.line}), key=len, reverse=True)
        pat = "|".join(re.escape(a).replace(r"\ ", r"[\s-]*") for a in alts)
        rx = re.compile(rf"(?<![\w-])(?:{pat})(?:[\s-]*\d{{2,4}}[a-z]{{0,2}})?(?![\w-])", re.I)
        out = rx.sub(" ", raw or "", count=1)
        return re.sub(r"\s+", " ", out).strip()


def counts_by_brand(catalog, brand: str, covers) -> list[tuple[str, str, int]]:
    """A brand's categories in the catalog, most products first: [(family, category name, count)]."""
    import numpy as np
    m = np.isin(catalog.brand, list(covers or {brand}))
    fams = Counter(catalog.family[m].tolist())
    out = []
    for f, c in fams.most_common():
        j = int(np.flatnonzero(m & (catalog.family == f))[0])
        out.append((str(f), str(catalog.cat3[j]), int(c)))
    return out
