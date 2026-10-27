"""Search results page service: filters, facets, availability and product detail.

`results()` returns everything a results page needs in one call:
  - ranked items (up to 300 after filtering) with price, availability at the
    pickup branch and nearby, purchase history, list membership and reasons
  - quick-filter counts (nearby, previously purchased, on my lists, in stock
    at my branch, lead-free)
  - disjunctive facet counts (each facet counted with every *other* filter
    applied) for family, brand, size, type, connection, material, price and
    auto-selected spec axes of the dominant product family
  - visual "shop by type" tiles and the axes the size x type matrix view uses
  - account blocks (orders / lists / content) from the federated search

Filters apply to a 600-candidate ranked pool. That is enough for a results
page; a production build pushes structured filters into OpenSearch so
counts cover the full match set (SPEC.md section 6).
"""
from __future__ import annotations

import json
import math
import re
import time
from collections import Counter, defaultdict
from datetime import date

import numpy as np
import pandas as pd

from webbsearch.config import settings
from webbsearch.merch.rules import (active_rules, adjust_scores, any_mask, diversify, hidden_skus, pin_badges,
                                   pinned_skus, place_pins, rewrite_query)
from webbsearch.rank.features import FEATURES
from webbsearch.retrieval import intents
from webbsearch.retrieval import phrase as phmod, query_forms as qforms, requirements as reqmod, trade_terms
from webbsearch.retrieval.analyzer import NUM_UNIT, clean_dashes, compact_id, expand_concepts, normalize
from webbsearch.retrieval.entitlements import Principal
from webbsearch.retrieval.hvac import climate_zone
from webbsearch.retrieval.hybrid import Candidates
from webbsearch.retrieval.webb_items import GENERIC_CODE

F = {f: i for i, f in enumerate(FEATURES)}
POOL = 600
MAX_ITEMS = 300
SIZE_ORDER = ["1/8", "1/4", "3/8", "1/2", "5/8", "3/4", "7/8", "1", "1-1/4", "1-1/2", "2", "2-1/2", "3", "4",
              "5", "6", "8", "10", "12", "16"]
AXIS_LABEL = {"seer": "SEER2", "ref": "Refrigerant", "hspf2": "HSPF2", "eer2": "EER2", "ton": "Tons", "btu": "BTU",
              "gal": "Gallons", "afue": "AFUE", "mfd": "MFD", "gpf": "GPF", "hp": "HP", "mbh": "MBH", "volt": "Voltage"}
QUICK = ("nearby", "instock", "purchased", "lists", "leadfree")
TOOL_WORDS = {"tool", "tools", "jaw", "jaws", "crimper", "crimpers", "expander", "expanders", "cutter", "cutters",
              "crimping", "presser"}
# a page with fewer products than this is widened, one labeled step at a time (v2.43); WEBB_RELAX_BELOW overrides
RELAX_BELOW = int(__import__("os").environ.get("WEBB_RELAX_BELOW", "5"))
FACETS = ("family", "brand", "size", "type", "connection", "material", "series")
# spec axes read as numbers, so the rail can offer a range ("40 to 75 gallons") instead of one chip per value (v2.42)
NUM_AXES = {"gal": "gal", "btu": "BTU", "mbh": "MBH", "ton": "ton", "hp": "HP", "gpm": "GPM", "seer": "SEER2",
            "gpf": "GPF", "mfd": "MFD", "psi": "PSI", "afue": "%", "rpm": "RPM", "length": "ft", "dia": "in."}


def axis_num(v) -> float | None:
    """The number in a spec value: "1/3 HP" 0.333, "150,000" 150000, "40" 40, "45/5" 45, "1.28 GPF" 1.28."""
    s_ = str(v or "").replace(",", "")
    m = re.match(r"\s*(\d+)\s*-\s*(\d+)/(\d+)", s_) or None
    if m:
        return int(m.group(1)) + int(m.group(2)) / int(m.group(3))
    m = re.match(r"\s*(\d+)/(\d+)\b(?!\s*mfd)", s_, re.I)
    if m and int(m.group(2)) and int(m.group(1)) < int(m.group(2)):
        return int(m.group(1)) / int(m.group(2))
    m = re.match(r"\s*(\d+(?:\.\d+)?)", s_)
    return float(m.group(1)) if m else None

MATERIAL_RULES = [
    ("Copper", r"wrot copper|\bcopper\b|acr"), ("Bronze", r"bronze"), ("Brass", r"brass"),
    ("Stainless", r"stainless|304|316"), ("Black iron", r"black malleable|black iron|\bblack\b(?! matte)"),
    ("Galvanized", r"galvanized"), ("Cast iron", r"cast iron|no-hub|ductile"), ("CPVC", r"cpvc"),
    ("PVC", r"\bpvc\b"), ("ABS", r"\babs\b"), ("PEX", r"\bpex\b"), ("Carbon steel", r"carbon steel|a105|a234|wpb"),
    ("Poly", r"\bpoly\b|polypropylene"),
]


# ---------------------------------------------------------------- query/title heads
# English trade phrases are head-final: "water heater" is a heater, "water heater connector" is a connector.
# A product whose own head noun isn't the query's head is an accessory for it, so it ranks below the real thing.
UNITS = {"gal", "ft", "volt", "amp", "psi", "btu", "mbh", "ton", "hp", "mfd", "gpm", "gph", "gpf", "lb", "oz", "awg",
         "leadfree", "seer", "seer2", "watt", "kw", "cfm", "in", "inch", "sch", "schedule"}
GENERIC_HEADS = {"pump", "valve", "unit", "fitting", "kit", "part", "assembly", "system", "tank", "stop", "coupling", "set"}
# words that describe the trade or the shopper, not the product: never a reason to say "no match"
COVER_SKIP = {"hvac", "plumbing", "heating", "cooling", "residential", "commercial", "new", "replacement", "universal",
              "standard", "good", "best", "cheap", "quality", "contractor", "grade", "type", "line"}
COVER_SYN = {"iron": {"malleable", "castiron"}, "flex": {"flexible", "corrugated"}, "tstat": {"thermostat"},
             "pressure": {"psi", "relief", "prv", "reducing"}}   # a relief valve rated in PSI is a pressure relief valve
HEAD_EQUIV = {"preventer": {"assembly", "preventer"}, "cap": {"capacitor"}, "condensing": {"condensing", "condenser"},
              "tubing": {"tube", "tubing"}, "tube": {"tube", "tubing"}}
MODEL_EXCLUDE = GENERIC_CODE
# spec nouns that follow a number ("20 ft lift", "15 ft head"): part of a spec, never the product asked for
SPEC_NOUNS = {"lift", "head", "long", "wide", "deep", "high", "tall", "rise", "run", "length", "width", "depth",
              "height", "capacity", "rating", "rated", "flow", "angle", "pattern", "spray", "cone", "hollow", "solid",
              "pole", "phase", "stage", "speed", "zone", "coil", "loop", "port"}


# title words that end a title without being its product: "Nipple Close", "Bowl Only"; after a size, an end or a
# form ("Filter Drier Liquid Line 3/8 SAE", "CSST Fitting 250 ft Coil"); "Liquid Line" says which line a drier is for
_TITLE_TAIL = {"close", "only", "suction", "liquid", "discharge"}
_TITLE_AFTER_SIZE = {"sae", "sweat", "flare", "coil", "npt", "fip", "mip", "odf", "straight", "stick",
                     "high", "wide", "deep", "long"}     # v2.44: "Panel Radiator 12 in. High" is a radiator
_TITLE_LINE = re.compile(r"\b(?:liquid|suction|discharge)\s+line\b", re.I)
_TITLE_TOK = re.compile(r"(?<!\d-)[A-Za-z][A-Za-z]+|\d[\d/.\-]*")    # not "Way" in "Zone Valve 3-Way"
_TITLE_WORD = re.compile(r"(?<!\d-)[A-Za-z][A-Za-z]+")


_TITLE_COMMA = re.compile(r",(?!\d{3})")            # "30,000 BTU" is a number, not the end of the name


def title_head(title: str) -> str:
    t = re.sub(r"\([^)]*\)", " ", _TITLE_COMMA.split(title)[0])
    t = re.split(r"\s+(?:for|with|w/)\s+|\s+-\s+", t)[0]
    toks = _TITLE_TOK.findall(_TITLE_LINE.sub(" ", t))
    for k in range(len(toks) - 1, -1, -1):
        w = toks[k]
        if w[0].isdigit():
            continue
        n = normalize(w)
        if not n or len(n[-1]) < 3 or n[-1] in UNITS or n[-1] in _TITLE_TAIL:
            continue
        prev = toks[k - 1] if k else ""
        before = toks[k - 2] if k >= 2 else ""
        if n[-1] in _TITLE_AFTER_SIZE and (prev[:1].isdigit() or (((normalize(prev) and normalize(prev)[-1] in UNITS)
                                                                    or prev.lower() in ("in", "ft", "mm", "cm"))
                                                                   and before[:1].isdigit())):
            continue
        return n[-1]
    return ""


def paren_head(title: str) -> str:
    """The product a parenthesis names: "Coupling (Joint Restraint)" is also a joint restraint."""
    m = re.search(r"\(([^)]*)\)", _TITLE_COMMA.split(title)[0])
    if not m:
        return ""
    ws = _TITLE_WORD.findall(m.group(1))
    n = normalize(ws[-1]) if ws else ()
    return n[-1] if n and len(n[-1]) >= 3 else ""


def content_words(tokens, brand_tokens: set) -> list[str]:
    """The query's product words in order: no units, brands, numbers or the spec noun after a number."""
    out = []
    for j, t in enumerate(tokens):
        if not (t.isalpha() and len(t) >= 3 and t not in UNITS and t not in brand_tokens):
            continue
        prev = tokens[j - 1] if j else ""
        if t in SPEC_NOUNS and prev and (prev in UNITS or prev[0].isdigit()):
            continue
        out.append(t)
    return out


def query_heads(tokens, brand_tokens: set) -> set:
    words = content_words(tokens, brand_tokens)
    if not words:
        return set()
    heads = {words[-1]}
    if words[-1] in GENERIC_HEADS and len(words) >= 2:
        heads.add(words[-2])
    for h in list(heads):
        heads |= HEAD_EQUIV.get(h, set())
    return heads


def head_match(h: str, heads: set) -> bool:
    # a cut-off or plural form ("valv", "valves"), never a different word that starts the same ("red" isn't "reducer")
    return bool(h) and any(h == t or (len(t) >= 4 and h.startswith(t) and len(h) - len(t) <= 3)
                           or (len(h) >= 4 and t.startswith(h) and len(t) - len(h) <= 3) for t in heads)


def raw_phrase(raw: str, toks: list[str]) -> str:
    """The words as the shopper typed them for these analyzer tokens ("reducing" -> "reducer")."""
    want, out = set(toks), []
    for c in raw.split():
        w = c.strip(",;:()[]\"'.")
        if w and want & set(normalize(w)):
            out.append(w)
    return " ".join(out) or " ".join(toks)


def model_tokens(raw: str) -> list[str]:
    """Chunks the user typed that look like a model / part number (TH6320U2008, 77317, NPE-240A2)."""
    out = []
    chunks = raw.split()
    for j, c in enumerate(chunks):
        c = c.strip(",;:()[]\"'")
        k = compact_id(c)
        if len(k) < 4 or not any(ch.isdigit() for ch in k) or "/" in c or MODEL_EXCLUDE.match(k) or trade_terms.is_term(c) \
                or NUM_UNIT.match(c):
            continue
        nxt = normalize(chunks[j + 1]) if j + 1 < len(chunks) else ()
        if k.isdigit() and (re.fullmatch(r"\d{1,3}(,\d{3})+", c) or (nxt and nxt[0] in UNITS) or
                            (len(k) <= 6 and int(k) % 500 == 0)):
            continue      # 60,000 btu / 30000 btu / "mini split 12000" are quantities
        if any(ch.isalpha() for ch in k) or len(k) >= 5:
            out.append(c)
    return out


def material_group(title: str, attrs: dict, brand: str | None = None) -> str | None:
    """The material a product is made of, from its material attribute and title; never from its brand name
    (Merit Brass makes stainless unions)."""
    if brand and title.startswith(brand):
        title = title[len(brand):]
    t = (attrs.get("material") or "") + " " + title
    t = t.lower()
    for name, pat in MATERIAL_RULES:
        if re.search(pat, t):
            return name
    return None


def connection_of(attrs: dict) -> str | None:
    return attrs.get("connection") or attrs.get("end")


def size_key(v: str) -> float:
    try:
        return SIZE_ORDER.index(v)
    except ValueError:
        pass
    m = re.match(r"[\d.,]+", v or "")
    return 100 + float(m.group(0).replace(",", "")) if m else 1e9


def _with_extra(c: Candidates, extra: list[int]) -> Candidates:
    """The candidate pool plus items a merchandising rule pins (they carry no retrieval signal)."""
    k = len(extra)
    z = np.zeros(k, np.float32)
    return Candidates(np.concatenate([c.idx, np.array(extra, c.idx.dtype)]), np.concatenate([c.rrf, z]),
                      np.concatenate([c.bm25, z]), np.concatenate([c.lex_rank, np.zeros(k, c.lex_rank.dtype)]),
                      np.concatenate([c.dense_sim, z]), np.concatenate([c.dense_rank, np.zeros(k, c.dense_rank.dtype)]),
                      np.concatenate([c.exact, np.zeros(k, c.exact.dtype)]), c.lex_top_idx)


_MEASURE_LEADS = {"under", "over", "below", "above", "than", "least", "most", "to", "max", "maximum", "min", "minimum",
                  "od", "o.d."}


# words a spelling fix never touches: ordinary English and the phrase reader's own word lists
# a repair part listed for one unit: "Toto Flushometer Rebuild Kit for JPL766W", "Riello Aquastat Relay for FUZL320F Series"
_FOR_MODEL = re.compile(r"\bfor\s+([A-Z0-9][A-Za-z0-9.\-/]{2,})(?:\s+Series)?\s*$")


def fit_model(title: str) -> str | None:
    """The unit model a repair part's title says it fits, if any."""
    m = _FOR_MODEL.search(title or "")
    return m.group(1) if m and any(ch.isdigit() for ch in m.group(1)) else None


_SIZE_ONLY = re.compile(r"(?:an?\s+)?\d[\d/\-.\s]*(?:\s*(?:in\.?|inch(?:es)?|\"))?", re.I)
_NO_SPELL = phmod.COMMON | phmod.DESCRIPTORS | phmod.DROP | phmod.STANDARDS | phmod.SPEC_NOUNS | phmod.UNITS | phmod.PROTECT
# query head -> (item heads, words the item must name): the same product under another name
_ALSO_HEAD = {"reducing": ({"coupling", "bushing"}, ("reducing", "reducer"))}
# heads a kind word can stand in for, and where the catalog can be asked "does it have this kind at all?"
PROBE_HEADS = GENERIC_HEADS | {"switch", "box", "drain", "adapter"}


def _phrase_heads(ph) -> set:
    """The product nouns a phrase accepts: its head, and for a generic head (pump, valve, kit) the kind word
    before it too ("circulator pump" is a circulator)."""
    hs = {ph.head} | HEAD_EQUIV.get(ph.head, set())
    if ph.head in GENERIC_HEADS and ph.mods:
        hs.add(ph.mods[0])
    return hs


def _without_brands(toks: list[str], brands: list[str]) -> list[str]:
    """The tokens without the brand names typed: "Merit Brass" names a brand, not a material."""
    out = list(toks)
    for b in brands:
        bt = list(normalize(b))
        for j in range(len(out) - len(bt), -1, -1):
            if bt and out[j:j + len(bt)] == bt:
                del out[j:j + len(bt)]
    return out


def _same_head(h: str, w: str) -> bool:
    return h == w or w in HEAD_EQUIV.get(h, ()) or h in HEAD_EQUIV.get(w, ()) or \
        (len(w) >= 4 and len(h) >= 4 and abs(len(h) - len(w)) <= 2 and (h.startswith(w) or w.startswith(h)))


class _Lexicon(phmod.Lexicon):
    """The phrase reader's view of the catalog, from the ranked pool of one search."""

    def __init__(self, svc: "ResultsService", idx):
        self.s = svc
        self.top = [int(i) for i in idx[:200]]
        self.hc = Counter(h for i in self.top for h in svc.heads(i) if h)
        self._pt: set | None = None
        self._tc: Counter | None = None
        self._th: Counter | None = None

    def pool_toks(self) -> set:
        if self._pt is None:
            self._pt = set().union(*(self.s._toks(i) for i in self.top)) if self.top else set()
        return self._pt

    def known(self, w: str) -> bool:
        pt = self.pool_toks()
        return w in pt or bool(COVER_SYN.get(w, set()) & pt) or self.s.e.backend.term_exists(w)

    def pool_head(self, w: str) -> bool:
        return any(n >= 2 and _same_head(h, w) for h, n in self.hc.items())

    def filed_under(self, noun: str, other: str) -> bool:
        cat = self.s.e.catalog
        its = [i for i in self.top[:60] if any(_same_head(h, noun) for h in self.s.heads(i))]
        if not its:
            return False
        hit = sum(any(_same_head(t, other) for t in normalize(str(cat.cat3[i]))) for i in its)
        return hit >= 0.5 * len(its)

    def top_heads(self) -> dict:
        if self._th is None:
            c: Counter = Counter()
            for i in self.top[:30]:
                for h in self.s.heads(i):
                    c[h] += 1
                    for e_ in HEAD_EQUIV.get(h, ()):
                        if e_ != h:
                            c[e_] += 1
            self._th = c
        return self._th

    def global_head(self, w: str) -> bool:
        return self.s.global_heads().get(w, 0) >= 3

    def correct(self, w: str) -> str | None:
        """A catalog word this is: trade shorthand ("std", "fem", "prog"), a cut-off product noun ("3/4 ball valv")
        or a misspelling. Not any longer word it starts: "bolt" isn't "bolted"."""
        if w in _NO_SPELL:
            return None
        if w in phmod.SHORTHAND:
            return phmod.SHORTHAND[w]
        if len(w) >= 4:
            heads = [h for h, n in self.hc.most_common() if n >= 2 and h.startswith(w) and len(h) - len(w) <= 3]
            if heads:
                return heads[0]
        cands = [t for t in self.pool_toks() if t[:1] == w[:1] and t.isalpha() and len(t) >= 4]
        return phmod.fuzzy_pick(w, cands) if len(w) >= 4 else None


GUIDE_STOP = {"troubleshooting", "troubleshoot", "common", "problem", "install", "installing", "guide", "how", "use",
              "can", "the", "and", "with", "for", "not", "but", "will", "run", "runs", "start", "work", "fitting"}


class ResultsService:
    def __init__(self, engine):
        self.e = engine
        c = pd.read_parquet(settings.canonical_dir / "content.parquet")
        self.docs_by_sku: dict[str, list[dict]] = defaultdict(list)
        self.docs_by_brand_cat: dict[tuple, list[dict]] = defaultdict(list)
        cats = set(engine.catalog.cat3)
        for r in c.itertuples():
            d = {"content_id": r.content_id, "title": r.title, "type": r.content_type, "url": r.url}
            try:
                for s in json.loads(r.related_skus or "[]"):
                    self.docs_by_sku[str(s)].append(d)
            except Exception:
                pass
            if r.content_type in ("spec_sheet", "install_manual"):
                for c3 in cats:
                    if c3 in r.title:
                        self.docs_by_brand_cat[(r.title.split(" ")[0], c3)].append(d)
                        break
        self._list_skus: dict[tuple, set] = {}
        self._heads: dict[int, str] = {}
        self._pheads: dict[int, str] = {}
        self._tok: dict[int, frozenset] = {}
        self._gheads: Counter | None = None
        self._cities: dict | None = None
        # the catalog's product nouns and words (phrase reading, spelling): built in the background at start
        import threading
        threading.Thread(target=self._catalog_words, daemon=True).start()

    # ------------------------------------------------------------------ helpers
    def _list_skus_for(self, p: Principal) -> set:
        if not p.is_authenticated or not p.can("lists"):
            return set()
        key = (p.account_id, p.user_id)
        if key not in self._list_skus:
            L = self.e.accounts.visible_lists(p)
            ids = set(L.index[L["list_type"] == "CUSTOMER"])
            li = self.e.accounts.list_items
            self._list_skus[key] = set(li.loc[li["list_id"].isin(ids), "sku"])
            if len(self._list_skus) > 2000:
                self._list_skus.clear()
        return self._list_skus[key]

    def _purchased(self, p: Principal) -> dict[str, int]:
        if not p.is_authenticated:
            return {}
        if p.role == "tech":
            return {s: int(v["times"]) for s, v in self.e.accounts.user_history(p.user_id).items()}
        ap = self.e.features.acct_prod.get(p.account_id, {})
        live = {}
        cat = self.e.catalog
        for i, (n, _, _) in ap.items():
            live[str(cat.sku[i])] = int(n)
        return live

    def outdoor_brands(self) -> list[str]:
        if not hasattr(self, "_outdoor_brands"):
            cat = self.e.catalog
            self._outdoor_brands = sorted(set(cat.brand[np.isin(cat.family, list(self.e.hvac.OUTDOOR))]))
        return self._outdoor_brands

    def _attr_fn(self):
        """Specs for rule conditions: the catalog attributes plus the connection and material groups the filters use."""
        cat = self.e.catalog

        def f(i: int) -> dict:
            a = cat.attrs(i)
            return {**{k.lower(): v for k, v in a.items()}, "connection": connection_of(a) or "",
                    "material": material_group(str(cat.title[i]), a, str(cat.brand[i])) or a.get("material", "")}
        return f

    def head(self, i: int) -> str:
        h = self._heads.get(i)
        if h is None:
            h = self._heads[i] = title_head(self.e.catalog.title[i])
            if len(self._heads) > 400_000:
                self._heads.clear()
        return h

    def heads(self, i: int) -> tuple:
        """The product nouns a title names: its head, and a parenthesis's ("Coupling (Joint Restraint)")."""
        ph = self._pheads.get(i)
        if ph is None:
            ph = self._pheads[i] = paren_head(str(self.e.catalog.title[i]))
            if len(self._pheads) > 400_000:
                self._pheads.clear()
        h = self.head(i)
        return (h, ph) if ph and ph != h else (h,)

    def global_heads(self) -> Counter:
        """Product nouns across the catalog (every fifth title): is a word the name of a product anywhere?"""
        if self._gheads is None:
            self._catalog_words()
        return self._gheads

    def _catalog_words(self):
        """One pass over every fifth title: the product nouns (heads) and the words (for spelling)."""
        titles = self.e.catalog.title
        c: Counter = Counter()
        raw: Counter = Counter()
        for t in titles[::5]:
            t = str(t)
            c[title_head(t)] += 1
            ph = paren_head(t)
            if ph:
                c[ph] += 1
            raw.update(_TITLE_WORD.findall(t.lower()))
        voc: Counter = Counter()
        for w, k in raw.items():
            if len(w) >= 4:
                voc[w] += k          # as titles spell it ("condenser", "gallon"): what people misspell
            for n in normalize(w):
                if n.isalpha() and len(n) >= 4 and n != w:
                    voc[n] += k
        by_first: dict = defaultdict(list)
        for w, k in voc.items():
            if k >= 5:
                by_first[w[0]].append(w)
        self._vocab_first = by_first
        self._voc = voc
        self._gheads = c

    def spell(self, text: str) -> tuple[str, dict]:
        """Misspelled words fixed before searching ("condinsate pump" -> "condensate pump"): a word of 5+ letters
        the catalog doesn't have, one edit from a catalog word (a swap counts as one) or close in spelling."""
        be = self.e.backend
        fixed: dict = {}

        def fix(m):
            w = m.group(0)
            n = normalize(w)
            if len(n) == 1 and n[0] in phmod.SHORTHAND and not be.term_exists(n[0]):
                fixed[w] = phmod.SHORTHAND[n[0]]
                return fixed[w]
            if len(n) == 1 and len(w) >= 4 and len(n[0]) < 4 and not be.term_exists(n[0]) and w.isalpha() \
                    and not self._is_brand_name(w):
                bc = phmod.fuzzy_pick(w.lower(), self._brand_words())      # "wats" reads as "wat": still Watts
                if bc:
                    fixed[w] = bc
                    return bc
            # a brand-master alias the catalog never writes ("navian", "honywell"): the brand's own name, so the
            # page searches what the catalog says (v2.45)
            if len(n) == 1 and n[0].isalpha() and not be.term_exists(n[0]):
                b_ = self._alias_brand(n[0])
                if b_:
                    fixed[w] = b_
                    return b_
            # 3 letters with a doubled letter left out ("bal" for ball): a common catalog word (v2.45)
            if len(n) == 1 and len(w) == 3 and w.isalpha() and not be.term_exists(n[0]) and not self._is_brand_name(w):
                if self._gheads is None:
                    self._catalog_words()
                dd = w.lower() + w[-1].lower()
                if (getattr(self, "_voc", {}) or {}).get(dd, 0) >= 200:
                    fixed[w] = dd
                    return dd
            if len(n) == 1 and n[0].isalpha() and len(n[0]) >= 4 and not be.term_exists(n[0]) \
                    and n[0] not in _NO_SPELL and not self._is_brand_name(w) \
                    and (w.lower() in self._real_words() or w.lower() in self._english()):
                # a real word the catalog doesn't have, one key off a catalog word that the rest of the search goes
                # with: "steel pile" is steel pipe, "gare valve" gate valve; "air handler" stays (v2.45)
                rw = self._real_word_slip(w.lower(), text)
                if rw:
                    fixed[w] = rw
                    return rw
                return w
            if len(n) != 1 or not n[0].isalpha() or len(n[0]) < 4 or be.term_exists(n[0]) or n[0] in _NO_SPELL \
                    or w.lower() in self._real_words() or w.lower() in self._english() or self._is_brand_name(w):
                return w
            if self._gheads is None:
                self._catalog_words()
            # a misspelled brand ("doormont", "honeywel", "rinnia") before splitting it into words ("door mount") (v2.43)
            if len(w) >= 4:
                bws = self._brand_words()
                kbb = phmod.kb_pick(w.lower(), bws)
                bc = phmod.fuzzy_pick(w.lower(), bws)
                if kbb and (not bc or kbb[1] < phmod.kb_dist(w.lower(), bc)):      # "rinmai" is Rinnai (v2.45)
                    bc = kbb[0]
                # a brand spelled the way it sounds ("lockinvar", "grunfos", "kaleffi", "rijid", v2.45): the sound
                # wins over a spelling neighbor that sounds different ("yello" is Yellow Jacket, not Cello)
                gen_kb = phmod.kb_pick(w.lower(), self._kb_pool(n[0]), freq=lambda c: (getattr(self, "_voc", {}) or {}).get(c, 5))
                sb = phmod.sound_pick(w.lower(), self._sound_bidx(), brands=True)
                if sb and gen_kb and gen_kb[1] <= 0.45 and phmod.kb_dist(w.lower(), sb) > gen_kb[1]:
                    sb = None                     # "mising valve" is mixing, not Myson; "rised clamp" riser, not Resideo
                if sb and bc and phmod.ratio(w.lower(), sb) < phmod.ratio(w.lower(), bc):
                    sb = None
                if not sb and bc and not set(phmod.sounds(bc)) & set(phmod.sounds(w.lower())) \
                        and not (kbb and kbb[0] == bc and kbb[1] <= 0.45):
                    sb = self._sound_word(n[0], w.lower())     # "yello" sounds like yellow (Yellow Jacket), not Cello
                if sb and (not bc or not set(phmod.sounds(bc)) & set(phmod.sounds(w.lower()))) \
                        and not (kbb and kbb[1] <= 0.45 and kbb[0] == bc):
                    bc = sb
                if bc:
                    bc = self._alias_brand(bc) or bc        # "hunnywell" -> honywell, an alias: Honeywell (v2.45)
                    fixed[w] = bc
                    return bc
            if len(n[0]) >= 6:        # two words run together: "ballvalve" -> ball valve, "pextee" -> pex tee
                w0 = n[0]
                bw_ = set(self._brand_words())

                def known(x):        # a catalog word, as the analyzer reads it ("watts" is indexed as "watt"), or a
                    nx = normalize(x)   # brand or product line people type ("kohler", "knight")
                    return len(nx) == 1 and (be.term_exists(nx[0]) or be.term_exists(x) or x in bw_ or nx[0] in bw_
                                             or x in rw_ or x in dw_)
                dw_ = self._dict_words()
                rw_ = self._real_words()
                stop_ = phmod.COMMON | phmod.DROP
                for k in range(3, len(w0) - 2):
                    a_, b_ = w0[:k], w0[k:]
                    # (v2.43) any two catalog words, so "oilfilter", "rinnaiboiler", "bradfordwhite" split too; never
                    # into a stop word ("there", "need")
                    if known(a_) and known(b_) and a_ not in stop_ and b_ not in stop_ and max(len(a_), len(b_)) >= 4:
                        fixed[w] = f"{a_} {b_}"
                        return fixed[w]
                # one word, one slip ("thermostatt"): that comes before reading it as two words with a slip
                c1 = self._pick(n[0], w.lower())
                if c1:
                    fixed[w] = c1
                    return c1
                # run together and one half misspelled (v2.43): "balvalve" -> ball valve, "ballvalv" -> ball valve
                best_ = None
                for k in range(3, len(w0) - 2):
                    a_, b_ = w0[:k], w0[k:]
                    if a_ in stop_ or b_ in stop_:
                        continue
                    for x_, y_, left in ((a_, b_, True), (b_, a_, False)):
                        # the half spelled right is a real word of 4+ letters ("valve", "ball"), never a prefix
                        # like "pre" ("preventor" isn't "pre vector")
                        if not known(x_) or len(x_) < 4:
                            continue
                        y_fix = self._spell_half(y_)
                        if y_fix:
                            cand_ = f"{x_} {y_fix}" if left else f"{y_fix} {x_}"
                            score_ = self._gheads.get(y_fix, 0) + self._gheads.get(x_, 0)
                            if best_ is None or score_ > best_[0]:
                                best_ = (score_, cand_)
                if best_:
                    fixed[w] = best_[1]
                    return fixed[w]
                # last: a product noun at the end of a word nothing else explains ("boosterpump" -> booster pump); the
                # first part, if the catalog doesn't know it, is then dropped and said so (v2.43)
                for k in range(3, len(w0) - 3):
                    a_, b_ = w0[:k], w0[k:]
                    if len(b_) >= 4 and self._gheads.get(b_, 0) >= 5 and a_.isalpha() and a_ not in stop_:
                        fixed[w] = f"{a_} {b_}"
                        return fixed[w]
            # the word as typed too: the plural rule makes "tankles" "tankle", two letters short of "tankless"
            c = self._pick(n[0], w.lower())
            if c:
                fixed[w] = c
                return c
            return w
        text = self._rejoin(self._unglue(text, fixed), fixed)
        out = re.sub(r"(?<![A-Za-z0-9\-])[A-Za-z]{3,}(?![A-Za-z0-9\-])", fix, text)      # whole words, never inside a part #
        return out, fixed

    def _dict_words(self) -> set:
        """Words of the search dictionary's live terms ("scoop", "monster", "hepex"): the speller fixes slips in them
        too, and the dictionary then reads the fixed word (v2.44). Rebuilt when the dictionary changes."""
        dic = getattr(self.e, "vocab", None)
        ver = getattr(dic, "version", None)
        if getattr(self, "_dw_ver", object()) != ver:
            ws = set()
            for e_ in (dic.entries() if dic is not None else []):
                if e_.get("status") == "live" and e_.get("kind") in ("synonym", "spelling", "brand"):
                    ws.update(w for w in re.findall(r"[a-z]+", str(e_.get("term", "")).lower()) if len(w) >= 4)
            self._dw, self._dw_ver = ws, ver
            merged = {k: list(v) for k, v in self._vocab_first.items()}
            for w in sorted(ws):
                lst = merged.setdefault(w[0], [])
                if w not in set(lst):
                    lst.append(w)
            self._vf_merged = merged
        return self._dw

    def _vfirst(self, ch: str) -> list:
        """Spelling candidates starting with this letter: catalog words, then dictionary words."""
        self._dict_words()
        return self._vf_merged.get(ch, [])

    def _real_word_slip(self, w: str, text: str) -> str | None:
        """One key off a catalog word, for a real English word the catalog lacks: only when the search has other words
        and 20+ catalog titles carry the fixed word with all of them."""
        others = [t for t in normalize(re.sub(rf"\b{re.escape(w)}\b", " ", text, flags=re.I))
                  if t.isalpha() and len(t) >= 3 and self.e.backend.term_exists(t)]
        if not others:
            return None
        if self._gheads is None:
            self._catalog_words()
        pool = list(self._vfirst(w[0]))
        for ch in phmod.NEIGH.get(w[0], ""):
            pool += self._vfirst(ch)
        voc = getattr(self, "_voc", {}) or {}
        best = None
        sk = set(phmod.sounds(w))
        snd = {c for k in sk for c in self._sound_idx().get(k, ())} if len(min(sk, key=len)) >= 2 else set()
        snd |= {c for k in sk for c in self._sound_bidx().get(k, ())}
        snd = set(sorted(snd, key=lambda c: -voc.get(c, 5))[:8])         # each is a lexical check: the common few
        for c in list(dict.fromkeys(pool + sorted(snd))):
            if c == w or c in self._english() and voc.get(c, 0) < 50:
                continue
            if c in snd and abs(len(c) - len(w)) <= 3 and (self._gheads.get(c, 0) >= 5 or c in self._brand_words()):
                d = 0.45                  # sounds the same, and names a product or a brand ("moan faucet",
                                          # "condensing unite", v2.45); "pool heater" isn't "poly", "tub spout" isn't spot
            else:
                if abs(len(c) - len(w)) > 1:
                    continue
                d = phmod.kb_dist(w, c, 0.45)
                if d > 0.45:
                    continue
            cn = list(normalize(c)) or [c]
            ix, _ = self.e.backend.lexical(others + cn, 200, brand_tokens=set())
            need = set(others) | set(cn)
            n_ = sum(1 for i in ix if need <= set(normalize(str(self.e.catalog.title[int(i)]))))
            if n_ >= 20 and (best is None or (d, -n_) < best[0]):
                best = ((d, -n_), c)
        return best[1] if best else None

    def _pick(self, n0: str, raw: str) -> str | None:
        """The word a misspelled one meant. Two readings: close in spelling (fuzzy_pick, same first letter), and
        fat-fingered (kb_pick: keys next to the right ones cost less, and the first letter may be a neighbor too,
        v2.45). The cheaper slip wins; a tie goes to the spelling reading."""
        cand = self._vfirst(n0[0])
        old = phmod.fuzzy_pick(n0, cand) or (phmod.fuzzy_pick(raw, cand) if raw != n0 else None)
        pool = list(cand)
        for ch in phmod.NEIGH.get(n0[0], ""):
            pool += self._vfirst(ch)
        voc = getattr(self, "_voc", {}) or {}
        kb = None
        for t in dict.fromkeys((raw, n0)):
            kb = phmod.kb_pick(t, pool, freq=lambda c: voc.get(c, 5))
            if kb:
                break
        snd = self._sound_word(n0, raw)
        if kb is None:
            pick = old
        elif old is None or old == kb[0]:
            pick = kb[0]
        else:
            pick = kb[0] if kb[1] < min(phmod.kb_dist(raw, old), phmod.kb_dist(n0, old)) else old
        # spelled the way it sounds ("fernace", "expanshun", "sement", v2.45): when the spelling readings found nothing,
        # or found a word that sounds different and isn't a single fat-fingered key
        if snd and snd != pick:
            if pick is None:
                return snd
            single = kb is not None and kb[0] == pick and kb[1] <= 0.45
            if not single and not set(phmod.sounds(pick)) & set(phmod.sounds(raw)) \
                    and phmod.ratio(raw, snd) >= phmod.ratio(raw, pick):      # "mostor" stays motor, not moisture
                return snd
        return pick

    def _alias_brand(self, x: str) -> str | None:
        """A brand-master alias the catalog never writes ("navian", "honywell") -> the brand's name as the catalog
        writes it, when that's one brand without a context rule; else None."""
        be = self.e.backend
        bm_ = getattr(self.e, "brands", None)
        n = normalize(x)
        if bm_ is None or len(n) != 1 or be.term_exists(n[0]):
            return None
        ents = bm_.alias.get((n[0],)) or []
        if len(ents) != 1 or ents[0].context is not None or not ents[0].carried or ents[0].choose:
            return None
        name_ok = all(be.term_exists(t) for t in normalize(ents[0].name))
        if not name_ok and len(ents[0].covers) != 1:
            return None
        b_ = ents[0].name if name_ok else next(iter(ents[0].covers))
        return b_ if tuple(normalize(b_)) != tuple(n) else None

    def _kb_pool(self, n0: str) -> list:
        pool = list(self._vfirst(n0[0]))
        for ch in phmod.NEIGH.get(n0[0], ""):
            pool += self._vfirst(ch)
        return pool

    def _sound_word(self, n0: str, raw: str) -> str | None:
        voc = getattr(self, "_voc", {}) or {}
        return phmod.sound_pick(raw, self._sound_idx(), freq=lambda c: voc.get(c, 5)) or (
            phmod.sound_pick(n0, self._sound_idx(), freq=lambda c: voc.get(c, 5)) if n0 != raw else None)

    def _sound_idx(self) -> dict:
        """Sound key -> catalog and dictionary words (rebuilt with the dictionary)."""
        self._dict_words()
        if getattr(self, "_sidx_ver", object()) != getattr(self, "_dw_ver", None) or not hasattr(self, "_sidx"):
            voc = getattr(self, "_voc", {}) or {}
            dw = self._dw
            # words that are words: a vowel, and common in titles or a dictionary term ("brzn", "cdac" are codes)
            self._sidx = phmod.sound_index(w for ws in self._vf_merged.values() for w in ws
                                           if re.search(r"[aeiouy]", w) and (voc.get(w, 0) >= 30 or w in dw))
            self._sidx_ver = getattr(self, "_dw_ver", None)
        return self._sidx

    def _sound_bidx(self) -> dict:
        if not hasattr(self, "_sbidx"):
            self._sbidx = phmod.sound_index(self._brand_words())
        return self._sbidx

    def _spell_half(self, h: str) -> str | None:
        """One half of a run-together word, fixed: a catalog word, or one slip from one ("bal" -> "ball" with its
        doubled letter, "valv" -> "valve")."""
        be = self.e.backend
        if be.term_exists(h):
            return h
        cand = self._vfirst(h[0])
        if len(h) >= 4:       # cut off by one letter ("valv" for "valve"): the most common such word
            cut = [c for c in cand if len(c) == len(h) + 1 and c.startswith(h)]
            if cut:
                return max(cut, key=lambda c: self._gheads.get(c, 0))
        if len(h) == 3:       # three letters: only a doubled letter left out ("bal" for "ball", "tee" stays "tee")
            for c in cand:
                if len(c) == 4 and c[:3] == h and c[3] == c[2]:
                    return c
            return None
        return phmod.fuzzy_pick(h, cand)

    def _unglue(self, text: str, fixed: dict) -> str:
        """Words typed together with a number (v2.43): a brand and its model ("watts911" -> watts 911, "taco007"),
        a size and a word ("3/4ballvalve" -> 3/4 ballvalve). A token that is a catalog part number stays as typed."""
        bm = getattr(self.e, "brands", None)
        out = []
        for tok in text.split(" "):
            m = re.fullmatch(r"([A-Za-z][A-Za-z&'.]{3,})(\d[\w\-]*)", tok)
            idm = getattr(self.e.retriever, "idmatch", None)
            if m and bm is not None and not self.e.catalog.id_lookup(compact_id(tok)) \
                    and not (idm is not None and idm.suggest(tok, 1)):      # one slip from a real part number: keep it
                hits = bm.detect(m.group(1))
                if hits and hits[0].span == (0, len(normalize(m.group(1)))):
                    new = f"{m.group(1)} {m.group(2)}"
                    fixed[tok] = new
                    out.append(new)
                    continue
            m = re.fullmatch(r"(\d+(?:-\d+/\d+|/\d+)?)(\"|in\.?|inch)?([A-Za-z]{3,})", tok)
            # a size and a word, not a model number ("9999XYZ"): a fraction, an inch mark, or a word the catalog knows
            if m and not NUM_UNIT.match(tok) and not self.e.catalog.id_lookup(compact_id(tok)) and (
                    "/" in m.group(1) or m.group(2) or self.e.backend.term_exists(m.group(3).lower())
                    or m.group(3).lower() in self._real_words()):
                new = f"{m.group(1)}{m.group(2) or ''} {m.group(3)}"
                fixed[tok] = new
                out.append(new)
                continue
            out.append(tok)
        return " ".join(out)

    def _rejoin(self, text: str, fixed: dict) -> str:
        """A word typed in two pieces ("thermo stat", "flush ometer"): joined when the whole is a catalog word and
        a piece isn't (v2.43)."""
        be = self.e.backend
        ws = text.split(" ")
        out, i = [], 0
        while i < len(ws):
            a = ws[i]
            if i + 1 < len(ws) and a.isalpha() and ws[i + 1].isalpha() and len(a) >= 2 and len(ws[i + 1]) >= 2:
                j = (a + ws[i + 1]).lower()
                # a word titles write with a hyphen ("lead free" = Lead-Free) already reads right: left alone
                if be.term_exists(j) and (not be.term_exists(a.lower()) or not be.term_exists(ws[i + 1].lower())) \
                        and len(j) >= 6 and normalize(f"{a}-{ws[i + 1]}") != (j,):
                    fixed[f"{a} {ws[i + 1]}"] = j
                    out.append(j)
                    i += 2
                    continue
            out.append(a)
            i += 1
        return " ".join(out)

    def _real_words(self) -> frozenset:
        """Words customers type that are real words even when the demo catalog lacks them (config/dictionary/
        real_words.txt, from the GA4 search log): never respelled into another word (v2.43)."""
        if not hasattr(self, "_rwords"):
            from pathlib import Path as _P
            f = _P(__file__).resolve().parents[2] / "config" / "dictionary" / "real_words.txt"
            self._rwords = frozenset(ln.strip().lower() for ln in (f.read_text().splitlines() if f.exists() else [])
                                     if ln.strip() and not ln.startswith("#"))
        return self._rwords

    def _english(self) -> frozenset:
        """Common English words (config/dictionary/english_common.txt, v2.45): never respelled, never split."""
        if not hasattr(self, "_ewords"):
            from pathlib import Path as _P
            f = _P(__file__).resolve().parents[2] / "config" / "dictionary" / "english_common.txt"
            self._ewords = frozenset(ln.strip() for ln in (f.read_text().splitlines() if f.exists() else [])
                                     if ln.strip() and not ln.startswith("#"))
        return self._ewords

    def _is_brand_name(self, w: str) -> bool:
        """A brand the brand master knows, carried or not ("trane" is Trane, never "crane")."""
        bm = getattr(self.e, "brands", None)
        return bm is not None and bool(bm.alias.get(tuple(normalize(w))))

    def _brand_words(self) -> list:
        """Catalog brands that are one word ("dormont", "rinnai", "honeywell"), as the analyzer reads them."""
        if not hasattr(self, "_bwords"):
            ws = {normalize(b)[0] for b in self._brands() if len(normalize(b)) == 1}
            ws |= {b.lower() for b in self._brands() if b.isalpha()}        # as written: "watts", not "watt"
            bm = getattr(self.e, "brands", None)
            if bm is not None:      # the brand master's names and the product lines people type (v2.43)
                ws |= {k[0] for k in bm.alias if len(k) == 1}
                ws |= {k[0] for k in bm.lines if len(k) == 1}
            self._bwords = sorted(w for w in ws if len(w) >= 4 and w.isalpha())
        return self._bwords

    def _brands(self) -> set:
        if not hasattr(self, "_brand_set"):
            self._brand_set = set(map(str, set(self.e.catalog.brand)))
        return self._brand_set

    def cities(self) -> dict:
        """Branch city (lower-cased) -> branch id, for "at Bedford" in a search."""
        if self._cities is None:
            av = self.e.availability
            self._cities = {str(c).lower(): str(av.ids[k]) for k, c in enumerate(av.city) if av.counter[k] and str(c)}
            # "in Portland" is the South Portland branch when no branch is in Portland itself (v2.0)
            for name, bid in list(self._cities.items()):
                m = re.match(r"(?:south|north|east|west)\s+(.+)$", name)
                if m and m.group(1) not in self._cities:
                    self._cities[m.group(1)] = bid
        return self._cities

    def _toks(self, i: int) -> frozenset:
        """Title, attributes, brand, category and the index-time trade synonyms (ProPress for press, flex for gas
        connectors): what a product can be said to 'carry' when deciding whether the catalog has what was asked for."""
        t = self._tok.get(i)
        if t is None:
            cat = self.e.catalog
            base = self.e.fb.doc_tokens(i)[1]
            syn = frozenset(normalize(expand_concepts((cat.title[i] + " " + cat.cat3[i]).lower())))
            t = self._tok[i] = base | syn
            if len(self._tok) > 300_000:
                self._tok.clear()
        return t

    def _covers(self, i: int, words: list[str]) -> bool:
        all_t = self._toks(i)
        for w in words:
            if w in all_t or COVER_SYN.get(w, set()) & all_t:
                continue
            if not any((len(w) >= 4 and t.startswith(w)) or (len(t) >= 4 and w.startswith(t)) for t in all_t):
                return False
        return True

    def _is_head(self, i: int, qh: set) -> bool:
        if any(head_match(h, qh) for h in self.heads(i)):
            return True
        # a kit or set of the thing asked for is that thing: a "press tool kit" is a press tool (v2.43)
        if self.head(i) in ("kit", "set"):
            tw = list(normalize(str(self.e.catalog.title[i])))
            if any(tw[k + 1] in ("kit", "set") and head_match(tw[k], qh) for k in range(len(tw) - 1)):
                return True
        # the same fitting under another name: a reducing coupling is a reducer
        return any(h in qh and any(self._names(i, w) for w in ws) and any(x in _ALSO_HEAD[h][0] for x in self.heads(i))
                   for h, (_, ws) in ((h, _ALSO_HEAD[h]) for h in qh if h in _ALSO_HEAD))

    def _ident(self, idx, qh: set, mods=()) -> np.ndarray:
        """Items that are the product asked for: their head noun is the query's, and they carry one of the words
        before it (`mods`, the phrase's kind words): a condensate pump, not any pump; a weld neck flange."""
        mods = [m for m in mods if m]
        return np.array([self._is_head(int(i), qh)
                         and (not mods or any(self._names(int(i), m) for m in mods)) for i in idx], bool)

    def _names(self, i: int, w: str) -> bool:
        """Does the product's own name (title without the brand, and type; not its category: every valve in
        "Gate, Globe & Check Valves" isn't a gate valve) carry this word?"""
        cat = self.e.catalog
        t, b = str(cat.title[i]), str(cat.brand[i])
        if t.startswith(b):
            t = t[len(b):]
        toks = set(normalize(f"{t} {cat.attrs(i).get('type') or ''}"))
        return w in toks or any((len(w) >= 4 and t_.startswith(w)) or (len(t_) >= 4 and w.startswith(t_)) for t_ in toks) \
            or bool(COVER_SYN.get(w, set()) & toks)

    def _names_part(self, text: str) -> bool:
        """Does a qualifier name a brand or a model ("for Moen 1225", "fits Taco 007")? Then it is part of what
        to search: it says which part."""
        return bool(self.e.parser.detect_brands(normalize(text)) or model_tokens(text))

    def _conflict(self, ph, core_toks: list[str]) -> dict | None:
        """Words that can't all hold: two fuels ("electric natural gas water heater"); a size for the product and
        another for its ends ("2 inch valve with 1 inch inlet and 1 inch outlet")."""
        what = ph.head or "product"
        fuels = reqmod.conflicts(core_toks)
        if fuels and re.search(r"(?:gas|ng|lp|propane|electric)\s*(?:/|or|&)\s*(?:gas|ng|lp|propane|electric|natural)"
                               r"|convertible|dual[\s-]*fuel|either", ph.core, re.I):
            fuels = []           # "Natural Gas/Propane Convertible": one product that runs on both
        if fuels:
            return {"terms": fuels, "text": f"Your search names {' and '.join(f.lower() for f in fuels)}; a {what} runs on "
                    "one of them. Both are shown below; add the one you need to your search."}
        size = [t for t in ph.core_toks if t[:1].isdigit() and SIZE_ORDER.count(t)]
        for lead, tl in ph.tails:
            tt = normalize(tl)
            ends = [w for w in tt if w in ("inlet", "outlet", "end", "connection", "thread", "port", "ends")]
            ts = [t for t in tt if t[:1].isdigit() and SIZE_ORDER.count(t)]
            if size and ends and ts and not set(ts) & set(size):
                a, b = size[0], ts[0]
                parts = " and ".join(dict.fromkeys("end" if w == "ends" else w for w in ends))
                return {"terms": [f"{a} in.", f"{b} in."],
                        "text": f"Your search gives the {what} as {a} in. and its {parts} as {b} in. A {what} is sized by "
                                f"its ends, so these can't both hold. These are {a} in. {what}s; for {b} in. ends, search "
                                f"a {b} in. {what}."}
        return None

    def _real_kinds(self, wi: dict) -> list[str]:
        """The product nouns of the exact real F.W. Webb items ("Circulator pump ECM cast iron ..." -> circulator, pump)."""
        gh = self.global_heads()
        return list(dict.fromkeys(w for it in wi["items"] for w in normalize(it.get("kit_desc") or it.get("title") or "")
                                  if w.isalpha() and len(w) >= 3 and gh.get(w, 0) >= 3))

    def _kind_items(self, kinds: list[str], brands: list[str], idx) -> list[int]:
        """Demo products whose head noun is one of these, the searched brand's first."""
        have = set(idx.tolist())
        cat = self.e.catalog
        got = []
        for k in kinds[:2]:
            try:
                ix, _ = self.e.backend.lexical([k], 400, brand_tokens=set())
            except Exception:
                continue
            got += [int(i) for i in ix if int(i) not in have and k in self.heads(int(i))]
        got = list(dict.fromkeys(got))
        if brands:
            got.sort(key=lambda i: str(cat.brand[i]) not in brands)
        return got[:120]

    def _rescue(self, ph, idx) -> list[int]:
        """Products of the kind asked for when the retrieved pool has none: the head noun with one of its kind words
        (and the typed size, first). Empty when the pool has them, or the phrase has a word the catalog lacks."""
        if not ph or not ph.head or ph.unknown:
            return []
        hq = _phrase_heads(ph)
        kinds = [m for m in ph.mods if m not in phmod.DESCRIPTORS]

        def fits(i):
            return any(head_match(h, hq) for h in self.heads(i)) and (not kinds or any(self._names(i, m) for m in kinds))
        if any(fits(int(i)) for i in idx[:300]):
            return []
        sizes = [t for t in ph.core_toks if t[:1].isdigit()]
        try:
            ix, _ = self.e.backend.lexical(kinds + [ph.head] + sizes, 300, brand_tokens=set())
        except Exception:
            return []
        have = set(idx.tolist())
        got = []
        for i in ix:
            if int(i) not in have and fits(int(i)):
                got.append(int(i))
                if len(got) >= 120:
                    break
        cap_ = 60
        bare = all(t in set(normalize(ph.head)) | set(kinds) or self._is_brand_name(t)
                   for t in ph.core_toks if t.isalpha())
        if not got and bare:  # the word's in thousands of other titles ("toilet" carriers): search the families it heads (v2.42)
            fams = [f for f, _ in self._head_families().get(ph.head, Counter()).most_common(4)]
            if fams:
                try:
                    ix, _ = self.e.backend.lexical(kinds + [ph.head] + sizes, 400, brand_tokens=set(), filters={"family": fams})
                    got = [int(i) for i in ix if int(i) not in have and fits(int(i))][:300]
                    cap_ = 300
                except Exception:  # noqa: BLE001
                    pass
        if sizes:           # stable: the typed size first
            cat = self.e.catalog
            got = sorted(got, key=lambda i: not set(sizes) <= set(normalize(str(cat.title[i]))))
        return got[:cap_]

    def _head_families(self) -> dict:
        """Which families each product noun heads (every fifth title): "toilet" -> the toilets family."""
        if getattr(self, "_hfam", None) is None:
            cat = self.e.catalog
            m: dict = defaultdict(Counter)
            for i in range(0, len(cat.title), 5):
                m[title_head(str(cat.title[i]))][str(cat.family[i])] += 1
            self._hfam = m
        return self._hfam

    def _fitting_cats(self) -> list[tuple[str, set]]:
        """The catalog's fitting categories and their words ("PEX Fittings", "Copper Press Fittings")."""
        if not hasattr(self, "_fcats"):
            self._fcats = [(c, set(normalize(c)) - {"fitting", "fittings", "and"})
                           for c in sorted(set(map(str, self.e.catalog.cat3))) if "fitting" in c.lower()]
        return self._fcats

    def _category_intent(self, ph, core_toks: list[str], brands: frozenset = frozenset()) -> list[str]:
        """A search for a kind of fitting ("pex fittings", "1/2 copper fittings no brass") is that category: PEX tees
        and elbows don't say "fitting" in their titles, gas-line fittings do (v2.0). A brand's fittings ("nibco
        fittings") are the fitting categories that brand makes (v2.42)."""
        if not ph or ph.head not in ("fitting", "fittings"):
            return []
        toks = {t for t in core_toks if t.isalpha()} - {"fitting", "fittings"}
        picked = [c for c, ws in self._fitting_cats() if ws & toks]
        if not picked and brands:
            cat = self.e.catalog
            have = set(map(str, cat.cat3[np.isin(cat.brand, list(brands))]))
            picked = [c for c, _ in self._fitting_cats() if c in have]
        return picked

    def _cat_items(self, cats: list[str], toks: list[str], n: int = 400) -> list[int]:
        """Products of these categories for the pool: the ones the search words find first, then the rest."""
        cat = self.e.catalog
        want = set(cats)
        out: list[int] = []
        try:
            ix, _ = self.e.backend.lexical([t for t in toks if t not in ("fitting", "fittings")] or toks, 1500,
                                           brand_tokens=set())
            out = [int(i) for i in ix if str(cat.cat3[int(i)]) in want][:n]
        except Exception:
            pass
        if len(out) < 60:
            have = set(out)
            out += [i for i in np.flatnonzero(np.isin(cat.cat3, list(want)))[:n].tolist() if i not in have]
        return out[:n]

    def _catalog_has(self, head: str, mods: list[str]) -> bool:
        """Does the catalog anywhere have this product (the head noun with one of its kind words)? A lexical probe
        for when the ranked pool doesn't: "float switch" and "curb box" are in no title."""
        be = self.e.backend
        for m in mods:
            try:
                ix, _ = be.lexical([m, head], 300, brand_tokens=set())
            except Exception:
                return True
            if any(any(head_match(h, {head}) for h in self.heads(int(i))) and self._names(int(i), m) for i in ix[:300]):
                return True
        return False

    def _identity(self, q, pq, qh, ph, brand_toks, idx, rel, fallback, real_exact, kits=None, cats=None):
        """Page membership by what the product is (see the call site). `q` is the searched text, `ph` its product
        phrase (phrase.analyze), `idx` the ranked pool, `rel` the items that cleared the relevance floor. Returns
        the new membership and what was decided."""
        e = self.e
        fam = e.catalog.family[idx]
        words = list(ph.words) if ph else []
        mods = list(ph.mods) if ph else []
        typed = {v: k for k, v in (ph.corrected if ph else {}).items()}      # catalog word -> as typed
        raw = lambda ws: raw_phrase(q, [typed.get(w, w) for w in ws])     # noqa: E731
        hi = words.index(ph.head) if ph and ph.head in words else len(words) - 1
        top = [int(i) for i in idx[:200]]
        zero = np.zeros_like(rel)
        # 1. a brand the demo catalog doesn't carry ("2 inch fernco ..."): the cross-reference knows it, the catalog
        # doesn't
        if pq.brands and not real_exact:
            mine = self._brands()
            gone = [b for b in pq.brands if b not in mine]
            if gone and len(gone) == len(pq.brands):
                return zero, {"kind": "not_carried", "word": gone[0], "words": gone, "brand": True,
                              "phrase": raw(words[-3:]) or gone[0]}
        # 2. a product word nothing in the catalog carries: "purge valve", "hose bibb", "magnetic dirt separator"
        if ph and ph.unknown and not real_exact:
            first = words.index(ph.unknown[0])
            return zero, {"kind": "not_carried", "word": raw(ph.unknown[:1]), "words": [raw([u]) for u in ph.unknown],
                          "phrase": raw(words[max(0, first - 1):hi + 1][-6:])}
        # 3. a product noun the catalog has, but never of this kind: "float switch", "curb box"
        kit_names = {w for k in (kits or []) for w in normalize(k.get("name", ""))}
        two = any(self.global_heads().get(m, 0) >= 3 for m in mods[1:])      # "ice maker connector 72 in. supply stop"
        if ph and ph.head in PROBE_HEADS and mods and not real_exact and not pq.brands and not two \
                and not (ph.head == "kit" and set(mods) & kit_names):     # a job kit answers "toilet install kit"
            hq = _phrase_heads(ph)
            kinds = [m for m in mods if m not in phmod.DESCRIPTORS and m not in COVER_SKIP]
            is_noun = self.global_heads().get(ph.head, 0) >= 3 or any(head_match(h, hq) for i in top for h in self.heads(i))
            if kinds and is_noun and not any(any(head_match(h, hq) for h in self.heads(i)) and
                                             any(self._names(i, m) for m in kinds) for i in top) \
                    and not self._catalog_has(ph.head, kinds):
                return zero, {"kind": "not_carried", "word": raw([kinds[0], ph.head]), "words": [raw([kinds[0], ph.head])],
                              "phrase": raw(words[max(0, hi - 2):hi + 1]), "kind_miss": True}
        # 3b. pipe of a material the catalog doesn't carry ("2 inch PVC pipe sch 40"): say so, not cast-iron pipe (v2.0)
        if ph and ph.head in ("pipe", "tube", "tubing") and not real_exact and not pq.brands:
            mats = [w for w in ph.core_toks if w in reqmod.MAT_WORDS and w not in ("black", "cast", "carbon")]
            heads = {"pipe", "tube", "tubing"}

            def is_pipe(i):
                return any(h in heads for h in self.heads(i))
            if mats and not any(is_pipe(i) and self._covers(i, mats) for i in top) \
                    and not any(self._catalog_has(h, mats) for h in ("pipe", "tube")):
                return zero, {"kind": "not_carried", "word": raw(mats[:1] + [ph.head]), "words": [raw(mats[:1] + [ph.head])],
                              "phrase": raw(mats[:1] + [ph.head]), "kind_miss": True}
        # 4. standards and connection codes in the product phrase ("8 x 6 ductile iron MJ reducer"): only the families
        # that can hold them. A code in a qualifier ("joint restraint for C900 PVC pipe") is noted, not a filter
        info: dict = {}
        core_len = len(ph.core) if ph else len(q)
        for t in trade_terms.find(q):
            if t["pos"] >= core_len:
                info.setdefault("tail_terms", []).append(t)
                continue
            fams = set(t["families"])
            tok = compact_id(t["text"]).lower()
            if any(fam[k] in fams and self._covers(int(idx[k]), [tok]) for k in range(min(len(idx), 200))):
                continue                     # the catalog has it: ranking handles it
            dm = np.isin(fam, list(fams)) if fams else np.zeros(len(idx), bool)
            ident = self._ident(idx, qh, mods) if qh else None
            if dm.any() and ident is not None:
                dm &= ident
            if not dm.any() and ident is not None and ident.any() and t.get("kind") == "pipe" and \
                    ph and ph.head not in ("pipe", "tube", "tubing"):
                dm = ident.copy()            # a coupling or adapter *for* SDR 35 pipe: the right kind, says so
            if dm.any() and t.get("material"):   # SDR 35 is PVC: PVC fittings before ABS ones
                mm = dm & np.array([material_group(str(e.catalog.title[i]), e.catalog.attrs(int(i)), str(e.catalog.brand[i]))
                                    == t["material"] for i in idx], bool)
                dm = mm if mm.any() else dm
            if not dm.any():
                return zero, {"kind": "not_carried", "term": t, "phrase": raw(words[-3:])}
            nums = [w for w in (ph.core_toks + ph.spec_toks if ph else pq.tokens) if w[:1].isdigit()
                    and not trade_terms.TOKEN_RE.match(w)]
            if not (rel & dm).any() and nums:
                sz = dm & np.array([set(nums) <= self._toks(int(i)) for i in idx], bool)
                dm = sz if sz.any() else dm       # the typed size, if any product of that kind has it
            rel = (rel & dm) if (rel & dm).any() else (dm & (np.cumsum(dm) <= 12))
            info.setdefault("terms", []).append(t)
            info["chosen"] = True
        # 5. nothing that clears the floor is the product asked for, but the catalog has it: keep the product and drop
        # the look-alikes that only match the numbers ("condensate pump 115v": no pump lists 115V; mini-splits do)
        if cats:                 # a kind of fitting ("PEX crimp fittings"): its category, not anything titled "fitting"
            cm = np.isin(e.catalog.cat3[idx], cats)
            if cm.any():
                rel = (rel & cm) if (rel & cm).any() else (cm & (np.cumsum(cm) <= 60))
                info["chosen"] = True
        if not info.get("chosen") and qh and not pq.brands and words:
            im = self._ident(idx, qh, mods)              # the head noun and one of its kind words
            if im.any() and (rel & im).sum() < min(3, int(im.sum())):
                nums = [w for w in (ph.core_toks if ph else []) if w[:1].isdigit()]
                if nums:              # the typed size first ("1 inch CSST": 1 in. CSST, not 1/2 in.)
                    sz = im & np.array([set(nums) <= self._toks(int(i)) for i in idx], bool)
                    im = sz if sz.any() else im
                rel = (rel & im) | (im & (np.cumsum(im) <= 12))
                info.update(kind="spec_miss", chosen=True, phrase=raw(words[max(0, hi - 2):hi + 1]))
        return rel, info

    def _apply_requirements(self, reqs, idx, relax: set, stock: dict | None = None,
                            unstated: list | None = None) -> np.ndarray:
        """Mark each requirement applied / unmet / relaxed / n/a and return the page membership that meets the
        applied ones. Sequential: a requirement that would empty the page together with the others is unmet.
        `stock`: {"at": on hand at the pickup branch, "best": most at any one branch nearby} per item, for
        requirements a search states about buying ("in stock", "8 at one branch")."""
        items = self._req_items(idx, stock)
        keep = np.ones(len(items), bool)
        unst = np.zeros(len(items), bool)
        for r in reqs:
            # a product that doesn't state the property is kept (and counted), never assumed to fail or pass
            fam_ok = np.array([r.families is None or it["family"] in r.families for it in items], bool)
            known = np.array([r.known is None or bool(r.known(it)) for it in items], bool)
            appl = fam_ok & known
            ok = np.array([bool(r.test(it)) for it in items], bool)
            r.count = int((ok & appl).sum())
            r.unknown = int((fam_ok & ~known).sum())
            if not appl.any():
                # the products this could apply to don't state it at all (forged tees don't list their ends): kept,
                # and the page says so instead of hiding the requirement
                r.status = "unstated" if fam_ok.any() and r.unknown else "n/a"
            elif r.key in relax:
                r.status = "relaxed"
            else:
                nk = keep & (ok | ~appl)
                if not (nk & appl).any():
                    r.status = "unmet"
                    if r.strict:          # v2.4 (G01): never relaxed to fill the page
                        keep = np.zeros(len(items), bool)
                else:
                    keep, r.status = nk, "applied"
                    # products kept only because they don't state it rank after the ones that do (v2.4, G02)
                    unst |= fam_ok & ~known
        if unstated is not None:
            unstated.append(unst)
        return keep

    def _req_items(self, idx, stock: dict | None = None) -> list[dict]:
        """What a requirement test reads per product: its connection (stated, or fixed by its family: black malleable
        is threaded) and its title without the brand, lower-cased; material group; family; words."""
        cat = self.e.catalog
        items = []
        for k, i in enumerate(np.asarray(idx).tolist()):
            t, b, a = str(cat.title[i]), str(cat.brand[i]), cat.attrs(i)
            own = t[len(b):] if t.startswith(b) else t
            conn = connection_of(a) or reqmod.FAMILY_CONN.get(str(cat.family[i]))
            items.append({"text": (str(conn or "") + " " + own).lower(), "family": cat.family[i],
                          "material": material_group(t, a, b), "conn": conn, "toks": set(normalize(own)),
                          "brand": b, "series": a.get("series"),
                          **({"at": int(stock["at"][k]), "best": int(stock["best"][k]),
                              "sum": int(stock["sum"][k]) if "sum" in stock else int(stock["best"][k])} if stock else {})})
        return items

    def _qualify(self, idx, reqs, sizes: list[str], exact: np.ndarray) -> np.ndarray:
        """v2.4 (review G11): the last check before the page is built, after every adjustment that can move or add a
        product (pins, purchase history, affinity, availability, crosses, learned choices). Every product on the page
        meets every applied requirement and carries every size typed; an exact part number is the answer and passes.
        Returns the products that fail (normally none: this is the guarantee, not the filter)."""
        if not len(idx):
            return np.zeros(0, bool)
        applied = [r for r in reqs if r.status == "applied" and r.kind not in ("type", "stock")]
        items = self._req_items(idx) if applied else None
        bad = np.zeros(len(idx), bool)
        for r in applied:
            for k, it in enumerate(items):
                if bad[k] or exact[k]:
                    continue
                if (r.families is None or it["family"] in r.families) and (r.known is None or r.known(it)) \
                        and not r.test(it):
                    bad[k] = True
        if sizes:
            cat = self.e.catalog
            for k, i in enumerate(np.asarray(idx).tolist()):
                if bad[k] or exact[k]:
                    continue
                a = cat.attrs(int(i))
                have = {str(a.get("size") or "")} | set(re.findall(r"\d+-\d+/\d+|\d+/\d+|\d+(?:\.\d+)?",
                                                                  str(cat.title[i]) + " " + str(a.get("size") or "")))
                if not all(sz in have for sz in sizes):
                    bad[k] = True
        return bad

    def _notices(self, out, q, pq, idx, X, n, any_exact, fallback, qh, sees, advisor, ident=None, reqs=(), ph=None):
        """Say plainly when the page is not what was asked for: a model we don't have, a product phrase
        nothing in the catalog carries, a brand only the real Webb item list knows, or specs nothing meets."""
        e = self.e
        ident = ident or {}
        if advisor:
            if n == 0:
                out["notice"] = {"kind": "advisor_none", "text": "No outdoor unit meets every constraint. The answer "
                                 "above shows where the list went to zero and what to relax."}
            return
        strict = [r for r in reqs if getattr(r, "strict", False) and r.status == "unmet"]
        if strict and not any_exact:      # v2.4 (G01): a metric size nothing is listed in
            r = strict[0]
            mm = float(r.key.split(":")[1][:-2])
            out["notice"] = {"kind": "unit_unmet", "terms": [r.label],
                             "text": f"No product here is listed in {r.label} ({mm / 25.4:.2f} in.). Metric sizes don't map "
                                     "one-to-one to US nominal sizes (a nominal 3/8 in. copper tube is 1/2 in. outside), so "
                                     "nothing is substituted. Search the inch size, or ask your branch or Ask Webb."}
            return
        ln = out.get("line")
        if ln and not ln.get("carried") and n and not any_exact and ident.get("kind") != "not_carried":
            kind_pl = ln["kind"] + ("es" if ln["kind"].endswith(("s", "sh", "ch", "x")) else "s")
            who = "" if ln.get("generic") else ln["brand"] + " "
            out["notice"] = {"kind": "line_miss", "terms": [ln["line"]],
                             "text": (f"{ln['line']} is a {ln['brand']} product line. " if ln.get("generic") else
                                      f"No {ln['brand']} {ln['line']} in the demo catalog. ")
                                     + f"These are {who}{kind_pl}."
                                     + ("" if ln.get("generic") else " Ask your branch for the exact model.")}
            if ln.get("note"):
                out["notice"]["text"] = ln["note"]
            return
        real = out.get("webb_items")
        if any_exact:              # the demo catalog has it: that's the answer
            out["webb_items"] = None
            out["xref"] = None
            return
        xr = out.get("xref")
        if xr and not (real and real["match"] == "exact"):
            out["notice"] = {"kind": "xref", "text": xr["summary"] + (" Shown from the Webb cross-reference; the demo "
                             "catalog below is related products." if n else " Shown from the Webb cross-reference.")}
            return
        if real and real["match"] == "exact":
            out["notice"] = {"kind": "real_only", "text": "That item isn't in the synthetic demo catalog. The exact "
                             "F.W. Webb item is shown first" +
                             ("; the products below are related demo items." if n else ".")}
            return
        # does anything on the first screen carry every word of the query? Words outside the catalog vocabulary
        # are usually typos ("toliet"), so they only count when a real Webb item has them ("plate heat exchanger")
        be = e.backend
        # words an applied requirement already holds for every product here ("npt": every valve is threaded) and words
        # that don't describe the product ("only", "replacement") aren't a reason to say the page misses something
        held = set().union(*(getattr(r, "words", set()) for r in reqs if r.status == "applied")) if reqs else set()
        context = set(ph.mods[1:]) if ph else set()     # "toilet" in "toilet wax ring": context, not a spec
        if context and ph:
            # ...but not the second word of a two-word kind: "pump" in "heat pump water heater" is the kind (v2.44)
            ct = list(ph.core_toks)
            brand_t = {t for b in pq.brands for t in normalize(b)}
            context = {w for w in context if not any(
                k and ct[k] == w and ct[k - 1].isalpha() and len(ct[k - 1]) >= 3 and ct[k - 1] not in brand_t
                and ct[k - 1] not in phmod.DROP for k in range(len(ct)))}
        words_all = [t for t in pq.tokens if t.isalpha() and len(t) >= 3 and t not in UNITS and t not in COVER_SKIP
                     and t not in held and t not in phmod.DROP and t not in context]
        words = [t for t in words_all if be.term_exists(t)]
        top = [int(i) for i in idx[:12]]
        covered_all = bool(top) and any(self._covers(i, words_all) for i in top)
        if real:                   # brand + description: only news if the demo catalog doesn't already have it
            if covered_all:
                out["webb_items"] = None
            else:
                b = pq.brands[0] if pq.brands else None
                out["notice"] = {"kind": "brand_miss", "text": (
                    f"The demo catalog has {b}, but not this product. The matching F.W. Webb item is shown first; "
                    f"the products below are other {b} items." if b else
                    "That brand isn't in the synthetic demo catalog. Matching F.W. Webb items are shown first; "
                    "the products below are other brands.")}
                return
        if ident.get("kind") == "not_carried":
            rel0 = e.webb_items.related(q, limit=4)
            if rel0:
                out["webb_items"] = {"match": "related", "items": [e.webb_items.card(it, "description", sees) for it in rel0]}
            more = (" Real F.W. Webb items that match are shown." if rel0 else "") + \
                " Ask your branch, or describe the job to Ask Webb."
            if ident.get("term"):
                t = ident["term"]
                out["notice"] = {"kind": "not_carried", "terms": [t["text"]], "what": t["label"],
                                 "text": f"Nothing in the demo catalog is {t['label']}, so it has no match for “{q.strip()}”." + more}
            else:
                ws = ident.get("words") or [ident["word"]]
                called = " or ".join("“" + w + "”" for w in ws)
                if ident.get("brand"):
                    out["notice"] = {"kind": "not_carried", "terms": ws, "what": ident["phrase"],
                                     "text": f"The demo catalog has no {' or '.join(ws)} products, so it has no match for "
                                             f"“{q.strip()}”." + more}
                    return
                out["notice"] = {"kind": "not_carried", "terms": ws, "what": ident["phrase"],
                                 "text": f"The demo catalog has no “{ident['phrase']}”: nothing in it is called {called}."
                                         + ("" if ident.get("kind_miss") else " Check the spelling.") + more}
            return
        models = model_tokens(q)
        if models:
            out["notice"] = {"kind": "no_exact_model", "terms": models,
                             "text": f"No exact match for {', '.join('“' + m + '”' for m in models)} in the demo catalog. "
                                     + ("The products below are related, not that model. " if n else "")
                                     + "Check the number, or ask your branch."}
            return
        if ident.get("conflict") and n:
            c = ident["conflict"]
            out["notice"] = {"kind": "conflict", "terms": c["terms"], "text": c["text"]}
            return
        what = (ph.head if ph and ph.head else "product")
        if ident.get("tail_terms") and n and not ident.get("terms"):
            labels = [t["label"] for t in ident["tail_terms"]]
            out["notice"] = {"kind": "standard", "terms": [t["text"] for t in ident["tail_terms"]],
                             "text": f"No demo-catalog {what} is listed as or for {', '.join(labels)}. These are the closest "
                                     f"{what}s; check the connection and standard before ordering."}
            return
        if ident.get("terms"):
            labels = [t["label"] for t in ident["terms"]]
            toks_t = {compact_id(t["text"]).lower() for t in ident["terms"]}
            top0 = [int(i) for i in idx[:12]]
            extra = [w for w in pq.tokens if w.isalpha() and len(w) >= 3 and w not in UNITS and w not in COVER_SKIP
                     and w not in toks_t and top0 and not any(self._covers(i, [w]) for i in top0)]
            out["notice"] = {"kind": "standard", "terms": [t["text"] for t in ident["terms"]] + extra,
                             "text": f"No demo-catalog product is listed as {', '.join(labels)}"
                                     + (f" or described as {', '.join('“' + w + '”' for w in extra)}" if extra else "")
                                     + ". These are the closest of the right kind; check the connection and standard before ordering."}
            return
        # a material the product is made of and nothing of this kind is: "CPVC ball valve", "black steel pipe"
        tr = next((r for r in reqs if r.key == "type" and r.status == "applied"), None)
        mu = [r for r in reqs if r.kind == "material" and r.status == "unmet" and not r.count]
        if tr and mu and n:
            m_ = mu[0].label
            out["notice"] = {"kind": "req_unmet", "terms": [m_],
                             "text": f"The demo catalog has no {m_} {tr.label.lower()}. These are {tr.label.lower()}s in other "
                                     "materials; check before ordering."}
            return
        if ident.get("kind") == "spec_miss":
            rel1 = e.webb_items.related(q, limit=4)
            if rel1:
                out["webb_items"] = {"match": "related", "items": [e.webb_items.card(it, "description", sees) for it in rel1]}
            out["notice"] = {"kind": "spec_miss", "terms": [ident["phrase"]],
                             "text": f"No {ident['phrase']} in the demo catalog matches every spec in “{q.strip()}”."
                                     + (" Real F.W. Webb items that do are shown first." if rel1 else "")
                                     + " These are the closest; check the specs before ordering."}
            return
        # qualifiers and specs the page can't confirm: "24V normally closed", "with end switch", "under 60 in. tall"
        if ph and n and not phmod.QUESTION.match(q):
            unconf = self._unconfirmed(q, ph, idx, reqs)
            if unconf:
                out["notice"] = {"kind": "partial", "terms": unconf,
                                 "text": f"The demo catalog doesn't list {', '.join('“' + u + '”' for u in unconf)} for these "
                                         "products. They match the rest of your search; check before ordering."}
                return
        rel = e.webb_items.related(q, limit=4) if n else []
        allidx = [int(i) for i in idx[:POOL]]

        def anywhere(ws):     # does any product on the page (not just the first screen) carry all these words?
            sw = set(ws)
            return any(sw <= self._toks(i) for i in allidx) or any(self._covers(i, ws) for i in allidx[:60])
        miss = bool(rel) and len(words_all) >= 2 and not anywhere(words_all)
        if not miss and len(words) >= 2 and top and not anywhere(words):
            if re.search(r"\band\b|&|\+", q):
                return        # two products asked for at once ("primer and cement"): each is fine on its own
            if qh and any(head_match(self.head(i), qh) for i in top):
                # the right kind of product, but a qualifier the catalog never pairs with it
                # ("refrigerant" press coupling: water press fittings aren't refrigerant-rated)
                missing = [w for w in words if not any(self._covers(i, [w]) for i in top)]
                if missing:
                    out["notice"] = {"kind": "partial", "terms": missing,
                                     "text": f"No demo-catalog product is described as {', '.join('“' + w + '”' for w in missing)} "
                                             "together with the rest of your search. These are the closest; check they suit the job."}
                    return
            else:
                miss = True
        if miss:
            if rel:
                out["webb_items"] = {"match": "related", "items": [e.webb_items.card(it, "description", sees) for it in rel]}
            phrase = re.sub(r"\s+", " ", q).strip()
            out["notice"] = {"kind": "phrase_miss", "terms": words_all,
                             "text": f"No demo-catalog product matches all of “{phrase}”."
                                     + (" Real F.W. Webb items that do are shown first."
                                        if rel else "") + " The products below are the closest."}
            return
        if fallback and n:
            out["notice"] = {"kind": "fallback", "text": f"Nothing in the demo catalog matches every part of “{q.strip()}”. "
                             "The products below are the closest; check them before ordering."}
            return
        unmet = [r for r in reqs if r.status == "unmet"]
        if unmet and n:
            def say(r):
                if r.kind == "brand":
                    return "made by " + r.label
                if r.kind == "line":
                    return "in the " + r.label + " line"
                return (r.label + " connection" if r.kind == "connection" else r.label if r.kind == "stock"
                        else r.label[4:] + " (every product here is one)" if r.kind == "exclude" else r.label.lower())
            out["notice"] = {"kind": "req_unmet", "terms": [r.label for r in unmet],
                             "text": f"No product here is {' and '.join(say(r) for r in unmet)}"
                                     f" together with the rest of your search. These are the closest; check before ordering."}

    def _unconfirmed(self, q: str, ph, idx, reqs) -> list[str]:
        """What the search states that no leading product carries: its qualifiers ("with end switch"), the specs after
        the product noun ("24V normally closed", "ASSE 1070") and describing words ("fire protection")."""
        top = [int(i) for i in idx[:12]]
        used = set().union(*(getattr(r, "words", set()) for r in reqs)) if reqs else set()
        skip = UNITS | phmod.DROP | used | {"inch", "inche", "tall", "long", "wide", "high"}
        out = []

        def covered(ws):
            return any(self._covers(i, ws) for i in top)
        spec_ws = [w for w in ph.spec_toks if w.isalpha() and len(w) >= 3 and w not in skip]
        if spec_ws and not covered(spec_ws):
            out.append(raw_phrase(q, ph.spec_toks))
        loose = [w for w in ph.descriptors if w not in skip and w not in ph.spec_toks and len(w) >= 3]
        miss = [w for w in dict.fromkeys(loose) if not covered([w])]
        if miss:
            out.append(raw_phrase(q, miss))
        for lead, tl in ph.tails:
            tt = normalize(tl)
            ws = [w for w in tt if w.isalpha() and len(w) >= 3 and w not in skip]
            if lead.split()[-1] in _MEASURE_LEADS:
                out.append(f"{lead} {tl}".strip())        # a limit ("under 60 in. tall"): never listed, never checked
            elif lead in ("for", "to fit", "fits", "fit", "compatible with") and not (
                    set(tt) & (reqmod.MAT_WORDS | reqmod.WORDS) or any(w[:1].isdigit() for w in tt)
                    or trade_terms.find(tl) or self._names_part(tl)):
                continue                                  # the job it's for ("for closed loop boiler"): not a spec
            elif ws and not covered(ws):
                out.append(f"{lead} {tl}".strip())
        return [u for u in out if u][:3]

    # ------------------------------------------------------------------ results
    def results(self, q: str, p: Principal, *a, _nested: bool = False, **kw) -> dict:
        """The results page, and when it comes up thin, the same search widened one labeled step at a time (v2.43):
        words nothing in the catalog is called are dropped, then the brand, then a material or finish. Size, product
        type, strict units and exact part numbers are never widened. relax="ladder:off" turns the widening off (the
        page's "search exactly what I typed")."""
        out = self._results(q, p, *a, _nested=_nested, **kw)
        if _nested:
            return out
        try:
            out = self._ladder(q, p, out, a, kw)
            self._did_you_mean(out)
        except Exception:  # noqa: BLE001        widening is a convenience; the page as searched always stands
            pass
        return out

    # ------------------------------------------------------------------ thin pages (v2.43)
    LADDER_KINDS = ("brand", "line", "feature", "finish", "material", "connection")   # widened in this order
    NO_LADDER = {"unit_unmet", "brand_not_carried", "model_miss", "id_contains", "line_miss", "account",
                 "price_miss", "conflict"}

    def _ladder(self, q: str, p: Principal, out: dict, a: tuple, kw: dict) -> dict:
        relax = set(kw.get("relax") or ())
        if "ladder:off" in relax or kw.get("merch") is False or kw.get("vocab") is False:
            return out
        total = int(out.get("total") or 0)
        nt = out.get("notice") or {}
        if total >= RELAX_BELOW or out.get("route") or out.get("account_question") or out.get("did_you_mean") \
                or nt.get("kind") in self.NO_LADDER or (out.get("webb_items") or {}).get("match") == "exact" \
                or (out.get("xref") or {}).get("replacements"):
            return out
        if any(r.get("strict") and r.get("status") != "relaxed" for r in out.get("requirements") or []):
            return out
        steps, best, best_q, cur = [], out, q, set(relax)
        # 1. words nothing in the catalog is called ("zzqx valve")
        unk = list((out.get("phrase") or {}).get("unknown") or [])
        if not unk and nt.get("kind") == "not_carried":
            unk = [t for t in nt.get("terms") or [] if t and " " not in t and not any(c.isdigit() for c in t)]
        # only words that look like noise are dropped ("zzqx", "tace"): a real word the catalog lacks ("conversion kit",
        # "booster pump") names a product it doesn't have, and the page says so instead of showing look-alikes
        rw = self._real_words()
        # (abbreviations of 3 letters or fewer, MJ, PE, HDPE-style trade terms and EPDM are kept: they're words here)
        unk = [w for w in unk if w.lower() not in rw and len(w) >= 4 and not trade_terms.is_term(w)
               and w.lower() not in ("epdm", "hdpe", "ldpe", "cpvc", "nptf", "fnpt", "mnpt", "dwv", "abs")
               and (not re.search(r"[aeiouy]", w, re.I) or re.search(r"(.)\1\1|[^aeiouy\W]{4}", w, re.I))]
        if unk:
            q2 = q
            for a_, b_ in (out.get("spelling") or {}).items():      # the words as read ("boosterpump" -> booster pump)
                q2 = q2.replace(a_, b_)
            for w in unk:
                q2 = re.sub(rf"(?<![\w-]){re.escape(w)}(?![\w-])", " ", q2, flags=re.I)
            q2 = re.sub(r"\s+", " ", q2).strip()
            if re.search(r"[A-Za-z]{3,}", q2) and q2.lower() != q.lower():
                r2 = self._results(q2, p, *a, **{**kw, "relax": cur})
                if int(r2.get("total") or 0) > total and (r2.get("notice") or {}).get("kind") != "not_carried":
                    best, best_q, total = r2, q2, int(r2.get("total") or 0)
                    steps.append({"kind": "drop", "words": unk})
        # 2. what the search asked for, least essential first: brand, line, a feature or finish, material, ends
        for kind in self.LADDER_KINDS:
            if total >= RELAX_BELOW:
                break
            # a brand or line is widened when the page is thin; a spec the search states (Type L, lead-free, press
            # ends) only when nothing at all meets it: four Type L tubes are the answer, not a thin page
            if kind not in ("brand", "line") and total > 0:
                break
            for r in [r for r in best.get("requirements") or [] if r.get("kind") == kind
                      and r.get("status") == "applied" and not r.get("fixed") and not r.get("strict")]:
                trial = cur | {r["key"]}
                r2 = self._results(best_q, p, *a, **{**kw, "relax": trial})
                if int(r2.get("total") or 0) > total:
                    best, cur, total = r2, trial, int(r2.get("total") or 0)
                    steps.append({"kind": "req", "key": r["key"], "label": r["label"], "req_kind": kind})
                if total >= RELAX_BELOW:
                    break
        if not steps:
            return out
        best = dict(best)
        best["query"] = q
        if out.get("spelling") and not best.get("spelling"):
            best["spelling"] = out["spelling"]
        what = []
        for st in steps:
            if st["kind"] == "drop":
                what.append(" and ".join(f"“{w}”" for w in st["words"])
                            + (" (nothing in the catalog is called that)" if len(st["words"]) == 1 else
                               " (nothing in the catalog is called those)"))
            else:
                what.append(f"{st['label']}")
        n0 = int(out.get("total") or 0)
        best["relaxed"] = {"from": q, "to": best_q, "count_all": n0, "steps": steps}
        best["notice"] = {"kind": "relaxed", "terms": [w for st in steps for w in st.get("words", [st.get("label")])],
                          "text": (f"Nothing matches all of “{q.strip()}”" if not n0 else
                                   f"Only {n0} product{'s' if n0 != 1 else ''} match{'' if n0 != 1 else 'es'} all of “{q.strip()}”")
                                  + f". Showing results without {', '.join(what)}."}
        return best

    def _did_you_mean(self, out: dict) -> None:
        """Other searches worth a click when a page is thin or labeled closest: part numbers one slip away (already
        set), the category or type most of the page is, a brand's categories (v2.43)."""
        total = int(out.get("total") or 0)
        nk = (out.get("notice") or {}).get("kind")
        weak = total < RELAX_BELOW or nk in ("partial", "phrase_miss", "fallback", "spec_miss", "req_unmet", "relaxed",
                                              "not_carried", "standard")
        if not weak or out.get("route") or out.get("account_question"):
            return
        dym = list(out.get("did_you_mean") or [])
        seen = {d.get("label") for d in dym}
        ph_ = out.get("phrase") or {}
        head = ph_.get("head")
        if not total and head and nk in ("not_carried", "phrase_miss") and self.global_heads().get(head, 0) >= 3 \
                and head.lower() != (out.get("query") or "").strip().lower():
            dym.append({"kind": "search", "label": f"All {head}{'' if head.endswith('s') else 's'}", "q": head})
            seen.add(head)
        for t in (out.get("tiles") or [])[:3]:
            if t.get("label") and t["label"] not in seen and t.get("count"):
                dym.append({"kind": "category", "label": t["label"], "facet": t["facet"], "value": t["value"],
                            "count": t["count"]})
                seen.add(t["label"])
        bl = out.get("brand_landing") or out.get("brand_elsewhere")
        if bl:
            for c in (bl.get("categories") or [])[:2]:
                lab = f"{bl['brand']} {c['label']}"
                if lab not in seen:
                    dym.append({"kind": "brand_category", "label": lab, "q": bl["brand"], "family": c["family"],
                                "count": c["count"]})
                    seen.add(lab)
        if dym:
            out["did_you_mean"] = dym[:4]

    def _results(self, q: str, p: Principal, branch: str | None = None, quick: set | None = None,
                 filters: dict | None = None, price_min: float | None = None, price_max: float | None = None,
                 radius: float = 25.0, personalize: bool = True, llm: str = "auto", merch: bool = True,
                 settings: dict | None = None, explain: bool = False, vocab: list | bool | None = None,
                 relax: set | None = None, fits: str | None = None, _nested: bool = False) -> dict:
        """`settings`: merchandising settings to use instead of the live ones (the dashboard's draft preview, an A/B
        variant). `explain`: each item carries the merchandising adjustments that moved it (the dashboard's "why").
        `vocab`: the search dictionary's entries to use instead of the live ones (a "try it" preview); False: none.
        `relax`: keys of requirements read from the query that the shopper removed ("leadfree", "mat:brass")."""
        q = clean_dashes(q)                  # en / em dashes and the minus sign read as hyphens (1–1/4 = 1-1/4)
        e = self.e
        t0 = time.perf_counter()
        # merchandising: rewrites first (the whole page searches the rewritten words), then the rules for it
        ms = settings if settings is not None else (e.merch.get() if merch else None)
        q_in, rewrites, vocab_hits = q, [], []
        # the same thing typed another way: 16 x 25 x 1, 150000 BTU boiler, temperature and pressure (v2.0)
        q, forms = qforms.rewrite(q)
        form_hits = [{"id": None, "kind": "form", "from": f["from"], "to": f["to"], "why": f.get("why"),
                      "lang": f.get("lang")} for f in forms]
        if ms:
            q, rewrites = rewrite_query(q, ms)
        dic = getattr(e, "vocab", None)
        if dic is not None and vocab is not False:
            q, vocab_hits = dic.apply(q, vocab if isinstance(vocab, list) else None)
        rules = active_rules(ms, q, date.today(), p) if ms else []
        pins, hidden = pinned_skus(rules), hidden_skus(rules)
        badges = pin_badges(rules)
        only_c = [c for r in rules for c in r.get("only") or []]
        excl_c = [c for r in rules for c in r.get("exclude") or []]
        quick = {x for x in (quick or set()) if x in QUICK}
        filters = {k: [v for v in vs if v] for k, vs in (filters or {}).items() if k in FACETS or k == "spec"}
        branch = branch or (p.home_branch if p.is_authenticated else None)
        # federated search: parse, Claude QU, account blocks (no product work here)
        fed = e.service.search(q, p, vertical="all", llm=llm, personalize=personalize, skip_products=True)
        # what the search says about buying (a quantity, a branch, "in stock", "cheapest") and what it rules out
        # ("not a wye") come off before the engine searches, and so do the product phrase's qualifiers ("for C900 PVC
        # pipe", "with end switch", "under 60 in. tall"): the page checks those, it doesn't search for them. A
        # qualifier that names a brand or a model ("cartridge for Moen 1225") stays in the search
        buy, q_prod = phmod.Buying(), q
        # a job, a symptom, a question, a comparison or a swap: the product phrase in it is searched, and the page
        # offers kits, guides and Ask Webb besides (v2.01). Read before the buying words: "no heat" is a symptom,
        # not an exclusion
        intent = intents.read(q) if not fed.get("account_question") else None
        if intent and not re.search(r"[a-z]{2}", intent.product, re.I):
            intent = None
        if intent:
            q_prod = intent.product
        # "GE 3ton vs Rheem 3ton": each side's best product, compared; the grid shows the shared product from both
        # brands (v2.02). Each side is an ordinary search, so this never recurses
        cmp_out = None
        sides = e.compare.sides(intent) if intent and not _nested else None
        if sides:
            try:
                cmp_out = e.compare.from_search(sides, p, branch, llm=llm, settings=settings, vocab=vocab,
                                                personalize=personalize)
            except Exception:
                cmp_out = None
            if cmp_out:
                g = e.compare.grid(sides, [s_["pick"] for s_ in cmp_out["sides"]])
                if g and (filters or {}).get("brand"):
                    q_prod = g[0]       # a brand the customer picked stays picked
                elif g:
                    q_prod = g[0]
                    filters = {**filters, "brand": g[1]}
                elif intent.product.strip().lower() == sides[0].strip().lower():
                    q_prod = " ".join(sides)      # two different products: both sides' words find both
        if not fed.get("account_question"):
            t_, b_ = phmod.split(q_prod, self.cities())
            if re.search(r"[a-z0-9]", t_, re.I):
                q_prod, buy = t_, b_
        if buy.branch:
            branch = buy.branch
        words_price = bool(buy.price) and price_min is None and price_max is None
        if words_price:        # "under $50": the price filter, set from the words (v2.01)
            price_min, price_max = buy.price
        spelled: dict = {}
        if not fed.get("account_question") and "spell:off" not in (relax or ()):
            q_prod, spelled = self.spell(q_prod)
            # a word the speller fixed can be a dictionary term ("airscoop" -> air scoop -> air separator, v2.44):
            # the dictionary reads the corrected words, and only those
            if spelled and dic is not None and vocab is not False:
                fixed = {w for v in spelled.values() for w in normalize(str(v))}
                q2, h2 = dic.apply(q_prod, vocab if isinstance(vocab, list) else None)
                if h2 and all(set(normalize(x["from"])) & fixed for x in h2):
                    q_prod, vocab_hits = q2, list(vocab_hits) + h2
            # ...and so do the trade forms ("condensor" -> condenser -> condensing unit, v2.45)
            if spelled:
                q3, f3 = qforms.rewrite(q_prod)
                if f3 and q3 != q_prod:
                    q_prod = q3
                    form_hits = list(form_hits) + [{"id": None, "kind": "form", "from": f["from"], "to": f["to"],
                                                    "why": f.get("why"), "lang": f.get("lang")} for f in f3]
        # product lines (v2.42): a line the catalog lists becomes a requirement below ("moen align"); a line it doesn't
        # list searches that brand's products of the line's kind and says so ("lochinvar knight": Lochinvar boilers).
        # A line the trade uses for the product type ("propress", "megapress") searches the kind with no brand held
        bmast = getattr(e, "brands", None)
        line_hit, line_note = None, None
        if bmast is not None and not fed.get("account_question") and not _nested and not cmp_out:
            line_hit = bmast.line(q_prod, bmast.detect(q_prod))
            if line_hit is not None and not line_hit.carried and line_hit.kind not in ("", "none") and not e.webb_items.exact(q):
                rest = bmast.without_line(q_prod, line_hit)
                rest_words = [w for w in normalize(rest) if w.isalpha() and len(w) >= 3
                              and not any(w in normalize(b) for b in (line_hit.brand,))]
                if line_hit.generic and rest_words:     # "pro press tool kit": the name of the tool, searched as typed
                    line_hit, rest = None, ""
            if line_hit is not None and not line_hit.carried and line_hit.kind not in ("", "none") and not e.webb_items.exact(q):
                kind = line_hit.kind.split()
                add = kind if not rest_words else kind[:-1]
                add = [w for w in add if w.lower() not in rest.lower().split()]
                lead = line_hit.brand + " " if (line_hit.alone and not line_hit.generic
                                                and line_hit.brand.lower() not in rest.lower()) else ""
                q_new = re.sub(r"\s+", " ", f"{lead}{rest} {' '.join(add)}").strip()
                if q_new and q_new.lower() != q_prod.strip().lower():
                    line_note = {**line_hit.to_dict(), "from": q_prod.strip(), "to": q_new}
                    q_prod = q_new
        qc = phmod.question_core(q_prod) if phmod.QUESTION.match(q_prod) else None
        core, tails = (q_prod.strip(), []) if phmod.QUESTION.match(q_prod) and not qc else \
            phmod.cut(qc or q_prod, lambda w: w.isalpha() and len(w) >= 3 and w not in phmod.DROP
                      and w not in phmod.SPEC_NOUNS and self.global_heads().get(w, 0) >= 3)
        # "with" names what the product has (w/ drain, with oxygen barrier): searched. What it's for, a limit, what
        # it's without: checked, not searched, unless it names a brand or model ("for Moen 1225")
        q_search = " ".join([core] + [f"{lead} {tl}" for lead, tl in tails
                                      if lead in ("with", "w/", "including", "plus") or self._names_part(tl)]
                            # a size alone after "for" is the product's size ("what ball valve do I need for 1/2")
                            + [tl for lead, tl in tails if lead == "for" and _SIZE_ONLY.fullmatch(tl)])
        od = [n_ for lead, tl in tails if lead.split()[-1] in ("od", "o.d.") for n_ in phmod.od_nominal(tl)]
        if len(set(od)) == 1 and not re.search(r"\d", core):     # "pipe OD 6.60 to 6.90": 6 in. pipe
            q_search = f"{od[0]} inch {q_search}"
        pq = e.parser.parse(q_search[:500])
        e.service._flag_unknown_vocab(pq)
        qu = (fed.get("llm") or {}).get("qu")
        if qu:
            e.qu.apply(pq, qu)
        out = {"query": q, "parsed": fed["parsed"], "blocks": fed["blocks"], "top_answer": fed.get("top_answer"),
               "llm": fed["llm"], "account_question": fed.get("account_question", False), "denied": fed.get("denied"),
               "branch": e.availability.branch_info(branch), "radius": radius, "items": [], "total": 0,
               "candidates": 0, "quick": {}, "facets": {}, "tiles": [], "dominant": None,
               "merch": {"rewritten_from": q_in if rewrites or vocab_hits or form_hits else None, "rewrites": rewrites,
                         "vocab": form_hits + vocab_hits,
                         "rules": [{"id": r["id"], "name": r["name"]} for r in rules],
                         "banner": next((r["banner"] for r in rules if r.get("banner")), None),
                         "redirect": next((r["redirect"] for r in rules if r.get("redirect")), None)},
               "layout": {"group_variants": bool((ms or {}).get("layout", {}).get("group_variants", 0)),
                          "facets": (ms or {}).get("facets")}}
        if line_note:
            out["line"] = line_note
        elif line_hit is not None and line_hit.carried:
            out["line"] = line_hit.to_dict()
        out["kits"] = [] if fed.get("account_question") else e.kits.search(q)
        if cmp_out:
            out["compare"] = cmp_out
        if intent:
            out["intent"] = intent.to_dict()
            if intent.kind == "job":
                ks = e.kits.search(intent.topic) + (e.kits.search(intent.product.split()[-1] + " install")
                                                    if intent.kind == "job" else e.kits.search(intent.product))
                seen_k: set = set()
                out["kits"] = [k for k in ks if not (k.get("kit_id") in seen_k or seen_k.add(k.get("kit_id")))][:4] \
                    or out["kits"]
            if intent.ask:
                try:
                    gs = e.service.search_vertical("content", e.parser.parse(intent.topic or intent.product), p, 6)
                    # only guides about the thing asked about ("furnace", "pex"), not every troubleshooting article
                    words = {w for w in normalize(intent.topic + " " + q) if w.isalpha() and len(w) >= 3
                             and w not in GUIDE_STOP}
                    out["guides"] = [g for g in gs if words & set(normalize(g.get("title") or ""))][:3]
                except Exception:
                    out["guides"] = []
        if q_search.strip().lower() != q.strip().lower():
            out["product_query"] = q_search
        if spelled:
            out["spelling"] = spelled
        if buy.any():
            out["buying"] = buy.to_dict()
        if buy.cheapest:
            out["sort_hint"] = "price_asc"
        elif buy.priciest:
            out["sort_hint"] = "price_desc"
        # account questions typed on All open that tab's own page ("my invoices" -> the Invoices tab, filtered)
        intent = fed["parsed"].get("intent")
        if fed.get("account_question"):
            out["route"] = intent if intent in ("orders", "invoices") else \
                ("invoices" if re.search(r"invoice|bill|owe|balance|past due|overdue|statement|paid", q, re.I) else "orders")
        elif re.search(r"^\s*(?:(?:show|see|open)\s+)?(?:(?:me|my|our|all|shared|saved|team|company|private)\s+)*lists?\s*$", q, re.I):
            out["route"] = "lists"
        # natural-language product questions get an inline "Ask Webb" answer on the results page, and the
        # product grid searches the equipment the question is about instead of the whole sentence
        from webbsearch.llm.advisor import FILLER, OUTDOOR_RE, AdvisorTools, RuleAdvisor, parse_constraints, render
        out["advisor"] = (not fed.get("account_question") and RuleAdvisor(e).handles(q)
                          and (len(q.split()) >= 5 or bool(re.search(r"pad|footing|slab|fits?\b", q, re.I))))
        adv_pool = None
        if out["advisor"]:
            if OUTDOOR_RE.search(q):
                # the answer and the grid read one constraint model: the grid is exactly the units the
                # answer counts (same filters, same order), so "91 fit a 48x24 pad" shows 91 units that fit
                c = parse_constraints(q, self.outdoor_brands())
                tools = AdvisorTools(e, p, branch)
                tools.find_outdoor_units(**{k: v for k, v in c.items() if k != "raw"})
                adv_pool = np.array(tools.pool[:2000], np.int64)      # every unit the answer counts (v2.01: R-410A stays in)
                out["advisor_count"] = len(tools.pool)
                pq2 = " ".join(x for x in [f"{c['ton']:g} ton" if c.get("ton") else "",
                                           "heat pump" if c.get("kind") == "HP" else "", c.get("refrigerant", "")] if x)
                pq = e.parser.parse(pq2 or "condensing unit")
                out["product_query"] = render({k: v for k, v in c.items() if k != "raw"})
            else:
                q_adv = re.sub(r"\s+", " ", FILLER.sub(" ", q_search)).strip(" ?.!,")
                if q_adv and q_adv != q_search:
                    pq = e.parser.parse(q_adv)
                    out["product_query"] = q_adv
        if p.is_authenticated and not p.sees_prices:
            out["kits"] = [k | {"min_total": None, "max_total": None} for k in out["kits"]]
        sees = not (p.is_authenticated and not p.sees_prices)
        real_exact = [] if (fed.get("account_question") or adv_pool is not None) else e.webb_items.exact(q)
        out["webb_items"] = {"match": "exact" if any(h != "brand + description" for _, h in real_exact) else "brand",
                             "items": [e.webb_items.card(it, h, sees) for it, h in real_exact]} if real_exact else None
        real_kinds = self._real_kinds(out["webb_items"]) if out["webb_items"] and out["webb_items"]["match"] == "exact" \
            else []
        real_own = False
        out["notice"] = None
        # OEM / competitor part numbers -> the Webb items that replace them (Webb xRef lake)
        out["xref"] = None
        if e.xref.ok and not fed.get("account_question") and adv_pool is None:
            from webbsearch.retrieval.xref import summary, worth_looking_up
            if worth_looking_up(q):
                xr = e.xref.lookup(q, branch=branch)
                if xr and (xr["self"] or xr["replacements"] or xr.get("others")):
                    xr["summary"] = summary(xr)
                    out["xref"] = xr
        if fed.get("account_question") or not (pq.tokens or pq.id_chunks) or not p.can("products"):
            out["timings"] = {"total_ms": _ms(t0)}
            return out
        pers = ms["personalization"] if ms else {"personal_model": True, "history_lane": True}
        personal = personalize and p.is_authenticated and pers["personal_model"]
        hist_p = p if (personalize and p.is_authenticated and pers["history_lane"]) else None
        cat_fam = e.catalog.family
        ph, found, core_toks, cat_pick = None, [], list(normalize(core)), []
        brand_req, brand_cov, brand_hits_core, other_idx, brand_only = None, frozenset(), [], None, False
        if adv_pool is not None:
            m_ = len(adv_pool)
            z = np.zeros(m_, np.float32)
            cands = Candidates(adv_pool, z, z, np.zeros(m_, np.int64), np.ones(m_, np.float32), np.zeros(m_, np.int64),
                               z, adv_pool[:50])
        else:
            rel_w = (ms or {}).get("relevance") or {}
            cands = e.retriever.retrieve(pq, principal=hist_p, limit=POOL, history_k=200,
                                         field_w=rel_w.get("fields") or None)
            # the product phrase, read against the retrieved pool (see phrase.py); requirements come from it
            brand_toks = {t for b in pq.brands for t in normalize(b)}
            skip = phmod.component_materials(core_toks, reqmod.MAT_WORDS)
            found = reqmod.detect(_without_brands(core_toks, pq.brands), pq.sizes, skip=skip)
            # a tool for a connection ("pro press tool", "crimp tool", "pex expansion jaws"): the connection word says
            # what the tool works on, not the tool's own ends; it's matched in the title, not required (v2.43)
            if TOOL_WORDS & set(core_toks):
                found = [reqmod.Requirement("tool-" + r_.key, "feature", r_.label, r_.test, words=r_.words)
                         if r_.kind == "connection" else r_ for r_ in found]
            mreq = reqmod.metric_requirement(q_prod)       # v2.4 (G01): 10 mm / 1 cm is a size, never 10 in. / 1 in.
            if mreq:
                found.append(mreq)
            # the brand typed is a requirement (v2.42): other brands leave the grid for an "Other brands" row. Brands
            # named after "for" are what the part fits, not who makes it ("cartridge for Moen 1225")
            if bmast is not None and not cmp_out and not (line_hit is not None and line_hit.generic):
                core_hits = [h for h in bmast.detect(core) if h.carried]
                if core_hits:
                    brand_cov = frozenset().union(*(h.covers for h in core_hits))
                    brand_req = reqmod.Requirement(
                        "brand:" + "-".join(sorted(h.name for h in core_hits)).lower().replace(" ", "_"), "brand",
                        " or ".join(h.name for h in core_hits), lambda it, c=brand_cov: it["brand"] in c,
                        words={t for h in core_hits for t in normalize(h.name)})
                    brand_hits_core = core_hits
                    have = set(cands.idx.tolist())
                    try:          # the brand's own matches join the pool (a small brand can be crowded out of it)
                        more_b = e.retriever.retrieve(pq, filters={"brand": sorted(brand_cov)}, limit=300,
                                                      lanes=("exact", "lexical", "dense"))
                        extra = [int(i) for i in more_b.idx if int(i) not in have]
                        # the brand alone ("navien"): a sample of each thing it makes, so its boilers and heaters are
                        # in the pool and not only the thousands of repair parts its name is on
                        btoks = {t for h in core_hits for t in normalize(h.name)}
                        if not [w for w in core_toks if w.isalpha() and len(w) >= 2 and w not in btoks]:
                            cb_ = e.catalog
                            bmask = np.isin(cb_.brand, sorted(brand_cov))
                            seen_ = have | set(extra)
                            for f_ in Counter(cb_.family[bmask].tolist()):
                                ii = np.flatnonzero(bmask & (cb_.family == f_))[:40]
                                extra += [int(i) for i in ii if int(i) not in seen_]
                        if extra:
                            cands = _with_extra(cands, extra)
                    except Exception:  # noqa: BLE001
                        pass
            if line_hit is not None and line_hit.carried and line_hit.series:
                ss = set(line_hit.series)
                found.append(reqmod.Requirement("line:" + line_hit.line.lower(), "line", f"{line_hit.brand} {line_hit.line}",
                                                lambda it, ss=ss: it.get("series") in ss,
                                                known=lambda it: bool(it.get("series")),
                                                words=set(line_hit.tokens)))
            # an empty pool too: a word nothing retrieves is still a word the catalog lacks ("ballcock")
            used = set().union(*(r.words for r in found)) if found else set()
            terms_q = {compact_id(t["text"]).lower() for t in trade_terms.find(q_prod)}
            # model numbers' letters ("HWD" in HWD-654) aren't product words
            terms_q |= {t for m_ in model_tokens(q_prod) for t in normalize(m_) if t.isalpha()}
            ph = phmod.analyze(q_prod, _Lexicon(self, cands.idx), brand_toks, used, terms_q)
            cat_pick = self._category_intent(ph, [t for t in core_toks if t not in brand_toks], brand_cov)
            if cat_pick:
                have = set(cands.idx.tolist())
                extra = [i for i in self._cat_items(cat_pick, core_toks) if i not in have]
                if extra:
                    cands = _with_extra(cands, extra)
            # specs can crowd the product out of the pool ("1-1/2 CPVC schedule 80 true union ball valve socket":
            # 600 CPVC fittings, no ball valve): the catalog's products of that kind join it
            more = self._rescue(ph, cands.idx)
            if more:
                cands = _with_extra(cands, more)
            if real_kinds and not pq.brands:
                real_kinds, real_own = [], True    # a bare part number: the real item is the answer, as before
            if real_kinds and ph is not None and ph.head and ph.head not in real_kinds:
                real_kinds, real_own = [], True    # the search names its own product ("titan pro 55 dual run cap")
            if real_kinds:       # an exact real item: demo products of its kind, its brand first, can be related
                more = self._kind_items(real_kinds, pq.brands, cands.idx)
                if more:
                    cands = _with_extra(cands, more)
            if pins:        # pinned items the search didn't retrieve still go on the page
                have = set(cands.idx.tolist())
                extra = [e.catalog.sku_to_idx[s_] for s_, _ in pins if s_ in e.catalog.sku_to_idx
                         and e.catalog.sku_to_idx[s_] not in have]
                if extra:
                    cands = _with_extra(cands, extra)
        X = e.fb.build(pq, cands, p if p.is_authenticated else None, personal=personal)
        if adv_pool is not None:
            s = -np.arange(len(adv_pool), dtype=np.float32)      # keep the advisor's order
        else:
            s = e.ranker.score(X, personal=personal) + 100.0 * X[:, F["exact_id"]]
        # compliance-aware ranking: outdoor units below the DOE minimum, and R-410A units (state rules vary since EPA's
        # May 2026 rule; v2.01 shows them, it doesn't filter them), rank after current ones, unless the query is about
        # a repair or names R-410A
        offset = X[:, F["exact_id"]] > 0          # scores carrying a fixed offset (exact id, compliance), for v2.3
        if adv_pool is None and not re.search(r"410|repair|existing|replace", q, re.I):
            hv = e.hvac
            for k, i in enumerate(cands.idx):
                if cat_fam[i] in hv.OUTDOOR and (hv.install_note(int(i)) or hv.spec(int(i))["refrigerant"] == "R-410A"):
                    s[k] -= 50.0
                    offset[k] = True
        exp: dict = {} if explain else None
        attr_fn = self._attr_fn()
        if ms and adv_pool is None:
            # ranking weights (previously purchased, brand affinity, stock, popularity) and rule boosts / buries
            at_b = e.availability.summary(cands.idx, branch, radius)["at_branch"] if ms["weights"]["branch_stock"] else None
            acct = ({"segment": p.segment, "lines": e.features.acct_lines.get(p.account_id, 0),
                     "brand_share": e.features.acct_brand_all.get(p.account_id, {})} if p.is_authenticated else None)
            fr = getattr(e, "fieldrel", None)
            rel_s = ms.get("relevance")
            fadj = (fr.adjust(cands.idx, fr.query_tokens(pq.tokens, pq.sizes), rel_s)
                    if fr is not None and fr.active(rel_s) else None)
            s = adjust_scores(s, X, cands.idx, e.catalog, F, ms, personalize and p.is_authenticated, rules, at_b,
                              acct=acct, attr_fn=attr_fn, explain=exp, field_adj=fadj)
        order = np.argsort(-s, kind="stable")
        idx, X = cands.idx[order], X[order]
        s_ord, off_ord = s[order], offset[order]       # for availability, after the page's members are decided
        if exp:
            exp = {k: np.asarray(v)[order] for k, v in exp.items()}
        # Relevance floor for the page (the ranker orders; this decides membership, so facet
        # counts and tiles describe real matches, not the tail of a 600-candidate pool):
        # every numeric spec the user typed must match, and at least half the words.
        has_digits = any(any(ch.isdigit() for ch in t) for t in pq.tokens)
        cov, spec = X[:, F["coverage"]], X[:, F["spec_coverage"]]
        rel = (X[:, F["exact_id"]] > 0) | (((cov >= 0.5) | (X[:, F["dense_sim"]] >= 0.8)) &
                                           ((spec >= 1.0) | (not has_digits)))
        if adv_pool is not None:
            rel[:] = True          # every advisor match meets the constraints by construction
        elif pq.id_chunks and len(idx) and not (X[:, F["exact_id"]] > 0).any():
            # part numbers that start with or contain the typed one are on the page, labeled (v2.43)
            part_set = set(e.retriever.partial_ids(pq))
            if part_set:
                rel |= np.array([int(i) in part_set for i in idx], bool)
        pin_idx = {e.catalog.sku_to_idx[s_] for s_, _ in pins if s_ in e.catalog.sku_to_idx}
        if adv_pool is None and (only_c or excl_c):
            # rule filters: "only show" keeps items matching any of its conditions; hide conditions drop items
            if only_c:
                rel &= any_mask(only_c, idx, e.catalog, attr_fn)
            if excl_c:
                rel &= ~any_mask(excl_c, idx, e.catalog, attr_fn)
            out["merch"]["filtered"] = True
        if pin_idx:
            rel |= np.isin(idx, list(pin_idx))
        fallback = not rel.any()
        brand_toks = {t for b in pq.brands for t in normalize(b)}
        qh = _phrase_heads(ph) if ph and ph.head else (set() if ph else query_heads(pq.tokens, brand_toks))
        # product identity: a product the catalog doesn't carry gets no page of look-alikes; a standard (C900, SDR 35)
        # or connection (MJ) keeps only the families that can hold it; specs nothing meets keep the product, not the
        # numbers (a condensate pump with no 115V listing, not a 115V mini-split)
        ident: dict = {}
        real_exact = bool(out.get("webb_items") and out["webb_items"]["match"] == "exact")
        if adv_pool is None and (len(idx) or (ph is not None and ph.unknown)) \
                and not bool((X[:, F["exact_id"]] > 0).any()) and not out.get("xref"):
            rel, ident = self._identity(q_prod, pq, qh, ph, brand_toks, idx, rel, fallback, real_exact, out.get("kits"),
                                        cats=cat_pick)
        if fallback and not ident.get("chosen") and ident.get("kind") != "not_carried":
            rel[:12] = True        # never leave the page empty when nothing clears the bar
        # a brand and a model number the demo catalog doesn't have ("Sloan A-41-A"): that brand's products, labeled
        # related, not other brands that share the number's digits
        if adv_pool is None and pq.brands and ident.get("kind") != "not_carried" and model_tokens(q_prod) \
                and not bool((X[:, F["exact_id"]] > 0).any()):
            bm = np.isin(e.catalog.brand[idx], pq.brands)
            if bm.any():
                rel = (rel & bm) if (rel & bm).any() else (bm & (np.cumsum(bm) <= 12))
        # under an exact real F.W. Webb item, related demo items are the same kind of product (circulators under a
        # Taco 007E, not aquastats), or none (v2.01, ChatGPT QA F12)
        if adv_pool is None and real_exact and not real_own and rel.any() and not bool((X[:, F["exact_id"]] > 0).any()):
            # (a page with no related items stays that way: the real item is the answer)
            km = np.array([any(w in self.heads(int(i)) for w in real_kinds) for i in idx], bool)
            if pq.brands and (km & np.isin(e.catalog.brand[idx], pq.brands)).any():
                km &= np.isin(e.catalog.brand[idx], pq.brands)
            rel = (rel & km) if (rel & km).any() else (km & (np.cumsum(km) <= 12))
        if ph:
            ident["conflict"] = self._conflict(ph, core_toks)
            # how the search was read (the QA ledger and the dashboard's "why" use it)
            out["phrase"] = {"product": ph.core, "head": ph.head, "kind": ph.mods, "unknown": ph.unknown,
                             "specs": ph.spec_toks, "qualifiers": [f"{a} {b}" for a, b in ph.tails],
                             "corrected": ph.corrected}
        idx, X = idx[rel], X[rel]
        if exp:
            exp = {k: v[rel] for k, v in exp.items()}
        s_ord, off_ord = s_ord[rel], off_ord[rel]
        # availability tiers (v2.3): in stock nearby, in the region, at Webb, then from the manufacturer. Applied after
        # membership is decided, so it only reorders the page: the count, the filters and the fallbacks are unchanged
        xt = getattr(e, "extended", None)
        acfg = xt.cfg(ms) if xt is not None and ms is not None else None
        if acfg is not None and acfg["enabled"] and adv_pool is None and len(idx) > 1:
            s2, _ = xt.coded_adjust(s_ord, idx, ~off_ord, branch, max(1, buy.qty or 1), acfg)
            s2 = s2 + np.where(off_ord & (X[:, F["exact_id"]] > 0), 1e6, 0.0)      # an exact part number stays first
            o2 = np.argsort(-s2, kind="stable")
            idx, X = idx[o2], X[o2]
            if exp:
                exp = {k: v[o2] for k, v in exp.items()}
        if fallback and (not any(ch.isalpha() for t in pq.tokens for ch in t) or out.get("xref")):
            idx, X = idx[:0], X[:0]   # a bare number, or a part the cross-reference answered: no page of noise
        n = len(idx)
        cat = e.catalog
        any_exact = bool(n) and bool((X[:, F["exact_id"]] > 0).any())
        # the kind of product asked for is a requirement too: "3/4 press ball valve lead free" is ball valves, not the
        # lead-free press couplings that share every other word (they led a price sort)
        type_req = None
        if n and cat_pick and adv_pool is None and not any_exact:
            im = np.isin(cat.cat3[idx], cat_pick)
            if im.any():
                tr = reqmod.Requirement("type", "type", " or ".join(cat_pick), lambda it: True, count=int(im.sum()))
                if "type" in (relax or set()):
                    tr.status = "relaxed"
                else:
                    idx, X = idx[im], X[im]
                    if exp:
                        exp = {k: v[im] for k, v in exp.items()}
                    n = len(idx)
                type_req = tr
        if n and qh and ph and adv_pool is None and not any_exact and pq.style != "part" and type_req is None:
            im = self._ident(idx, qh, ph.mods)
            if pin_idx:          # a merchandiser's pin stays on the page (it carries its badge)
                im |= np.isin(idx, list(pin_idx))
            if im.sum() >= 3 and (~im).any():
                # its name: the head noun, with the word before it when the products carry that word too
                # ("line set", "curb stop"; not "refrigerant coupling", which nothing here is)
                hi = ph.words.index(ph.head)
                nw = ph.words[:hi + 1]
                name = nw[-1:]
                if len(nw) >= 2:
                    sel = [int(i) for i in idx[im][:40]]
                    if sum(self._covers(i, [nw[-2]]) for i in sel) >= 0.8 * len(sel):
                        name = nw[-2:]
                typed = {v: k for k, v in ph.corrected.items()}
                lab = raw_phrase(q_prod, [typed.get(w, w) for w in name])
                tr = reqmod.Requirement("type", "type", lab[:1].upper() + lab[1:], lambda it: True,
                                        count=int(im.sum()))
                if "type" in (relax or set()):
                    tr.status = "relaxed"
                else:
                    idx, X = idx[im], X[im]
                    if exp:
                        exp = {k: v[im] for k, v in exp.items()}
                    n = len(idx)
                type_req = tr
        # requirements the search states (lead-free, press, brass, a dual MFD rating): every product on the page meets
        # them, whatever the sort; one nothing meets isn't applied and the page says so
        reqs: list = [type_req] if type_req else []
        if n and adv_pool is None and not any_exact:
            found = list(found)
            for x in buy.exclusions:         # "not a wye", "no heat pump": products named that are off the page
                found.append(reqmod.Requirement("not:" + "-".join(x["words"]), "exclude", "Not " + x["text"],
                                                lambda it, ws=tuple(x["words"]): not all(w in it["toks"] for w in ws)))
            stk = None
            split_need = 0
            if buy.stock or buy.one_branch:  # "in stock", "available today", "8 at one branch"
                av = e.availability
                bi = av.branch_index(branch)
                qm_ = av._qty(np.asarray(idx, np.int64), np.concatenate([[bi], av.near_counters(bi, radius)]))
                stk = {"at": qm_[:, 0], "best": qm_.max(axis=1), "sum": qm_.sum(axis=1)}
                need = max(1, buy.qty or 1)
                bname = str(av.names[bi]).split(",")[0]
                if buy.one_branch:
                    found.append(reqmod.Requirement("stock", "stock", f"{need} at one branch within {radius:g} mi of {bname}",
                                                    lambda it, k_=need: it["best"] >= k_))
                elif need > 1:
                    # "12 today" with 8 on the shelf: the 8 today and 4 by transfer, said on the card; never all 12
                    # today (v2.43, review T19). Products the shelf covers come first
                    found.append(reqmod.Requirement("stock", "stock", f"{need} from {bname}, today or by transfer",
                                                    lambda it, k_=need: it["at"] >= k_ or it["sum"] >= k_))
                    split_need = need
                else:
                    found.append(reqmod.Requirement("stock", "stock", f"In stock at {bname}", lambda it: it["at"] >= 1))
            if found:
                reqs += found
                # a pin survives the product-type requirement, not the buyer's own words: a merchandiser can feature a
                # related product, never a non-lead-free valve on a lead-free search
                unst_: list = []
                keep = self._apply_requirements(found, idx, relax or set(), stock=stk, unstated=unst_)
                if not keep.all():
                    idx, X = idx[keep], X[keep]
                    if exp:
                        exp = {k: v[keep] for k, v in exp.items()}
                    n = len(idx)
                if split_need and n and stk is not None:
                    at_ = e.availability._qty(np.asarray(idx, np.int64), np.array([e.availability.branch_index(branch)]))[:, 0]
                    short = at_ < split_need
                    if short.any() and (~short).any():
                        o2 = np.argsort(short, kind="stable")
                        idx, X = idx[o2], X[o2]
                        if exp:
                            exp = {k: v[o2] for k, v in exp.items()}
                    out["split_need"] = int(split_need)
                # products kept only because they don't state an applied requirement go after the ones that meet it
                # (v2.4, review G02): a forged tee that doesn't list its ends never leads a "press tee" page
                u = unst_[0][keep] if unst_ else np.zeros(n, bool)
                if u.any() and (~u).any():
                    o2 = np.argsort(u, kind="stable")
                    idx, X = idx[o2], X[o2]
                    if exp:
                        exp = {k: v[o2] for k, v in exp.items()}
                    out["unstated_last"] = int(u.sum())
            if brand_req is not None and n:
                bok = np.isin(cat.brand[idx], list(brand_cov))
                brand_req.count = int(bok.sum())
                if brand_req.key in (relax or set()):
                    brand_req.status = "relaxed"
                elif bok.any():
                    brand_req.status = "applied"
                    if (~bok).any():
                        other_idx = idx[~bok][:8]
                        idx, X = idx[bok], X[bok]
                        if exp:
                            exp = {k: v[bok] for k, v in exp.items()}
                        n = len(idx)
                else:
                    brand_req.status = "unmet"
                reqs.append(brand_req)
            # a brand on its own ("navien", "bradford white"): its equipment before its repair parts
            if brand_hits_core and n and not [w for w in core_toks if w.isalpha() and len(w) >= 2
                                              and not any(w in normalize(h.name) for h in brand_hits_core)]:
                brand_only = True
                cov_ = frozenset().union(*(h.covers for h in brand_hits_core))
                from webbsearch.retrieval.brands import counts_by_brand
                out["brand_landing"] = {"brand": " or ".join(h.name for h in brand_hits_core),
                                        "categories": [{"family": f, "label": l_, "count": c_}
                                                       for f, l_, c_ in counts_by_brand(cat, "", cov_)[:12]]}
        out["requirements"] = ([{"key": "size:" + s_, "kind": "size", "label": f"{s_} in.", "status": "applied",
                                 "fixed": True} for s_ in pq.sizes if s_ in SIZE_ORDER] if n else []) + \
            [r.to_dict() for r in reqs if r.status != "n/a"]
        # head-noun tiering: for "water heater", heaters before water-heater connectors; then, within each tier, the
        # products that also carry the qualifier's words ("tapping sleeve ... for ductile iron": ductile iron first)
        if n and qh and adv_pool is None and not any_exact and pq.style != "part":
            hm = np.array([any(head_match(h, qh) for h in self.heads(int(i))) for i in idx])
            tier = hm.sum() >= 5 and (~hm).any()
            tw = [w for w in (ph.tail_words if ph else []) if not w[:1].isdigit() and w not in phmod.DROP]
            tc = np.array([sum(w in self._toks(int(i)) for w in tw) for i in idx]) if tw else np.zeros(n, int)
            if tier or tc.any():
                o2 = np.lexsort((np.arange(n), -tc, ~hm if tier else np.zeros(n, bool)))
                idx, X = idx[o2], X[o2]
                if exp:
                    exp = {k: v[o2] for k, v in exp.items()}
                if tier:
                    out["head_tiered"] = sorted(qh)
        cap = int((ms or {}).get("layout", {}).get("brand_cap", 0))
        if cap and n > cap and adv_pool is None and not any_exact and not pq.brands and pq.style != "part":
            perm = diversify([str(b_) for b_ in e.catalog.brand[idx]], cap, groups=[str(f_) for f_ in e.catalog.family[idx]])
            if perm != list(range(n)):
                idx, X = idx[perm], X[perm]
                if exp:
                    exp = {k: v[perm] for k, v in exp.items()}
                out["diversified"] = cap
        pinned_set: set = set()
        if (pins or hidden) and adv_pool is None and n:
            perm, pinned_set = place_pins(idx, e.catalog, pins, hidden)
            idx, X = idx[perm], X[perm]
            if exp:
                exp = {k: v[perm] for k, v in exp.items()}
            n = len(idx)
        # repair parts listed "for <model>" (flushometer kits, aquastats, control boards): hundreds match a broad search
        # and the shopper wants the one for their unit. Ask what it's for; a model number filters to parts that fit
        # (v2.0)
        if n and adv_pool is None and not any_exact:
            fm = [fit_model(str(t)) for t in cat.title[idx[:200]]]
            top_ = fm[:40]
            if sum(1 for x in top_ if x) >= 0.6 * len(top_) and len({x for x in fm if x}) >= 8:
                out["fits"] = {"ask": True, "example": next(x for x in fm if x), "n": int(n)}
        fq = compact_id(fits or "").lower()
        if n and len(fq) >= 3:
            allm = [fit_model(str(t)) for t in cat.title[idx]]
            keep = np.array([bool(m_) and fq in compact_id(m_).lower() for m_ in allm], bool)
            if not keep.any():           # a unit brand typed instead ("Moen"): that brand's parts
                keep = np.array([str(b_).lower() == fits.strip().lower() for b_ in cat.brand[idx]], bool)
            f_ = out.setdefault("fits", {"ask": True, "example": next((x for x in allm if x), None), "n": int(n)})
            f_.update(applied=fits.strip()[:40], matched=int(keep.sum()), none=not keep.any())
            if keep.any():
                idx, X = idx[keep], X[keep]
                if exp:
                    exp = {k: v[keep] for k, v in exp.items()}
                n = len(idx)
        # v2.4 (review G11): qualification is the last word. After pins, history, affinity, availability, crosses and
        # learned choices, every product left meets every applied requirement and size, or it leaves the page
        if n and adv_pool is None and not fallback:
            # a page already labeled as the closest products ("no union matches every spec": sizes relaxed, and said)
            # keeps its labeled relaxation; the requirements still hold
            relaxed_sizes = ident.get("kind") == "spec_miss"
            bad = self._qualify(idx, reqs, [] if relaxed_sizes else [s_ for s_ in pq.sizes if s_ in SIZE_ORDER],
                                np.asarray(X[:, F["exact_id"]] > 0))
            if bad.any():
                out["qualification"] = {"removed": int(bad.sum()),
                                        "skus": [str(e.catalog.sku[i]) for i in idx[bad][:10]]}
                idx, X = idx[~bad], X[~bad]
                if exp:
                    exp = {k: v[~bad] for k, v in exp.items()}
                n = len(idx)
        if brand_only and n:       # equipment before repair parts (sorted last, after every other ordering)
            part = np.array([bool(fit_model(str(cat.title[i]))) or " for " in str(cat.title[i]) for i in idx], bool)
            if part.any() and (~part).any():
                o2 = np.argsort(part, kind="stable")
                idx, X = idx[o2], X[o2]
                if exp:
                    exp = {k: v[o2] for k, v in exp.items()}
        out["candidates"] = int(n)
        self._notices(out, q_prod, pq, idx, X, n, any_exact, fallback, qh, sees, adv_pool is not None, ident, reqs, ph)
        # a part number one slip from one we have, or part of longer ones (v2.43): suggested, never substituted
        idm = getattr(e.retriever, "idmatch", None)
        if idm is not None and not any_exact and not _nested and adv_pool is None and model_tokens(q_prod):
            dym = []
            for tok in model_tokens(q_prod)[:2]:
                for sgg in idm.suggest(tok):
                    j = sgg["idx"]
                    dym.append({"kind": "part", "typed": tok, "q": str(cat.mfr_part[j]), "label": str(cat.mfr_part[j]),
                                "sku": str(cat.sku[j]), "title": str(cat.title[j]), "why": sgg["why"]})
            if dym:
                out["did_you_mean"] = dym[:3]
                nt = out.get("notice") or {}
                if nt.get("kind") in (None, "no_exact_model", "phrase_miss", "partial", "not_carried"):
                    out["notice"] = {"kind": "no_exact_model", "terms": [d["typed"] for d in dym],
                                     "text": f"No exact match for “{dym[0]['typed']}”. Did you mean "
                                             + " or ".join(f"{d['label']} ({d['title']})" for d in dym[:3])
                                             + "? The products below are related, not that number."}
            else:
                for tok in model_tokens(q_prod)[:1]:
                    ci, cn = idm.contains(tok, 60)
                    if cn:
                        keep = set(ci)
                        m_ = np.array([int(i) in keep for i in idx], bool) if n else np.zeros(0, bool)
                        if m_.any():
                            o2 = np.argsort(~m_, kind="stable")[: int(m_.sum())]
                            idx, X = idx[o2], X[o2]
                            if exp:
                                exp = {k: v[o2] for k, v in exp.items()}
                            n = len(idx)
                            out["id_contains"] = {"typed": tok, "count": cn}
                            out["notice"] = {"kind": "id_contains", "terms": [tok],
                                             "text": f"No part number is exactly “{tok}”. "
                                                     + (f"These {cn} part numbers contain it." if cn > 1 else
                                                        "This part number contains it.") + " Check it's the one you need."}
        # a line with no stand-in here ("megapress": steel press fittings; copper press ones don't fit steel pipe):
        # nothing on the page, and the line's own note says why (v2.42)
        if line_hit is not None and not line_hit.carried and line_hit.kind == "none" and not any_exact:
            out["notice"] = {"kind": "line_miss", "terms": [line_hit.line], "text": line_hit.note or
                             f"No {line_hit.brand} {line_hit.line} in the demo catalog. Ask your branch."}
            out["line"] = line_hit.to_dict()
            out["items"], out["total"], out["candidates"] = [], 0, 0
            idx, X, n = idx[:0], X[:0], 0
            if exp:
                exp = {k: v[:0] for k, v in exp.items()}
        # a brand and a model number the catalog lacks ("watts 911"): the brand's products, not other brands' parts
        # that fit some 911 (v2.42)
        if brand_req is not None and brand_req.status == "unmet" and not _nested and not any_exact:
            rest_t = [t for t in normalize(core) if t not in (brand_req.words or set())]
            if rest_t and all(any(ch.isdigit() for ch in t) or len(t) <= 2 for t in rest_t):
                try:
                    alt = self.results(brand_req.label.split(" or ")[0], p, branch=branch, quick=quick, filters=filters,
                                       price_min=price_min, price_max=price_max, radius=radius,
                                       personalize=personalize, llm="off", merch=merch, relax=relax, _nested=True)
                except Exception:  # noqa: BLE001
                    alt = None
                if alt and alt.get("items"):
                    model = " ".join(w for w in core.split() if any(c.isdigit() for c in w)) or core
                    alt["query"] = q
                    alt["model_miss"] = {"brand": brand_req.label, "model": model}
                    if spelled:
                        alt["spelling"] = spelled
                    alt["notice"] = {"kind": "model_miss",
                                     "text": f"No {brand_req.label} {model} in the demo catalog. These are "
                                             f"{brand_req.label} products; ask your branch for the exact model."}
                    alt["timings"] = {"total_ms": _ms(t0)}
                    return alt
        # a carried brand that doesn't make the product ("webstone flange"): say so, show other brands', and name what
        # the brand does make here
        if brand_req is not None and brand_req.status == "unmet" and n and not any_exact:
            from webbsearch.retrieval.brands import counts_by_brand
            made = counts_by_brand(cat, "", brand_cov)[:3]
            rest = " ".join(w for w in core.split() if not (set(normalize(w)) and set(normalize(w)) <= (brand_req.words or set())))
            out["notice"] = {"kind": "brand_miss",
                             "text": f"No {brand_req.label} {rest} in the demo catalog. These are other brands'; "
                                     f"{brand_req.label} here makes " + ", ".join(l_.lower() for _, l_, _ in made) + "."}
            out["brand_elsewhere"] = {"brand": brand_req.label,
                                      "categories": [{"family": f, "label": l_, "count": c_} for f, l_, c_ in made]}
        # brands (v2.42): the typed brand's products fill the grid; the best of other brands follow in their own row
        if other_idx is not None and len(other_idx) and not (line_hit is not None and line_hit.carried):
            out["other_brands"] = {"label": "Other brands", "brand": brand_req.label,
                                   "items": [self._mini(int(i), p) for i in other_idx]}
        # a brand F.W. Webb sells that the demo catalog lacks ("ao smith water heater"): the same product, other brands
        nb = [h for h in (bmast.detect(core) if bmast is not None else []) if not h.carried]
        if nb and not _nested and not any_exact:
            ct = list(normalize(core))
            span_t = {t for h in nb for t in ct[h.span[0]:h.span[1]]}
            rest = " ".join(w for w in core.split() if not (set(normalize(w)) and set(normalize(w)) <= span_t))
            words = [w for w in normalize(rest) if w.isalpha() and len(w) >= 3]
            if not words and nb[0].makes:           # the brand alone ("kohler", "fernco 4 inch"): what it's known for
                rest = f"{rest} {nb[0].makes}".strip()
                words = [nb[0].makes]
            alt = None
            if words:
                try:
                    alt = self.results(rest, p, branch=branch, personalize=personalize, llm="off", _nested=True)
                except Exception:  # noqa: BLE001
                    alt = None
            # the brand's own words only find look-alikes ("smith" in Jay R. Smith): other brands' products of the
            # same kind are the page, and it says so; when the rest of the search has no clean match either, the page
            # says the brand isn't carried and shows nothing
            if alt and alt.get("items") and (alt.get("notice") or {}).get("kind") in (None, "line_miss"):
                alt["query"] = q
                alt["brand_not_carried"] = {"brand": nb[0].name, "searched": rest}
                alt["notice"] = {"kind": "brand_not_carried", "terms": [nb[0].name],
                                 "text": f"The demo catalog doesn't carry {nb[0].name}. These are other brands' "
                                         f"matches for “{rest}”; ask your branch for {nb[0].name}."}
                alt["timings"] = {"total_ms": _ms(t0)}
                return alt
            out["notice"] = {"kind": "brand_not_carried", "terms": [nb[0].name],
                             "text": f"The demo catalog doesn't carry {nb[0].name}"
                                     + (f", and nothing else here is a clear match for “{rest}”" if words else "")
                                     + f". Ask your branch for {nb[0].name}"
                                     + ("." if words else ", or search for the kind of product to see other brands.")}
            out["brand_not_carried"] = {"brand": nb[0].name, "searched": rest if words else ""}
            out["items"], out["total"] = [], 0
            idx, X, n = idx[:0], X[:0], 0
            if exp:
                exp = {k: v[:0] for k, v in exp.items()}

        if n == 0:
            out["timings"] = {"total_ms": _ms(t0)}
            return out
        attrs = [cat.attrs(int(i)) for i in idx]
        fam = cat.family[idx]
        hv = e.hvac
        for r_, i in enumerate(idx):  # derived outdoor-unit specs (cabinet, discharge, EER2, HSPF2)
            if fam[r_] in hv.OUTDOOR:
                attrs[r_] = {**attrs[r_], **hv.attrs_extra(int(i))}
        brand = cat.brand[idx]
        title = cat.title[idx]
        skus = [str(x) for x in cat.sku[idx]]
        bmf = getattr(e, "brands", None)
        vals = {
            "family": list(fam), "brand": [bmf.facet_name(str(b_)) for b_ in brand] if bmf is not None else list(brand),
            "series": [a.get("series") for a in attrs],
            "size": [a.get("size") for a in attrs],
            "type": [a.get("type") for a in attrs],
            "connection": [connection_of(a) for a in attrs],
            "material": [material_group(t, a, b_) for t, a, b_ in zip(title, attrs, brand)],
        }
        prices = np.array([e.accounts.price(p, int(i))["price"] or float(cat.list_price[i]) for i in idx], np.float32)
        av = e.availability.summary(idx, branch, radius)
        bought = self._purchased(p) if personal or p.is_authenticated else {}
        on_list = self._list_skus_for(p)
        qm = {
            "nearby": (av["at_branch"] + av["nearby_qty"]) > 0,
            "instock": av["at_branch"] > 0,
            "purchased": np.array([s_ in bought for s_ in skus]),
            "lists": np.array([s_ in on_list for s_ in skus]),
            "leadfree": np.array(["lead-free" in t.lower() or "leadfree" in t.lower() for t in title]),
        }
        fm = {k: np.array([v in set(filters[k]) for v in vals[k]]) if filters.get(k) else np.ones(n, bool)
              for k in FACETS}
        # dynamic spec filters: "axis=value"
        spec_sel: dict[str, set] = defaultdict(set)
        spec_rng: dict[str, tuple] = {}
        for sv in filters.get("spec", []):
            if "~" in sv:                         # a range on a numeric axis: "gal~40~75" (v2.42)
                k, lo_, hi_ = (sv.split("~") + ["", ""])[:3]
                try:
                    spec_rng[k] = (float(lo_) if lo_ else None, float(hi_) if hi_ else None)
                except ValueError:
                    pass
            elif "=" in sv:
                k, v = sv.split("=", 1)
                spec_sel[k].add(v)
        spec_ax: dict[str, np.ndarray] = {}     # each spec axis's own mask, so its counts leave out its own choice
        for k, vs in spec_sel.items():
            spec_ax[k] = np.array([a.get(k) in vs for a in attrs], bool)
        for k, (lo_, hi_) in spec_rng.items():
            nv = [axis_num(a.get(k)) for a in attrs]
            spec_ax[k] = spec_ax.get(k, np.ones(n, bool)) & np.array(
                [v_ is not None and (lo_ is None or v_ >= lo_ - 1e-9) and (hi_ is None or v_ <= hi_ + 1e-9) for v_ in nv], bool)
        spec_mask = np.ones(n, bool)
        for m_ in spec_ax.values():
            spec_mask &= m_
        price_mask = np.ones(n, bool)
        if price_min is not None:
            price_mask &= prices >= price_min
        if price_max is not None:
            price_mask &= prices <= price_max
        if words_price and not price_mask.any():
            # nothing at that price: the page stays, sorted by price, and says so
            lo, hi = buy.price
            say = (f"under ${hi:,}" if lo is None else f"over ${lo:,}" if hi is None else f"between ${lo:,} and ${hi:,}")
            out["notice"] = {"kind": "price_miss", "text": f"Nothing here is {say}. These are the closest; "
                                                           "sorted by price."}
            out["sort_hint"] = "price_asc" if hi is not None else "price_desc"
            price_mask[:] = True
            price_min = price_max = None
        quick_mask = np.ones(n, bool)
        for k in quick:
            quick_mask &= qm[k]
        base = quick_mask & spec_mask & price_mask

        def all_but(skip: str | None) -> np.ndarray:
            m = base.copy()
            for k in FACETS:
                if k != skip:
                    m &= fm[k]
            return m

        final = all_but(None)
        # quick-filter counts: with everything else applied except that quick filter itself
        facet_m = np.ones(n, bool)
        for k in FACETS:
            facet_m &= fm[k]
        for k in QUICK:
            m = spec_mask & price_mask & facet_m
            for j in quick:
                if j != k:
                    m &= qm[j]
            out["quick"][k] = int((m & qm[k]).sum())
        if not p.is_authenticated:
            out["quick"]["purchased"] = None
            out["quick"]["lists"] = None
        # facets (disjunctive)
        facets = {}
        for k in FACETS:
            m = all_but(k)
            cnt = Counter(v for v, ok in zip(vals[k], m) if ok and v)
            sel = set(filters.get(k, []))
            items = [{"value": v, "count": c, "selected": v in sel} for v, c in cnt.most_common(40)]
            for v in sel - set(cnt):
                items.append({"value": v, "count": 0, "selected": True})
            if k == "size":
                items.sort(key=lambda x: size_key(x["value"]))
            if k == "family":
                for it in items:
                    hit = np.flatnonzero(fam == it["value"])
                    # a selected category none of the candidates is in (a stale link) keeps its own name
                    it["label"] = cat.cat3[idx[int(hit[0])]] if len(hit) else it["value"].replace("_", " ").title()
            facets[k] = items
        pm = all_but(None) if price_min is None and price_max is None else (quick_mask & spec_mask & facet_m)
        pv = prices[pm]
        if len(pv):
            lo, hi = float(np.percentile(pv, 1)), float(np.percentile(pv, 99))
            edges = np.geomspace(max(lo, 0.5), max(hi, lo + 1, 1.0), 13)
            hist, _ = np.histogram(np.clip(pv, edges[0], edges[-1]), bins=edges)
            uo = Counter(str(u_).upper() for u_, ok in zip(cat.uom[idx], pm) if ok)
            facets["price"] = {"min": round(lo, 2), "max": round(hi, 2), "edges": [round(float(x), 2) for x in edges],
                               "hist": hist.tolist(), "sel": [price_min, price_max],
                               # the price filter compares the price per unit sold: pipe per foot, fittings each
                               "uoms": dict(uo.most_common(4))}
        # dominant family and its spec axes (Mouser/Digi-Key-style parametric facets)
        # dominant family: rank-weighted share of what the user will actually see first
        fam_final = Counter()
        for r_, (f, ok) in enumerate(zip(fam, final)):
            if ok:
                fam_final[f] += 1.0 / math.sqrt(r_ + 1)
        dom = fam_final.most_common(1)[0][0] if fam_final else None
        if dom:
            dm = final & (fam == dom)
            axes = Counter()
            ax_vals: dict[str, Counter] = defaultdict(Counter)
            for a, ok in zip(attrs, dm):
                if ok:
                    for k2, v in a.items():
                        if v:
                            ax_vals[k2][v] += 1
            if spec_ax:            # an axis with a choice made counts without that choice (the others still apply)
                for k2 in spec_ax:
                    m_ = quick_mask & price_mask & facet_m & (fam == dom)
                    for k3, mm in spec_ax.items():
                        if k3 != k2:
                            m_ &= mm
                    ax_vals[k2] = Counter(a.get(k2) for a, ok in zip(attrs, m_) if ok and a.get(k2))
            spec = []
            for k2, cnt in ax_vals.items():
                if k2 in ("size", "type", "connection", "end", "material", "series", "cabinet") or not (2 <= len(cnt) <= (80 if k2 in NUM_AXES else 24)):
                    continue
                vals_sorted = sorted(cnt.items(), key=lambda kv: (size_key(kv[0]) if k2 in ("gal", "btu", "ton", "mfd", "mbh", "hp", "amp", "dia", "length") else 0, -kv[1]))
                ent = {"key": k2, "label": AXIS_LABEL.get(k2, k2.replace("_", " ").title()),
                       "values": [{"value": v, "count": c, "selected": v in spec_sel.get(k2, set())}
                                  for v, c in vals_sorted]}
                if k2 in NUM_AXES:
                    per = Counter()
                    for v_, c_ in cnt.items():
                        x_ = axis_num(v_)
                        if x_ is not None:
                            per[round(x_, 4)] += c_
                    nums = sorted(per)
                    if len(nums) >= 4 and len(nums) <= 80:     # a range the rail can drag (v2.42)
                        ent["range"] = {"min": nums[0], "max": nums[-1], "unit": NUM_AXES[k2], "stops": nums,
                                        "counts": [per[x_] for x_ in nums], "sel": list(spec_rng.get(k2, (None, None)))}
                spec.append(ent)
            # the dominant family's specs, most useful first: numbers with a range, then the fewest choices
            spec.sort(key=lambda x: (0 if "range" in x else 1, len(x["values"])))
            facets["spec"] = spec[:6]
            # matrix axes: rows = a size-like axis, cols = type-like axis
            row_ax = next((a for a in ("size", "gal", "btu", "ton", "mfd", "mbh", "hp", "amp", "dims", "dia", "length", "gpf")
                           if 2 <= len(ax_vals.get(a, {})) <= 20), None)
            col_ax = next((a for a in ("type", "connection", "end", "fuel", "material", "vent", "volt", "finish", "pole", "class")
                           if a != row_ax and 2 <= len(ax_vals.get(a, {})) <= 14), None)
            if row_ax is None:  # e.g. one capacitor rating: compare brands across types instead
                row_ax = "_brand"
            j = int(np.flatnonzero(fam == dom)[0])
            out["dominant"] = {"family": dom, "label": cat.cat3[idx[j]], "axes": sorted(ax_vals, key=lambda a: -len(ax_vals[a])),
                               "matrix": {"rows": row_ax, "cols": col_ax}}
        out["facets"] = facets
        # one name, several companies ("mueller"): the page asks which (v2.42)
        ch = next((h for h in brand_hits_core if h.choose), None)
        if ch is not None and not filters.get("brand"):
            bc = Counter(str(b_) for b_, ok in zip(brand, final) if ok)
            opts = [{"value": bmf.facet_name(b_) if bmf else b_, "count": bc.get(b_, 0)} for b_ in ch.choose]
            if sum(1 for o in opts if o["count"]) >= 2:     # a real choice: two or more of them have matches
                out["brand_choice"] = {"typed": ch.name, "options": sorted([o for o in opts if o["count"]],
                                                                           key=lambda o: -o["count"])}
        # tiles: families when mixed, otherwise the dominant family's types
        fams_top = Counter(f for f, ok in zip(fam, final) if ok)
        if len(fams_top) >= 2 and not filters.get("family"):
            for f, c in fams_top.most_common(8):
                j = int(np.flatnonzero(fam == f)[0])
                out["tiles"].append({"facet": "family", "value": f, "label": cat.cat3[idx[j]], "count": c,
                                     "family": f, "type": vals["type"][j], "material": vals["material"][j]})
        elif dom:
            tc = Counter(t for t, f, ok in zip(vals["type"], fam, final) if ok and f == dom and t)
            if len(tc) >= 2 and not filters.get("type"):
                for t, c in tc.most_common(12):
                    j = next(k for k in range(n) if vals["type"][k] == t and fam[k] == dom)
                    out["tiles"].append({"facet": "type", "value": t, "label": t, "count": c, "family": dom,
                                         "type": t, "material": vals["material"][j]})
        # items
        keep = np.flatnonzero(final)[:MAX_ITEMS]
        out["total"] = int(final.sum())
        items = []
        tiers = None
        if getattr(e, "extended", None) is not None and len(keep):
            need_ = max(1, buy.qty or 1)
            tiers = e.extended.coded_tiers(idx[keep], branch, need_, acfg)
            bname_ = str(e.availability.names[tiers["branch"]]).split(",")[0]
        for kk, r in enumerate(keep.tolist()):
            i = int(idx[r])
            pr = e.accounts.price(p, i)
            hb = e.accounts.history_badge(p, i)
            items.append({
                **({"tier": e.extended.card(str(tiers["tier"][kk]), tiers["lo"][kk], tiers["hi"][kk], brand[r], bname_,
                                            tiers["at_branch"][kk], need_)} if tiers is not None else {}),
                "rank": r + 1, "sku": skus[r], "title": title[r], "brand": brand[r], "mfr_part": cat.mfr_part[i],
                "family": fam[r], "cat3": cat.cat3[i], "cat2": cat.cat2[i], "attrs": attrs[r], "uom": cat.uom[i],
                "price": pr["price"], "price_basis": pr["basis"], "list_price": float(cat.list_price[i]),
                "stock_tier": cat.stock_tier[i], "status": cat.status[i],
                "material": vals["material"][r], "connection": vals["connection"][r],
                "avail": {"at_branch": int(av["at_branch"][r]), "nearby_qty": int(av["nearby_qty"][r]),
                          "nearby_branches": int(av["nearby_branches"][r]), "eta": av["eta"][r]},
                "history": hb, "on_list": bool(qm["lists"][r]), "leadfree": bool(qm["leadfree"][r]),
                "exact": bool(X[r, F["exact_id"]] > 0),
                # "12 today" with 8 here: 8 today, 4 by transfer (v2.43)
                **({"split": {"need": out["split_need"], "today": int(av["at_branch"][r]),
                              "transfer": int(min(out["split_need"] - int(av["at_branch"][r]), int(av["nearby_qty"][r])))}}
                   if out.get("split_need") and int(av["at_branch"][r]) < out["split_need"] else {}),
                "install_note": hv.install_note(i) if fam[r] in hv.OUTDOOR else None,
                "reasons": e.service._reasons(X[r], i, p, pq)[:3],
                "pinned": i in pinned_set,
                **({"badge": badges.get(skus[r], "Featured")} if i in pinned_set else {}),
                **({"adj": {k: round(float(v[r]), 2) for k, v in exp.items() if abs(float(v[r])) >= 0.005}} if exp else {}),
            })
        out["items"] = items
        out["model"] = "personal" if personal else "anonymous"
        # AHRI system matchups for the top outdoor unit on the page (MatchPro)
        top_out = next((it for it in items[:12] if it["family"] in hv.OUTDOOR), None)
        if top_out and dom in hv.OUTDOOR:
            j = cat.sku_to_idx[top_out["sku"]]
            s_ = hv.spec(j)
            out["hvac"] = {"sku": top_out["sku"], "title": top_out["title"], "spec": s_, "checks": hv.checks(s_),
                           "matchups": hv.matchups(j, limit=4),
                           "zone": climate_zone((out["branch"] or {}).get("lat"))}
            if p.is_authenticated and not p.sees_prices:
                for m in out["hvac"]["matchups"]:
                    m["indoor"]["list_price"] = None
        out["timings"] = {"total_ms": _ms(t0)}
        return out

    # ------------------------------------------------------------------ mini PDP
    def product(self, sku: str, p: Principal, branch: str | None = None) -> dict | None:
        e = self.e
        cat = e.catalog
        i = cat.sku_to_idx.get(str(sku))
        if i is None:
            return None
        branch = branch or (p.home_branch if p.is_authenticated else None)
        card = cat.card(i)
        a = card["attrs"]
        card["price"] = e.accounts.price(p, i)
        card["material"] = material_group(card["title"], a, card.get("brand"))
        card["connection"] = connection_of(a)
        card["history"] = e.accounts.history_badge(p, i)
        card["recent_orders"] = []
        if p.is_authenticated and p.can("orders"):
            card["recent_orders"] = e.accounts.find_orders(p, sku=str(sku), limit=4)
        card["availability"] = e.availability.by_branch(i, branch, k=6)
        card["map"] = e.availability.map_all(i, branch)
        card["branch"] = e.availability.branch_info(branch)
        card["eta"] = e.availability.summary(np.array([i]), branch)["eta"][0]
        # frequently bought with: co-occurrence in orders containing this SKU (all accounts)
        acc = e.accounts
        orders = list(acc._orders_by_sku.get(str(sku), ()))[:600]
        co = Counter()
        if not hasattr(self, "_line_skus"):
            self._line_skus = acc.lines["sku"].to_numpy()
        for oid in orders:
            pos = acc._line_pos.get(oid)
            if pos is not None:
                for s2 in self._line_skus[pos]:
                    if s2 != str(sku):
                        co[s2] += 1
        fbt = []
        for s2, c in co.most_common(8):
            j = cat.sku_to_idx.get(s2)
            if j is not None and len(fbt) < 6:
                fbt.append(self._mini(j, p) | {"together": c})
        card["bought_with"] = fbt
        # alternates (same specs, other brands) and other sizes (same brand/type, other size)
        alt, sizes = [], []
        pq = e.parser.parse(" ".join(w for w in card["title"].split()[1:]))
        c2 = e.retriever.retrieve(pq, filters={"family": [card["family"]]}, lanes=("lexical",), limit=150)
        # a repair part is defined by the equipment it fits ("Gasket for GRR849 Series"): only the same series is the
        # same spec, so another brand's part for other equipment is never offered as an alternate
        fits_series = bool(a.get("series")) and bool(re.search(r"\bfor\b", card["title"]))
        key = {k: v for k, v in a.items() if k not in ("series",) or fits_series}
        for j in c2.idx.tolist():
            if j == i:
                continue
            aj = cat.attrs(j)
            same_spec = bool(key) and all(aj.get(k) == v for k, v in key.items())
            if same_spec and cat.brand[j] != card["brand"] and len(alt) < 5:
                alt.append(self._mini(j, p))
            elif cat.brand[j] == card["brand"] and a.get("size") and aj.get("size") != a.get("size") and \
                    all(aj.get(k) == v for k, v in key.items() if k != "size"):
                sizes.append({"sku": str(cat.sku[j]), "size": aj.get("size")})
        sizes = sorted({s["size"]: s for s in sizes}.values(), key=lambda s: size_key(s["size"]))
        card["alternates"] = alt
        card["alternates_basis"] = [k.replace("_", " ") for k in key]
        card["other_sizes"] = sizes
        docs = list(self.docs_by_sku.get(str(sku), []))
        docs += self.docs_by_brand_cat.get((card["brand"].split(" ")[0], card["category"][2]), [])[:3]
        seen, dd = set(), []
        for d in docs:
            if d["content_id"] not in seen:
                seen.add(d["content_id"]); dd.append(d)
        card["documents"] = dd[:5]
        try:                                  # the Knowledge Center's documents for this item (v2.1)
            kln = e.knowledge.lib.line_for_sku(sku)
            kdocs = e.knowledge.documents_for(sku) if kln else []
            # guests learn that documents exist (a sign-in prompt), not what they are
            card["kc"] = {"line": kln.id, "name": kln.name, "count": len(kdocs),
                          "documents": kdocs if p.is_authenticated else []} if kln else None
        except Exception:
            card["kc"] = None
        card["kits"] = e.kits.for_product(card["family"], card["title"])
        if e.hvac.is_outdoor(i):
            s_ = e.hvac.spec(i)
            card["install_note"] = e.hvac.install_note(i)
            card["attrs"] = {**card["attrs"], **e.hvac.attrs_extra(i)}
            card["hvac"] = {"spec": s_, "checks": e.hvac.checks(s_), "matchups": e.hvac.matchups(i, limit=8),
                            "zone": climate_zone((card["branch"] or {}).get("lat"))}
            if p.is_authenticated and not p.sees_prices:
                for m in card["hvac"]["matchups"]:
                    m["indoor"]["list_price"] = None
        if p.is_authenticated and not p.sees_prices:
            card["kits"] = [k | {"min_total": None, "max_total": None} for k in card["kits"]]
        return card

    def _mini(self, j: int, p: Principal) -> dict:
        cat = self.e.catalog
        a = cat.attrs(j)
        return {"sku": str(cat.sku[j]), "title": cat.title[j], "brand": cat.brand[j], "family": cat.family[j],
                "attrs": a, "price": self.e.accounts.price(p, j)["price"], "uom": cat.uom[j],
                "material": material_group(cat.title[j], a, cat.brand[j]), "stock_tier": cat.stock_tier[j]}


def _ms(t: float) -> float:
    return round((time.perf_counter() - t) * 1000, 1)
