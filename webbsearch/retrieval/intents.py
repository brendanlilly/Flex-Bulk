"""One search box, many intents (v2.01, from ChatGPT's QA review F07).

People type more than product names into the box: a job ("everything I need to replace a 50 gallon gas water
heater"), a symptom ("furnace blower runs but no heat"), a how-to question ("can I use PEX crimp fittings with
expansion rings"), a comparison ("compare 3/4 sweat and press ball valves") or a swap ("replace this capacitor 45/5
370v with 440v"). Searched word for word, the extra words emptied the page. This reads the shape of the sentence and
returns the product phrase to search, plus what the page should offer besides products: job kits, guides, FlexIQ.

    read(q) -> Intent | None
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class Intent:
    kind: str                      # job | symptom | question | compare | replace
    product: str                   # what to search for products
    topic: str = ""                # what to search for kits or guides
    ask: bool = False              # offer FlexIQ ("Ask FlexIQ about this")
    alternatives: list = field(default_factory=list)   # compare: the options typed ("sweat", "press")
    note: str = ""

    def to_dict(self) -> dict:
        return {"kind": self.kind, "product": self.product, "topic": self.topic, "ask": self.ask,
                "alternatives": self.alternatives, "note": self.note}


_SP = re.compile(r"\s+")


def _clean(s: str) -> str:
    s = re.sub(r"^\s*(?:a|an|the|my|our|this|that|some|new)\s+", "", s.strip(" ?.!,"), flags=re.I)
    return _SP.sub(" ", s).strip(" ?.!,")


# ---------------------------------------------------------------- jobs
_JOB = re.compile(r"\b(?:everything|all(?:\s+the)?(?:\s+(?:stuff|parts|materials|fittings))?|what(?:\s+all)?)\s+"
                  r"(?:i|we|you)(?:'ll|\s+will|\s+would)?\s+need\b|\b(?:materials?|parts?(?:\s+list)?|supplies)\s+"
                  r"(?:for|to|needed\s+(?:for|to))\b|\bwhat\s+do\s+(?:i|we)\s+need\s+(?:for|to)\b", re.I)
_JOB_VERB = re.compile(r"\b(?:replace|replacing|replacement\s+of|install|installing|installation\s+of|put\s+in|"
                       r"swap(?:ping)?\s+out|hook\s*up|hooking\s+up|do|doing)\b", re.I)
_JOB_FILL = re.compile(r"\b(?:everything|all\s+the\s+(?:stuff|parts|materials|fittings)|all|what(?:\s+all)?|"
                       r"(?:i|we|you)(?:'ll|\s+will|\s+would)?\s+need|need|needed|materials?|parts?(?:\s+list)?|supplies|"
                       r"do\s+(?:i|we)|for|to|a|an|the|my|our|job)\b", re.I)

# ---------------------------------------------------------------- symptoms
_SYMPTOM = re.compile(r"\b(?:won'?t|will\s+not|doesn'?t|does\s+not|isn'?t|is\s+not|not\s+(?:working|heating|cooling|"
                      r"starting|turning\s+on|running|draining|flushing|firing|lighting|spinning)|no\s+(?:heat|hot\s+water|"
                      r"cold\s+water|cooling|cool\s+air|air\s*flow|power|flame|pressure|water)|hums?|humming|buzz(?:es|ing)?|"
                      r"leaks|leaking|(?:a|the|fix(?:ing)?)\s+leak(?=\s+(?:under|at|from|in|on|behind)\b|\s*$)|leak\s+(?:under|at|from|in|on|behind)|drips?|dripping|trips?|tripping|short[\s-]+cycl\w*|noisy|bang(?:s|ing)|"
                      r"click(?:s|ing)|squeal(?:s|ing)|runs?\s+but|keeps?\s+(?:running|shutting|tripping|cycling)|"
                      r"stopped|frozen|freezes|smells?|making\s+(?:a\s+)?noise)\b", re.I)
_SYMPTOM_FILL = re.compile(r"\b(?:but|and|when|after|then|it|its|it's|still|is|are|was|keeps?|runs?|running|will|"
                           r"start|starts|starting|come\s+on|comes\s+on|turn\s+on|turns\s+on|work|works|any|at\s+all|"
                           r"anymore|now|always|sometimes|again|on|off|up|the|my|a|an|our|this|that|at|around|behind|from|base|bottom)\b", re.I)

# the parts a counter would reach for first, for the commonest symptoms (shown as parts, with FlexIQ to diagnose)
SYMPTOM_PARTS = [
    (re.compile(r"\b(?:ac|a/c|air\s+condition\w*|condens(?:er|ing\s+unit)|heat\s+pump|outdoor\s+unit)\b", re.I),
     re.compile(r"\bhums?\b|humming|buzz|won'?t\s+start|will\s+not\s+start|not\s+starting|fan\s+(?:won'?t|will\s+not|"
                r"doesn'?t|not)|not\s+cooling", re.I), "dual run capacitor"),
    (re.compile(r"\bfurnace\b", re.I),
     re.compile(r"no\s+heat|not\s+heating|won'?t\s+(?:light|ignite|fire)|not\s+(?:lighting|firing)|cold\s+air", re.I),
     "hot surface igniter"),
    (re.compile(r"\btoilet\b", re.I), re.compile(r"leak\w*\s+(?:at|around|from)\s+(?:the\s+)?(?:base|bottom|floor)", re.I),
     "toilet wax ring"),
    (re.compile(r"\btoilet\b", re.I), re.compile(r"\b(?:keeps?\s+)?running|won'?t\s+stop|runs\s+constantly|"
                                                  r"won'?t\s+fill|not\s+filling", re.I), "toilet fill valve"),
    (re.compile(r"\bwater\s+heater\b", re.I), re.compile(r"no\s+hot\s+water|pilot|won'?t\s+light", re.I), "thermocouple"),
    (re.compile(r"\b(?:under\s+(?:the\s+)?(?:kitchen\s+|bathroom\s+)?sink|sink\s+drain|p[\s-]?trap)\b", re.I),
     re.compile(r"\bleak", re.I), "p-trap"),
]

# ---------------------------------------------------------------- questions
_QUESTION = re.compile(r"^\s*(?:can|could|should|may)\s+(?:i|we|you)\b|^\s*how\s+(?:do|to|can|should|would)\b|"
                       r"^\s*is\s+it\s+(?:ok|okay|safe|possible|legal|code)\b|^\s*do\s+(?:i|we)\s+(?:have\s+to|need\s+to)\b|"
                       r"^\s*(?:why|when)\s+(?:does|do|is|should|would)\b|^\s*will\s+(?:a|an|the|this|my)\b", re.I)
_Q_LEAD = re.compile(r"^\s*(?:can|could|should|may|how|is|do|why|when|will)\b(?:\s+(?:i|we|you|it|to|do|does|need\s+to|"
                     r"have\s+to|ok|okay|safe|possible|legal|code|a|an|the|this|my))*\s+", re.I)
_Q_VERB = re.compile(r"^\s*(?:use|install|connect|join|mix|put|run|replace|fix|size|choose|pick|solder|sweat|crimp|"
                     r"press|thread|glue|cement|repair|vent|pipe|wire|hook\s+up|convert|adapt)\s+", re.I)

# ---------------------------------------------------------------- compare and replace
_COMPARE = re.compile(r"^\s*(?:compare|comparing|comparison\s+of|difference\s+between)\s+(.+)$", re.I)
_VS = re.compile(r"\b(?:vs\.?|versus)\b", re.I)
_VS_SPEED = re.compile(r"\bvs\s+(?:motor|ecm|circulator|circ|pump|blower|furnace|drive|vfd|compressor|fan|air\s+handler|"
                       r"condenser|heat\s+pump|boiler)s?\b", re.I)
_ALT = re.compile(r"\b([\w/.-]+)\s+(?:and|or|vs\.?|versus)\s+([\w/.-]+)\b", re.I)
_REPLACE = re.compile(r"^\s*(?:replace|replacing|swap|swapping|upgrade|change)\s+(?:out\s+)?(.+?)\s+(?:with|for|to)\s+"
                      r"(?:a|an|the)?\s*(.+)$", re.I)
_RATED = re.compile(r"^(\d+(?:\.\d+)?(?:/\d+(?:\.\d+)?)?)\s*(v|vac|volts?|a|amps?|mfd|uf|hp|psi|ton|tons|btu|mbh|gal|gallons?)$",
                    re.I)


def _unit(tok: str) -> str | None:
    m = _RATED.match(tok)
    if not m:
        return None
    u = m.group(2).lower()
    return {"vac": "v", "volt": "v", "volts": "v", "amp": "a", "amps": "a", "uf": "mfd", "tons": "ton",
            "gallon": "gal", "gallons": "gal"}.get(u, u)


# ---------------------------------------------------------------- procedures (v2.44)
# trade procedures typed into the search box: a procedure is not a product. Ask Webb walks through it; the products
# the job uses are below
PROCEDURES = [
    (re.compile(r"^\s*(?:how\s+(?:do\s+(?:i|you)|to)\s+)?pump(?:ing)?[\s-]*down\b", re.I), "manifold gauge set",
     "Pumping a system down is a refrigeration procedure, not a product: refrigerant is moved into the condenser by "
     "closing the liquid line and running the compressor. Ask Webb walks through it; gauges for it are below."),
    (re.compile(r"^\s*(?:how\s+(?:do\s+(?:i|you)|to)\s+)?(?:pull(?:ing)?\s+(?:a\s+)?vacuum|evacuat(?:e|ing)\s+"
                r"(?:the\s+|a\s+)?(?:system|line\s*set|lines))\b", re.I), "vacuum pump",
     "Pulling a vacuum is a procedure, not a product. Ask Webb walks through it; vacuum pumps are below."),
    (re.compile(r"^\s*(?:how\s+(?:do\s+(?:i|you)|to)\s+)?(?:recover(?:ing)?\s+(?:the\s+)?refrigerant)\b", re.I),
     "recovery machine", "Recovering refrigerant is a procedure, not a product. Ask Webb walks through it; recovery "
                         "machines are below."),
]


def read(q: str) -> Intent | None:
    t = (q or "").strip()
    if len(t) < 6:
        return None
    for rx, prod, note in PROCEDURES:
        if rx.search(t):
            return Intent("procedure", prod, topic=t, ask=True, note=note)
    m = _REPLACE.match(t)
    if m:
        old, new = _clean(m.group(1)), _clean(m.group(2))
        nt = new.split()
        if nt and all(_unit(x) for x in nt):            # "with 440v": swap the rating, keep the product
            units = {_unit(x): x for x in nt}
            toks = [units.pop(_unit(x)) if _unit(x) in units else x for x in old.split()]
            prod = " ".join(toks + list(units.values()))
        else:
            prod = new
        return Intent("replace", prod, note=f"Searched for the replacement: “{prod}”.")
    m = _COMPARE.match(t)
    # "should I use PEX vs copper in a basement" is a question; "Taco VS circulator" / "VS motor" is variable speed
    if m or (_VS.search(t) and not _QUESTION.match(t) and not _VS_SPEED.search(t)):
        body = m.group(1) if m else t
        alts = []
        sides = [x.strip() for x in re.split(r"\b(?:vs\.?|versus)\b", body, maxsplit=1, flags=re.I)]
        mm = _ALT.search(body)
        if len(sides) == 2 and all(len(x.split()) >= 2 for x in sides):
            # two products ("Taco 007 vs Grundfos UPS15"): the first is searched, both are named
            return Intent("compare", _clean(sides[0]), alternatives=sides,
                          note=f"Comparing {sides[0]} and {sides[1]}: search each, then compare them side by side.")
        if mm:
            alts = [mm.group(1), mm.group(2)]
            prod = _clean(body[:mm.start()] + " " + body[mm.end():])
        else:
            prod = _clean(body)
        if not re.search(r"[a-z]{3}", prod, re.I):      # "compare Taco 007 and Grundfos UPS15": both are products
            prod, alts = _clean(re.sub(r"\b(?:and|or|vs\.?|versus)\b", " ", body, flags=re.I)), alts
        return Intent("compare", prod, alternatives=alts,
                      note=("Comparing " + " and ".join(alts) + ": the spec table lines them up.") if alts else "")
    if _JOB.search(t):
        topic = _clean(_SP.sub(" ", _JOB_FILL.sub(" ", t)))
        prod = _clean(_SP.sub(" ", _JOB_VERB.sub(" ", topic)))
        prod = re.sub(r"\b(?:install|installation|replacement|job)\b", " ", prod, flags=re.I)
        prod = _clean(prod)
        if not prod:
            return None
        return Intent("job", prod, topic=prod + " install",
                      note="A whole job: the job kits list what it takes. The products below are the equipment.")
    if _QUESTION.match(t):
        body = _Q_VERB.sub("", _Q_LEAD.sub("", t))
        prod = _clean(re.split(r"\s+(?:with|on|in|into|for|to|and|or|instead\s+of|together)\s+", body, maxsplit=1)[0])
        return Intent("question", prod, topic=_clean(body), ask=True,
                      note="This reads like a question. Ask Webb can answer it; guides and products are below.")
    if _SYMPTOM.search(t):
        prod = _clean(_SP.sub(" ", _SYMPTOM_FILL.sub(" ", _SYMPTOM.sub(" ", t))))
        if not prod:
            return None
        for eq, sym, part in SYMPTOM_PARTS:
            if eq.search(t) and sym.search(t):
                return Intent("symptom", part, topic=prod + " troubleshooting", ask=True,
                              note=f"This reads like a problem to troubleshoot. Below: {part}s, a common fix for it. "
                                   "Ask Webb can walk through the diagnosis.")
        return Intent("symptom", prod, topic=prod + " troubleshooting", ask=True,
                      note="This reads like a problem to troubleshoot. Ask Webb can walk through it; guides and parts "
                           "are below.")
    return None
