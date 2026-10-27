"""Requirements a search states: what every product on the page must have, whatever the sort.

"3/4 press ball valve lead free" asks for lead-free, press ends and a 3/4 in. size. The ranker already puts those
first, but a price sort or a stock filter used to surface valves that are neither. Requirements read from the query
turn into applied constraints on page membership, shown above the results ("From your search: 3/4 in. · Press ·
Lead-free") and removable one at a time. A requirement nothing on the page meets is not applied; the page says so.

Materials are read from the product's own material, never its brand: Merit Brass makes stainless unions.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

# connection words -> (key, label, test on "connection attr + title" lower-cased)
_CONN = [
    ({"press", "propress", "megapress"}, "press", "Press", r"\bpress\b|propress|megapress"),
    ({"sweat", "solder", "soldered"}, "sweat", "Sweat", r"\bsweat\b|\bsolder"),
    ({"npt", "threaded", "thread", "ips"}, "threaded", "Threaded", r"fip|mip|\bnpt\b|thread|fpt|mpt"),
    ({"fip", "fpt"}, "fip", "FIP", r"fip|fpt|\bfemale\b"),
    ({"mip", "mpt"}, "mip", "MIP", r"mip|mpt|\bmale\b"),
    ({"grooved"}, "grooved", "Grooved", r"groov"),
    ({"flanged"}, "flanged", "Flanged", r"flanged"),
    ({"compression"}, "compression", "Compression", r"compression|\bcomp\b"),
    ({"socket"}, "socket", "Socket", r"socket"),
    ({"flare"}, "flare", "Flare", r"flare"),
    ({"crimp"}, "crimp", "Crimp", r"crimp"),
    ({"pex"}, "pex", "PEX", r"\bpex\b"),
    ({"push"}, "push", "Push-to-connect", r"push"),
]
# material words -> (key, label, test on the product's material group)
_MAT = [
    ({"brass"}, "brass", "Brass", {"Brass"}),
    ({"bronze"}, "bronze", "Bronze", {"Bronze"}),
    ({"copper"}, "copper", "Copper", {"Copper"}),
    ({"stainless", "ss"}, "stainless", "Stainless", {"Stainless"}),
    ({"cpvc"}, "cpvc", "CPVC", {"CPVC"}),
    ({"pvc"}, "pvc", "PVC", {"PVC"}),
    ({"abs"}, "abs", "ABS", {"ABS"}),
    ({"galvanized", "galv"}, "galvanized", "Galvanized", {"Galvanized"}),
]
_BIGRAM_MAT = [(("black", "iron"), "black_iron", "Black iron", {"Black iron"}),
               (("black", "malleable"), "black_iron", "Black iron", {"Black iron"}),
               (("black", "steel"), "black_iron", "Black iron", {"Black iron"}),
               (("carbon", "steel"), "carbon_steel", "Carbon steel", {"Carbon steel"}),
               (("cast", "iron"), "cast_iron", "Cast iron", {"Cast iron"}),
               (("ductile", "iron"), "ductile", "Ductile iron", None)]
# "black" on its own is black iron for the products that come in it ("3/4 x close black nipple")
_BLACK_NOUNS = {"nipple", "pipe", "fitting", "elbow", "tee", "coupling", "union", "cap", "plug", "bushing", "malleable",
                "cross", "reducing", "flange"}
# features the product's own title states: bigrams (or single words) -> (key, label, test, known)
_FEATURES = [
    (("full", "port"), "full_port", "Full port", r"full port|\bfp\b", r"\bport\b"),
    (("standard", "port"), "std_port", "Standard port", r"standard port|\bstd port", r"\bport\b"),
    (("reduced", "port"), "std_port", "Standard port", r"standard port|reduced port", r"\bport\b"),
    (("power", "vent"), "power_vent", "Power vent", r"power (?:direct )?vent", r"\bvent\b"),
    (("direct", "vent"), "direct_vent", "Direct vent", r"direct vent", r"\bvent\b"),
    (("3", "piece"), "3pc", "3-piece", r"3-piece|3 piece|three piece|\b3 ?pc\b", r"piece|\bpc\b"),
    (("three", "piece"), "3pc", "3-piece", r"3-piece|3 piece|three piece|\b3 ?pc\b", r"piece|\bpc\b"),
    (("2", "piece"), "2pc", "2-piece", r"2-piece|2 piece|two piece|\b2 ?pc\b", r"piece|\bpc\b"),
    (("insulated",), "insulated", "Insulated", r"insulat", None),
    (("raised", "face"), "rf", "Raised face", r"\brf\b|raised face", r"\b(?:rf|ff|raised face|flat face|full face)\b"),
    (("flat", "face"), "ff", "Flat face", r"\bff\b|flat face|full face", r"\b(?:rf|ff|raised face|flat face|full face)\b"),
    (("type", "k"), "type_k", "Type K", r"\btype k\b", r"\btype [klm]\b|\bdwv\b|\bacr\b|\bmedical\b"),
    (("type", "l"), "type_l", "Type L", r"\btype l\b", r"\btype [klm]\b|\bdwv\b|\bacr\b|\bmedical\b"),
    (("type", "m"), "type_m", "Type M", r"\btype m\b", r"\btype [klm]\b|\bdwv\b|\bacr\b|\bmedical\b"),
    (("pre", "charged"), "precharged", "Pre-charged", r"pre-?charged", None),
]
# fuels: what the product burns or runs on, for products that state one (water heaters, boilers, furnaces)
_FUELS = [(("natural", "gas"), "natural_gas", "Natural gas", r"natural gas|\bng\b"),
          (("natgas",), "natural_gas", "Natural gas", r"natural gas|\bng\b"),
          (("propane",), "propane", "Propane", r"propane|\blp\b"),
          (("electric",), "electric", "Electric", r"electric")]
_FUEL_ANY = re.compile(r"natural gas|propane|\blp\b|electric|\bgas\b|\boil\b")
# families where material is a property of the product (pipe, fittings, valves, sinks), not of what it's for
MATERIAL_FAMILIES = {"ball_valve", "buttweld", "cu_fitting_press", "cu_fitting_sweat", "cu_pipe", "dwv_fitting", "flange",
                     "gate_check_valve", "grooved", "iron_fitting", "nohub", "pex_fitting", "pex_tubing", "push_fitting",
                     "pvc_pressure", "ss_fitting", "waterworks", "stop_valve", "mixing_valve", "relief_valve", "prv",
                     "backflow", "sink", "drainage", "gas", "hangers"}
# every word a requirement reads: not part of the product's name ("brass union" is a union that must be brass)
WORDS = set().union(*(w for w, *_ in _CONN), *(w for w, *_ in _MAT), *(set(b) for b, *_ in _BIGRAM_MAT), {"leadfree"})
MAT_WORDS = set().union(*(w for w, *_ in _MAT), {"black", "cast", "ductile", "carbon"})
_CONN_ANY = re.compile("|".join(p for *_, p in _CONN))
# v2.4 (review G02/G11): families whose ends are fixed by what they are, so a title that doesn't say them still states them.
# Black and galvanized malleable 150# fittings are threaded; DWV fittings are hub (solvent weld); no-hub is no-hub. A
# "3/4 pro press elbow" can no longer admit a black malleable elbow because it never mentions its ends
FAMILY_CONN = {"iron_fitting": "threaded fip", "dwv_fitting": "hub", "nohub": "no-hub"}
# metric lengths, typed with their unit: millimeters and centimeters only. A bare "m" is Type M copper in the trade
# ("1 1/4 m copper", 96 searches a year on fwwebb.com), never meters
_METRIC = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*(mm|millimet(?:er|re)s?|cm|centimet(?:er|re)s?)\b", re.I)
# shapes a product states in its title (capacitors round or oval; toilets and seats round or elongated; duct)
_SHAPES = {"round": "Round", "oval": "Oval", "elongated": "Elongated", "rectangular": "Rectangular", "square": "Square"}
_SHAPE_ANY = re.compile(r"\b(?:round|oval|elongated|rectangular|square)\b")
# both ends the same, the trade's shorthand: CxC (sweat x sweat), PxP (press x press). The catalog writes "Sweat CxC",
# "Press PxP", or for valves just "Sweat" / "Press" (both ends); a CxMIP adapter or a PxFIP elbow is not CxC / PxP
_OTHER_END = r"mip|fip|mpt|fpt|ftg|male|female|thread|npt|pex|push|groov|flare|compression|crimp|socket|flange"
_SAME_ENDS = [
    ({"cxc"}, ("c", "c"), ("sweat", "sweat"), "cxc", "C x C (sweat both ends)",
     rf"\bcxc\b|\bc x c\b|^(?!.*(?:{_OTHER_END}|press|\bpx|xp\b)).*\b(?:sweat|solder)"),
    ({"pxp"}, ("p", "p"), ("press", "press"), "pxp", "P x P (press both ends)",
     rf"\bpxp\b|\bp x p\b|^(?!.*(?:{_OTHER_END}|sweat|\bcx|xc\b)).*\b(?:press|propress|megapress)\b"),
]


def metric_sizes(raw: str) -> list[tuple[float, str]]:
    """Millimeter values typed in a search, with how they were typed: "10mm" -> [(10.0, "10 mm")], "1 cm" -> [(10.0, "1 cm")]."""
    out = []
    for m in _METRIC.finditer(raw or ""):
        v = float(m.group(1))
        unit = "cm" if m.group(2).lower().startswith("c") else "mm"
        out.append((v * 10 if unit == "cm" else v, f"{m.group(1)} {unit}"))
    return out


def _metric_rx(mm: float) -> re.Pattern:
    alts = [rf"{mm:g}\s?mm", rf"{mm / 10:g}\s?cm"]
    return re.compile(r"(?<![\d.])(?:" + "|".join(alts) + r")\b", re.I)


def metric_requirement(raw: str) -> Requirement | None:
    """v2.4 (review G01): a size typed in millimeters or centimeters is a strict requirement on the product's own size.
    10 mm and 1 cm are the same length; neither is a 10 in. or a 1 in. product."""
    ms = metric_sizes(raw)
    if not ms:
        return None
    mm, typed = ms[0]
    rx = _metric_rx(mm)
    return Requirement(f"size:{mm:g}mm", "size", typed, lambda it, rx=rx: bool(rx.search(it["text"])),
                       words={"mm", "cm", "millimeter", "centimeter"}, strict=True)
_DUAL_MFD = re.compile(r"^(\d{2,3}(?:\.\d)?)/(\d{1,2}(?:\.\d)?)$")


@dataclass
class Requirement:
    key: str
    kind: str                       # leadfree | connection | material | type
    label: str
    test: Callable[[dict], bool] = field(repr=False)
    families: set | None = None     # applies only to these families (others: not applicable, kept)
    status: str = "applied"         # applied | unmet | relaxed | n/a
    count: int = 0
    known: Callable[[dict], bool] | None = field(default=None, repr=False)   # does the product state this at all?
    unknown: int = 0                # products kept because they don't state it (a forged tee that doesn't say its ends)
    words: set = field(default_factory=set, repr=False)   # the query words it reads (not part of the product's name)
    # v2.4 (review G01): a strict requirement is never relaxed to fill the page. Nothing meets it: no products, and the
    # page says why (a metric size the catalog doesn't list is not an inch size)
    strict: bool = False

    def to_dict(self) -> dict:
        return {"key": self.key, "kind": self.kind, "label": self.label, "status": self.status, "count": self.count,
                "unknown": self.unknown, **({"strict": True} if self.strict else {})}


def _has(toks: list[str], gram: tuple) -> bool:
    n = len(gram)
    return any(tuple(toks[j:j + n]) == gram for j in range(len(toks) - n + 1))


def detect(tokens: list[str], sizes: list[str], skip: set | frozenset = frozenset()) -> list[Requirement]:
    """Requirements stated by a query's tokens (analyzer output: the product phrase and its specs, not what follows
    "for" or "with"). Each test takes an item dict with `text` (the connection attribute and the title without the
    brand, lower-cased), `material` (its group) and `family`. `skip`: material words that name a component ("closet
    flange stainless steel ring": the ring is stainless, the flange PVC)."""
    toks = list(tokens)
    have = set(toks)
    out: list[Requirement] = []
    if "leadfree" in have:
        out.append(Requirement("leadfree", "leadfree", "Lead-free", lambda it: "leadfree" in it["text"] or "lead-free" in it["text"],
                               words={"leadfree"}))
    for words, key, label, pat in _CONN:
        if have & words:
            rx = re.compile(pat)
            out.append(Requirement("conn:" + key, "connection", label, lambda it, rx=rx: bool(rx.search(it["text"])),
                                   known=lambda it: bool(it["conn"]) or bool(_CONN_ANY.search(it["text"])),
                                   words=have & words))
    # "FTG x C": a fitting end by a cup end (the catalog writes FTGxC)
    for j in range(len(toks) - 1):
        if toks[j] == "ftg" and toks[j + 1] in ("c", "p", "fip", "mip", "ftg"):
            other = toks[j + 1]
            rx = re.compile(rf"ftg\s*x?\s*{other}\b|ftgx{other}\b")
            out.append(Requirement("conn:ftgx" + other, "connection", f"FTG x {other.upper()}",
                                   lambda it, rx=rx: bool(rx.search(it["text"])),
                                   known=lambda it: bool(it["conn"]) or bool(_CONN_ANY.search(it["text"])),
                                   words={"ftg", other}))
            break
    # "C x M": sweat by male thread, the trade's shorthand for a male adapter's ends
    for j in range(len(toks) - 1):
        if toks[j] == "c" and toks[j + 1] in ("m", "f"):
            male = toks[j + 1] == "m"
            rx = re.compile(r"\bmale\b|mip|mpt|cxm" if male else r"\bfemale\b|fip|fpt|cxf")
            out.append(Requirement("conn:" + ("cxm" if male else "cxf"), "connection", "C x M" if male else "C x F",
                                   lambda it, rx=rx: bool(rx.search(it["text"])),
                                   known=lambda it: bool(it["conn"]) or bool(_CONN_ANY.search(it["text"])),
                                   words={"c", toks[j + 1]}))
            break
    # both ends the same (v2.4, review G02): "cxc", "c x c", "sweat x sweat"; "pxp", "press x press"
    three_end = any(toks[j:j + 3] in (["c", "c", "f"], ["c", "c", "m"], ["c", "c", "fip"], ["c", "c", "mip"],
                                      ["p", "p", "f"], ["p", "p", "m"]) for j in range(len(toks) - 2)) \
        or bool(have & {"cxcxf", "cxcxm", "cxcxc", "pxpxf", "pxpxp"})      # a tee's three ends: not "both ends"
    for words, pair, pair2, key, label, pat in ([] if three_end else _SAME_ENDS):
        if have & words or _has(toks, pair) or _has(toks, pair2):
            rx = re.compile(pat)
            out = [r for r in out if not (r.kind == "connection" and r.key in ("conn:sweat", "conn:press"))]
            out.append(Requirement("conn:" + key, "connection", label, lambda it, rx=rx: bool(rx.search(it["text"])),
                                   known=lambda it: bool(it["conn"]) or bool(_CONN_ANY.search(it["text"])),
                                   words=(have & words) | set(pair) | set(pair2)))
            break
    # a shape the product states (v2.4, review G02): a round capacitor is never an oval one
    shapes = [w for w in _SHAPES if w in have]
    if len(shapes) == 1:
        w = shapes[0]
        rx = re.compile(rf"\b{w}\b")
        out.append(Requirement("shape:" + w, "shape", _SHAPES[w], lambda it, rx=rx: bool(rx.search(it["text"])),
                               known=lambda it: bool(_SHAPE_ANY.search(it["text"])), words={w}))
    mats = []
    for (a, b), key, label, groups in _BIGRAM_MAT:
        if _has(toks, (a, b)) and a not in skip:
            mats.append((key, label, groups, {a, b}))
    if "black" in have and "black" not in skip and not any(m[0] == "black_iron" for m in mats) and have & _BLACK_NOUNS:
        mats.append(("black_iron", "Black iron", {"Black iron"}, {"black"}))
    for words, key, label, groups in _MAT:
        if have & words and not (have & words) <= set(skip):
            mats.append((key, label, groups, have & words))
    mats = list(dict((m[0], m) for m in mats).values())
    if len(mats) >= 2:
        # two materials: a transition between them ("copper to PVC"), or a category word after the product ("Cast
        # Bronze ... copper", "CPVC ... pvc"). Neither holds for every product: none is required
        mats = []
    for key, label, groups, ws in mats:
        if groups is None:          # ductile iron: the words themselves
            test = (lambda it: "ductile" in it["text"])
        else:
            test = (lambda it, g=groups: it["material"] in g)
        out.append(Requirement("mat:" + key, "material", label, test, MATERIAL_FAMILIES,
                               known=lambda it: bool(it["material"]) or "ductile" in it["text"], words=set(ws) | {"steel"}))
    for gram, key, label, pat, known in _FEATURES:
        if _has(toks, gram) and not any(r.key == "feat:" + key for r in out):
            rx, kx = re.compile(pat), (re.compile(known) if known else None)
            out.append(Requirement("feat:" + key, "feature", label, lambda it, rx=rx: bool(rx.search(it["text"])),
                                   known=(lambda it, kx=kx: bool(kx.search(it["text"]))) if kx else None,
                                   words={g for g in gram if not g.isdigit() and len(g) > 1}))
    fuels = []
    for gram, key, label, pat in _FUELS:
        if _has(toks, gram) and key not in {f[0] for f in fuels}:
            fuels.append((key, label, pat, set(gram)))
    if len(fuels) == 1:             # two fuels is a contradiction the page reports (see conflicts())
        key, label, pat, ws = fuels[0]
        rx = re.compile(pat)
        out.append(Requirement("fuel:" + key, "fuel", label, lambda it, rx=rx: bool(rx.search(it["text"])),
                               known=lambda it: bool(_FUEL_ANY.search(it["text"])), words=ws | {"gas"}))
    # a dual rating (45/5 MFD) is a dual run capacitor by definition; "single" asks for a single run capacitor
    if any(_DUAL_MFD.match(s) and float(_DUAL_MFD.match(s).group(1)) > float(_DUAL_MFD.match(s).group(2)) for s in sizes) \
            and not ({"start", "hard", "kit", "motor"} & have):      # a start capacitor or hard start kit says so
        out.append(Requirement("type:dual_run", "type", "Dual run capacitor", lambda it: "dual run" in it["text"], {"capacitor"}))
    elif "capacitor" in have and "single" in have and not ({"phase", "pole", "stage"} & have):
        out.append(Requirement("type:single_run", "type", "Single run capacitor",
                               lambda it: not re.search(r"dual|start|\d/\d", it["text"]), {"capacitor"}, words={"single"}))
    return out


def conflicts(tokens: list[str]) -> list[str]:
    """Words in one search that can't both hold: two fuels ("electric natural gas water heater")."""
    toks = list(tokens)
    fuels = []
    for gram, key, label, _ in _FUELS:
        if _has(toks, gram) and label not in fuels:
            fuels.append(label)
    # "gas" alone is a fuel only next to "electric" ("gas electric water heater", review T14); elsewhere it names the
    # product's use (gas valve, gas connector) (v2.43)
    if "Electric" in fuels and "gas" in toks and not any(f in fuels for f in ("Natural gas", "Propane")):
        fuels.insert(0, "Gas")
    return fuels if len(fuels) > 1 else []
