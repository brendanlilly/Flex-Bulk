"""Paste a list: Quick order, takeoffs and incoming POs -> cartable lines.

One call resolves up to 100 pasted lines (the page sends a long paste in
chunks and shows progress). Each line goes through the same precedence a
counter person uses:

  1. catalog exact     Webb item #, manufacturer part # or UPC in the catalog
  2. real Webb item    a real item # / model from the job-kit and fwwebb.com seed data
  3. cross-reference   an OEM or competitor number -> the Webb item that
                       replaces it (Webb xRef lake), with band and score; the
                       other replacements become the line's alternates, and an
                       out-of-stock pick gets an in-stock equivalent
  4. best match        descriptive lines only ("3/4 cxc 90 elbow"): the top
                       search result, flagged "check it"

A line that looks like a part number and matches nothing says "Not found"
rather than guessing. Lines are read the way POs and takeoffs arrive:
"25 x NI71906-07", "NI71906-07   25", "10 EA HONEYWELL V8168A1069 GAS VALVE",
"Qty 4 - WR 50A55-843", CSV/TSV with a header row (Qty, Part, Description,
Manufacturer), numbered lines ("1. ...", "001  10  EA  ...").
"""
from __future__ import annotations

import csv
from collections import Counter
import io
import re
import time

import numpy as np

from webbsearch.retrieval.analyzer import compact_id, looks_like_part_number
from webbsearch.retrieval.webb_items import is_code
from webbsearch.retrieval.xref import SPEC_WORD, worth_looking_up

MAX_LINES = 2000          # a large takeoff; the page matches it in parallel chunks of 8 lines
UOM = r"(?:ea|each|pc|pcs|pce|pk|pkg|bx|box|boxes|cs|case|ft|lf|rl|roll|pr|pair|set|sets|kit|bg|bag|lot|ctn|carton)"
SIZE_UNIT = re.compile(r"^(?:in|inch|inches|\"|ton|tons|gal|gallon|gallons|v|volt|volts|amp|amps|psi|mfd|uf|btu|mbh|hp|"
                       r"way|pole|poles|lb|lbs|oz|mm|cm|deg|°|degree|degrees|x\d.*|%|ga|gauge|awg|cfm|gpm|seer2?)$", re.I)
LEAD_LINE_NO = re.compile(r"^\s*(?:line\s*)?\d{1,3}\.\s+", re.I)          # "1. item" (a line number; "10) item" is a quantity)
QTY_FIRST = re.compile(rf"^\s*(?:\(\s*(\d{{1,5}})\s*\)|(\d{{1,5}})\s*(?:\)|_|\s-\s|-(?=\s*[\d/(]|\s*{UOM}\b|\s*[A-Za-z])))\s*"
                       rf"(?:({UOM})\b\.?\s*[-_]?\s*)?(?=\S)", re.I)     # "(3) x", "10) x", "3_x", "30 - x", "1-x" (qty-first lists)
EQ_QTY = re.compile(rf"\s*=\s*(\d[\d,]*)\s*({UOM}|qty)?\.?\s*$", re.I)    # "3” Grooved Schedule 40 (Galv) = 400 LF"
# a MasterFormat section after an optional line number ("25 22 05 23 1/4 in. ...", RFQs and spec-driven takeoffs)
CSI_LEAD = re.compile(r"^\s*(?:\d{1,4}\s+)?(?:2[1-36]|33)\s\d{2}\s\d{2}(?:\.\d{2})?\s+(?=\S)")
BULLET = re.compile(r"^\s*(?:[•*·▪◦‣-]|\[\s?\]|☐)\s+")
QTY_WORD = re.compile(rf"\b(?:qty|quantity|qnty)\s*[:#=]?\s*(\d{{1,5}})(?:\s*{UOM}\b\.?)?\s*[-,;:]?\s*", re.I)
LEAD_QTY = re.compile(rf"^\s*(\d{{1,5}})\s*(?:(x|@|{UOM}\b\.?)\s*|-\s+)?(?=\S)", re.I)
TRAIL_QTY = re.compile(rf"(?P<sep>\s*[,;\t|]\s*|\s{{2,}}|\s+x\s*|\s+)(?:qty\s*)?(?P<q>\d{{1,5}})(?:\s*(?:{UOM}\b\.?|x))?\s*$", re.I)
PAREN_QTY = re.compile(r"\s*\((\d{1,5})\)\s*$")
MODEL = re.compile(r"(?=[\w./-]*\d)(?=[\w./-]*[A-Za-z])[A-Za-z0-9][\w./-]{2,}")
NOT_ITEM = re.compile(r"^\s*(?:p\.?\s?o\.?(?:\s*(?:#|no\.?|number))?\s*[:#]?\s*[\w-]*\d|purchase\s+order|ship\s*to|bill\s*to|sold\s*to|"
                      r"vendor|supplier|date\b|job\s*(?:name|#|no)|attn|attention|deliver(?:y)?\s|terms\b|page\s+\d|subtotal|total\b|"
                      r"tax\b|freight|notes?\s*:|requested\s+by|approved\s+by)", re.I)
HEADER_WORDS = {"qty": ("qty", "quantity", "qnty", "order qty", "qty ordered"),
                "part": ("part", "part #", "part no", "part number", "item", "item #", "item no", "mfr #", "mfr part",
                         "mfr part #", "model", "model #", "sku", "catalog", "catalog #", "cat #", "product", "number"),
                "desc": ("description", "desc", "item description", "product description", "name"),
                "brand": ("manufacturer", "mfr", "brand", "vendor", "make", "mfg")}
ROLES = ("qty", "part", "desc", "brand", "uom", "line", "size", "notes", "material", "category")


def _hdr(cell: str) -> str | None:
    """Column role from a header cell. Exact vocabulary first, then the words BOMs actually use
    ("Mfr Part Number", "Quantity Required", "Model No.", "Manufacturer Name", "U/M")."""
    c = re.sub(r"[^a-z# /]", "", cell.strip().lower()).strip()
    if not c or len(c) > 40:
        return None
    for k, words in HEADER_WORDS.items():
        if c in words and not (k == "part" and c in ("item", "item #", "item no", "number")):
            return k
    if re.search(r"\b(qty|quantity|qnty|count)\b", c):
        return "qty"
    if re.search(r"\b(part|model|catalog|cat|sku|mpn|upc)\b", c):
        return "part"
    if re.search(r"\bdesc", c) or c in ("product name", "item name"):
        return "desc"
    if c in ("material", "materials", "matl", "mat'l"):
        return "material"       # folded into the description; the description itself when there's no other
    if re.search(r"\b(manufacturer|mfr|mfg|brand|vendor|make)\b", c):
        return "brand"
    if c in ("uom", "u/m", "um", "unit", "units"):
        return "uom"
    if c in ("size", "sizes", "dia", "diameter", "nominal size"):
        return "size"
    if c in ("notes", "note", "remarks", "comments", "comment", "spec", "specs"):
        return "notes"
    if c in ("item", "item #", "item no", "line", "line #", "line no", "#", "no", "tag", "mark"):
        return "line"
    return None


def _split(row: str, delim: str) -> list[str]:
    return next(csv.reader([row], delimiter=delim)) if delim else row.split()


def detect_table(rows: list[str], scan: int = 12) -> dict | None:
    """Find a header row in the first rows of a paste or file: {delim, header_row, header, roles}.
    Title lines above the header (project name, date) are skipped."""
    for hi, row in enumerate(rows[:scan]):
        for delim in ("\t", ",", ";", "|"):
            if row.count(delim) < 1:
                continue
            cells = _split(row, delim)
            roles: dict[str, int] = {}
            for k, c in enumerate(cells):
                r = _hdr(c)
                if r and r not in roles:
                    roles[r] = k
            if "desc" not in roles and "material" in roles:      # "Qty, Material": the material list is the description
                roles["desc"] = roles.pop("material")
            if ("part" in roles or "desc" in roles) and len(roles) >= 2 and not any(
                    re.fullmatch(r"\s*\d+(?:\.\d+)?\s*", c) for c in cells):
                if "part" not in roles and "line" in roles and "desc" not in roles:
                    continue
                return {"delim": delim, "header_row": hi, "header": [c.strip() for c in cells], "roles": roles}
    return None


def clean_text(raw: str) -> str:
    """Smart quotes, primes and odd spaces from Word, Outlook and OCR -> plain inch and foot marks."""
    s = (raw or "").replace("\u200b", "").replace("\xa0", " ").strip()
    s = re.sub(r"[”“″＂]", '"', s)
    s = re.sub(r"[’‘′`´]", "'", s)
    return re.sub(r"[ \t]{2,}", "  ", s)


def parse_line(raw: str, qty_first: bool = False) -> dict:
    """One pasted line -> {text, qty, uom, qty_how, alt_text}. alt_text is the other reading of an
    ambiguous trailing number ("Amtrol Extrol 30": model 30, or quantity 30?)."""
    s = clean_text(raw)
    dash = bool(re.match(r"^\s*[\d(]{1,6}\)?\s*[–—]", raw))          # "30–3/4” Pro press 90": an en dash after a count
    s = re.sub(r"[–—]", " - " if dash else "-", s, count=1)
    s = re.sub(r"[–—]", "-", s)
    s = BULLET.sub("", s)
    if re.match(r"^\s*\d{1,3}\s+\d{1,5}\s+" + UOM + r"\b", s, re.I):     # "001  10  EA  ..." (line #, qty, UOM)
        s = re.sub(r"^\s*\d{1,3}\s+", "", s)
    s = LEAD_LINE_NO.sub("", s)
    s = CSI_LEAD.sub("", s)
    out = {"text": s, "qty": 1, "uom": "", "qty_how": "default", "alt_text": None}
    m = EQ_QTY.search(s)
    if m and s[:m.start()].strip():
        u = (m.group(2) or "").upper()
        out.update(qty=int(m.group(1).replace(",", "")), qty_how="= n", text=s[:m.start()].strip(),
                   uom={"LF": "FT", "QTY": ""}.get(u, u))
        return out
    m = QTY_FIRST.match(s)
    if m and (qty_first or m.group(1) or re.match(r"^\s*\d{1,5}\s*(?:\)|_|\s-\s)", s)):
        rest = s[m.end():].strip(" -_")
        if rest and re.search(r"[A-Za-z]", rest):
            u = (m.group(3) or "").upper()
            out.update(qty=int(m.group(1) or m.group(2)), qty_how="leading", text=rest,
                       uom={"BOXES": "BOX", "SETS": "SET", "PCS": "PC"}.get(u, u))
            return out
    m = QTY_WORD.search(s)
    if m:
        out.update(qty=int(m.group(1)), qty_how="qty word", text=(s[:m.start()] + " " + s[m.end():]).strip(" -,;:"))
        return out
    m = PAREN_QTY.search(s)
    if m and s[:m.start()].strip():
        out.update(qty=int(m.group(1)), qty_how="(n)", text=s[:m.start()].strip())
        return out
    m = re.match(r"^\s*(\d{1,4})\s+(\d{1,2}/\d{1,2})(?=[\s\"”]|$)", s)
    if m and re.search(r"[A-Za-z]", s[m.end():]) and (int(m.group(1)) > 3 or m.group(2) not in ("1/4", "1/2")):
        # "12 3/4 copper tee": 12-3/4 isn't a pipe size, so 12 is the count (1 1/2, 2 1/2, 3 1/4 stay sizes)
        out.update(qty=int(m.group(1)), qty_how="leading", text=s[m.start(2):].strip())
        return out
    m = re.match(r"^\s*(\d{1,4})\s+(\d{9,14})\s*$", s)
    if m:
        # "1 999999999999": a count and a long item number (a UPC is 12 digits), not one number (v2.01, W022)
        out.update(qty=int(m.group(1)), qty_how="leading", text=m.group(2))
        return out
    m = LEAD_QTY.match(s)
    if m:
        rest = s[m.end():].strip()
        nxt = rest.split()[0] if rest.split() else ""
        marked = bool(m.group(2))
        if rest and re.match(r'^(?:\d{1,2}(?:-\d{1,2}/\d{1,2})?\s*(?:"|in\b\.?|inch)|\d{1,2}-\d{1,2}/\d{1,2}\b)', rest):
            # "25 2" brz 90 ell", "5 2-1/2 ball vlv": a count, then a size ("1 1/2" copper tee" doesn't reach here as a count)
            out.update(qty=int(m.group(1)), qty_how="leading", text=rest.strip(" -,;:"))
            return out
        if rest and (marked or (MODEL.search(rest) and not SIZE_UNIT.match(nxt) and not nxt.startswith("/")
                                and not TRAIL_QTY.search(rest))):
            if marked and re.fullmatch(UOM, m.group(2).rstrip("."), re.I):
                out["uom"] = m.group(2).rstrip(".").upper()
            out.update(qty=int(m.group(1)), qty_how="leading", text=rest.strip(" -,;:"))
            return out
    m = TRAIL_QTY.search(s)
    if m and s[:m.start()].strip() and not s[:m.start()].rstrip().endswith("/"):
        head = s[:m.start()].strip(" -,;:")
        q = int(m.group("q"))
        sep = m.group("sep")
        # "45/5 cap 2" and "2360175, 4": a quantity. "Extrol 30": maybe a model; keep the other reading.
        angle = sep == " " and m.group("q") in ("22", "45", "90") and re.search(r"[A-Za-z]$", head)
        if angle:     # "2 inch pvc 90", "street elbow 45": a fitting angle, not a quantity
            return out
        if head and not re.search(r"[/-]$", head):
            out.update(qty=q, qty_how="trailing", text=head)
            if sep.strip() == "" and len(sep) == 1 and len(m.group("q")) <= 3:
                out["alt_text"] = s.strip()
            return out
    return out


# a part-number cell that isn't one ("See description", "N/A", "-")
PART_PLACEHOLDER = re.compile(r"^\s*(?:see\s+desc(?:ription)?\.?|n/?a|none|tbd|various|misc\.?|-+|\?+|0)\s*$", re.I)
SIZE_ONLY = re.compile(r"^\s*\d+(?:[-/ ]\d+(?:/\d+)?)?\s*(?:\"|in\.?|inch(?:es)?|od|id)?(?:\s*x\s*\d+(?:[-/ ]\d+(?:/\d+)?)?\s*(?:\"|in\.?|inch(?:es)?|od|id|'|ft|foot|feet)?)*\s*$", re.I)
UOM_CELL = re.compile(rf"^\s*(?:{UOM}|ea\.|lb|lbs|gal|oz|cf|sf|sy|yd|m|mt)\s*$", re.I)


def infer_columns(rows: list[str], delim: str, sample: int = 300) -> dict | None:
    """A table with no header row (an export straight from a system: manufacturer | part # | description |
    category | unit): each column's role from what's in it. Returns {roles, header} or None when the rows aren't
    one table. Roles: qty (small whole numbers), uom (EA, FT...), part (codes, one or two tokens, mostly unique),
    desc (the most words), brand (repeating names left of the part / description), category (repeating labels
    right of the description, not searched)."""
    body = [r for r in rows[:sample] if r.strip()]
    if len(body) < 3:
        return None
    grid = list(csv.reader(io.StringIO("\n".join(body)), delimiter=delim))
    widths = Counter(len(r) for r in grid)
    ncol, nrows = widths.most_common(1)[0]
    if ncol < 2 or nrows < 0.8 * len(grid):
        return None
    cols = [[(r[k] if k < len(r) else "").strip() for r in grid] for k in range(ncol)]
    stats = []
    for k, c in enumerate(cols):
        vals = [v for v in c if v]
        n = max(len(vals), 1)
        stats.append({"k": k, "fill": len(vals) / len(c), "uniq": len(set(vals)) / n,
                      "words": sum(len(v.split()) for v in vals) / n,
                      "qty": sum(bool(re.fullmatch(r"\d{1,5}(?:\.0+)?", v)) for v in vals) / n,
                      "uom": sum(bool(UOM_CELL.match(v)) for v in vals) / n,
                      "code": sum(bool(MODEL.fullmatch(v.replace(" ", "")) or re.fullmatch(r"\d{3,14}", v) or PART_PLACEHOLDER.match(v)
                                       or SIZE_ONLY.match(v)) for v in vals) / n,
                      "alpha": sum(bool(re.fullmatch(r"[A-Za-z][A-Za-z .&'/(),-]*", v)) for v in vals) / n})
    roles: dict[str, int] = {}
    for st in stats:
        if st["fill"] < 0.3:
            continue
        if st["uom"] >= 0.8 and "uom" not in roles:
            roles["uom"] = st["k"]
        elif st["qty"] >= 0.8 and st["words"] <= 1.05 and "qty" not in roles and st["uniq"] < 0.9:
            roles["qty"] = st["k"]
    rest = [st for st in stats if st["k"] not in roles.values() and st["fill"] >= 0.3]
    if not rest:
        return None
    desc = max(rest, key=lambda st: (st["words"], st["uniq"]))
    if desc["words"] < 1.6:
        return None                                      # nothing reads like a description: not a product table
    roles["desc"] = desc["k"]
    codes = [st for st in rest if st is not desc and st["code"] >= 0.6 and st["words"] <= 2.5 and st["uniq"] >= 0.5]
    if codes:
        roles["part"] = max(codes, key=lambda st: (st["code"], st["uniq"]))["k"]
    left = min(roles.get("part", desc["k"]), desc["k"])
    names = [st for st in rest if st["k"] not in roles.values() and st["alpha"] >= 0.7 and st["words"] <= 5]
    br = [st for st in names if st["k"] < left]
    if br:
        roles["brand"] = max(br, key=lambda st: st["k"])["k"]
    cat = [st for st in names if st["k"] > desc["k"] and st["uniq"] < 0.9]
    if cat:
        roles["category"] = cat[0]["k"]
    labels = {"qty": "Quantity", "uom": "Unit", "part": "Part #", "desc": "Description", "brand": "Manufacturer", "category": "Category"}
    by = {v: k for k, v in roles.items()}
    sample_ = grid[0]
    header = [f"{labels.get(by.get(k), 'Column ' + str(k + 1))} (e.g. {sample_[k][:24]})" if k < len(sample_) and sample_[k]
              else labels.get(by.get(k), f"Column {k + 1}") for k in range(ncol)]
    return {"roles": roles, "header": header}


def clean_part(part: str) -> tuple[str, str]:
    """(part #, size words moved to the description): "See description" is no part; "1/2inch" or "3/8X3/4X25'" is a
    size, not a part #; trailing punctuation goes."""
    p = (part or "").strip().strip(",;:").strip()
    if PART_PLACEHOLDER.match(p):
        return "", ""
    if SIZE_ONLY.match(p) and re.search(r"/|\"|'|(?i:inch|in\.?$|\bin\b|ft|foot|feet|od|id)|\d\s*[xX]\s*\d", p) \
            and not re.search(r"[A-Za-z]{3,}", re.sub(r"(?i)inch(es)?|feet|foot", "", p)):
        return "", p
    return p, ""


def _cells_line(cells: list[str]) -> dict | None:
    """A delimited row with no header: the small whole number is the quantity, cells with a
    part-number shape are the part, the rest is description."""
    cells = [c.strip() for c in cells if c.strip()]
    if len(cells) < 2:
        return None
    qk = next((k for k, c in enumerate(cells) if re.fullmatch(r"\d{1,5}(?:\.0+)?", c)
               and any(not re.fullmatch(r"[\d.]+", x) or len(x) > 5 for j, x in enumerate(cells) if j != k)), None)
    rest = [c for k, c in enumerate(cells) if k != qk]
    parts = [c for c in rest if MODEL.fullmatch(c.replace(" ", "")) or re.fullmatch(r"\d{6,14}", c)]
    words = [c for c in rest if c not in parts]
    text = " ".join(parts) if parts else " ".join(words)
    if not text:
        return None
    return {"text": text, "desc": " ".join(words) if parts else "", "qty": int(float(cells[qk])) if qk is not None else 1,
            "uom": "", "qty_how": "column" if qk is not None else "default", "alt_text": None}


def _row_line(cells: list[str], roles: dict[str, int]) -> dict | None:
    get = lambda k: clean_text(cells[roles[k]]) if k in roles and roles[k] < len(cells) else ""
    part, desc, brand = get("part"), get("desc"), get("brand")
    part, size_ = clean_part(part)
    if size_ and size_.replace(" ", "").lower() not in desc.replace(" ", "").lower():
        desc = f"{size_} {desc}".strip()
    size, notes, mat = get("size"), get("notes"), get("material")
    if mat and mat.lower() not in desc.lower():
        desc = f"{mat} {desc}".strip()               # "Copper" + "90 Elbow Sweat" (estimating exports)
    if size and desc and size.replace(" ", "") not in desc.replace(" ", ""):
        desc = f"{size} {desc}"                      # "3"" + "Weld 90° Elbows (LR)"
    if notes and desc and MODEL.search(notes):
        desc = f"{desc} {notes}"                     # "3/4" Ball Valves" + "Milwaukee BA-100S" (not "or approved equal")
    qc = get("qty")
    mq = re.match(r"^\s*(\d[\d,]*(?:\.\d+)?)", qc)      # "70'ft", "40'ft(10' long)", "12", "1 box"
    q = mq.group(1).replace(",", "") if mq else ""
    qc_uom = "FT" if re.search(r"'|\bft\b|\blf\b|feet", qc[len(mq.group(0)) if mq else 0:], re.I) else \
        (re.search(rf"\b({UOM})\b", qc, re.I).group(1).upper() if re.search(rf"\b({UOM})\b", qc, re.I) else "")
    if not part and not desc:
        return None
    text = " ".join(x for x in (brand if part and brand and brand.lower() not in part.lower() else "", part) if x) or \
        " ".join(x for x in (brand if brand and brand.lower() not in desc.lower() else "", desc) if x)
    try:
        qty = max(1, int(round(float(q)))) if q else 1
    except ValueError:
        qty = 1
    return {"text": text, "desc": desc if part else "", "brand": brand, "part": part, "qty": qty,
            "uom": (get("uom").upper() or qc_uom)[:6], "qty_how": "column" if q else "default", "alt_text": None}


def qty_first_list(rows: list[str]) -> bool:
    """Most lines start with a count ("1-1¼" Nibco...", "2 - 3/4 ball valves", "(3) tees"): read "N-" as a quantity."""
    rows = [clean_text(r) for r in rows if r.strip()]
    hits = sum(1 for r in rows if re.match(r"^\s*(?:\(\s*\d{1,5}\s*\)|\d{1,5}\s*(?:\)|_|[-–—.:]))\s*\S", r))
    return len(rows) >= 2 and hits >= max(2, 0.4 * len(rows))


def parse_table(text: str, max_lines: int = MAX_LINES, roles: dict | None = None, header_row: int | None = None,
                delim: str | None = None, qty_first: bool | None = None) -> dict:
    """Pasted text or a CSV/TSV/Excel/PDF-text BOM -> {lines, header, roles, header_row, delim, total, truncated}.
    With a header row (found in the first rows, or given with `roles`), columns map to qty / part / description /
    manufacturer / UOM; without one, each line is read like a counter paste (see parse_line)."""
    rows = [r for r in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    rows = [r for r in rows if r.strip()]
    out: list[dict] = []
    meta = {"header": None, "roles": None, "header_row": None, "delim": None}
    if not rows:
        return {"lines": [], "total": 0, "truncated": False, **meta}
    t = None
    if roles:
        d = delim or ("\t" if "\t" in rows[min(header_row or 0, len(rows) - 1)] else ",")
        t = {"delim": d, "header_row": header_row if header_row is not None else -1,
             "header": _split(rows[header_row], d) if header_row is not None and 0 <= header_row < len(rows) else
             [f"Column {k + 1}" for k in range(len(_split(rows[0], d)))],
             "roles": {k: int(v) for k, v in roles.items() if k in ROLES and v is not None and int(v) >= 0}}
    else:
        t = detect_table(rows)
        if t is None:                           # no header row: a system export? columns from their contents
            d0 = next((d for d in ("\t", "|", ";", ",") if sum(r.count(d) >= 1 for r in rows[:50]) >= 0.8 * min(len(rows), 50)), None)
            inf = infer_columns(rows, d0) if d0 else None
            if inf and ("part" in inf["roles"] or "brand" in inf["roles"] or "qty" in inf["roles"]):
                t = {"delim": d0, "header_row": -1, "header": inf["header"], "roles": inf["roles"], "inferred": True}
    n_all = 0
    if t:
        meta.update(header=t["header"], roles=t["roles"], header_row=t["header_row"], delim=t["delim"], inferred=bool(t.get("inferred")))
        body = rows[t["header_row"] + 1:]
        for n, cells in enumerate(csv.reader(io.StringIO("\n".join(body)), delimiter=t["delim"]), start=t["header_row"] + 2):
            ln = _row_line(cells, t["roles"])
            if not ln:
                continue
            n_all += 1
            if len(out) < max_lines:
                out.append({"n": n, "raw": t["delim"].join(cells), **ln})
        return {"lines": out, "total": len(out), "truncated": n_all > max_lines, **meta}
    qf = qty_first_list(rows) if qty_first is None else qty_first
    two_col_header = rows[0].count(",") == 1 and all(_hdr(c) for c in rows[0].split(","))   # "Part,Qty"
    d = "\t" if "\t" in rows[0] else "," if rows[0].count(",") >= 2 or two_col_header else None
    for n, r in enumerate(rows, start=1):
        if d and r.count(d) >= 1:
            ln = _cells_line(next(csv.reader([r], delimiter=d)))
            if ln:
                n_all += 1
                if len(out) < max_lines:
                    out.append({"n": n, "raw": r.strip(), **ln})
                continue
            r = re.sub(r"\s*[,\t]\s*", "  ", r.strip().strip(","))
        ln = parse_line(r, qty_first=qf)
        if not re.search(r"[A-Za-z0-9]", ln["text"]):
            continue
        n_all += 1
        if len(out) < max_lines:
            out.append({"n": n, "raw": r.strip(), "desc": "", **ln})
    return {"lines": out, "total": len(out), "truncated": n_all > max_lines, "qty_first": qf, **meta}


def parse_text(text: str, max_lines: int = MAX_LINES) -> list[dict]:
    """Lines only (Quick order paste). See parse_table."""
    return parse_table(text, max_lines)["lines"]


def part_tokens(text: str) -> list[str]:
    """Part-number-shaped tokens in a PO line, with the word before each as a possible brand."""
    words = text.split()
    out = []
    for k, w in enumerate(words):
        t = w.strip(",;:()[]#")
        if MODEL.fullmatch(t) and len(re.sub(r"\W", "", t)) >= 4 and not re.fullmatch(r"\d+(?:/\d+)?", t) and is_code(t):
            if k and words[k - 1].isalpha() and len(words[k - 1]) >= 2:
                out.append(f"{words[k - 1]} {t}")
            out.append(t)
    return list(dict.fromkeys(out))


class Resolver:
    def __init__(self, engine):
        self.e = engine

    # ------------------------------------------------------------ item shapes
    def _catalog_item(self, j: int, p, branch) -> dict:
        e = self.e
        cat = e.catalog
        m = e.results._mini(j, p)
        av = e.availability.summary(np.array([j]), branch)
        m.update({"kind": "catalog", "mfr_part": cat.mfr_part[j], "list_price": float(cat.list_price[j]),
                  "stock": {"at_branch": int(av["at_branch"][0]), "nearby_qty": int(av["nearby_qty"][0]),
                            "eta": av["eta"][0], "demo": False}})
        if p.is_authenticated and not p.sees_prices:
            m["price"] = m["list_price"] = None
        return m

    def _webb_item(self, c: dict) -> dict:
        """A cross-reference card as a cartable item (no demo price: these are real Webb items)."""
        return {"kind": "webb", "sku": c["sku"], "title": c["title"], "brand": c["brand"], "mfr_part": c["mpn"],
                "webb_item": c["webb_item"], "url": c["url"], "price": None, "uom": "EA", "family": "boiler_parts",
                "attrs": {}, "stock": c.get("stock"), "noun": c.get("noun", "")}

    def _real_item(self, card: dict) -> dict:
        return {"kind": "webb", "sku": card["sku"], "title": card["title"], "brand": card["manufacturer"],
                "mfr_part": card["mpn"], "webb_item": card["webb_item"], "url": card["url"],
                "price": card["list_price"], "uom": "EA", "family": "tools", "attrs": {}, "stock": None}

    # ------------------------------------------------------------ one line
    def one(self, ln: dict, p, branch, best: bool = True) -> dict:
        e = self.e
        text = ln["text"]
        if NOT_ITEM.match(ln.get("raw") or text) and not ln.get("_alt"):
            return {**ln, "status": "skip", "via": "Not an item line", "item": None, "alternates": [], "band": None,
                    "wmcr": None, "note": "Header or footer line (PO number, ship-to, totals): skipped", "swap": None}
        r = {**ln, "status": "none", "via": None, "item": None, "alternates": [], "band": None, "wmcr": None,
             "note": "", "swap": None}
        sees = not (p.is_authenticated and not p.sees_prices)
        # 1. catalog exact (item #, mfr part #, UPC): the whole line, then each part-shaped token
        keys = [compact_id(text)] + [compact_id(t.split()[-1]) for t in part_tokens(text)]
        hits = []
        for k in dict.fromkeys(keys):
            if len(k) >= 4 and (looks_like_part_number(k) or k.isdigit()):
                hits = e.catalog.id_lookup(k)
                if hits:
                    break
        if not hits:
            # Flex Bulk: a manufacturer's all-digit part number with the manufacturer on the line ("Webstone 51414"):
            # exact when an item with that part number is that manufacturer's
            low = text.lower()
            for w in re.findall(r"[A-Za-z0-9][\w./-]{3,}", text):
                k = compact_id(w)
                if len(k) < 4 or not any(ch.isdigit() for ch in k):
                    continue
                hits = [j for j in e.catalog.id_lookup(k) if len(str(e.catalog.brand[j])) >= 2
                        and re.search(rf"\b{re.escape(str(e.catalog.brand[j]).lower())}\b", low)]
                if hits:
                    break
        if hits:
            items = [self._catalog_item(j, p, branch) for j in hits[:4]]
            r.update(status="exact", via="Exact", item=items[0], alternates=items[1:])
            if len(hits) > 1:
                r["note"] = f"{len(hits)} catalog items share this number; check the brand"
            return r
        # 2. real Webb items (job-kit and fwwebb.com seed data)
        real = e.webb_items.exact(text)
        if real:
            cards = [e.webb_items.card(it, how, sees) for it, how in real]
            r.update(status="webb", via="Webb item" if real[0][1] != "brand + description" else "Webb item (brand + description)",
                     item=self._real_item(cards[0]),
                     alternates=[self._real_item(c) for c in cards[1:3]])
            return r
        # 3. cross-reference lake
        if e.xref.ok:
            tries = [text] if worth_looking_up(text) else []
            tries += [t for t in part_tokens(text) if t != text]
            for t in tries[:6]:
                xr = e.xref.lookup(t, limit=6, branch=branch)
                if not xr or not (xr["self"] or xr["replacements"]):
                    continue
                pt = xr["part"]
                if pt["close"] and (len(re.sub(r"\W", "", t)) < 6 or " " in t.strip() or SPEC_WORD.search(t)):
                    continue        # a near-miss counts only for one real part-number token, not "150LB" or a whole line
                named = f"{pt['brand']} {pt['part']}".strip()
                if xr["self"]:
                    r.update(status="webb", via="Webb item (xRef)", item=self._webb_item(xr["self"]),
                             alternates=[self._webb_item(c) | {"band": c["band"], "wmcr": c["wmcr"]} for c in xr["replacements"][:4]])
                    swaps = xr["self"].get("swaps") or []
                    if swaps:
                        r["swap"] = self._webb_item(swaps[0]) | {"how": swaps[0]["how"]}
                else:
                    top = xr["replacements"][0]
                    r.update(status="xref", via=f"Replaces {named}", item=self._webb_item(top),
                             band=top["band"], wmcr=top["wmcr"],
                             alternates=[self._webb_item(c) | {"band": c["band"], "wmcr": c["wmcr"]} for c in xr["replacements"][1:5]])
                    if top["differ"]:
                        r["note"] = "Differs: " + "; ".join(f"{d['attr']} {d['yours'] or '?'} → {d['webb'] or '?'}" for d in top["differ"][:2])
                    pick = xr.get("in_stock_pick")
                    if pick:
                        alt = next(c for c in xr["replacements"] if c["webb_item"] == pick["webb_item"])
                        r["swap"] = self._webb_item(alt) | {"how": f"Also replaces {named} · {alt['band']} {alt['wmcr']}"}
                if pt["close"]:
                    r["note"] = (r["note"] + " · " if r["note"] else "") + f"Close match on the number ({xr['part']['why']}): check it"
                r["xref_part"] = {"id": pt["id"], "name": named, "why": pt["why"]}
                return r
            # the lake knows the part but no Webb item replaces it: say so instead of guessing
            for t in tries[:3]:
                xr = e.xref.lookup(t, branch=branch)
                if xr:
                    pt = xr["part"]
                    r["note"] = (f"{pt['brand']} {pt['part']} is in the cross-reference, but no Webb stock item replaces it"
                                 + (f"; it crosses to {xr['others'][0]['brand']} {xr['others'][0]['part']} (special order)"
                                    if xr.get("others") else ""))
                    return r
        # an ambiguous trailing number might have been part of the model ("Extrol 30")
        if ln.get("alt_text") and not ln.get("_alt"):
            alt = self.one({**ln, "text": ln["alt_text"], "qty": 1, "qty_how": "default", "_alt": True}, p, branch, best=False)
            if alt["status"] in ("exact", "webb", "xref"):
                alt["note"] = (alt["note"] + " · " if alt["note"] else "") + \
                    f"Read “{ln['alt_text']}” as the part, quantity 1 (not {ln['qty']})"
                return alt
        # 4. best match, descriptive lines only
        partish = bool(part_tokens(text)) and len(text.split()) <= 2
        if not best or partish:
            if partish:
                r["note"] = "Looks like a part number; no catalog, Webb item or cross-reference match"
            return r
        q = text if not ln.get("desc") else f"{text} {ln['desc']}"
        res = e.results.results(q, p, branch=branch, llm="off")
        items = res.get("items") or []
        if items:
            idx = [e.catalog.sku_to_idx.get(i["sku"]) for i in items[:4]]
            cands = [self._catalog_item(j, p, branch) for j in idx if j is not None]
            if cands:
                exact = bool(items[0].get("exact"))
                r.update(status="exact" if exact else "best", via="Exact" if exact else "Best match",
                         item=cands[0], alternates=cands[1:])
                if not exact and res.get("notice") and res["notice"].get("kind") in ("fallback", "partial", "phrase_miss"):
                    r["note"] = "Weak match: not every word matched"
                # the line named a part number that matched nothing: the description match is a guess, not an answer
                pts = [t for t in part_tokens(text) if " " not in t]
                if not exact and pts:
                    r["weak"] = True
                    r["note"] = (f"No match for part # {pts[0]}"
                                 + ("" if self.e.xref.ok else " (cross-reference not loaded on this computer)")
                                 + "; closest by description, check it before ordering")
        return r

    def resolve(self, lines: list[dict], p, branch=None, best: bool = True) -> dict:
        t0 = time.perf_counter()
        branch = branch or (p.home_branch if p.is_authenticated else None)
        out = [self.one(ln, p, branch, best=best) for ln in lines[:100]]
        counts = {k: sum(1 for x in out if x["status"] == k) for k in ("exact", "webb", "xref", "best", "none", "skip")}
        return {"lines": out, "counts": counts, "branch": self.e.availability.branch_info(branch),
                "ms": round((time.perf_counter() - t0) * 1000, 1)}
