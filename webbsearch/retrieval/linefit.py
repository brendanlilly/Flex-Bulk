"""How well a catalog item fits one line of a material list.

Search ranks by words; a counter person reads a line by the product it names. "3" NH wye" is a wye, not the
no-hub coupling whose title happens to say "No-Hub"; "2" black elbows" means 90s; "1/2" copper elbows" means
sweat unless it says press; "2" x 1" tees" is a 2" reducing tee; "3" Class 300 gate valves" is a gate valve,
whatever else matched "300". `spec()` reads the line once, `fit()` scores one candidate against it, and
`focus_query()` is the short search to run when the first search found none of the right product.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# (label, how a line says it, how a catalog title says it). Order matters: specific names first, so
# "reducing tee" is a tee, "pipe hanger" a hanger, "ball valve" before "valve", "trap primer" before "primer".
HEADS = [
    # a dual-run rating ("45/5 cap", "35+5"), not a pipe fraction ("1/4 copper cap", "1-1/4 pvc cap")
    ("capacitor", r"capacitor|\bmfd\b|(?<![\d-])[1-9]\d{1,2}\s*[/+]\s*\d{1,2}(?:\.\d)?\b(?!\s*(?:\"|in\b)).*\bcaps?\b|"
                  r"\bcaps?\b.*\b\d{3}\s*v(?:ac)?\b", r"capacitor"),
    ("water hammer arrester", r"hammer\s*arrest", r"hammer arrester"),
    ("trap primer", r"trap\s*primer", r"trap primer"),
    ("threaded rod", r"threaded\s*rod|all[\s-]?thread", r"threaded rod|all[\s-]?thread"),
    ("split ring hanger", r"split\s*ring", r"split ring"),
    ("clevis hanger", r"clevis", r"clevis"),
    ("hanger", r"\bhangers?\b|\bj[\s-]?hooks?\b", r"hanger|j-hook"),
    ("strut channel", r"\bstrut\b(?!\s*clamp)|unistrut channel", r"channel"),
    ("strut clamp", r"strut\s*clamp", r"strut clamp"),
    ("floor sink", r"floor\s*sinks?", r"floor sink"),
    ("floor drain", r"floor\s*drains?", r"floor drain"),
    ("p-trap", r"\bp[\s-]?traps?\b", r"p-trap"),
    ("ball valve", r"ball\s*valves?", r"ball valve"),
    ("gate valve", r"gate\s*valves?", r"gate valve"),
    ("check valve", r"check\s*valves?", r"check valve"),
    ("butterfly valve", r"butterfly\s*valves?", r"butterfly valve"),
    ("globe valve", r"globe\s*valves?", r"globe valve"),
    ("weldolet", r"weldolets?", r"weldolet"),
    ("flange", r"\bflanges?\b", r"flange"),
    ("nipple", r"\bnipples?\b", r"nipple"),
    ("female adapter", r"female\s*adapters?|\bfa\b", r"female adapter"),
    ("male adapter", r"male\s*adapters?|\bma\b", r"male adapter"),
    ("adapter", r"adapters?\b", r"adapter"),
    ("union", r"\bunions?\b", r"\bunion\b"),
    ("wye", r"\bwyes?\b", r"\bwye\b"),
    ("tee", r"\btees?\b", r"\btee\b"),
    ("elbow", r"\belbows?\b|\bells?\b|\b(?:90|45)s?\b(?!\s*/)|\bbends?\b|\bsweeps?\b", r"elbow|\bbend\b|sweep"),
    ("reducer", r"\breducers?\b", r"\breducer\b|reducing coupling|bushing"),
    ("bushing", r"\bbushings?\b", r"bushing"),
    ("coupling", r"\bcouplings?\b", r"coupling"),
    ("cap", r"\bcaps?\b", r"\bcap\b"),
    ("plug", r"\bplugs?\b", r"\bplug\b"),
    ("cement", r"\bcement\b|\bglue\b", r"cement"),
    ("primer", r"\bprimer\b|\bprime\b", r"primer"),
    ("gasket", r"\bgaskets?\b", r"gasket"),
    ("bolt", r"\bbolts?\b", r"\bbolt"),
    ("pipe", r"\b(?:pipe|tube|tubing)\b(?!\s*(?:strap|hanger|clamp|dope|wrench|cutter|insulation|support|sealant|tape|thread))",
     r"\b(?:pipe|tube|tubing)\b(?!\s*(?:strap|hanger|clamp|dope|wrench|cutter|insulation|support|sealant|tape|thread))"),
    ("valve", r"\bvalves?\b", r"valve"),
]
_HEADS = [(n, re.compile(q, re.I), re.compile(t, re.I)) for n, q, t in HEADS]

# materials: a line that names one should not get another (copper fittings are wrot copper or cast bronze)
MATERIALS = {
    "copper": (r"\bcopper\b|\bcu\b", r"copper|bronze|wrot"),
    "black": (r"\bblack\b", r"\bblack\b"),
    "galvanized": (r"\bgalv", r"galv"),
    "cpvc": (r"\bcpvc\b", r"\bcpvc\b"),
    "pvc": (r"\bpvc\b", r"\bpvc\b"),
    "cast iron": (r"cast\s*iron|no-hub", r"cast iron"),
    "stainless": (r"stainless|\bss\b", r"stainless"),
    "pex": (r"\bpex\b", r"\bpex\b"),
    "abs": (r"\babs\b", r"\babs\b"),
    "carbon steel": (r"carbon\s*steel|forged\s*steel|\ba105\b|\ba106\b", r"carbon steel|forged steel|a105|\bblack\b"),
    "brass": (r"\bbrass\b", r"\bbrass\b|bronze"),
}
# how the ends join; a title's is read in this order ("Copper Tee, Press PxMIP" is press, not threaded)
CONNECTIONS = [
    ("press", r"\bpress\b|propress", r"\bpress\b"),
    ("push", r"push|sharkbite", r"push"),
    ("sweat", r"\bsweat\b|\b[cf]\s*x\s*c\b|\bc\s*x\s*(?:street|ftg)\b|\bftg\b|\bsolder", r"\bsweat\b"),
    ("grooved", r"groov", r"groov"),
    ("socket weld", r"socket\s*weld", r"socket weld"),
    ("butt weld", r"\bweld\b(?!\s*neck)|butt\s*weld", r"butt ?weld|\bbw\b"),
    ("threaded", r"thread|\bips\b|\bfip\b|\bmip\b|\bnpt\b|\bscrew", r"thread|\bfip\b|\bmip\b|\bnpt\b|\bfpt\b|\bmpt\b"),
]
FLANGE_KINDS = [("weld neck", r"weld\s*neck", r"weld neck"), ("threaded", r"thread|screw|\bips\b", r"threaded"),
                ("slip-on", r"slip[\s-]?on", r"slip-on"), ("blind", r"\bblind\b", r"\bblind\b"),
                ("socket weld", r"socket\s*weld", r"socket weld")]
FRAC = {"½": "1/2", "¼": "1/4", "¾": "3/4", "⅛": "1/8", "⅜": "3/8", "⅝": "5/8", "⅞": "7/8"}
NUM = r"\d{1,2}-\d/\d{1,2}|\d{1,2} \d/\d{1,2}|\d/\d{1,2}|\d{1,2}(?:\.\d+)?"
INCH = re.compile(rf"(?<![A-WYZa-wyz0-9_/.#-])({NUM})\s*(?:\"|”|″|''|in\b\.?|inch(?:es)?\b)", re.I)
FRAC_FOOT = re.compile(r"(?<![\w/.#-])(\d{1,2}-\d/\d)\s*'(?!')")              # 1-1/2' is inches: pipe isn't sold in 1.5 ft
AFTER_X = re.compile(rf"[x×]\s*({NUM})(?![\w/'\"”.-])", re.I)                    # 4"x2 tee: the 2 is inches too
LEAD_X = re.compile(rf"^\s*({NUM})\s*[x×]\s*({NUM})\s+(?=[a-z])", re.I)          # "2x1 blk bushing"
LEAD = re.compile(rf"^\s*({NUM})\s+(?=[a-z])", re.I)                          # "3 NH wye" once the quantity is gone
CLASS = re.compile(r"\b(125|150|200|250|300|400|600|800|900|1500|2000|3000|6000)\s*(?:#|lbs?\b|wog\b)|\bclass\s*(\d{3,4})\b", re.I)
TYPE_LETTER = re.compile(r"\b(?:type\s*)?([klm])\b(?=[^\n]*\b(?:pipe|tube|tubing|copper)\b)|\bcopper\s+([klm])\b", re.I)


def norm_size(s: str) -> str:
    s = s.strip().replace(" ", "-")
    if re.fullmatch(r"\d+\.0+", s):
        s = s.split(".")[0]
    return {"1.5": "1-1/2", "2.5": "2-1/2", "1.25": "1-1/4", "0.5": "1/2", "0.75": "3/4", ".5": "1/2", ".75": "3/4"}.get(s, s)


FRAC_RE = [(k, v, re.compile(rf"(\d)\s*{k}")) for k, v in FRAC.items()]

def sizes_in(text: str) -> list[str]:
    t = text or ""
    for k, v, rx in FRAC_RE:
        if k in t:
            t = rx.sub(rf"\1-{v}", t).replace(k, v)
    hits = [(m.start(), m.group(1)) for rx in (INCH, FRAC_FOOT) for m in rx.finditer(t)]
    if hits:
        first = min(hits)[0]
        hits += [(m.start(), m.group(1)) for m in AFTER_X.finditer(t) if m.start() > first]
    else:
        m = LEAD_X.match(t) or LEAD.match(t)
        if m:
            hits += [(0, m.group(1))] + ([(1, m.group(2))] if m.re is LEAD_X else [])
    out = []
    for _, s in sorted(hits):
        n = norm_size(s)
        if n not in out and n not in ("0",):
            out.append(n)
    return out


@dataclass
class Spec:
    text: str
    head: str | None = None
    head_rx: re.Pattern | None = None
    sizes: list = field(default_factory=list)
    material: str | None = None
    conn: str | None = None
    klass: str | None = None
    type_letter: str | None = None
    angle: str | None = None           # "45" or "90" for elbows (90 unless the line says 45)
    street: bool = False
    reducing: bool = False
    flange_kind: str | None = None
    length_hint: str | None = None     # "straight" when the line gives a stick length (x10', x20')
    uom: str = ""

    @property
    def size(self):
        return self.sizes[0] if self.sizes else None


def spec(text: str, uom: str = "") -> Spec:
    t = " " + (text or "") + " "
    s = Spec(text=text or "", uom=(uom or "").upper())
    for name, qrx, trx in _HEADS:
        if qrx.search(t):
            s.head, s.head_rx = name, trx
            break
    if not s.head and re.search(r"\btbe\b|threaded both ends|\btoe\b|[x×]\s*\d{1,2}\s*'\s*\d{0,2}", t, re.I):
        s.head, s.head_rx = "pipe", dict((n, r) for n, _, r in _HEADS)["pipe"]        # "1 x 10'6" S80 BLK TBE" is a length of pipe
    if not s.head and (uom or "").upper() in ("FT", "LF") and re.search(r"sch|schedule|groov|copper|black|galv|pvc|type\s*[klm]\b", t, re.I):
        s.head, s.head_rx = "pipe", dict((n, r) for n, _, r in _HEADS)["pipe"]        # 400 FT of "3" grooved sch 40" is pipe
    s.sizes = sizes_in(text)
    for name, (qrx, _) in MATERIALS.items():
        if re.search(qrx, t, re.I):
            s.material = name
            break
    for name, qrx, _ in CONNECTIONS:
        if re.search(qrx, t, re.I):
            s.conn = name
            break
    if s.conn == "sweat" and not s.material:
        s.material = "copper"                  # CxC, FxC: copper fittings
    m = CLASS.search(t)
    s.klass = (m.group(1) or m.group(2)) if m else None
    m = TYPE_LETTER.search(t)
    s.type_letter = (m.group(1) or m.group(2)).upper() if m else None
    if s.head == "elbow":
        s.angle = "45" if re.search(r"\b45\b|1/8\s*bend", t) else "90"
    s.street = bool(re.search(r"\bstreet\b|\bst\.?\s+(?:elbow|ell|90|45|reducer|male|female|adapter|tee|coupling)", t, re.I))
    s.reducing = s.head in ("tee", "coupling", "elbow", "reducer", "bushing", "wye") and len(s.sizes) >= 2 or bool(re.search(r"reduc", t, re.I))
    if s.head == "flange":
        for name, qrx, _ in FLANGE_KINDS:
            if re.search(qrx, t, re.I):
                s.flange_kind = name
                break
    if re.search(r"[x×]\s*\d{1,2}\s*'(?!')|\bsticks?\b|\blengths?\b", t, re.I):
        s.length_hint = "straight"
    return s


def _title_conn(title: str) -> str | None:
    for name, _, trx in CONNECTIONS:
        if re.search(trx, title, re.I):
            return name
    return None


STRICT_CONN = ("grooved", "press", "push", "butt weld", "socket weld")   # a title that doesn't say it isn't it
SHEET_METAL = re.compile(r"\b\d{2} ga\b")                                # HVAC duct: "Wye, 26 GA" is not a pipe wye


def fit(s: Spec, item: dict) -> dict:
    """How one search result fits the line: {noun, size, good, score, misses[]}. `noun`: it is the product the
    line names; `size`: its size is the line's; `good`: nothing the line asked for is missing or contradicted;
    `misses`: what the line asked for that this item isn't (size, material, joint, type...)."""
    brand = (item.get("brand") or "").lower()
    title = (item.get("title") or "").lower()
    if brand and title.startswith(brand):
        title = title[len(brand):]              # "Charlotte Pipe ..." is not a pipe; "Tyler Pipe" neither
    q = s.text.lower()
    attrs = item.get("attrs") or {}
    out = {"noun": True, "size": True, "good": True, "score": 0.0, "misses": [], "soft": []}
    soft = 0
    valve = (s.head or "").endswith("valve") or s.head == "union"      # material is a spec, not the product
    if s.head:
        out["noun"] = bool(s.head_rx.search(title))
        if s.head == "cap" and "capacitor" in title:
            out["noun"] = False
        if s.head == "bushing" and s.material == "copper" and re.search(r"reduc", title):
            out["noun"] = True                  # a copper "bushing" is a fitting reducer (FTG x C)
        if SHEET_METAL.search(title) and not re.search(r"\bga\b|duct|gauge", q):
            out["noun"] = False
        if not out["noun"]:
            out["misses"].append(s.head)
    isz = norm_size(str(attrs.get("size") or ""))
    if s.size and isz:
        out["size"] = isz == s.size
        if not out["size"]:
            out["misses"].append(f'{s.size}"')
    sc = 0.0
    if s.material:
        own = re.search(MATERIALS[s.material][1], title, re.I) or (valve and s.material == "copper" and re.search(r"brass|bronze", title))
        other = [m for m, (_, trx) in MATERIALS.items() if m != s.material and re.search(trx, title, re.I)
                 and not (s.material == "carbon steel" and m == "black") and not (s.material == "black" and m == "carbon steel")]
        if own:
            sc += 2
        elif other:
            sc -= 4
            out["misses"].append(MATERIAL_LABEL.get(s.material, s.material))
            if not valve:
                out["noun"] = False             # a cast iron cap is not a bronze cap; PVC pipe is not copper pipe
    tc = _title_conn(title)
    if s.conn:
        if tc == s.conn:
            sc += 2
        elif tc or s.conn in STRICT_CONN:
            sc -= 3
            out["misses"].append(s.conn)
            if s.conn in ("grooved", "butt weld", "socket weld") and not valve:
                out["noun"] = False             # a threaded elbow is no use on a butt-weld or grooved job
    elif s.material == "copper" and s.head not in ("pipe", None):
        # "copper elbow" at the counter is a sweat elbow, all-copper ends unless it says otherwise
        sc += 3 if tc == "sweat" else (-2 if tc in ("press", "push") else 0)
        if tc in ("press", "push") and not valve:
            soft += 1
        if tc == "sweat" and re.search(r"cxc|pxp", title):
            sc += 1
    elif s.material in ("pvc", "cpvc", "abs") and s.head not in ("pipe", "cement", "primer", None):
        sc += 3 if "socket" in title else (-2 if "thread" in title else 0)     # PVC fittings are glued
        soft += "thread" in title
    # FTG ends are street-type fittings (FTGxC reducer, FTGxP tee); a plain fitting has two cup/press ends
    if "ftgx" in title and s.head not in ("male adapter", "female adapter", "adapter"):
        if re.search(r"\bftg\b|\bf\s*x\s*[cpm]\b", q) or s.street or s.head == "bushing":
            sc += 2
        else:
            sc -= 2
            soft += 1
    if s.head in ("tee", "elbow", "coupling", "cap", "wye", "reducer", "bushing") and not re.search(r"thread|mip|fip|npt|ips|adapter", q):
        if re.search(r"x?(?:mip|fip)\b", title):
            sc -= 2                              # plain fittings first: a tee, not a tee with a threaded branch
            soft += 1
    for sysw in ("pex", "push"):                 # a system the line doesn't mention
        if sysw in title and sysw not in q and not (sysw == "push" and "sharkbite" in q):
            sc -= 3
            soft += 1
    if s.klass:
        m = re.search(r"\b(\d{3,4})\s*(?:#|wog)", title)
        if m:
            sc += 2 if m.group(1) == s.klass else -2
    if s.type_letter:
        m = re.search(r"type ([klm])\b", title)
        if m:
            if m.group(1).upper() == s.type_letter:
                sc += 3
            else:
                sc -= 4
                out["misses"].append(f"type {s.type_letter}")
    elif s.head == "pipe" and s.material == "copper" and "type l" in title:
        sc += 2                                  # water pipe with no type given: L is the usual
    if s.head == "elbow":
        t45 = bool(re.search(r"\b45\b|1/8 bend", title))
        t90 = bool(re.search(r"\b90\b|1/4 bend|sweep", title))
        if s.angle == "45":
            sc += 3 if t45 else -4
            if not t45:
                soft += 1
                out["soft"].append("45")
        else:
            sc += 2 if t90 else (-4 if t45 else 0)
            if t45:
                soft += 1
                out["soft"].append("90")
        for sw in ("short sweep", "long sweep"):
            if sw in q:
                sc += 2 if sw in title else 0
    tst = "street" in title or (s.head not in ("elbow", None) and "ftgx" in title)
    if s.street:
        sc += 3 if tst else -2
        if not tst:
            soft += 1
            out["soft"].insert(0, "street")
    elif tst and not re.search(r"\bftg\b|\bf\s*x\s*[cpm]\b", q) and s.head != "bushing":
        sc -= 3
        soft += 1
    tred = "reduc" in title
    if s.reducing and s.head not in ("reducer", "bushing", "nipple"):
        sc += 3 if tred else -1
        if not tred:
            soft += 1
            out["soft"].insert(0, "reducing")
    elif tred and s.head not in ("reducer", "bushing"):
        sc -= 3
        soft += 1
    if s.head == "tee" and s.material == "cast iron" and "cleanout" not in q:
        sc += 2 if "sanitary" in title else (-2 if "cleanout" in title else 0)
    if s.head in ("female adapter", "male adapter"):
        want = s.head.split()[0]
        if ("female" in title) != (want == "female"):
            sc -= 4
            soft += 1
    if s.flange_kind:
        own = re.search(dict((n, r) for n, _, r in FLANGE_KINDS)[s.flange_kind], title)
        if own:
            sc += 3
        elif any(re.search(r, title) for n, _, r in FLANGE_KINDS if n != s.flange_kind):
            sc -= 3
            out["misses"].append(f"{s.flange_kind}")
    if s.head == "pipe":
        if s.length_hint == "straight":
            sc += 2 if "straight" in title else (-2 if "coil" in title else 0)
            if "coil" in title:
                soft += 1
                out["soft"].append("straight-length")
        elif s.uom not in ("FT", "LF") and s.material == "copper":
            sc += 2 if "straight" in title else 0     # "10 1/2 copper pipe" is ten sticks
            if "coil" in title:
                soft += 1
                out["soft"].append("straight-length")
    if s.head == "nipple" and len(s.sizes) >= 2:
        m = re.search(r"nipple ([\d/-]+) in", title)
        if m:
            if norm_size(m.group(1)) == s.sizes[1]:
                sc += 3
            else:
                sc -= 1
                soft += 1
                out["soft"].append(f'{s.sizes[1]}" long')
    if s.head == "nipple" and "close" in q:
        sc += 3 if "close" in title else -1
    out["score"] = sc
    out["good"] = out["noun"] and out["size"] and not out["misses"] and soft == 0
    return out


def head_label(s: Spec) -> str:
    return "45 elbow" if s.head == "elbow" and s.angle == "45" else (s.head or "item")


MATERIAL_LABEL = {"pvc": "PVC", "cpvc": "CPVC", "pex": "PEX", "abs": "ABS"}


def describe(s: Spec, only: list | None = None) -> str:
    """'3" galvanized grooved pipe': what the line asks for, for "No ... in this catalog"."""
    parts = [f'{s.size}"' if s.size else "", MATERIAL_LABEL.get(s.material or "", s.material or ""),
             s.conn if s.conn in STRICT_CONN else "", f"type {s.type_letter}" if s.type_letter else "", head_label(s)]
    return " ".join(p for p in parts if p)


def focus_query(s: Spec, with_size: bool = True) -> str | None:
    """A short search for the product the line names: size, material, joint, head noun."""
    if not s.head:
        return None
    parts = []
    if s.size and with_size:
        parts.append(f"{s.size} in.")
    if s.material and s.material != "cast iron":
        parts.append(s.material)
    elif s.material == "cast iron":
        parts.append("cast iron")
    if s.conn and s.conn not in ("butt weld",) and not s.flange_kind:
        parts.append(s.conn)
    elif not s.conn and s.material == "copper" and s.head in ("tee", "elbow", "coupling", "cap"):
        parts.append("sweat" if s.street else "sweat CxC")      # plain copper fittings
    if s.klass:
        parts.append(s.klass + "#")
    if s.type_letter:
        parts.append(f"type {s.type_letter}")
    if s.street:
        parts.append("street")
    if s.reducing and s.head in ("tee", "coupling", "elbow"):
        parts.append("reducing")
    if s.head in ("reducer", "coupling") and re.search(r"\bftg\b|\bf\s*x\s*c\b", s.text, re.I):
        parts.append("FTGxC")
    if s.flange_kind:
        parts.append(s.flange_kind)
    if s.head == "pipe" and (s.length_hint == "straight" or (s.material == "copper" and s.uom not in ("FT", "LF"))):
        parts.append("straight length")
    if s.head == "pipe" and s.material == "copper" and not s.type_letter:
        parts.append("type L")
    head = {"reducer": "reducing coupling"}.get(s.head, s.head)          # the catalog's word for a copper reducer
    if s.head == "bushing" and s.material == "copper":
        head = "reducing coupling FTGxC"
    if s.head == "elbow":
        head = ("1/8 bend" if s.angle == "45" else "1/4 bend") if s.material == "cast iron" else f"{s.angle} elbow"
    parts.append(head)
    return " ".join(parts)
