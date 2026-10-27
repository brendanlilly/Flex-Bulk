"""The same thing typed another way (v2.0): rewrites a search into the catalog's own notation before anything reads it.

    16 x 25 x 1 filter                  -> 16x25x1 filter            (filter and duct sizes are one token in titles)
    150000 BTU boiler, 150,000 btu      -> 150 MBH boiler            (boilers are rated in MBH)
    199 MBH tankless water heater       -> 199000 BTU tankless ...   (furnaces, tankless, mini-splits in BTU)
    temperature and pressure relief     -> T&P relief                (titles say T&P)
    le 2634207                          -> LE2634207                 (a part number typed with a space)
    36000 btu condenser                 -> 3 Ton condenser           (split-system condensers are rated in tons, v2.42)
    1.5 ton mini split                  -> 18000 BTU mini split      (mini-splits are listed in BTU, v2.42)
    half horse sump pump                -> 1/2 HP sump pump          (horsepower in words, v2.42)
    5 gpm tankless                      -> tankless                  (the demo catalog doesn't list flow rates, v2.42)

Each rewrite is reported, so the page can say "Searched as ...". With the real catalog, the BTU/MBH choice should come
from how each family is listed (or both forms indexed); here it follows the demo catalog.
"""
from __future__ import annotations

import re

_DIM3 = re.compile(r"\b(\d{1,3})\s*[x×]\s*(\d{1,3})\s*[x×]\s*(\d{1,2}(?:\.\d+)?)\b(?!\s*/)", re.I)
# two numbers are a dimension only next to a word that has one (a 2 x 1 reducer is two pipe sizes)
_DIM2 = re.compile(r"\b(\d{1,3})\s*[x×]\s*(\d{1,3})\b(?![\s\-]*\d*/)(?=[^/]*\b(?:filters?|grilles?|registers?|diffusers?|returns?|"
                   r"duct|ducts|dampers?|plenums?|boots?)\b)", re.I)
_BTU = re.compile(r"\b(\d{1,3}(?:,\d{3})+|\d{4,7}|\d{1,3}k)\s*(?:btu(?:h|/h|/hr)?s?)\b", re.I)
_MBH = re.compile(r"\b(\d{1,4})\s*(?:mbh|mbtu(?:h|/h)?)\b", re.I)
_BOILER = re.compile(r"\bboilers?\b", re.I)
_BTU_FAMILY = re.compile(r"\b(?:tankless|furnaces?|mini[\s-]?splits?|minisplits?|heat\s+pumps?|air\s+handlers?|"
                         r"unit\s+heaters?|water\s+heaters?|ductless|condensers?)\b", re.I)
_TP = [(re.compile(r"\btemp(?:erature|\.)?\s*(?:and|&|/|n'?)\s*press(?:ure|\.)?(?=\s|$)", re.I), "T&P"),
       # without "and" only before relief or valve ("temperature pressure relief valve"; a gauge keeps its words)
       (re.compile(r"\btemp(?:erature|\.)?\s+press(?:ure|\.)?(?=\s+(?:relief|valve))", re.I), "T&P"),
       (re.compile(r"\bt\s*(?:and|&|/|n)\s*p\b(?=\s+(?:relief\s+)?valves?\b|\s*$)", re.I), "T&P")]
# letters then digits, apart: "le 2634207", "NI 71906-07". Not a PO, a quantity or a word ("pex 1000 ft")
_SPLIT_ID = re.compile(r"(?<![\w-])([A-Za-z]{2,3})\s+(\d{5,9}(?:-\d{1,3})?)(?![\w/-])")
# "1/2 cxc 90s": plural angles are elbows' angles
_ANGLE_S = re.compile(r"\b(90|45|22)s\b", re.I)
# "press 90 3/4", "sweat 45": a bare angle after a fitting word is an elbow (as "cxc 90" already is)
_FIT_CTX = re.compile(r"\b(?:press|sweat|solder|cxc|ftg|ftgxc|pxp|copper|cu|pvc|cpvc|abs|pex|dwv|galv(?:anized)?|black|brass|bronze|"
                      r"street|socket|threaded|fip|mip|sharkbite|push|grooved|no[\s-]?hub)\b", re.I)
_HAS_FITTING = re.compile(r"\b(?:elbows?|ells?|bends?|tees?|couplings?|wyes?|adapters?|unions?|caps?|plugs?|valves?|"
                          r"reducers?|bushings?|nipples?|sweeps?|flanges?|traps?)\b", re.I)
_BARE_ANGLE = re.compile(r"(?<![\d/.-])\b(90|45)\b(?![\d/.%°-]|\s*(?:deg|degrees?|mfd|uf|psi|amps?|a\b|ton|gal|lbs?|v\b|volts?|"
                         r"ft|in\b|inch|min|minutes?|day|days|series|degree))", re.I)
# a transformer's rating is written as one token in titles (40VA): "40 VA", "40 va" (v2.01, Q114)
_VA = re.compile(r"\b(\d{2,4})\s+(va|kva)\b", re.I)
# a pump's discharge size isn't in the titles, and its number matched the horsepower ("2 inch discharge" -> 2 HP
# ejectors, Q078): searched without it, and the page says so. With real data this becomes a discharge attribute
_PORT = re.compile(r"\b(\d+(?:-\d+/\d+|/\d+)?)\s*(?:inch(?:es)?|in\.?|\")\s*(discharge|outlet|inlet|suction)\b", re.I)
_PUMP = re.compile(r"\bpumps?\b|\bejectors?\b", re.I)
# tons and BTU (v2.42): 12,000 BTU/h is one ton. Split-system condensers and heat pumps are listed in tons; mini-splits in BTU
_TON = re.compile(r"\b(\d{1,2}(?:\.\d)?|\d-1/2|\d\s+1/2)\s*-?\s*tons?\b", re.I)
_TON_FAMILY = re.compile(r"\b(?:condensers?|condensing\s+units?|heat\s+pumps?|air\s+conditioners?|a/?c\s+units?|ac\b|"
                         r"split\s+systems?|outdoor\s+units?)\b", re.I)
_MINI = re.compile(r"\b(?:mini[\s-]?splits?|minisplits?|ductless|single[\s-]zone|multi[\s-]zone|wall\s+mount)", re.I)
# "condenser" alone is the outdoor unit; titles call it a condensing unit (v2.42). A condenser's parts keep the word
_CONDENSER = re.compile(r"\bcondensers?\b(?!\s*(?:fan|motor|coil|pad|cover|capacitor|contactor|blade|guard|fin|cleaner|"
                        r"water|tube|tubing|bracket|stand|parts?|board|control))", re.I)
_CONDENSER_PART = re.compile(r"\b(?:fan|motor|coil|pad|cover|capacitor|contactor|blade|guard|cleaner|for|parts?|bracket|"
                             r"stand|board|control|relay|switch|valve|filter|drier|dryer)\b", re.I)
# horsepower in words: "half horse", "3/4 horsepower", "quarter hp", "one horse" (v2.42)
_HP_WORD = {"half": "1/2", "quarter": "1/4", "third": "1/3", "one third": "1/3", "a third": "1/3", "three quarter": "3/4",
            "three quarters": "3/4", "3 quarter": "3/4", "one half": "1/2", "a half": "1/2", "one": "1", "two": "2",
            "one quarter": "1/4", "a quarter": "1/4", "one and a half": "1-1/2", "1 and a half": "1-1/2"}
_HP = re.compile(r"\b(one and a half|1 and a half|three quarters?|3 quarter|one third|a third|one half|a half|one quarter|"
                 r"a quarter|half|quarter|third|one|two|\d+(?:\.\d+)?|\d+/\d+|\d-\d/\d)\s*-?\s*"
                 r"(?:horse\s*power|horsepower|horses?|hp)\b", re.I)
# flow rate (v2.42): the demo catalog lists no GPM for water heaters, and the number matched duct sizes ("5 gpm
# tankless" -> 5 in. duct). Searched without it, and the page says so; with real data it's a flow-rate attribute
_GPM = re.compile(r"\b\d+(?:\.\d+)?\s*-?\s*gpm\b", re.I)
_GPM_HEATER = re.compile(r"\b(?:tankless|water\s+heaters?|on[\s-]demand)\b", re.I)
# a thermocouple's or thermopile's length (v2.44): "thermocouple 24", "36 in thermocouple". The demo catalog lists
# thermocouples by the unit they fit, not by length; the number matched model numbers ("for BJFK-24 Series")
_TC = re.compile(r"\bthermo(?:couple|pile)s?\b", re.I)
_TC_LEN = re.compile(r"\b(1[2-9]|[2-4]\d)\s*(?:\"|in\.?|inch(?:es)?)?(?=\s|$)", re.I)
_NOT_ID = {"po", "pos", "qty", "pcs", "ea", "no", "num", "inv", "ord", "id", "sku", "upc", "pex", "pvc", "abs", "the",
           "and", "for", "btu", "mbh", "gal", "psi", "ft", "lb", "lbs", "oz", "kw", "hp", "amp", "rpm", "cfm", "gpm"}


def _num(s: str) -> int:
    s = s.lower().replace(",", "")
    return int(s[:-1]) * 1000 if s.endswith("k") else int(s)


def rewrite(q: str) -> tuple[str, list[dict]]:
    """(the search in catalog notation, [{"from", "to", "why"}])."""
    out, notes = q or "", []

    def sub(rx, fn, why):
        nonlocal out

        def f(m):
            to = fn(m)
            if to is not None and to.lower() != m.group(0).lower():
                notes.append({"from": m.group(0), "to": to, "why": why})
                return to
            return m.group(0)
        out = rx.sub(f, out)

    # trade words in 12 languages (v2.4; Spanish since v2.01): read into the catalog's English first
    from webbsearch.retrieval import lexicon
    t, lang = lexicon.translate(out)
    if lang is not None and t != out:
        notes.append({"from": out, "to": t, "why": lexicon.name(lang), "lang": lang})
        out = t
    sub(_DIM3, lambda m: f"{m.group(1)}x{m.group(2)}x{m.group(3)}", "dimensions")
    sub(_DIM2, lambda m: f"{m.group(1)}x{m.group(2)}", "dimensions")
    if re.search(r"\bcombi", out, re.I):
        pass                        # combi units are listed both ways (boilers in MBH, combi water heaters in BTU)
    elif _BOILER.search(out) and not _BTU_FAMILY.search(out):
        def to_mbh(m):
            n = _num(m.group(1))
            return f"{n // 1000} MBH" if n >= 10000 and n % 1000 == 0 else None
        sub(_BTU, to_mbh, "boilers are rated in MBH")
    elif _BTU_FAMILY.search(out) and not _BOILER.search(out):
        sub(_MBH, lambda m: f"{int(m.group(1)) * 1000} BTU", "listed in BTU")
    if not _CONDENSER_PART.search(out) and not _MINI.search(out):
        sub(_CONDENSER, lambda m: "condensing unit", "outdoor units are listed as condensing units")
    if _TON_FAMILY.search(out) and not _MINI.search(out) and not _BOILER.search(out):
        def to_ton(m):
            n = _num(m.group(1))
            if 18000 <= n <= 60000 and n % 6000 == 0:
                t = n / 12000
                return f"{t:g} Ton"
            return None
        sub(_BTU, to_ton, "condensers are rated in tons (12,000 BTU = 1 ton)")
    elif _MINI.search(out):
        def to_btu(m):
            g = m.group(1).replace(" ", "-")
            t = int(g[0]) + 0.5 if "1/2" in g else float(g)
            return f"{int(round(t * 12000)):,} BTU" if 0.5 <= t <= 5 else None
        sub(_TON, to_btu, "mini-splits are listed in BTU (1 ton = 12,000 BTU)")
    sub(_HP, lambda m: f"{_HP_WORD.get(m.group(1).lower(), m.group(1))} HP", "horsepower")
    if _GPM_HEATER.search(out):
        sub(_GPM, lambda m: "", "the demo catalog doesn't list water heaters' flow rates (GPM)")
    if _TC.search(out):
        sub(_TC_LEN, lambda m: "", "the demo catalog doesn't list thermocouple lengths")
    if _PUMP.search(out):
        sub(_PORT, lambda m: "", "the demo catalog doesn't list discharge sizes")
    sub(_VA, lambda m: f"{m.group(1)}{m.group(2).upper()}", "rating")
    sub(_ANGLE_S, lambda m: m.group(1), "angle")
    if _FIT_CTX.search(out) and not _HAS_FITTING.search(out) and not re.search(r"\bstreet\b", out, re.I):
        sub(_BARE_ANGLE, lambda m: f"{m.group(1)} elbow", "a 90 is an elbow")
    if re.search(r"\btraps?\b", out, re.I):
        sub(re.compile(r"\bf\s*(?:&|and|/|n)\s*t\b", re.I), lambda m: "Float & Thermostatic", "F&T")
    for rx, to in _TP:
        sub(rx, lambda m, t=to: t, "T&P")
    sub(_SPLIT_ID, lambda m: None if m.group(1).lower() in _NOT_ID else f"{m.group(1).upper()}{m.group(2)}",
        "part number")
    return re.sub(r"\s+", " ", out).strip(), notes
