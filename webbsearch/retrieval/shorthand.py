"""Trade shorthand -> catalog words, for searching lines from handwritten notes, emails and material lists.

"2-1¼ 3M F/S IPS Tee" is a 3000# forged steel threaded tee; "set 1¼ clo-6 XH BLK nip" is extra-heavy black
nipples, close to 6"; "4 NH wye" is no-hub; "Wibco LD 2003 L/O BFV" is a NIBCO lever-operated butterfly valve
(the W is how the scan read the N). The line the customer wrote is kept and shown; this only changes what
is searched, and the row says "searched as ...".
"""
from __future__ import annotations

import re

# whole-token replacements (lower-case keys); multi-word keys first
PHRASES = [
    (r"\b(\d{2,4})\s*(?:lbs?|#)(?=\s|$)", r"\1#"), (r"\bst\.?\s+(?:elbows?|ells?|90s?)\b", "street elbow"), (r"\bst\.?\s+45s?\b", "street 45 elbow"),
    (r"\bl\s*/\s*r\b", "long radius"), (r"\bs\s*/\s*w\b", "socket weld"), (r"\br\s*/\s*f\b", "rf"), (r"\bf\s*/\s*s\b", "forged steel"),
    (r"\bb\s*/\s*[vu]\b", "ball valve"), (r"\bl\s*/\s*o\b", "lever"), (r"\bw\s*/\s*", "with "), (r"\bst\s+ells?\b", "street elbow"),
    (r"\bslip\s*-?\s*on\b", "slip-on"), (r"\bslip\s*un\b", "slip-on"), (r"\bpro\s*-?\s*press\b", "press"), (r"\bcap\s+bolts?\b", "bolt"),
    (r"\bthread(ed)?\s*rods?\b|\btreadrods?\b", "threaded rod"), (r"\bno\s*-?\s*hub\b", "no-hub"),
    (r"\b(90|45)\s*'?\s*s\b", r"\1 elbow"), (r"\b(90|45)\s*°", r"\1"),
]
TOKENS = {
    "3m": "3000#", "3000": "3000#", "ips": "threaded", "fip": "fip", "wnf": "weld neck flange", "flg": "flange", "flgs": "flange",
    "fl": "flange", "fls": "flange", "fle": "flange", "ell": "elbow", "ells": "elbow", "el": "elbow", "eul": "elbow", "euls": "elbow",
    "blk": "black", "bck": "black", "buk": "black", "bek": "black", "nip": "nipple", "nips": "nipple", "clo": "close",
    "tbe": "threaded both ends", "toe": "threaded one end", "xh": "extra heavy", "std": "standard", "ste": "standard",
    "bv": "ball valve", "bfv": "butterfly valve", "bfu": "butterfly valve", "bfvs": "butterfly valve", "scv": "swing check valve",
    "scu": "swing check valve", "gv": "gate valve", "nh": "no-hub", "hn": "no-hub", "hd2000": "heavy duty coupling cast iron", "hd": "heavy duty", "cs": "carbon steel", "fxm": "ftg male", "glue": "cement",
    "galv": "galvanized", "cplg": "coupling", "cplgs": "coupling", "wol": "weldolet", "hummer": "hammer", "arrestor": "arrester",
    "wyes": "wye", "wye's": "wye", "pvc": "pvc", "kindorf": "strut", "busing": "bushing", "gaskets": "gasket", "baskets": "gasket",
}
# words as the handwriting reader tends to return them (Apple Vision on counter notes): gaskets, unions,
# bushings, nipples, gauges, couplings. Only whole words, so ordinary text is not touched.
TOKENS.update({
    "guskets": "gasket", "gukets": "gasket", "luskets": "gasket", "basketss": "gasket", "gasket": "gasket",
    "unin": "union", "uniun": "union", "unim": "union", "uin": "union", "unian": "union",
    "busins": "bushing", "bushins": "bushing", "bushinss": "bushing", "bushinq": "bushing",
    "nis": "nipple", "vis": "nipple", "vin": "nipple", "uis": "nipple", "nibs": "nipple",
    "couptins": "coupling", "couplins": "coupling", "cuplings": "coupling", "cplings": "coupling",
    "qauce": "gauge", "guoge": "gauge", "gauqe": "gauge", "qauge": "gauge", "pressuse": "pressure",
    "eus": "elbow", "euls": "elbow", "elus": "elbow", "13fu": "butterfly valve", "bfus": "butterfly valve", "13fv": "butterfly valve",
    "bults": "bolt", "bult": "bolt", "rie": "rf", "rif": "rf", "slipm": "slip-on", "slipun": "slip-on",
})
BRAND_FIX = {"wibco": "NIBCO", "wibe": "NIBCO", "wibro": "NIBCO", "nibeo": "NIBCO", "nibc0": "NIBCO", "wib": "NIBCO", "nibco": "NIBCO"}


def _dl1(a: str, b: str) -> bool:
    """Damerau-Levenshtein distance <= 1."""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        d = [i for i in range(len(a)) if a[i] != b[i]]
        return len(d) == 1 or (len(d) == 2 and d[1] == d[0] + 1 and a[d[0]] == b[d[1]] and a[d[1]] == b[d[0]])
    s, t = (a, b) if len(a) < len(b) else (b, a)
    return any(s == t[:i] + t[i + 1:] for i in range(len(t)))


def expand(text: str, brands: dict[str, str] | None = None) -> str:
    """Shorthand line -> search words. `brands` maps lower-case one-word brand names to their spelling."""
    t = " " + (text or "") + " "
    t = re.sub(r"\bwib\s+co\b", "wibco", t, flags=re.I)
    # typed without spaces: "300GateValves" -> "300 Gate Valves", "300#CS" -> "300# CS"
    t = re.sub(r"(\d)(?=[A-Z][a-z]{2,})", r"\1 ", t)
    t = re.sub(r"([a-z]{3,})(?=[A-Z][a-z]{2,})", r"\1 ", t)
    t = re.sub(r"#(?=[A-Za-z])", "# ", t)
    t = re.sub(r"(\d\s*[\"”])(?=[A-Za-z])(?![xX×]\s*\d)", r"\1 ", t)          # 2"NH 90 -> 2" NH 90
    t = re.sub(r"(?<![\d/.-])([1-9])(1/[248]|3/4)(?![\d/])", r"\1-\2", t)          # 11/4 -> 1-1/4, 21/2 -> 2-1/2
    for pat, rep in PHRASES:
        t = re.sub(pat, rep, t, flags=re.I)
    out = []
    for w in t.split():
        k = w.lower().strip(",.;:()")
        if k in BRAND_FIX:
            out.append(BRAND_FIX[k]); continue
        if k in TOKENS:
            out.append(TOKENS[k]); continue
        if brands and len(k) >= 5 and k.isalpha() and k not in brands:
            hit = next((v for b, v in brands.items() if len(b) >= 5 and (_dl1(k, b) or _dl1("n" + k[1:], b))), None)
            if hit:
                out.append(hit); continue
        out.append(w)
    t = re.sub(r"\s+", " ", " ".join(out)).strip()
    # on no-hub (cast iron DWV) an elbow is a bend: a 90 is a 1/4 bend, a 45 a 1/8 bend, a 22 a 1/16 bend (v2.4)
    if re.search(r"\bno-hub\b", t, re.I):
        t = re.sub(r"\b90s?\b(?:\s*(?:elbows?|ells?|els?|degrees?|deg))?", "1/4 bend", t, flags=re.I)
        t = re.sub(r"\b45s?\b(?:\s*(?:elbows?|ells?|els?|degrees?|deg))?", "1/8 bend", t, flags=re.I)
        t = re.sub(r"\b22(?:-1/2|\.5)?s?\b(?:\s*(?:elbows?|ells?|els?|degrees?|deg))?", "1/16 bend", t, flags=re.I)
    return t


# ------------------------------------------------------------------ handwriting read by OCR
# How a handwriting reader returns sizes on counter notes. A written "1¼" comes back as 14, 114, 1y, 1x" or 1/t;
# "¾" as 34; "45" as +5. A 14" forged-steel threaded fitting doesn't exist, so on a small-bore threaded line
# (3000#, F/S, IPS, XH, nipples, unions, bushings) a leading 14 is 1¼. Large-bore weld lines (14x6 weldolet)
# are left alone. Used only to search a line read from a note; the line and its picture stay as written.
SMALL_BORE = re.compile(r"\b(?:3m|3000|f/s|ips|i[dlt1/]s|xh|nip|nis|vis|bck|blk|bushin|uni|scu|b/[uv]|ell|eul|tee|gauge|guoge|qauce|"
                        r"gask|guske|flex|a106|sch\s*80|tbe)", re.I)
WELD = re.compile(r"\b(?:wol|weld|welu|wnf|slip|blind)", re.I)          # large-bore weld work: sizes like 14 are real
OCR_FIX = [
    (r"^\s*[LlI|]\s*-\s*", "1- "),                                     # a 1 read as L or I in "1- ..."
    (r"\b[I1l]\s*/?\s*[DI1lt/5]\s*S\b|\bJPS\b|\bItS\b|\bIlS\b|\bI/5\b", "IPS"),
    (r"\b(?:1|/)?5[0O]\s*(?:lbs?|4b|lb|l8)\b|\b15OLB\b|\b1504b\b|\b/59lb\b|\b/50lb\b", "150#"),
    (r"\b(?:3|B)[0Oo][0Oo]\s*(?:lbs?|lb)\b|\b30uu[bß]\b", "300#"),
    (r"[+€]5(?=\s*(?:e[lu]|$|\s))", "45"),
    (r"\b[5S]80\b", "sch 80"), (r"\bAL06\b", "A106"),
    (r"\bwe(?:ll|lu|l1)\s+(?=(?:e[lu][lsu]?s?|elbows?|90|45)\b)", "weld "),
    (r"\b5/T\b", "S/T"),
]


def ocr_repair(text: str) -> str:
    t = text or ""
    for pat, rep in OCR_FIX:
        t = re.sub(pat, rep, t, flags=re.I)
    if SMALL_BORE.search(t) and not WELD.search(t):
        # the size at the start (after any count and "set"/"pc"): 14 / 114 / 1y / 1x" / 1/t / 11% -> 1¼; 34 -> ¾
        t = re.sub(r"^(\s*(?:\d{1,3}\s*-\s*)?(?:sets?|pcs?|sct)?\s*)(?:114|14\.?|1y|1x(?=\s*[\"”])|1/ti?|11%|1%4)(?=[\s\"”x×]|$)",
                   r"\g<1>1¼", t, flags=re.I)
        t = re.sub(r"^(\s*(?:\d{1,3}\s*-\s*)?(?:sets?|pcs?|sct)?\s*)34(?=[\s\"”x×]|$)", r"\g<1>¾", t, flags=re.I)
        t = re.sub(r"^(\s*(?:\d{1,3}\s*-\s*)?)/4(?=\s*[\"”])", r"\g<1>1¼", t)        # "2-1¼"" read as "2-/4""
        t = re.sub(r"^(\s*(?:\d{1,3}\s*-\s*)?(?:sets?|pcs?|sct)?\s*)1%½", r"\g<1>1½", t, flags=re.I)
    return t
