"""Trade-aware text analysis shared by indexing and querying.

Distribution search lives or dies on a handful of normalizations that generic
analyzers get wrong:
  * sizes: 3/4", 3/4in, 3/4 inch, .75  -> "3/4";  1 1/4 / 1-1/4" -> "1-1/4"
  * glued units: 50gal -> 50 gal, 45/5mfd -> 45/5 mfd
  * trade abbreviations: ell/el -> elbow, cplg -> coupling, fpt/fnpt -> fip ...
  * part numbers: LF25AUB-Z3 / lf25aub z3 / LF25AUBZ3 must all collide -> compact ids

The OpenSearch backend reproduces this with a custom analyzer (char_filter +
synonym_graph + word_delimiter_graph); see deploy/opensearch/products_template.json.
Keeping one Python implementation here lets query parsing, the local engine,
the evaluation harness and the synthetic data generator agree exactly.
"""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

# ---------------------------------------------------------------------------
# Canonicalization: one-to-one or one-to-many token rewrites applied to both
# documents and queries.
# ---------------------------------------------------------------------------
CANON: dict[str, str] = {
    # fittings
    "ell": "elbow", "el": "elbow", "elb": "elbow", "elbows": "elbow", "ells": "elbow",
    "cplg": "coupling", "cpl": "coupling", "coup": "coupling", "cplgs": "coupling",
    "adp": "adapter", "adapt": "adapter", "adaptor": "adapter", "adpt": "adapter",
    "reducer": "reducing", "rdcr": "reducing", "rdc": "reducing",
    "fpt": "fip", "fnpt": "fip", "mpt": "mip", "mnpt": "mip",
    "solder": "sweat", "swt": "sweat",
    "st": "street", "str": "street",
    # materials
    "cu": "copper", "copr": "copper", "blk": "black", "galv": "galvanized",
    "brs": "brass", "ss": "stainless", "sst": "stainless", "cpvc": "cpvc",
    "lf": "leadfree", "nl": "leadfree", "leadfree": "leadfree",
    "ci": "castiron",
    # equipment / fuels
    "elec": "electric", "lp": "propane", "lpg": "propane", "ng": "natgas",
    "wh": "water heater", "hwh": "water heater", "w/h": "water heater",
    "tstat": "thermostat", "stat": "thermostat",
    "circ": "circulator", "circulating": "circulator", "circulater": "circulator", "circulatior": "circulator",
    "hsi": "igniter", "ignitor": "igniter",
    "disposer": "disposal", "gd": "disposal",
    "condenser": "condensing", "ac": "air conditioner", "a/c": "air conditioner",
    "minisplit": "mini split", "mini-split": "mini split", "ductless": "mini split",
    "tp": "t&p", "rpz": "rpz", "bfp": "backflow", "wifi": "wi fi",
    "prv": "pressure reducing valve", "bv": "ball valve",
    "xtank": "expansion tank",
    # units
    "gallon": "gal", "gallons": "gal", "gals": "gal",
    "feet": "ft", "foot": "ft", "'": "ft",
    "amps": "amp", "volts": "volt", "vac": "volt",
    "tons": "ton", "btuh": "btu", "btus": "btu", "btu/h": "btu",
    "uf": "mfd", "µf": "mfd",
    "degree": "", "degrees": "", "deg": "",
    "inch": "", "inches": "", "inche": "", "in": "", "x": "", "by": "",
    # stopwords that hurt BM25 on short trade queries
    "for": "", "the": "", "a": "", "an": "", "and": "", "with": "", "of": "",
    "to": "", "w": "", "&": "", "-": "", "i": "", "need": "", "my": "", "looking": "", "what": "",
    "which": "", "do": "", "does": "", "want": "", "any": "", "some": "", "please": "", "im": "", "got": "",
}

# Index-time concept expansion: when a product carries the concept on the
# left, index the trade vocabulary on the right in a low-boost synonym field.
# In production this table is generated/curated by the Claude enrichment job
# (webbsearch/llm/enrich.py) and reviewed by product managers.
CONCEPT_SYNONYMS: dict[str, str] = {
    "push-to-connect": "sharkbite pushfit push connect",
    "press": "propress megapress xpress",
    "90 elbow": "90 ell",
    "pressure reducing valve": "prv reducing",
    "t&p relief valve": "t&p relief tp",
    "water heater": "wh hwh",
    "tankless": "on demand instantaneous",
    "capacitor": "cap run capacitor",
    "thermostat": "tstat stat",
    "circulator": "circ pump",
    "disposal": "disposer garbage",
    "igniter": "hsi ignitor",
    "flame sensor": "flame rod",
    "condensing unit": "condenser ac outdoor unit",
    "mini split": "ductless minisplit",
    "expansion tank": "xtank thermal expansion",
    "backflow": "bfp rpz dcva",
    "closet flange": "toilet flange",
    "no-hub": "nohub hubless",
    "pex": "pex tubing crosslinked",
    "contactor": "relay contactor",
    "condensate pump": "condensate removal",
    "sump pump": "sump",
    "gas connector": "appliance connector flex",
    "csst": "flex gas pipe tracpipe gastite",
}

# Trade numbers people type that the catalog never contains: Unicode vulgar
# fractions, sizes in words, "45+5" dual capacitors. Rewritten to the catalog
# notation before tokenizing (applied to documents too, where they are no-ops).
_VULGAR = {"½": "1/2", "¼": "1/4", "¾": "3/4", "⅛": "1/8", "⅜": "3/8", "⅝": "5/8", "⅞": "7/8",
           "⅓": "1/3", "⅔": "2/3"}
_VULGAR_RE = re.compile(r"(\d)?\s?([½¼¾⅛⅜⅝⅞⅓⅔])")
_INCH = r"(?:[\s-]*(?:inch(?:es)?\b|in\b\.?|\"|”))"
_WORD_SIZES = [   # longest first; "half" / "quarter" alone stay words (half coupling, quarter-turn)
    (r"\b(?:one|1)[\s-]+and[\s-]+(?:a[\s-]+)?half" + _INCH + "?", "1-1/2"),
    (r"\binch[\s-]+and[\s-]+a[\s-]+half\b", "1-1/2"),
    (r"\b(?:one|1)[\s-]+and[\s-]+(?:a[\s-]+)?quarter" + _INCH + "?", "1-1/4"),
    (r"\binch[\s-]+and[\s-]+a[\s-]+quarter\b", "1-1/4"),
    (r"\b(?:two|2)[\s-]+and[\s-]+(?:a[\s-]+)?half" + _INCH + "?", "2-1/2"),
    (r"\bthree[\s-]+quarters?" + _INCH + "?", "3/4"),
    (r"\b(?:one[\s-]+)?half" + _INCH, "1/2"),
    (r"\b(?:one[\s-]+)?quarter" + _INCH, "1/4"),
    (r"\b(?:three|3)[\s-]+eighths?" + _INCH + "?", "3/8"),
    (r"\b(?:five|5)[\s-]+eighths?" + _INCH + "?", "5/8"),
    (r"\b(?:seven|7)[\s-]+eighths?" + _INCH + "?", "7/8"),
    (r"\b(?:one[\s-]+)?eighth" + _INCH, "1/8"),
]
_WORD_SIZES = [(re.compile(p), r) for p, r in _WORD_SIZES]
_NUM_WORDS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "eight": "8", "ten": "10",
              "twelve": "12"}
_WORD_INCH = re.compile(r"\b(" + "|".join(_NUM_WORDS) + r")(?:[\s-]*(?:inch(?:es)?\b|\"|”))")
_DUAL_CAP = re.compile(r"\b(\d{1,3}(?:\.\d)?)\s*\+\s*(\d{1,2}(?:\.\d)?)\b")


# Accented letters fold to their base letter (Zoëller = Zoeller, électrique = electrique): canonical decomposition
# (NFD) then the combining marks dropped, so µ, ½, ™ and ″ are left for the rules below. A few letters NFD doesn't split.
_FOLD_EXTRA = str.maketrans({"ß": "ss", "ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE", "ł": "l", "Ł": "L",
                             "đ": "d", "Đ": "D", "ı": "i",
                             # dashes word processors and PDFs put in: hyphen, non-breaking hyphen, figure, en and em
                             # dashes, horizontal bar, minus, small and full-width hyphen-minus -> "-"
                             **{d: "-" for d in "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe58\ufe63\uff0d"}})


def clean_dashes(s: str) -> str:
    """Any dash a word processor or PDF puts in, as a hyphen (for raw text read outside normalize)."""
    return s if s.isascii() else s.translate(_FOLD_EXTRA_DASHES)


_FOLD_EXTRA_DASHES = str.maketrans({d: "-" for d in "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe58\ufe63\uff0d"})


def fold(s: str) -> str:
    if s.isascii():
        return s
    s = unicodedata.normalize("NFD", s.translate(_FOLD_EXTRA))
    return "".join(ch for ch in s if unicodedata.category(ch) != "Mn")


def prenormalize(s: str) -> str:
    """Lower-cased text -> the same text with trade numbers in catalog notation."""
    s = s.replace("⁄", "/").replace("×", "x")
    if any(ch in s for ch in _VULGAR):
        s = _VULGAR_RE.sub(lambda m: (m.group(1) + "-" if m.group(1) else " ") + _VULGAR[m.group(2)], s)
    if any(w in s for w in ("half", "quarter", "eighth", "inch", " in")):
        for rx, rep in _WORD_SIZES:
            s = rx.sub(" " + rep + " ", s)
        s = _WORD_INCH.sub(lambda m: " " + _NUM_WORDS[m.group(1)] + " ", s)
    if "+" in s:
        s = _DUAL_CAP.sub(r"\1/\2", s)
    return s


_MIXED_FRAC = re.compile(r"\b(\d+)[\s-]+(\d+/\d+)\b")
# a number glued to its unit (50gal, 30a, 24v); not inside a model or refrigerant code (r410a, xb300a)
_GLUED_UNIT = re.compile(r"(?<![a-z0-9.])(\d+(?:\.\d+)?)(gal|gpf|gpm|mfd|uf|btu|mbh|ton|ft|hp|v|a|lb|oz|psi|awg|mm|in)\b")
# refrigerants as one token: R-410A, R410A, r 410a -> r410a; R-22 -> r22 (not "410 amp")
_REFRIG = re.compile(r"\br[\s-]?(\d{2,3}[a-z]?)\b")
# sizes joined by x: 3/4x1/2, 1x3/4, 2x1-1/2 -> two sizes (dimensions like 16x25x5 or 2x4 stay one token)
_X_FRAC_FIRST = re.compile(r"(\d+/\d+)\s*x\s*(?=\d)")
_X_FRAC_SECOND = re.compile(r"(\d)\s*x\s*(?=\d+(?:-\d+)?/\d+)")
# the foot mark after a number: 10', 10’, 10′ -> 10 ft (5'6" -> 5 ft 6)
_FEET = re.compile(r"(\d)\s*['’′](?!['’′])")
# hyphenated words also as one word, right after their parts: pre-charged -> pre charged precharged, PEX-A -> pex a pexa
# (so "precharged" and "PEXA" match, and PEX-A isn't PEX-B); the last word of a phrase stays last
_COMPOUND = re.compile(r"(?<![a-z0-9])[a-z]+(?:-[a-z]+)+(?![a-z0-9])")
_DIMS = re.compile(r"^(\d+)x(\d+)(?:x(\d+))?$")
_THOUSANDS = re.compile(r"(\d),(\d{3})\b")
_K_SUFFIX = re.compile(r"\b(\d{1,3})k\b")
_UNIT_WORD = {"a": "amp", "v": "volt", "uf": "mfd", "in": ""}
_DECIMAL_SIZES = {".25": "1/4", ".375": "3/8", ".5": "1/2", ".75": "3/4", "0.25": "1/4",
                  "0.375": "3/8", "0.5": "1/2", "0.75": "3/4", "1.25": "1-1/4", "1.5": "1-1/2",
                  "2.5": "2-1/2"}
_TOKEN = re.compile(r"\d+-\d+/\d+|\d+/\d+|\d*\.\d+|t&p|a/c|w/h|btu/h|[a-z0-9µ]+")
_NON_ALNUM = re.compile(r"[^A-Z0-9]")


def _stem(tok: str) -> str:
    # Minimal plural stemmer; trade vocabulary is mostly nouns.
    if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss") and not tok[-2].isdigit():
        if tok.endswith("ies"):
            return tok[:-3] + "y"
        return tok[:-1]
    return tok


@lru_cache(maxsize=200_000)
def normalize(text: str) -> tuple[str, ...]:
    """Text -> canonical token tuple. Cached; queries repeat heavily."""
    if not text:
        return ()
    s = prenormalize(fold(text).lower())
    s = _FEET.sub(r"\1 ft ", s)
    s = s.replace("″", '"').replace("”", '"').replace("“", '"').replace("°", " ")
    s = s.replace('"', " ").replace("lead-free", "leadfree").replace("lead free", "leadfree")
    s = s.replace("no lead", "leadfree").replace("mini-split", "minisplit")
    s = _THOUSANDS.sub(r"\1\2", s)          # 60,000 -> 60000
    s = _K_SUFFIX.sub(r"\g<1>000", s)       # 60k -> 60000
    s = _REFRIG.sub(r"r\1", s)
    s = _GLUED_UNIT.sub(lambda m: f"{m.group(1)} {_UNIT_WORD.get(m.group(2), m.group(2))}", s)
    s = _MIXED_FRAC.sub(r"\1-\2", s)
    s = _X_FRAC_SECOND.sub(r"\1 x ", _X_FRAC_FIRST.sub(r"\1 x ", s))
    s = _COMPOUND.sub(lambda m: m.group(0) + " " + m.group(0).replace("-", ""), s)
    out: list[str] = []
    for tok in _TOKEN.findall(s):
        if tok in _DECIMAL_SIZES:
            out.append(_DECIMAL_SIZES[tok])
            continue
        m = _DIMS.match(tok)
        if m:  # 16x25x5 -> also 16x25 so "16x25 filter" matches
            out.append(tok)
            if m.group(3):
                out.append(f"{m.group(1)}x{m.group(2)}")
            continue
        tok = _stem(tok)
        rep = CANON.get(tok)
        if rep is None:
            out.append(tok)
        elif rep:
            out.extend(rep.split())
    return tuple(out)


def analyze(text: str) -> str:
    return " ".join(normalize(text))


def parts_only(toks) -> tuple[str, ...]:
    """Tokens without the joined forms normalize() adds after hyphenated words (pre charged precharged -> pre
    charged). For checks that need every searched word: the joined form is an alternative, not one more word."""
    toks = tuple(toks)
    return tuple(t for j, t in enumerate(toks) if not joined_at(toks, j))


def word_groups(toks) -> list[set]:
    """One set per searched word, for counting words: the word, plus the joined form it is part of (pre charged
    precharged -> {pre, precharged}, {charged, precharged}). A product covers the word if it has any of the set."""
    toks = tuple(toks)
    out: dict[int, set] = {}
    for j, t in enumerate(toks):
        n = join_len(toks, j)
        if n:
            for k in range(j - n, j):
                out[k].add(t)
        else:
            out[j] = {t}
    return [out[k] for k in sorted(out)]


def word_spans(toks) -> list[tuple[str, ...]]:
    """Tokens in searched-word order: a word alone, a hyphenated word as (parts..., joined form)."""
    toks = tuple(toks)
    out: list[tuple[str, ...]] = []
    for j, t in enumerate(toks):
        n = join_len(toks, j)
        if n and len(out) >= n and all(len(x) == 1 for x in out[-n:]):
            out[-n:] = [tuple(x[0] for x in out[-n:]) + (t,)]
        else:
            out.append((t,))
    return out


def _is_join(parts, t: str) -> bool:
    """t is the parts written together, allowing a short word the analyzer dropped between them (push connect
    pushtoconnect: "to" is a stopword)."""
    if not t.isalpha() or not all(p.isalpha() for p in parts):
        return False
    pos = 0
    for k, p in enumerate(parts):
        i = t.find(p, pos)
        if i < 0 or (k == 0 and i) or i - pos > 3:
            return False
        pos = i + len(p)
    return pos == len(t)


def join_len(toks, j: int) -> int:
    """How many tokens right before toks[j] it is the joined form of (2-4), or 0."""
    return next((n for n in (2, 3, 4) if j >= n and _is_join(toks[j - n:j], toks[j])), 0)


def joined_at(toks, j: int) -> bool:
    """Is toks[j] the joined form of the tokens right before it (definite purpose definitepurpose)?"""
    return bool(join_len(toks, j))


def compact_id(s: str) -> str:
    """Part-number key: uppercase alphanumerics only. 'LF25AUB-Z3 3/4' -> 'LF25AUBZ334'."""
    return _NON_ALNUM.sub("", fold(s).upper())


# a number and its unit, typed as one token, is a spec, never a part number: "5.8Ah", "40VA", "1/2hp", "18000btu" (v2.43)
NUM_UNIT = re.compile(r"^\d+(?:[.,]\d+)?(?:/\d+)?(?:v|vac|vdc|va|kva|a|ah|mah|amps?|w|kw|kwh|wh|hz|hp|gpm|gph|gpf|psi|gal|"
                      r"gallons?|mfd|uf|btu|btuh|mbh|tons?|ft|lbs?|oz|in|inch|mm|cm|cfm|rpm|awg|seer2?|db|sones?|deg|f|c)$", re.I)


def looks_like_part_number(tok: str) -> bool:
    """Alphanumeric mix of >=4 chars with at least one digit and one letter, or a
    long pure-digit run (UPC / Webb item #). A number with its unit ("5.8Ah") is a spec."""
    if NUM_UNIT.match(tok.strip()):
        return False
    t = compact_id(tok)
    if len(t) >= 8 and t.isdigit():
        return True
    return len(t) >= 4 and any(c.isdigit() for c in t) and any(c.isalpha() for c in t)


def expand_concepts(text_lower: str) -> str:
    extra = [v for k, v in CONCEPT_SYNONYMS.items() if k in text_lower]
    return " ".join(extra)


SIZE_RE = re.compile(r"^(\d+-\d+/\d+|\d+/\d+|\d+)$")
