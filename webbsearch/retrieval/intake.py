"""Order intake for Bulk order (v1.3): the documents contractors actually send.

  .eml    a forwarded email chain: the item lines anywhere in it (the rep's forward note, the customer's
          message, the quoted original), plus its attachments (Excel, CSV, Word, PDF, photos). Inline
          images (signature logos) are listed, not read. PO numbers, the subject (usually the job) and who
          sent it become the order's header.
  .docx   Word material lists: tables read with their header row (Qty / Size / Description / Notes);
          "Project:", "Location:" and "Date:" lines become the header.
  images  photos and scans of handwritten notes (JPEG, PNG, HEIC...), read by ocr.py.
  .pdf    the text layer when there is one; otherwise the scanned pages go through ocr.py.

Typed tables go through resolve.parse_table as before. Free text (email bodies, notes, handwriting) is
read line by line and each line is classified: an item line has a quantity or a part number and something
that names a product; greetings, signatures, addresses, phone numbers, ship-to and tag lines are kept
visible as "not an item" so nothing disappears silently. A line repeated later in the chain (a reply
quoting the order) is marked as a duplicate.
"""
from __future__ import annotations

import base64
import io
import re
import unicodedata
import zipfile
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from xml.etree import ElementTree as ET

from webbsearch.retrieval.resolve import MAX_LINES, UOM, clean_text, parse_line, parse_table, part_tokens, qty_first_list

DOC_EXT = (".eml", ".docx")


# ------------------------------------------------------------------ text helpers
class _HTMLText(HTMLParser):
    """HTML email body -> text; table cells become tab-separated so pasted spreadsheets survive."""
    def __init__(self):
        super().__init__()
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script", "head"):
            self.skip += 1
        elif tag in ("br", "p", "div", "tr", "li", "h1", "h2", "h3", "table"):
            self.out.append("\n")
        elif tag in ("td", "th"):
            self.out.append("\t")

    def handle_endtag(self, tag):
        if tag in ("style", "script", "head"):
            self.skip = max(0, self.skip - 1)
        elif tag in ("p", "div", "tr", "li", "table"):
            self.out.append("\n")

    def handle_data(self, d):
        if not self.skip:
            self.out.append(d)


def html_text(html: str) -> str:
    p = _HTMLText()
    p.feed(html)
    t = "".join(p.out)
    return "\n".join(re.sub(r"[ \t]*\t[ \t]*", "\t", ln).strip("\t ") for ln in t.split("\n"))


CONFUSABLE = str.maketrans({"А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O", "Р": "P", "С": "C",
                            "Т": "T", "Х": "X", "Ш": "W", "а": "a", "в": "b", "е": "e", "к": "k", "м": "m", "н": "h",
                            "о": "o", "р": "p", "с": "c", "т": "t", "у": "y", "х": "x", "ш": "w", "і": "i", "ї": "i",
                            "Ị": "I", "ị": "i", "ı": "i", "ß": "B", "×": "x", "—": "-", "–": "-"})
KEEP = set("½¼¾⅛⅜⅝⅞°\"'")


def ocr_line_fix(t: str) -> str:
    """Handwriting-reader slips that change how a line is read: "L- 3m ..." is "1- 3m ...", "5/T" is "S/T"."""
    t = re.sub(r"^\s*[LlI|]\s*-\s*(?=\S)", "1- ", t)
    return re.sub(r"^\s*5/T\b", "S/T", t)


def ocr_fold(t: str) -> str:
    """OCR output -> plain ASCII-ish text: Cyrillic look-alikes to Latin, accents dropped, fractions kept."""
    t = (t or "").translate(CONFUSABLE)
    out = []
    for ch in t:
        if ch in KEEP or ord(ch) < 128:
            out.append(ch)
        else:
            d = unicodedata.normalize("NFKD", ch)
            base = "".join(c for c in d if ord(c) < 128 and not unicodedata.combining(c))
            out.append(base)
    return re.sub(r"\s{3,}", "  ", "".join(out)).strip()


# ------------------------------------------------------------------ free-text classification
GREETING = re.compile(r"^\s*(hi|hello|hey|dear|good (morning|afternoon|evening)|team|all|thanks|thank you|thx|ty|regards|"
                      r"best|cheers|sincerely|sent from|see attached|attached|fyi|please (see|find|note|confirm|advise|quote|let)|"
                      r"let me know|call me|caution|this email|confidential|disclaimer|in addition|in additional)\b", re.I)
META = re.compile(r"^\s*(from|sent|to|cc|bcc|subject|date|re|fw|fwd|project|location|job|customer|contact|attn|tag|"
                  r"s/t|ship to|sold to|bill to|po|p\.o\.|delivery|deliver|pick ?up|phone|cell|office|fax|branch|"
                  r"mailto|www|http)\b", re.I)
PHONE_LOCAL = re.compile(r"^\s*\(?\d{3}\)?[-. ]\d{4}\b.*\b(?:main|mobile|cell|fax|office|tel|phone|direct)\b|"
                         r"\b(?:main|mobile|cell|fax|office|tel|phone|direct)\b[\s:.]*\(?\d{3}\)?[-. ]\d{4}\s*$", re.I)   # "555-0100 Main"
PHONE = re.compile(r"\(?\b\d{3}\)?[-. ]\d{3}[-. ]\d{4}\b")
EMAIL = re.compile(r"\S+@\S+\.\w+")
URL = re.compile(r"https?://|www\.", re.I)
JOBREF = re.compile(r"#\s*\d{2,4}-\w{2,5}\b")
DATE_IN = re.compile(r"\b\d{1,2}/\d{1,2}/(?:\d{2,4}|\d[a-z])")
DATE = re.compile(r"^\s*\d{1,2}/\d{1,2}/\d{2,4}\s*$")
ADDRESS = re.compile(r"\b\d+\s*(st|nd|rd|th)?\s+(street|st\.?|avenue|ave\.?|place|pl\.?|road|rd\.?|blvd|floor|fl\.?|suite|"
                     r"plaza|parkway)\b|\b[A-Z]{2}\s+\d{5}\b", re.I)
PO_LINE = re.compile(r"\bp\.\s?o\.|\bpo\s*(?:#|:|no\.?\s)|\buse\s+po\b|\bpurchase\s+order\b", re.I)
REF = re.compile(r"^\s*#\s*[\w-]+\s*$|^\s*[-#]?\s*\d{2,4}-\d{2,5}[A-Z]{0,3}\s*$")     # "#504-132" (or "-504-132" as read)
SIGNOFF = re.compile(r"^\s*(thanks|thank you|regards|best regards|best|sincerely|cheers|--)\b[\s,!.]*$", re.I)
# a price sheet or quote line ends in its unit and price ("Boss 31500 Silicone sealant 10 oz EA 3.26")
PRICE_TAIL = re.compile(rf"\b{UOM}\.?\s+\$?\d[\d,]*\.\d{{2}}\s*$", re.I)
CODE = re.compile(r"(?<![\w./-])(?=[\w.-]*\d{3})[A-Za-z]{0,4}\d[\w.-]{3,}(?![\w/])")
TOTALS = re.compile(r"\b(?:sub\s*total|total|freight|tax|shipping|discount)\b", re.I)
SIZE = re.compile(r"\d\s*/\s*\d|\d\s*\"|\d\s*(in|inch)\b|[½¼¾⅛⅜⅝⅞]", re.I)
# trade words that name a product even when the catalog spells them out (ell, flg, nip, wol...)
TRADE_NOUNS = {"blind", "weldolet", "bolt", "bolts", "gauge", "gasket", "gaskets", "insert", "ell", "ells", "el", "elbow", "elbows", "90", "90s", "45", "45s", "tee", "tees", "wye", "wyes", "cross", "nip", "nips",
               "nipple", "nipples", "bushing", "bushings", "union", "unions", "cap", "caps", "plug", "plugs", "coupling",
               "couplings", "cplg", "reducer", "reducers", "adapter", "adapters", "adaptor", "flange", "flanges", "flg", "flgs",
               "fl", "fls", "gasket", "gaskets", "valve", "valves", "bv", "b/v", "bfv", "bfvs", "scv", "gv", "check", "pipe",
               "tube", "tubing", "wol", "weldolet", "sockolet", "threadolet", "insert", "bolts", "bolt", "nuts", "rod", "rods",
               "treadrods", "hanger", "hangers", "clamp", "clamps", "strap", "trap", "primer", "arrestor", "arrester", "sink",
               "drain", "gauge", "thermometer", "strainer", "pump", "tank", "heater", "boiler", "capacitor", "igniter",
               "pads", "tape", "solder", "flux", "glue", "cement", "primer", "hd2000", "propress", "press", "sweat", "ring",
               "split", "ftg", "spigot", "hose", "bibb", "sillcock", "faucet", "toilet", "lav", "urinal", "fitting", "fittings"}


def is_item_line(text: str, parsed: dict, vocab=None) -> tuple[bool, str]:
    """(is an order line?, why not). Items have a quantity or part number AND something naming a product."""
    t = text.strip()
    if not t or len(t) > 160:
        return False, "not an item line"
    if re.fullmatch(rf"{UOM}\.?\s+\$?[\d,]+(?:\.\d{{1,2}})?", t, re.I):
        return False, "unit and price only (a wrapped line)"
    if DATE.match(t) or REF.match(t):
        return False, "date or job reference"
    if META.match(t):
        return False, "header, ship-to or contact line"
    if PHONE.search(t) or EMAIL.search(t) or URL.search(t) or PHONE_LOCAL.search(t):
        return False, "contact details"
    if PO_LINE.search(t):
        return False, "PO / order instructions"
    if ADDRESS.search(t) and not TRADE_NOUNS & set(re.findall(r"[a-z0-9/]+", t.lower())):
        return False, "address"
    if DATE_IN.search(t) and not TRADE_NOUNS & set(re.findall(r"[a-z0-9/]+", t.lower())):
        return False, "date or job reference"      # "Cold Spring Harbor Labs 2/23/26": a heading, not an item
    words = re.findall(r"[A-Za-z][A-Za-z/'-]*|\d+s?\b", parsed["text"])
    lw = [w.lower().strip("'") for w in words]
    from webbsearch.retrieval.shorthand import expand
    lw += [w.lower() for w in re.findall(r"[A-Za-z]+", expand(parsed["text"]))]     # EULS -> elbow, flg -> flange
    has_qty = parsed["qty_how"] not in ("default",)
    has_part = bool(part_tokens(parsed["text"])) or (parsed["qty_how"] != "default" and bool(re.fullmatch(r"\s*\d{5,8}\s*|\s*\d{9,14}\s*", parsed["text"])))
    # ("1358103 25": an item # and a count; a lone 02115 is more likely a zip code)
    noun = trade_noun = any(w in TRADE_NOUNS or w.rstrip("s") in TRADE_NOUNS for w in lw)
    if not noun and vocab is not None:
        noun = sum(1 for w in lw if len(w) >= 3 and vocab(w)) >= max(1, len([w for w in lw if len(w) >= 3]) // 2)
    if GREETING.match(t) and not has_qty:
        return False, "greeting or note"
    if JOBREF.search(t) and not trade_noun:        # (catalog vocabulary doesn't count: "American" is a brand word)
        return False, "customer or job number"      # "American Regent  #504-132"
    if (has_qty or has_part) and (noun or has_part or SIZE.search(parsed["text"])):
        return True, ""
    pt = PRICE_TAIL.search(t)
    if pt and not TOTALS.search(t) and CODE.search(t[:pt.start()]) and re.search(r"[A-Za-z]{3,}", t[:pt.start()]):
        return True, ""            # a price-sheet line: a maker's number, a description, its unit and price
    if noun and SIZE.search(parsed["text"]) and len(words) <= 10:
        return True, ""            # "1-1/2" copper tee" with no count: an item, quantity 1
    if noun and re.search(r"\d", parsed["text"]) and len(words) <= 8 and not GREETING.match(t):
        return True, ""            # "11%" 3m F/S IPS EUL": a size the reader mangled, still an item
    return False, "no quantity or product"


def meta_from_text(lines: list[str]) -> dict:
    """Order header facts written in a note or email: PO, project/job, ship-to, reference #, tag, date."""
    m: dict = {}
    for ln in lines:
        t = clean_text(ln)
        if not t:
            continue
        g = re.search(r"\b(?:use\s+)?p\.?\s?o\.?\s*(?:#|no\.?|number)?\s*[:#]?\s*([A-Z]{0,4}-?\d[\w-]{2,})", t, re.I)
        if g and "po" not in m:
            m["po"] = g.group(1)
        for key, pat in (("project", r"^\s*project\s*[:\-]\s*(.+)"), ("location", r"^\s*location\s*[:\-]\s*(.+)"),
                         ("ship_to", r"^\s*(?:s/t|ship\s*to)\s*[:\-]?\s*(.+)"), ("tag", r"^\s*tag\s*[:\-]\s*(.+)"),
                         ("date", r"^\s*date\s*[:\-]\s*(.+)")):
            g = re.match(pat, t, re.I)
            if g and key not in m:
                m[key] = g.group(1).strip()[:80]
        g = re.search(r"(?:^|\s)#\s*(\d{2,4}-[\w]{2,5})\b", t) or re.match(r"^\s*-\s*(\d{2,4}-\d{2,5}[A-Z]{0,3})\s*$", t)
        if g and "ref" not in m:
            m["ref"] = g.group(1)
            who = t[:g.start()].strip(" -:,")
            if who and re.search(r"[A-Za-z]{3,}", who) and not re.match(r"^\s*(?:s/t|ship|tag|po\b|p\.o)", who, re.I) \
                    and not re.search(r"\d{2,}", who) and "customer" not in m:
                m["customer"] = who[:60]           # "American Regent  #504-132": the customer and the job number
        g = re.search(r"\b(pick\s?up at [\w ]+?|delivery (?:at|by|for) [\w: ]+?|for delivery)(?:[.,]|$)", t, re.I)
        if g and "fulfil" not in m:
            m["fulfil"] = g.group(1).strip()
    return m


# a count and an all-digit item number, in either order, with nothing else on the line (v2.4, review G04: "2<tab>1358103"
# was set aside as "no quantity or product"). The count is 1-4 digits, the item number 5-8, so a lone zip code stays out
_NUM_ITEM_QF = re.compile(r"^\s*(?:qty\.?|quantity)?\s*:?\s*(\d{1,4})\s*(?:x|×|ea\.?|pcs?\.?)?[\s,;|]+(?:item|webb|sku)?\s*(?:no\.?|#)?\s*:?\s*(\d{5,8})\s*$", re.I)
_NUM_ITEM_IF = re.compile(r"^\s*(?:item|webb|sku)?\s*(?:no\.?|#)?\s*:?\s*(\d{5,8})[\s,;|]+(?:x|×|qty\.?|quantity)?\s*:?\s*(\d{1,4})\s*(?:ea\.?|pcs?\.?)?\s*$", re.I)


def numeric_item_line(raw: str) -> dict | None:
    t = raw.replace("\t", " ")
    m = _NUM_ITEM_QF.match(t)
    if m:
        qty, item = int(m.group(1)), m.group(2)
    else:
        m = _NUM_ITEM_IF.match(t)
        if not m:
            return None
        item, qty = m.group(1), int(m.group(2))
    if qty < 1:
        return None
    return {"text": item, "qty": qty, "uom": "", "qty_how": "column", "alt_text": None}


def free_text_lines(text: str, source: dict, vocab=None, max_lines: int = MAX_LINES, heading: bool = False) -> list[dict]:
    """Free text -> every non-empty line, each marked item or not (with why), duplicates flagged. With `heading`
    (a photographed or scanned note), a first line with no count, part number or size is the note's heading
    ("42 Maple St - boiler swap"), not an order line."""
    rows = [clean_text(r) for r in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    rows = [r for r in rows if r.strip()]
    qf = qty_first_list([r for r in rows if re.match(r"^\s*[\d(]", r)]) or qty_first_list(rows)
    out, seen, in_sig = [], {}, False
    for n, r in enumerate(rows, start=1):
        if SIGNOFF.match(r):
            in_sig = True
        if re.match(r"^\s*(from|on .+ wrote)\b", r, re.I):
            in_sig = False                 # the next message in the chain starts
        ln = parse_line(r, qty_first=qf)
        ok, why = is_item_line(r, ln, vocab)
        if not ok:
            ln2 = numeric_item_line(r)
            if ln2:            # "2<tab>1358103", "2 1358103", "qty 3 item 1358103": a count and a Webb item number
                ln, ok, why = ln2, True, ""
        if heading and ok and n == 1 and ln["qty_how"] == "default" and not part_tokens(ln["text"]) and not SIZE.search(ln["text"]):
            ok, why = False, "heading"
        if in_sig and ok and not part_tokens(ln["text"]) and ln["qty_how"] == "default":
            ok, why = False, "signature"
        rec = {"n": n, "row": n, "raw": r, **ln, "desc": "", "src": source["id"], "src_label": source["label"]}
        if not ok:
            rec.update(item=False, why=why)
        else:
            key = (re.sub(r"\W+", "", ln["text"].lower()), ln["qty"])
            if key in seen:
                rec.update(item=False, why=f"same as line {seen[key]} (quoted again)")
            else:
                seen[key] = n
                rec["item"] = True
        out.append(rec)
        if sum(1 for x in out if x["item"]) >= max_lines:
            break
    return out


# ------------------------------------------------------------------ documents
def read_docx(raw: bytes) -> dict:
    """Word -> paragraphs (for the header) and tables as tab-separated rows."""
    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    z = zipfile.ZipFile(io.BytesIO(raw))
    root = ET.fromstring(z.read("word/document.xml"))
    body = root.find(W + "body")
    paras, tables = [], []
    for el in list(body):
        if el.tag == W + "p":
            t = "".join(x.text or "" for x in el.iter(W + "t")).strip()
            if t:
                paras.append(t)
        elif el.tag == W + "tbl":
            rows = []
            for tr in el.iter(W + "tr"):
                cells = ["".join(x.text or "" for x in tc.iter(W + "t")).replace("\t", " ").strip() for tc in tr.findall(W + "tc")]
                if any(cells):
                    rows.append("\t".join(cells))
            if rows:
                tables.append("\n".join(rows))
    return {"paragraphs": paras, "tables": tables}


def read_eml(raw: bytes) -> dict:
    """An email (usually forwarded) -> header, body text, attachments (bytes), inline images skipped."""
    m = BytesParser(policy=policy.default).parsebytes(raw)
    body_part = m.get_body(preferencelist=("plain", "html"))
    body = ""
    if body_part is not None:
        body = body_part.get_content()
        if body_part.get_content_type() == "text/html":
            body = html_text(body)
    html_part = m.get_body(preferencelist=("html",))
    html = html_part.get_content() if html_part is not None else ""
    atts, inline = [], []

    def parts(msg):                 # like walk(), but a forwarded message is one attachment, not its insides
        yield msg
        if msg.get_content_type() == "message/rfc822":
            return
        if msg.is_multipart():
            for sub in msg.get_payload():
                yield from parts(sub)

    for part in parts(m):
        ct = part.get_content_type()
        fn = part.get_filename()
        if ct == "message/rfc822" and part is not m:
            pl = part.get_payload()
            if (part.get("Content-Transfer-Encoding") or "").lower() == "base64":     # some clients base64 it
                txt = pl[0].get_payload() if isinstance(pl, list) else pl
                data = base64.b64decode(re.sub(r"\s", "", txt if isinstance(txt, str) else ""))
            elif isinstance(pl, list):
                data = pl[0].as_bytes()
            elif hasattr(pl, "as_bytes"):
                data = pl.as_bytes()
            else:
                data = pl.encode() if isinstance(pl, str) else (pl or b"")
            atts.append({"name": (fn or "forwarded.eml"), "data": data})
            continue
        if part.is_multipart() or part is body_part or part is html_part:
            continue
        if not fn:
            continue
        data = part.get_payload(decode=True) or b""
        cid = (part.get("Content-ID") or "").strip("<>")
        is_inline = (part.get_content_disposition() == "inline" or (cid and cid in html)) and ct.startswith("image/")
        (inline if is_inline else atts).append({"name": fn, "data": data, "type": ct, "bytes": len(data)})
    return {"subject": str(m["subject"] or ""), "from": str(m["from"] or ""), "date": str(m["date"] or ""),
            "body": body, "attachments": atts, "inline": inline}


def _sender(frm: str) -> str:
    g = re.match(r'\s*"?([^"<]+?)"?\s*<', frm or "")
    return g.group(1).strip() if g else (frm or "").split("@")[0]


def ingest(filename: str, raw: bytes, *, sheet: str | None = None, vocab=None, claude=None, depth: int = 0) -> dict:
    """Any supported document -> {kind, sources:[{id, label, kind, lines, table?, ocr?}], meta, notes}."""
    from webbsearch.retrieval.bom import read_upload        # Excel / CSV / text PDF (v1.2)
    name = (filename or "").lower()
    out = {"kind": None, "sources": [], "meta": {}, "notes": []}

    def add_source(label, kind, **kw):
        s = {"id": len(out["sources"]), "label": label, "kind": kind, **kw}
        out["sources"].append(s)
        return s

    def table_source(label, kind, text, meta_lines=()):
        t = parse_table(text, MAX_LINES)
        s = add_source(label, kind, header=t["header"], roles=t["roles"], header_row=t["header_row"],
                       truncated=t["truncated"])
        if t["header"] is None:            # no header row: read it as free text, with item classification
            s["lines"] = free_text_lines(text, s, vocab)
            s["kind"] = kind + "-text"
        else:
            s["lines"] = [{**ln, "src": s["id"], "src_label": label, "item": True} for ln in t["lines"]]
        out["meta"].update({k: v for k, v in meta_from_text(list(meta_lines) + text.split("\n")[:12]).items() if k not in out["meta"]})
        return s

    looks_eml = bool(re.match(rb"^[A-Za-z][A-Za-z-]{1,40}: ", raw[:60])) and (b"\nSubject:" in raw[:20000] or b"\nFrom:" in raw[:20000])
    if name.endswith(".eml") or looks_eml:
        e = read_eml(raw)
        out["kind"] = "eml"
        out["email"] = {"subject": e["subject"], "from": _sender(e["from"]), "date": e["date"],
                        "attachments": [a["name"] for a in e["attachments"]], "inline_skipped": [i["name"] for i in e["inline"]]}
        s = add_source(f"Email: {e['subject'][:60] or '(no subject)'}", "email")
        s["lines"] = free_text_lines(e["body"], s, vocab)
        out["meta"] = meta_from_text(e["body"].split("\n"))
        for ln in e["body"].split("\n"):        # the customer: the first sender in the chain who isn't Webb
            g = re.match(r"^\s*from:\s*\"?([^\"<]+?)\"?\s*<([^>]+@([^>]+))>", ln.strip(), re.I)
            if g and "fwwebb.com" not in g.group(3).lower():
                out["meta"]["customer"] = f"{g.group(1).strip()} ({g.group(3).strip()})"
                break
        subj = re.sub(r"^\s*((re|fw|fwd)\s*:\s*)+", "", e["subject"], flags=re.I).strip()
        if subj:
            out["meta"].setdefault("job", subj[:80])
        out["meta"].setdefault("sender", _sender(e["from"]))
        for a in e["attachments"][:12]:
            if depth >= 1 and a["name"].lower().endswith(".eml"):
                continue
            try:
                sub = ingest(a["name"], a["data"], vocab=vocab, claude=claude, depth=depth + 1)
            except Exception as ex:
                out["notes"].append(f"Couldn't read attachment {a['name']} ({type(ex).__name__})")
                continue
            for s2 in sub["sources"]:
                s3 = add_source(f"{a['name']}" + (f" · {s2['label']}" if len(sub["sources"]) > 1 else ""), s2["kind"],
                                **{k: v for k, v in s2.items() if k not in ("id", "label", "kind", "lines")})
                s3["lines"] = [{**ln, "src": s3["id"], "src_label": s3["label"]} for ln in s2["lines"]]
            out["notes"] += sub["notes"]
            for k, v in sub["meta"].items():
                out["meta"].setdefault(k, v)
        if e["inline"]:
            out["notes"].append(f"{len(e['inline'])} inline image{'s' if len(e['inline']) > 1 else ''} in the email body "
                                f"(signatures and logos) not read")
        return out
    if name.endswith(".docx"):
        d = read_docx(raw)
        out["kind"] = "docx"
        out["meta"] = meta_from_text(d["paragraphs"])
        for k, t in enumerate(d["tables"]):
            table_source("Word table" + (f" {k + 1}" if len(d["tables"]) > 1 else ""), "docx", t, d["paragraphs"])
        if not d["tables"]:
            s = add_source("Word document", "docx-text")
            s["lines"] = free_text_lines("\n".join(d["paragraphs"]), s, vocab)
        return out
    is_img = name.endswith(tuple(x for x in (".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".webp", ".bmp", ".gif"))) \
        or raw[:3] == b"\xff\xd8\xff" or raw[:8] == b"\x89PNG\r\n\x1a\n" or raw[4:12] in (b"ftypheic", b"ftypmif1", b"ftypheix")
    if is_img or name.endswith(".pdf") or raw[:5] == b"%PDF-":
        from webbsearch.retrieval import ocr
        images, kind = None, "image"
        if not is_img:
            src = read_upload(filename, base64.b64encode(raw).decode())
            if len(re.sub(r"\s", "", src["text"])) >= 40:     # a real text layer: v1.2 path
                table_source(filename, "pdf", src["text"])
                out["kind"], out["pages"] = "pdf", src.get("pages")
                return out
            images, kind = ocr.pdf_pages(raw, dpi=300), "scan"
        else:
            images = [ocr.open_image(raw)]
        r = ocr.read_images(images, claude=claude)
        out["kind"] = kind
        label = f"{filename} · " + ("handwritten/scanned PDF" if kind == "scan" else "photo")
        s = add_source(label, kind, ocr={"engine": r["engine"], "label": r.get("label"), "thumbs": r["thumbs"],
                                         "pages": r.get("pages", len(images))})
        if r.get("note"):
            out["notes"].append(r["note"])
        text = "\n".join(ocr_line_fix(ocr_fold(ln["text"])) for ln in r["lines"])
        lines = free_text_lines(text, s, vocab, heading=True)
        # attach what the note looked like to each line (free_text_lines numbers non-empty rows in order)
        crops = [ln for ln in r["lines"] if ocr_fold(ln["text"]).strip()]
        for ln in lines:
            o = crops[ln["n"] - 1] if 0 < ln["n"] <= len(crops) else None
            if o:
                ln.update(crop=o["crop"], conf=o["conf"], page=o["page"], ocr_raw=o["text"])
            ln["from_ocr"] = True
        s["lines"] = lines
        out["meta"] = meta_from_text(text.split("\n"))
        return out
    # Excel / CSV / TSV / TXT (v1.2)
    src = read_upload(filename, base64.b64encode(raw).decode(), sheet)
    out["kind"] = src["kind"]
    s = table_source(filename + (f" · {src['sheet']}" if src.get("sheet") else ""), src["kind"], src["text"])
    s.update({k: src[k] for k in ("sheets", "sheet") if k in src})
    return out
