"""Rule-based query understanding (always on, ~0.1 ms).

Handles the head and torso of trade search: sizes, part numbers, brands,
account entities (order #, invoice #, PO), date phrases and vertical intent.
Anything it is not confident about is flagged `needs_llm`, and the Claude
query-understanding step (webbsearch/llm/query_understanding.py) refines it
within a latency budget, with results cached per normalized query.
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from webbsearch.retrieval.analyzer import _DECIMAL_SIZES, SIZE_RE, compact_id, looks_like_part_number, normalize

ORDER_RE = re.compile(r"\b(?:order|so|sales\s*order)\s*(?:#|no\.?|number)?\s*(\d{6,8})\b", re.I)
INVOICE_RE = re.compile(r"\binv(?:oice)?\s*(?:#|no\.?|number)?\s*(\d{6,9})\b", re.I)
# a decimal before a rating unit is a rating, not a pipe size: "0.75 gph" is a nozzle's flow, "1.5 ton" a capacity
# a fraction that is a rating, not a pipe size: "1/2 hp", "1/3 HP motor" (v2.01, Q038)
RATING_FRAC_RE = re.compile(r"(?<![\d/.-])(\d+(?:-\d+)?/\d+)\s*(?:hp|horse\s*power|gph|gpm|kw|tons?)\b")
# an oil nozzle's flow before its spray angle and pattern: ".75 80A", "1.00 60W" (Q050)
NOZZLE_RE = re.compile(r"(?<![\d/.])(\d*\.\d+)\s+\d{2}\s*[abhsw]\b")
RATING_DEC_RE = re.compile(r"(?<![\d/.])(\d*\.\d+)\s*(?:gph|gpm|hp|tons?|kw|cfm|mfd|uf|psi|lbs?|oz|gal|gallons?|amps?|volts?|v|a)\b")
# a PO reference: "PO 4512", "PO# JOB-22", "P.O. number 88-A", "purchase order 991". "PO" has to stand alone (a word
# boundary after it), so powerhead, polyethylene, power vent and potable are never read as a PO; the value has a digit
PO_RE = re.compile(r"\b(?:p\.?\s?o\.?|purchase\s+order)(?![a-z])\s*(?:#|no\.?|number)?\s*(?=[\w-]*\d)([A-Za-z0-9][\w-]{1,20})\b", re.I)
QUESTION_RE = re.compile(r"^(what|which|how|when|where|why|who|do|does|did|can|could|should|is|are|show|find|"
                         r"list|give|tell|i need|need|looking|help)\b", re.I)

INTENT_WORDS = {
    "invoices": {"invoice", "invoices", "past due", "unpaid", "balance", "statement", "owe", "outstanding",
                 "overdue", "open invoices", "credit memo", "pay"},
    "orders": {"order", "orders", "ordered", "bought", "purchased", "last time", "reorder", "buy again",
               "shipped", "backorder", "backordered", "backorders", "back order", "back orders", "tracking",
               "po"},
    "lists": {"list", "lists", "my list", "van stock", "truck stock"},
    "content": {"how to", "how do", "guide", "manual", "spec sheet", "submittal", "sds", "training", "hours",
                "branch", "location", "return", "returns", "policy", "ach", "tax exempt", "credit application",
                "warranty", "code", "sizing", "troubleshoot", "troubleshooting", "install instructions"},
}
INTENT_RE = {k: [re.compile(rf"\b{re.escape(w)}\b") for w in words] for k, words in INTENT_WORDS.items()}
# product words that contain an account word: "pressure balance spool" isn't an account balance (v2.0)
NOT_ACCOUNT = re.compile(r"\bpressure[\s-]+balanc\w*|\bbalanc(?:e|ing)\s+(?:valves?|spools?|cartridges?|tees?|tanks?|weights?|"
                         r"kits?|bodies|body|tubes?)\b|\bbalancing\b|\bbalance\s+(?:point|type)\b", re.I)
# a plain-word account question names who bought or paid: "what did we buy last month", "what did I pay for 3/4 ball
# valves". It stays an account question when it names a product (the product words filter the orders) (v2.0)
ACCOUNT_PHRASE = re.compile(r"\b(?:what|which|when|where|how\s+(?:much|many|often))\s+(?:did|do|have|has)\s+(?:we|i|us|my\s+\w+)\s+"
                            r"(?:ever\s+)?(?:buy|bought|order|ordered|pay|paid|purchase|purchased|get|got|spend|spent)\b|"
                            r"\b(?:did|have)\s+(?:we|i)\s+(?:ever\s+)?(?:buy|bought|order|ordered|pay|paid|purchase|purchased)\b|"
                            r"\b(?:we|i)\s+(?:last\s+)?(?:bought|ordered|purchased|paid\s+for)\b|\bour\s+last\s+order\b", re.I)
MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
SEASONS = {"spring": (3, 5), "summer": (6, 8), "fall": (9, 11), "autumn": (9, 11), "winter": (12, 2)}
# compiled once: a bulk takeoff parses thousands of lines, and patterns built per call miss Python's regex cache
_STREET = r"(?!\s+(?:st|street|rd|road|ave|avenue|ln|lane|dr|drive|ct|court|way|pl|place|blvd)\b)"
SEASON_RE = {s: re.compile(rf"\b{s}\b{_STREET}") for s in SEASONS}
MONTH_RE = {m: re.compile(r"\b(in|since|during)\s+may\b|\bmay\s+20\d\d\b") if m == "may" else re.compile(rf"\b{m}\b{_STREET}")
            for m in MONTHS}


@dataclass
class ParsedQuery:
    raw: str
    tokens: list[str]
    id_chunks: list[str] = field(default_factory=list)      # compact candidate part numbers / ids
    sizes: list[str] = field(default_factory=list)
    brands: list[str] = field(default_factory=list)
    intent: str = "products"                                # products|orders|invoices|lists|content|question
    intent_scores: dict[str, float] = field(default_factory=dict)
    order_no: str | None = None
    invoice_no: str | None = None
    po: str | None = None
    date_range: tuple[date, date] | None = None
    status: str | None = None
    style: str = "keyword"                                  # part|keyword|natural
    account_phrase: bool = False                            # "what did we buy", "what did I pay for" (v2.0)
    needs_llm: bool = False
    rewritten: str | None = None                            # set by Claude QU
    llm_filters: dict = field(default_factory=dict)         # set by Claude QU
    source: str = "rules"
    brand_hits: list = field(default_factory=list)          # v2.42: brands.BrandHit per brand named (covers, carried)

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "brand_hits"}
        d["brand_info"] = [h.to_dict() for h in self.brand_hits]
        if self.date_range:
            d["date_range"] = [str(self.date_range[0]), str(self.date_range[1])]
        return d


LAST_N = re.compile(r"^\s*last\s*-?\s*(\d{1,3})(?:\s*days?)?\s*$", re.I)


def parse_dates(text: str, today: date) -> tuple[date, date] | None:
    t = text.lower()
    if "yesterday" in t:
        d = today - timedelta(days=1); return d, d
    if re.search(r"\btoday\b", t):
        return today, today
    m = re.search(r"last\s*(\d{1,3})(?:\s+days|\s*$)", t)
    if m:
        return today - timedelta(days=int(m.group(1))), today
    if "last week" in t:
        return today - timedelta(days=today.weekday() + 7), today - timedelta(days=today.weekday() + 1)
    if "this week" in t:
        return today - timedelta(days=today.weekday()), today
    if "last month" in t:
        first = today.replace(day=1); lm_end = first - timedelta(days=1)
        return lm_end.replace(day=1), lm_end
    if "this month" in t:
        return today.replace(day=1), today
    if "last year" in t:
        return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
    if "this year" in t or "ytd" in t:
        return date(today.year, 1, 1), today
    for s, (a, b) in SEASONS.items():
        if SEASON_RE[s].search(t):   # "Spring St job" is a street, not a season
            yr = today.year
            m2 = re.search(r"(20\d\d)", t)
            if m2:
                yr = int(m2.group(1))
            elif date(yr, 2 if s == "winter" else a, 1) > today:
                yr -= 1  # most recent occurrence ("last spring" in September = this year's spring)
            if s == "winter":
                return date(yr - 1, 12, 1), date(yr, 2, 28)
            return date(yr, a, 1), date(yr, b, calendar.monthrange(yr, b)[1])
    for name, mi in MONTHS.items():
        hit = MONTH_RE[name].search(t)
        if hit:
            yr = today.year
            m2 = re.search(r"(20\d\d)", t)
            if m2:
                yr = int(m2.group(1))
            elif date(yr, mi, 1) > today:
                yr -= 1
            return date(yr, mi, 1), date(yr, mi, calendar.monthrange(yr, mi)[1])
    return None


class QueryParser:
    def __init__(self, brands: list[str], today: date):
        self.today = today
        # brand lookup on normalized token n-grams, longest first
        self.brand_ngrams: dict[tuple[str, ...], str] = {}
        for b in brands:
            toks = normalize(b)
            if toks:
                self.brand_ngrams[toks] = b
        self.max_brand_len = max((len(k) for k in self.brand_ngrams), default=1)
        self.master = None                  # v2.42: brands.Brands (aliases, covers, not-carried brands, lines)

    def detect_brands(self, toks: tuple[str, ...], raw: str | None = None) -> list[str]:
        if self.master is not None:
            return [h.name for h in self.master.detect(raw if raw is not None else " ".join(toks), tuple(toks)) if h.carried]
        found, i = [], 0
        while i < len(toks):
            hit = None
            for L in range(min(self.max_brand_len, len(toks) - i), 0, -1):
                b = self.brand_ngrams.get(tuple(toks[i:i + L]))
                if b:
                    hit = (b, L); break
            if hit:
                found.append(hit[0]); i += hit[1]
            else:
                i += 1
        return found

    def parse(self, raw: str) -> ParsedQuery:
        raw = (raw or "").strip()
        low = raw.lower()
        toks = normalize(raw)
        pq = ParsedQuery(raw=raw, tokens=list(toks))
        # ids: raw whitespace chunks and adjacent pairs that look like part numbers / numbers
        chunks = raw.split()
        ids = []
        for n in (1, 2):
            for i in range(len(chunks) - n + 1):
                if n == 2 and any(x.isalpha() for x in chunks[i:i + 2]):
                    continue   # "over 1000", "Watts 9": a word next to a number is not one part number
                c = " ".join(chunks[i:i + n])
                if looks_like_part_number(c) or (n == 1 and compact_id(c).isdigit() and len(compact_id(c)) >= 6):
                    ids.append(compact_id(c))
        pq.id_chunks = list(dict.fromkeys(ids))
        pq.sizes = [t for t in toks if SIZE_RE.match(t) and ("/" in t or "-" in t)]
        for d in RATING_DEC_RE.findall(low) + NOZZLE_RE.findall(low):
            conv = _DECIMAL_SIZES.get(d)
            if conv in pq.sizes:
                pq.sizes.remove(conv)
        for f in RATING_FRAC_RE.findall(low):
            if f in pq.sizes:
                pq.sizes.remove(f)
        if self.master is not None:
            pq.brand_hits = self.master.detect(raw, toks)
            # the catalog's brands only (as before v2.42): a brand it doesn't carry is in brand_hits, marked not carried
            pq.brands = [h.name for h in pq.brand_hits if h.carried]
        else:
            pq.brands = self.detect_brands(toks)
        m = ORDER_RE.search(raw)
        if m:
            pq.order_no = m.group(1)
        m = INVOICE_RE.search(raw)
        if m:
            pq.invoice_no = m.group(1)
        m = PO_RE.search(raw)
        if m and m.group(1).lower() not in ("box",):
            pq.po = m.group(1)
        pq.date_range = parse_dates(raw, self.today)
        # word boundaries matter: "circulater" and "plate" both contain "late"
        if re.search(r"\bpast\s*due\b|\boverdue\b|\blate\b", low):
            pq.status = "PAST_DUE"
        elif re.search(r"\b(open|unpaid|outstanding)\b", low):
            pq.status = "OPEN"
        elif re.search(r"\bbackorder", low):
            pq.status = "BACKORDERED"
        # fwwebb.com's purchase-history shortcut: "last90" (53,638 searches a year, the most-typed term) and "last 30
        # days" are this account's orders in that window, not a part number (v2.4)
        m_sc = LAST_N.match(raw)
        if m_sc:
            pq.date_range = (self.today - timedelta(days=int(m_sc.group(1))), self.today)
            pq.id_chunks = []
            pq.account_phrase = True
        # intent scores (product words that hold an account word don't count: "pressure balance spool")
        low_i = NOT_ACCOUNT.sub(" ", low)
        sc = {k: 0.0 for k in ("products", "orders", "invoices", "lists", "content")}
        for k, pats in INTENT_RE.items():
            for pat in pats:
                if pat.search(low_i):
                    sc[k] += 1.0
        # "delivery" is an order word next to an account word ("my delivery", "when is the delivery"), not in
        # "pizza delivery" (v2.01, Q105)
        if re.search(r"\b(?:my|our|the|next|today'?s|tomorrow'?s)\s+deliver(?:y|ies)\b|\bdeliver(?:y|ies)\s+(?:status|date|time|"
                     r"schedule|window)\b|\bwhen\b.*\bdeliver|\bdeliver(?:y|ies)\b.*\b(?:order|po)\b", low_i):
            sc["orders"] += 1.0
        if ACCOUNT_PHRASE.search(low_i) or m_sc:
            pq.account_phrase = True
            sc["orders"] += 3
        if pq.order_no or pq.po:
            sc["orders"] += 2
        if pq.invoice_no:
            sc["invoices"] += 3
        # "late" alone is weak evidence ("late model furnace"); it counts only next to an account noun
        if pq.status == "PAST_DUE" and re.search(r"\bpast\s*due\b|\boverdue\b|invoice|\bbills?\b|payment|balance|statement", low_i):
            sc["invoices"] += 1
        if pq.sizes or pq.brands or (pq.id_chunks and not (pq.order_no or pq.invoice_no or pq.po)):
            sc["products"] += 1.5
        sc["products"] += 0.5  # prior: most traffic is product search
        pq.intent_scores = sc
        best = max(sc, key=sc.get)
        pq.intent = best
        n_words = len(raw.split())
        is_q = bool(QUESTION_RE.match(low)) or raw.endswith("?")
        if pq.id_chunks and n_words <= 3 and not is_q:
            pq.style = "part"
        elif is_q or n_words >= 7:
            pq.style = "natural"
        if is_q and best in ("orders", "invoices", "lists") or (is_q and pq.date_range):
            pq.intent = "question"
        # Claude is worth its latency when the query is conversational, long, or
        # contains words the rules could not map (checked later against the vocab).
        pq.needs_llm = pq.style == "natural"
        return pq
