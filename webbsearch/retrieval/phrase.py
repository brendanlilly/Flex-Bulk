"""What a product search asks for, read from its words (v1.8.1).

A contractor's search is a product phrase with other things around it:

    I need 12 | half inch lead free press ball valves | at Bedford today
    3/4 sweat zone valve | 24V normally closed | with end switch
    2 inch PVC sanitary tee | not a wye
    6 inch joint restraint | for C900 PVC pipe

`split()` takes off what is about buying rather than the product (a quantity, a branch, "in stock", "today",
"cheapest") and what the buyer rules out ("not a wye", "no heat pump"). The rest is what the engine searches.

`analyze()` finds the product phrase in that: the text before a qualifier ("for", "with", "under 60 in."), minus the
specs after the product noun ("24V normally closed") and the words that describe rather than name ("hydronic",
"replacement", "only"). Its last noun is the product (English trade phrases are head-final); the words before it
say which kind. Only the product phrase decides what the product is: a word nothing in the catalog carries there
("purge valve", "hose bibb") means the catalog doesn't have it. The same word in a qualifier ("with hose drain") is a
spec the page checks and, when it can't, says so.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

from webbsearch.retrieval.analyzer import joined_at, normalize

# ---------------------------------------------------------------------------------------------- buying and exclusions
_LEAD = re.compile(r"^\s*(?:(?:i|we)\s+(?:also\s+)?)?(?:need|want|needs|am\s+looking\s+for|looking\s+for|find(?:\s+me)?|get(?:\s+me)?|"
                   r"buy|grab)\b\s*", re.I)
# a question or a request around the product (v2.0): "do you have ...", "how much is a ...", "can I get ...". What's
# left is the product; the words never make a page "not carried"
_ASK = re.compile(r"^\s*(?:hi|hey|hello)?[,!]?\s*(?:"
                  r"(?:do|does)\s+(?:you(?:\s+guys)?|y'?all|webb|f\.?\s?w\.?\s+webb)\s+(?:have|carry|stock|sell|got)(?:\s+any)?|"
                  r"(?:have|got)\s+you\s+got(?:\s+any)?|(?:you|u)\s+(?:have|got|carry)(?:\s+any)?|got\s+any|"
                  r"is\s+there(?:\s+an?y?)?|are\s+there(?:\s+any)?|any\s+chance\s+you\s+have|"
                  r"(?:can|could|may)\s+(?:i|we)\s+(?:get|buy|order|have|grab|pick\s+up)|"
                  r"how\s+much\s+(?:is|are|for|does|do|would)(?:\s+(?:a|an|the|it\s+cost\s+for))?|"
                  r"what(?:'s|\s+is|\s+are)\s+the\s+(?:price|cost)s?\s+(?:of|for|on)|(?:price|cost|pricing)\s+(?:of|for|on)|"
                  r"where\s+(?:can|do)\s+(?:i|we)\s+(?:get|find|buy)|show\s+me(?:\s+your)?|"
                  r"(?:i'?m|i\s+am|we'?re|we\s+are)\s+(?:looking\s+for|after|shopping\s+for)|"
                  r"something\s+(?:to|for|that)|anything\s+(?:to|for|that)"
                  r")\b\s*", re.I)
_TRAIL_ASK = re.compile(r"\s*(?:\bplease\b|\bthanks?\b|\bthank\s+you\b|\bcost\b|\bprice\b|\bin\s+stock\s+anywhere\b)?\s*[?!.]*\s*$", re.I)
_ARTICLE = re.compile(r"^\s*(?:a|an|the|some|any)\s+", re.I)
_WORD_NUM = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
             "eleven": 11, "twelve": 12, "dozen": 12, "fifteen": 15, "twenty": 20}
_UNIT = (r"(?:inch(?:es)?|in\b|\"|ft|foot|feet|tons?|gal(?:lons?)?|hp|v\b|vac|volts?|amps?|a\b|psi|btu|mbh|mfd|uf|lbs?|"
         r"oz|gpm|gph|gpf|x\b|mm|cm|kw|w\b|watts?|seer2?|cfm|rpm|deg|degrees?|%|/|-|\.)")
# "12 half inch", "8 three quarter", "6 ball valves": a count, then a size word or a product word
_QTY = re.compile(rf"^\s*(\d{{1,4}})\s+(?!{_UNIT})(?=(?:half|quarter|three[\s-]+quarters?|one|[a-z]))", re.I)
_QTY_FRAC = re.compile(r"^\s*(\d{1,4})\s+(?=\d+/\d+)")          # "10 3/4 elbows" (a count only when > 5: 1 1/2 is a size)
_QTY_WORD = re.compile(r"^\s*(two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|dozen|fifteen|twenty)\s+"
                       r"(?!quarters?\b|inch|in\b|ft|foot|feet|ton|gal)(?=[a-z])", re.I)
_ONE_BRANCH = re.compile(r"\b(?:all\s+)?(?:at|from|in)\s+(?:one|a\s+single|the\s+same|a|one\s+single)\s+"
                         r"(?:branch|store|location|counter)\b", re.I)
_STOCK = re.compile(r"\b(?:in[\s-]+stock|on[\s-]+hand|on\s+the\s+shelf|(?:that\s+(?:is|are)\s+)?available(?:\s+now)?|"
                    r"(?:ready\s+)?(?:for\s+)?pick\s*-?\s*up|today|right\s+now|same[\s-]+day|"
                    r"near\s+(?:me|us|here|by)|nearby|close\s+(?:by|to\s+(?:me|us))|around\s+here)\b", re.I)
_PRICEY = re.compile(r"\b(?:the\s+)?(?:most\s+expensive|highest[\s-]+(?:price|cost)d?|priciest|top[\s-]+of[\s-]+the[\s-]+line)\b", re.I)
# a count written next to the product (v2.0): "qty 10", "(10)", "x10", "10 pcs", "2 dozen", "a box of 10"
_QTY_LABEL = re.compile(r"\b(?:qty|quantity|qnty)\s*[:#=]?\s*(\d{1,5})(?![\d/.-])\b", re.I)
_QTY_PAREN = re.compile(r"\s*\(\s*(?:qty\s*)?(\d{1,5})\s*(?:pcs?|ea)?\s*\)", re.I)
_QTY_X = re.compile(r"(?<=[a-z])\s*\bx\s*(\d{1,5})\s*$|\s+x(\d{1,5})\s*$", re.I)
_QTY_PCS = re.compile(r"(?<![\d/.-])\b(\d{1,5})\s*(?:pcs|ea|each)\b\.?", re.I)     # not "3 pc" / "3 piece": a 3-piece valve
_DOZEN = re.compile(r"\b(\d{1,3}|a|one|two|three|four|five|six|half\s+a)\s+dozen\b", re.I)
# "a box of 1/2 copper caps": how it's bought, said before the product. "PVC Cement Case of 12" names a 12-pack: kept
_BOX_OF = re.compile(r"^\s*(?:a\s+)?(?:box|bag|carton)\s+of\b(?!\s*\d+\b(?!/))()", re.I)
# a price in words needs a $ or "dollars": "under 30 inches wide" is a size (v2.01, Q005)
_AMT = r"(?:\$\s*(\d[\d,]*(?:\.\d{1,2})?)|(\d[\d,]*(?:\.\d{1,2})?)\s*(?:dollars|bucks|usd)\b)"
_PRICE_RANGE = re.compile(r"\b(?:between|from)?\s*" + _AMT + r"\s*(?:-|–|to|and)\s*" + _AMT, re.I)
_PRICE_MAX = re.compile(r"\b(?:under|below|less\s+than|no\s+more\s+than|at\s+most|max(?:imum)?|up\s+to|cheaper\s+than|"
                        r"<=?)\s*" + _AMT + r"|" + _AMT + r"\s*(?:or\s+(?:less|under|below)|max(?:imum)?)\b", re.I)
_PRICE_MIN = re.compile(r"\b(?:over|above|more\s+than|at\s+least|min(?:imum)?|>=?)\s*" + _AMT + r"|" + _AMT +
                        r"\s*(?:or\s+(?:more|over|above)|and\s+up)\b", re.I)


def _amt(*groups) -> float | None:
    for g in groups:
        if g:
            return float(g.replace(",", ""))
    return None


_CHEAP = re.compile(r"\b(?:the\s+)?(?:cheapest|least\s+expensive|lowest[\s-]+(?:price|cost)d?|lowest[\s-]+priced|best\s+price)\b", re.I)
_NEG = re.compile(r"\b(?:not\s+(?:an?\s+|the\s+|any\s+)?|no\s+(?!lead\b|hub\b)|but\s+not\s+|except\s+(?:for\s+)?|"
                  r"instead\s+of\s+(?:an?\s+)?|other\s+than\s+(?:an?\s+)?)([^,;]+?)(?=\s*(?:$|,|;|\s(?:and|but|with|for)\s))", re.I)

# trade shorthand typed in searches (from the Bulk order shorthand table; only the unambiguous ones)
SHORTHAND = {"std": "standard", "blk": "black", "flg": "flange", "flgs": "flange", "nip": "nipple", "nips": "nipple",
             "galv": "galvanized", "bfv": "butterfly", "scv": "check", "xh": "heavy", "wnf": "flange", "clo": "close",
             "fem": "female", "prog": "programmable", "cond": "condenser", "hdr": "header", "reg": "regulator",
             "tstat": "thermostat", "chk": "check", "brz": "bronze", "brs": "brass"}
UNITS = {"gal", "ft", "volt", "amp", "psi", "btu", "mbh", "ton", "hp", "mfd", "gpm", "gph", "gpf", "lb", "oz", "awg",
         "leadfree", "seer", "seer2", "watt", "kw", "cfm", "in", "inch", "inche", "sch", "schedule", "rpm", "od", "id"}


@dataclass
class Buying:
    qty: int | None = None
    stock: bool = False
    one_branch: bool = False
    branch: str | None = None          # branch id the search names ("at Bedford")
    branch_name: str | None = None
    cheapest: bool = False
    priciest: bool = False             # "most expensive": price high to low (v2.0)
    price: list | None = None          # [low, high] from "under $50", "between $20 and $40" (v2.01)
    exclusions: list[dict] = field(default_factory=list)   # [{"text": "heat pump", "words": ["heat", "pump"]}]
    removed: list[str] = field(default_factory=list)       # the phrases taken off, as typed

    def any(self) -> bool:
        return bool(self.qty or self.stock or self.one_branch or self.branch or self.cheapest or self.priciest
                    or self.exclusions or self.price)

    def to_dict(self) -> dict:
        return {"qty": self.qty, "stock": self.stock, "one_branch": self.one_branch, "branch": self.branch,
                "branch_name": self.branch_name, "cheapest": self.cheapest, "priciest": self.priciest,
                "price": self.price, "exclusions": [x["text"] for x in self.exclusions]}


def _branch_re(cities: dict[str, str]):
    names = sorted(cities, key=len, reverse=True)
    if not names:
        return None
    alt = "|".join(re.escape(n) for n in names)
    return re.compile(rf"\b(?:at|near|from|in|by|around)\s+(?:the\s+)?({alt})(?:,?\s+(?:[a-z]{{2}})\b)?"
                      rf"(?:\s+(?:branch|store|location|counter))?\b", re.I)


_BRANCH_CACHE: dict = {}


def split(q: str, cities: dict[str, str] | None = None) -> tuple[str, Buying]:
    """Take the buying words and exclusions off a product search. `cities`: lower-cased branch city -> branch id.
    Returns the text to search and what was taken off."""
    b = Buying()
    t = " " + (q or "") + " "
    key = id(cities)
    if cities and _BRANCH_CACHE.get("key") != key:
        _BRANCH_CACHE.update(key=key, rx=_branch_re(cities))
    brx = _BRANCH_CACHE.get("rx") if cities else None

    def cut(rx, fn=None):
        nonlocal t
        while True:
            m = rx.search(t)
            if not m:
                return
            if fn:
                fn(m)
            b.removed.append(m.group(0).strip())
            t = t[:m.start()] + " " + t[m.end():]

    cut(_ONE_BRANCH, lambda m: setattr(b, "one_branch", True))
    if brx is not None:
        def _br(m):
            name = m.group(1).lower()
            b.branch, b.branch_name = cities[name], m.group(1).title()
        cut(brx, _br)
    def _range(m):
        lo, hi = _amt(m.group(1), m.group(2)), _amt(m.group(3), m.group(4))
        b.price = [min(lo, hi), max(lo, hi)]
    cut(_PRICE_RANGE, _range)
    cut(_PRICE_MAX, lambda m: setattr(b, "price", [(b.price or [None, None])[0], _amt(*m.groups())]))
    cut(_PRICE_MIN, lambda m: setattr(b, "price", [_amt(*m.groups()), (b.price or [None, None])[1]]))
    if b.price:
        b.price = [int(x) if x is not None and float(x).is_integer() else x for x in b.price]
    cut(_CHEAP, lambda m: setattr(b, "cheapest", True))
    cut(_PRICEY, lambda m: setattr(b, "priciest", True))
    cut(_STOCK, lambda m: setattr(b, "stock", True))

    def _set_qty(n):
        if n and not b.qty:
            b.qty = int(n)
    cut(_QTY_LABEL, lambda m: _set_qty(m.group(1)))
    def _dozen(m):
        w = m.group(1).lower()
        n = int(w) * 12 if w.isdigit() else 6 if w.startswith("half") else 12 * _WORD_NUM.get(w, 1)
        _set_qty(n)
    cut(_DOZEN, _dozen)
    cut(_BOX_OF, lambda m: _set_qty(m.group(1)))
    cut(_QTY_PAREN, lambda m: _set_qty(m.group(1)))
    cut(_QTY_PCS, lambda m: _set_qty(m.group(1)))
    t = _TRAIL_ASK.sub("", t)
    cut(_QTY_X, lambda m: _set_qty(m.group(1) or m.group(2)))

    def _neg(m):
        words = [w for w in normalize(m.group(1)) if w.isalpha() and len(w) >= 3 and w not in UNITS]
        if words:
            b.exclusions.append({"text": m.group(1).strip(), "words": words})
    cut(_NEG, _neg)
    # "three quarter by half": the second size ("x 1/2"); the analyzer reads "half inch" but not "by half"
    t = re.sub(r"\bby\s+(half|a\s+half|quarter|three[\s-]+quarters?)\b",
               lambda m: "x " + {"half": "1/2", "quarter": "1/4"}.get(m.group(1).split()[-1], "3/4"), t, flags=re.I)
    t = re.sub(r"\s+", " ", t).strip(" ,;.")
    ask = _ASK.match(t)
    if ask and t[ask.end():].strip():
        b.removed.append(ask.group(0).strip())
        t = _ARTICLE.sub("", t[ask.end():])
    lead = _LEAD.match(t)
    if lead:
        b.removed.append(lead.group(0).strip())
        t = t[lead.end():]
    had_article = bool(_ARTICLE.match(t))       # "looking for a 2 type K": a size, not a count
    t = _ARTICLE.sub("", t)
    m = _QTY.match(t) if lead and not had_article else None
    if not m and not had_article and not b.qty:
        # "10 3/4 press elbows": 10-3/4 isn't a pipe size, so 10 is the count. 1 1/4, 1 1/2, 2 1/2 and 3 1/2 are
        # sizes (the same rule as Bulk order's line reader)
        mf = _QTY_FRAC.match(t)
        if mf:
            frac = re.match(r"\s*(\d+/\d+)", t[mf.end():]).group(1)
            n_ = int(mf.group(1))
            pipe = frac in ("1/4", "3/8", "1/2", "5/8", "3/4", "7/8")       # "2 1/16 bend" is a 2 in. 1/16 bend
            m = mf if pipe and ((lead and n_ > 5) or (n_ > 3 or frac not in ("1/4", "1/2")) and n_ >= 2) else None
    if not m:
        mw = _QTY_WORD.match(t)
        if mw:
            b.qty = _WORD_NUM[mw.group(1).lower()]
            b.removed.append(mw.group(1))
            t = t[mw.end():]
    if m:
        b.qty = int(m.group(1))
        b.removed.append(m.group(1))
        t = t[m.end():]
    elif not lead and not b.qty:
        # no verb: "12 half inch ball valves" still counts 12 (12-1/2 isn't a size); "1 1/2" stays a size
        m2 = re.match(r"^\s*(\d{1,4})\s+(?=half\b|quarter\b|three[\s-]+quarters?\b)", t, re.I)
        if m2 and int(m2.group(1)) > 5:
            b.qty = int(m2.group(1))
            b.removed.append(m2.group(1))
            t = t[m2.end():]
    t = re.sub(r"\s+", " ", _ARTICLE.sub("", t)).strip(" ,;.")
    return t, b


# ---------------------------------------------------------------------------------------------- the product phrase
# where the product phrase ends: what it's for, what it comes with, a limit ("under 60 in. tall", "pipe OD 6.60")
_CUT = re.compile(r"(?:\s*[,;]\s*|\s+(?:for|with|w/o|w/|without|that|which|including|plus|to\s+fit|fits?|compatible\s+with)\s+|"
                  r"\s+(?:pipe\s+)?o\.?d\.?\s+(?=[\d.])|"
                  r"\s+(?:under|over|below|above|less\s+than|more\s+than|at\s+least|at\s+most|up\s+to|max(?:imum)?|min(?:imum)?)\s+(?=[\d.]))",
                  re.I)
# words that say nothing about which product: never the product, never a reason to say "not carried"
DROP = {"need", "want", "find", "get", "buy", "looking", "only", "just", "replacement", "replace", "replacing", "new",
        "our", "one", "all", "also", "please", "type", "style", "kind", "grade", "quality", "universal", "oem",
        "genuine", "original", "heavy", "duty", "contractor", "good", "best", "cheap", "class", "under", "over",
        "complete", "whole", "entire", "unit's", "item", "part", "piece", "pieces", "each", "pack", "box", "case",
        "half", "quarter", "quarters", "eighth", "three", "two", "four", "five", "six", "size", "sized",
        # which model or how old, not what the product is ("late model furnace filter")
        "late", "early", "model", "models", "newer", "older", "old", "latest", "current", "previous", "generic",
        "aftermarket", "equivalent", "compatible", "similar", "spare", "extra", "backup", "emergency", "temporary",
        "used", "inexpensive", "affordable", "premium", "pro", "professional", "basic", "simple", "decent", "nice",
        # where it goes ("bathroom faucet" is a lavatory faucet; the catalog doesn't say bathroom)
        "bathroom", "restroom", "washroom", "basement", "garage", "attic", "crawlspace", "yard", "garden", "patio",
        "porch", "house", "home", "apartment", "condo", "school", "hospital", "restaurant", "building", "room",
        "customer", "job", "jobsite", "site", "project", "sundry", "sundries", "misc", "miscellaneous"}
DROP -= {"pack", "box", "piece"}      # a pack joint, a curb box, a 3-piece valve: product words
# describe the product rather than name it; checked on the page (the page says when it can't confirm them)
DESCRIPTORS = {"hydronic", "residential", "commercial", "industrial", "potable", "domestic", "reversible", "shielded",
               "actual", "dimension", "dimensions", "normally", "closed", "open", "asse", "astm", "ansi", "awwa",
               "ul", "fm", "csa", "nsf", "listed", "approved", "rated", "certified", "compliant", "white", "chrome",
               "polished", "anti", "siphon", "frost", "free", "adjustable", "heating", "cooling", "plumbing", "hvac",
               "standard", "outdoor", "indoor", "exterior", "interior", "wall", "mount", "mounted", "floor",
               "ceiling", "under", "above", "concealed", "exposed", "recessed", "pendent", "upright", "sidewall",
               "quick", "fast", "slow", "high", "low", "efficiency", "efficient", "energy", "star", "single", "double",
               "install", "installation", "installing", "rough", "finish", "ftg",
               # colors and finishes: which one, not what ("3/4 PEX-A Red" is PEX tubing)
               "red", "blue", "gray", "grey", "green", "yellow", "orange", "brown", "bone", "biscuit", "almond",
               "beige", "nickel", "satin", "matte", "brushed", "oil", "rubbed"}
DESCRIPTORS -= {"frost", "free", "floor", "wall", "pendent", "recessed", "outdoor", "single", "double", "standard"}
# ordinary words a search uses that no catalog title does: never "corrected" into a catalog word ("closed" isn't
# "closet"). Spelling fixes are for product words
COMMON = {"closed", "open", "opened", "normally", "there", "their", "where", "while", "which", "would", "could", "should",
          "about", "after", "before", "other", "another", "every", "right", "left", "front", "back", "upper", "lower",
          "inside", "outside", "between", "behind", "within", "along", "around", "through", "across", "against", "same",
          "different", "better", "larger", "smaller", "bigger", "cheaper", "faster", "quieter", "quiet", "safe", "easy",
          "soft", "clean", "dirty", "broken", "leaking", "leaky", "noisy", "fixed", "works", "working", "installed",
          "existing", "going", "doing", "making", "using", "used", "want", "wants", "needs", "needed", "have", "having",
          "this", "that", "these", "those", "them", "they", "then", "than", "when", "what", "with", "from", "into",
          "onto", "your", "yours", "mine", "ours", "just", "also", "very", "more", "most", "less", "least", "many",
          "much", "some", "same", "like", "look", "looks", "looking", "find", "show", "tell", "give", "send", "ship",
          "shipped", "order", "ordered", "today", "tomorrow", "week", "month", "year", "years", "days", "hours",
          # v2.0: what a person says around the product ("something to fix a leak", "how much", "you guys")
          "fix", "fixes", "fixing", "leak", "leaks", "repairing", "something", "anything", "stuff", "thing", "things",
          "you", "guys", "how", "did", "does", "price", "prices", "pricing", "cost", "costs", "near", "nearby",
          "help", "please", "thanks", "thank", "available", "stock", "carry", "sell", "anywhere", "somewhere"}
# trade words the catalog may not have: never "corrected" into a catalog word ("frost" isn't "front")
PROTECT = {"frost", "sillcock", "sillcocks", "bibb", "bibbs", "hydrant", "hydrants", "fernco", "dielectric", "dope",
           "teflon", "ballcock", "boiler", "hose", "spigot", "wye", "tankless"}
# standards codes and certifications: once the product noun is typed, what follows is a spec
STANDARDS = {"asse", "astm", "ansi", "awwa", "ul", "fm", "csa", "nsf", "cul", "etl", "iapmo", "aga", "ahri", "cee"}
# two-word phrases that say what a product is used with, not what it is ("water hammer arrester washing machine")
USE_PHRASES = [("washing", "machine"), ("fire", "protection"), ("fire", "service"), ("water", "service"),
               ("closed", "loop"), ("open", "loop"), ("potable", "water"), ("hot", "water"), ("cold", "water"),
               ("domestic", "water"), ("ice", "maker"), ("under", "sink"), ("wall", "mount"), ("floor", "mount"),
               ("deck", "mount"), ("new", "construction"), ("both", "end"), ("both", "ends"), ("raised", "face")]
# spec nouns after a number ("20 ft lift", "15 ft head"): part of a spec, never the product
SPEC_NOUNS = {"lift", "head", "long", "wide", "deep", "high", "tall", "rise", "run", "length", "width", "depth",
              "height", "capacity", "rating", "rated", "flow", "angle", "pattern", "spray", "cone", "hollow", "solid",
              "pole", "phase", "stage", "speed", "zone", "coil", "loop", "port", "gauge", "ga", "piece", "way",
              "bore", "face", "wire", "turn", "coat", "mesh", "micron", "straight", "stick", "roll", "reel"}
# a material word followed by one of these names a component's material, not the product's
COMPONENTS = {"ring", "seat", "stem", "trim", "handle", "screw", "bolt", "disc", "disk", "insert", "band", "cover",
              "lid", "grate", "spring", "shaft", "impeller", "liner", "lining", "nut", "washer", "hardware", "o-ring"}


@dataclass
class Phrase:
    text: str                                       # the searched text (after split)
    core: str                                       # the text before the first qualifier
    tails: list[tuple[str, str]]                    # [("with", "end switch"), ("under", "60 inches tall")]
    core_toks: list[str]                            # analyzer tokens of the core, before the spec tail
    spec_toks: list[str]                            # analyzer tokens of the specs after the product noun
    words: list[str]                                # product words in order (fuzzy-corrected)
    head: str | None = None
    mods: list[str] = field(default_factory=list)   # up to two words before the head: which kind
    unknown: list[str] = field(default_factory=list)            # product words the catalog has nowhere (at or before the head)
    corrected: dict = field(default_factory=dict)   # typed word -> catalog word ("condinsate" -> "condensate")
    descriptors: list[str] = field(default_factory=list)        # words after the head, and describing words
    tail_words: list[str] = field(default_factory=list)         # content words of the qualifiers

    @property
    def search_text(self) -> str:
        """What the engine searches: the product phrase and its specs, without the qualifiers."""
        return self.core

    def req_tokens(self) -> list[str]:
        """Tokens requirements are read from: the product phrase and its specs, never a qualifier ("for C900
        PVC pipe" doesn't make a joint restraint PVC)."""
        return self.core_toks + self.spec_toks


def cut(text: str, is_noun=None) -> tuple[str, list[tuple[str, str]]]:
    """Split at the first qualifier that follows a product noun (`is_noun`: is this analyzer token the name of a
    product?). "3/4 Sweat w/ Drain Brass ball valve" isn't cut: the product comes after "w/". Returns (core,
    [(lead, tail), ...])."""
    tails: list[tuple[str, str]] = []
    pos, core = 0, None
    for m in _CUT.finditer(text):
        before = text[pos:m.start()]
        if core is None:
            if not re.search(r"[a-z]{3,}", before, re.I):
                continue                # "under sink ...": nothing to qualify yet
            if is_noun is not None and not any(is_noun(t) for t in normalize(before)):
                continue                # "what copper do I need for 1/2 tee": the product is after "for"
            core = text[:m.start()]
        else:
            tails[-1] = (tails[-1][0], text[pos:m.start()].strip())
        lead = re.sub(r"\s+", " ", m.group(0).strip(" ,;").lower()) or ","
        tails.append((lead, ""))
        pos = m.end()
    if core is None:
        return text.strip(), []
    tails[-1] = (tails[-1][0], text[pos:].strip())
    return core.strip(), [(a, b) for a, b in tails if b]


def _is_num(t: str) -> bool:
    return bool(t) and (t[0].isdigit() or (t[0] == "." and t[1:2].isdigit()))


class Lexicon:
    """What the phrase reader asks the catalog. Implemented by ResultsService over the ranked pool."""

    def known(self, w: str) -> bool: ...              # a catalog word (title, specs, category, synonyms)
    def pool_head(self, w: str) -> bool: ...          # the head noun of products in the ranked pool
    def global_head(self, w: str) -> bool: ...        # the head noun of products anywhere in the catalog
    def correct(self, w: str) -> str | None: ...      # a catalog word this is a misspelling of, if the pool has it
    def top_heads(self) -> dict: ...                  # head noun -> how many of the best-matching products it names
    def filed_under(self, noun: str, other: str) -> bool: ...   # are the pool's `noun` products filed under `other`?


QUESTION = re.compile(r"^\s*(?:what|which|how|do|does|can|could|should|will|is|are|where|who)\b", re.I)
# "what size expansion tank (do I need) for a 50 gallon water heater": the product is the expansion tank (v2.0)
_Q_WRAP = re.compile(r"^\s*(?:what|which)\s+(?:(?:size|sized|kind|type|sort|brand|model|style)(?:\s+of)?\s+)?", re.I)
_Q_TAIL = re.compile(r"\s+(?:do|does|should|would|can|could|will)\s+(?:i|we|you|they|it)\s+(?:need|use|get|buy|want|have|go\s+with)\b",
                     re.I)


def question_core(text: str) -> str | None:
    """A "what/which <product> ... for <use>" question with the question words taken off, so the phrase reader can
    cut at "for" like any search; None when it isn't that kind of question."""
    m = _Q_WRAP.match(text or "")
    # only a sizing or kind question: in "what valve do I need for 1/2 gate valve" what follows "for" is the spec
    if not m or not re.search(r"\b(?:size|sized|kind|type|sort|brand|model|style)\b", text[:m.end()], re.I):
        return None
    t = _Q_TAIL.sub(" ", text[m.end():])
    t = re.sub(r"\s+", " ", t).strip(" ?.!")
    return t or None


def analyze(text: str, lex: Lexicon, brand_toks: set, req_words: set, term_toks: set = frozenset()) -> Phrase:
    # a question ("what contactor do I need for a 25 amp fan relay") is the advisor's: every word stays the product's
    qc = question_core(text) if QUESTION.match(text) else None
    core, tails = (text.strip(), []) if QUESTION.match(text) and not qc else \
        cut(qc or text, lambda w: w.isalpha() and len(w) >= 3 and w not in DROP and w not in SPEC_NOUNS
            and (lex.pool_head(w) or lex.global_head(w)))
    toks = list(normalize(core))
    # the specs after the product noun: once a word the pool has as a product noun is typed, a number or a
    # standards code starts the specs ("zone valve 24V normally closed", "mixing valve ASSE 1070")
    spec_at = len(toks)
    seen = False

    def noun(w):
        return w.isalpha() and len(w) >= 3 and w not in req_words and w not in SPEC_NOUNS and w not in DROP \
            and (lex.pool_head(w) or lex.global_head(w))
    for j, t in enumerate(toks):
        # ...unless a product noun follows ("boiler 3/4 relief valve": the valve is the product)
        if seen and (_is_num(t) or t in STANDARDS) and not any(noun(w) for w in toks[j + 1:]):
            spec_at = j
            break
        if noun(t):
            seen = True
    core_toks, spec_toks = toks[:spec_at], toks[spec_at:]
    use = set()
    for a, b in USE_PHRASES:
        for j in range(len(core_toks) - 1):
            if core_toks[j] == a and core_toks[j + 1] == b:
                use |= {j, j + 1}
    words: list[str] = []
    pos: list[int] = []
    desc: list[str] = []
    corrected: dict = {}
    unk_pos: set = set()
    for j, t in enumerate(core_toks):
        if not (t.isalpha() and len(t) >= 3) or t in UNITS or t in brand_toks or t in term_toks:
            continue
        prev = core_toks[j - 1] if j else ""
        if t in DROP or t in req_words or t in COMMON:
            continue
        # the analyzer's joined form of a hyphenated word (definite-purpose -> definite purpose definitepurpose) is
        # an alternative spelling for retrieval; its parts already carry the word here
        if joined_at(core_toks, j):
            continue
        if j in use or t in DESCRIPTORS:
            desc.append(t)
            continue
        # "3 zone", "2 pole" are counts; "3/4 zone valve" is a size and then the kind of valve
        if t in SPEC_NOUNS and prev and ((_is_num(prev) and "/" not in prev) or prev in UNITS):
            desc.append(t)
            continue
        if not lex.known(t):
            fix = lex.correct(t) if len(t) >= 4 else None
            if fix:
                corrected[t] = fix
                t = fix
            elif prev and ((_is_num(prev) and j >= 2) or prev in term_toks):
                desc.append(t)              # "x 7/8 ballcock", "SDR35 sewer": a spec, not the product
                continue
            else:
                unk_pos.add(len(words))
        words.append(t)
        pos.append(j)
    # a last word that repeats an earlier one is a category tag ("Circulator Flange Set ... circulator")
    if len(words) >= 3 and words[-1] in words[:-2]:
        desc.append(words.pop())
        unk_pos.discard(len(words))
    ph = Phrase(text=text, core=core, tails=tails, core_toks=core_toks, spec_toks=spec_toks, words=words,
                corrected=corrected)
    ph.tail_words = [w for _, tl in tails for w in normalize(tl) if w.isalpha() and len(w) >= 3 and w not in UNITS
                     and w not in DROP]
    if not words:
        ph.descriptors = desc
        return ph
    # the head: the last product word when it is one (unknown, a product noun here or anywhere in the catalog);
    # otherwise the last word the pool has as a product noun ("grooved check valve fire protection")
    last = len(words) - 1
    h = None
    trailing_part = words[last] in COMPONENTS and any(lex.pool_head(w) for w in words[:last])   # "... PTFE seats"
    if not trailing_part and (last in unk_pos or lex.pool_head(words[last]) or lex.global_head(words[last])):
        h = last
    else:
        h = next((k for k in range(last, -1, -1) if lex.pool_head(words[k])), None)
        if h is None:
            h = next((k for k in range(last, -1, -1) if k in unk_pos), last)
    # two product nouns ("Dip Tube water heater", "Gas Valve boiler"): a part named with what it's for. When the
    # earlier noun's products are filed under the later noun (Boiler Repair Parts), the earlier one is the product;
    # otherwise the last (head-final)
    if h not in unk_pos:
        top = lex.top_heads()
        nouns = [k for k in range(h) if k not in unk_pos and words[k] not in COMPONENTS
                 and (lex.pool_head(words[k]) or lex.global_head(words[k]))]
        n_h = top.get(words[h], 0)
        best = max(nouns, key=lambda k: (top.get(words[k], 0), k), default=None)
        # ...and the last noun is where those products are filed (dip tubes under Water Heater Repair Parts), so it
        # says what they're for; "true union ball valve" stays a valve (CPVC unions aren't filed under valves)
        if best is not None and (top.get(words[best], 0) >= max(5, 3 * n_h) or top.get(words[best], 0) >= 3) \
                and lex.filed_under(words[best], words[h]):
            h = best
    ph.head = words[h]
    ph.mods = [w for w in reversed(words[max(0, h - 2):h])]
    ph.unknown = [words[k] for k in sorted(unk_pos) if k <= h]
    ph.descriptors = desc + words[h + 1:]
    return ph


def component_materials(tokens: list[str], mat_words: set) -> set:
    """Material words that name a component's material, not the product's: "closet flange stainless steel ring",
    "ball valve PTFE seats". The component ends the phrase and a product word comes before the material."""
    out = set()
    for j, t in enumerate(tokens):
        if t in mat_words:
            k = j + 1
            while k < len(tokens) and tokens[k] in ("steel", "iron", "metal", "coated", "plated"):
                k += 1
            if k == len(tokens) - 1 and tokens[k] in COMPONENTS and \
                    any(x.isalpha() and len(x) >= 4 and x not in mat_words for x in tokens[:j]):
                out.add(t)
    return out


def _osa(a: str, b: str) -> int:
    """Edit distance counting a swap of neighbours as one edit ("toliet" -> "toilet")."""
    d = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        d[i][0] = i
    for j in range(len(b) + 1):
        d[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            c = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + c)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[-1][-1]


# v2.45 keyboard-aware spelling. QWERTY neighbors (same row either side, the row above and below, offset)
_ROWS = ("qwertyuiop", "asdfghjkl", "zxcvbnm")


def _neigh(c: str) -> str:
    for r, row in enumerate(_ROWS):
        i = row.find(c)
        if i < 0:
            continue
        out = [row[j] for j in (i - 1, i + 1) if 0 <= j < len(row)]
        for rr, off in ((r - 1, (0, 1)), (r + 1, (-1, 0))):
            if 0 <= rr < len(_ROWS):
                out += [_ROWS[rr][i + o] for o in off if 0 <= i + o < len(_ROWS[rr])]
        return "".join(out)
    return ""


NEIGH = {c: _neigh(c) for c in "abcdefghijklmnopqrstuvwxyz"}
KB_SUB, KB_INS, KB_DBL, KB_SWAP = 0.45, 0.5, 0.4, 0.6


def kb_dist(typed: str, word: str, cap: float = 9.0) -> float:
    """What it costs to have typed `typed` meaning `word`, slips weighted by how fingers miss: a key next to the
    right one (0.45), a neighbor pressed with the right key (0.5), a key pressed twice or a double letter typed once
    (0.4), two letters swapped (0.6); any other letter wrong, added or left out costs 1. Stops early above `cap`."""
    a, b = typed, word
    la, lb = len(a), len(b)
    if abs(la - lb) * KB_DBL > cap:           # each letter more or fewer costs at least a doubled key
        return cap + 1
    prev2 = None
    prev = [j * 1.0 for j in range(lb + 1)]
    for i in range(1, la + 1):
        cur = [i * 1.0] + [0.0] * lb
        ai = a[i - 1]
        for j in range(1, lb + 1):
            bj = b[j - 1]
            if ai == bj:
                sub = prev[j - 1]
            else:
                sub = prev[j - 1] + (KB_SUB if bj in NEIGH.get(ai, "") else 1.0)
            # an extra key typed: a neighbor of the letter before or after it, or the letter before again
            near = (i >= 2 and (ai == a[i - 2] or ai in NEIGH.get(a[i - 2], ""))) or \
                (i < la and ai in NEIGH.get(a[i], "")) or (j <= lb - 1 and ai in NEIGH.get(b[j], ""))
            ins = prev[j] + (KB_DBL if i >= 2 and ai == a[i - 2] else KB_INS if near else 1.0)
            # a letter left out: a double letter typed once ("fiting") is the cheap one
            dele = cur[j - 1] + (KB_DBL if j >= 2 and bj == b[j - 2] else 1.0)
            v = min(sub, ins, dele)
            if prev2 is not None and i > 1 and j > 1 and ai == b[j - 2] and a[i - 2] == bj:
                v = min(v, prev2[j - 2] + KB_SWAP)
            cur[j] = v
        if min(cur) > cap:
            return cap + 1
        prev2, prev = prev, cur
    return prev[lb]


_SOUND_RULES = [(r"^kn", "n"), (r"^wr", "r"), (r"^ps", "s"), (r"^pn", "n"), (r"^gn", "n"), (r"^wh", "w"),
                (r"ph", "f"), (r"ck", "k"), (r"q", "k"), (r"x", "ks"), (r"dg", "j"), (r"tch", "x"), (r"sch", "sk"),
                (r"[st]i(?=on)", "x"), (r"ch|sh", "x"), (r"c(?=[eiy])", "s"), (r"c", "k"), (r"z", "s"),
                (r"g(?=[eiy])", "j"), (r"gh", ""), (r"ing$", "n"), (r"ng$", "n"), (r"mb$", "m"), (r"nd(?=[^aeiou])", "n"),
                (r"(?<=[aeiou])w", ""), (r"(?<!^)h", ""), (r"(?<!^)y", "i")]


def sound(w: str) -> str:
    """How a word sounds, roughly (v2.45): "fernace" and "furnace" -> frns, "sharkbyte" and "sharkbite" -> xrkbt,
    "condinsate" -> kndnst, "expanshun" and "expansion" -> ekspnxn. Spelling rules first (ph, ck, soft c and g,
    -tion), then doubled letters merged and vowels dropped after the first letter."""
    w = re.sub(r"[^a-z]", "", w.lower())
    if not w:
        return ""
    for a, b in _SOUND_RULES:
        w = re.sub(a, b, w)
    w = re.sub(r"(.)\1+", r"\1", w)
    return (w[0] + re.sub(r"[aeiou]", "", w[1:])) if w else ""


def ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def sounds(w: str) -> tuple[str, ...]:
    """The sound keys of a word: as spelled, and with "ch" hard ("lochinvar" said "lock-invar")."""
    a = sound(w)
    b = sound(re.sub(r"ch", "k", w.lower())) if "ch" in w.lower() else a
    return (a,) if a == b else (a, b)


def sound_index(words) -> dict:
    idx: dict = {}
    for w in words:
        for k in sounds(w):
            if len(k) >= 2:
                idx.setdefault(k, []).append(w)
    return idx


def sound_pick(word: str, index: dict, freq=None, brands: bool = False) -> str | None:
    """A word that sounds like this one (same sound key), the closest in spelling, then the more common. A short key
    (two letters: "rm" for rheem and reme) needs the same first letter and a common word or a brand."""
    keys = [k for k in sounds(word) if len(k) >= 2]
    if not keys:
        return None
    best = None
    for c in {c for k in keys for c in index.get(k, ())}:
        k = min(keys, key=len)
        if c == word or abs(len(c) - len(word)) > 3:
            continue
        r = difflib.SequenceMatcher(None, word, c).ratio()
        # close enough in spelling: 0.7, or 0.5 when the sound key is long enough to mean something ("furnis")
        if r < 0.5 or (r < 0.7 and len(k) < 4):
            continue
        if brands and (r < 0.7 or (len(k) == 2 and r < 0.8)):   # a brand by sound: still close in spelling
            continue                                            # ("sanitery" isn't Century)
        f = freq(c) if freq else 0
        if len(k) == 2 and (c[0] != word[0] or not (brands or f >= 200)):
            continue
        key = (r, f)
        if best is None or key > best[0]:
            best = (key, c)
    return best[1] if best else None


def kb_budget(n: int) -> float:
    """How much slip a word of n letters can carry and still be read as a word we know."""
    return 0.0 if n < 4 else 0.45 if n == 4 else 0.9 if n <= 6 else 1.35 if n <= 9 else 1.8


def kb_pick(word: str, candidates, freq=None) -> tuple[str, float] | None:
    """The catalog word a fat-fingered word most likely meant: the cheapest within the word's budget, then the more
    common. Not a prefix of it either way ("magnet" isn't "magnetic"), unless the extra is a doubled or neighbor key."""
    bud = kb_budget(len(word))
    if not bud:
        return None
    best = None
    for c in candidates:
        if c == word or abs(len(c) - len(word)) > 2:
            continue
        if (word.startswith(c) or c.startswith(word)) and abs(len(c) - len(word)) > 1:
            continue
        d = kb_dist(word, c, bud)
        if d > bud:
            continue
        # a word that only adds a letter the end of another ("magnets") isn't that word unless it's a slip key
        if word.startswith(c) and len(word) == len(c) + 1 and word[-1] != c[-1] and word[-1] not in NEIGH.get(c[-1], ""):
            continue
        if c.startswith(word) and len(c) == len(word) + 1 and len(word) < 6:
            continue
        key = (d, -(freq(c) if freq else 0))
        if best is None or key < best[0]:
            best = (key, c)
    return (best[1], best[0][0]) if best else None


def fuzzy_pick(word: str, candidates) -> str | None:
    """A catalog word this is a misspelling of: one edit away (a swap counts as one) or close in spelling, and
    not a prefix of it ("magnet" isn't "magnetic") nor the other way round."""
    best = None
    for c in candidates:
        if abs(len(c) - len(word)) > 2 or c == word:
            continue
        # a word that runs on past a catalog word isn't a typo of it ("magnetic" isn't "magnet"), except a doubled
        # last letter on a long word ("thermostatt", v2.01)
        if word.startswith(c) and not (len(word) - len(c) == 1 and len(c) >= 4 and word[-1] == word[-2]):
            continue
        # a cut-off word is one letter short of a long one ("tankles", "honeywel"); "magnet" isn't "magnetic"
        if c.startswith(word) and not (len(c) - len(word) == 1 and len(word) >= 6):
            continue
        r = difflib.SequenceMatcher(None, word, c).ratio()
        one = _osa(word, c) == 1
        # one edit: a missing letter from 4 letters up ("vale" -> "valve", "filtr" -> "filter"); a swap or a wrong
        # letter from 5; an extra letter only from 6 ("purge" isn't "pure"). Two or more edits: close, from 6
        if one:
            # v2.43: a swap in a four-letter word ("tnak"), a doubled last letter on a four-letter word ("valvee")
            ok = (len(c) > len(word) and len(word) >= 4) or (len(c) == len(word) and len(word) >= 5) or len(word) >= 6 \
                or (len(word) == 4 == len(c) and sorted(word) == sorted(c)) \
                or (len(word) == len(c) + 1 and word[-1] == word[-2] and len(c) >= 4)
        else:
            # two slips in a long word ("thermostst", "expansoin tnak"): from 8 letters (v2.43), or very close from 6
            ok = (r >= 0.88 and len(word) >= 6) or (len(word) >= 8 and _osa(word, c) == 2 and r >= 0.8)
        if not ok:
            continue
        # a wrong letter next to the right one on the keyboard is the likelier slip: "frodt" is frost, not front (v2.43)
        if one and len(c) == len(word):
            diff = [(x, y) for x, y in zip(word, c) if x != y]
            if len(diff) == 1 and diff[0][1] in _KEYB.get(diff[0][0], ""):
                r += 0.05
        if best is None or r > best[0]:
            best = (r, c)
    return best[1] if best else None


_KEYB = dict(zip("qwertyuiopasdfghjklzxcvbnm",
                 ["wa", "qeas", "wrds", "etdf", "ryfg", "tugh", "yihj", "uojk", "ipkl", "ol", "qwsz", "awedxz", "serfcx",
                  "drtgvc", "ftyhbv", "gyujnb", "huikmn", "jiolm", "kop", "asx", "zsdc", "xdfv", "cfgb", "vghn", "bhjm",
                  "njk"]))


# pipe outside diameters (in.) -> nominal size: IPS (steel, PVC Sch 40/80) and CIOD (ductile iron, AWWA C900)
_OD = {"1/2": (0.840,), "3/4": (1.050,), "1": (1.315,), "1-1/4": (1.660,), "1-1/2": (1.900,), "2": (2.375,),
       "2-1/2": (2.875,), "3": (3.500, 3.96), "4": (4.500, 4.80), "6": (6.625, 6.90), "8": (8.625, 9.05),
       "10": (10.75, 11.10), "12": (12.75, 13.20), "16": (16.0, 17.40)}


def od_nominal(text: str) -> list[str]:
    """Nominal pipe sizes whose outside diameter falls in the typed OD range ("6.60 to 6.90" -> 6)."""
    nums = [float(x) for x in re.findall(r"\d+\.\d+|\d+", text or "")][:2]
    if not nums:
        return []
    lo, hi = min(nums), max(nums)
    return [n for n, ods in _OD.items() if any(lo - 0.06 <= d <= hi + 0.06 for d in ods)]
