"""Smart account search: orders, invoices, lists and content as their own pages.

"my invoices", "last month's invoices", "past due over $1,000", "orders for the
Lincoln St job", "web orders by Erin since June", "which orders had 3/4 ball
valves", "shared lists", "install manuals for Rinnai" -- the words people use
for account records are filters, not keywords. `AccountQueryParser` turns them
into a structured query and keeps the phrase each filter came from, so the page
can show its reading as removable chips ("Past due · Last month · Over $1,000").
`AccountSearch` runs it against the account's records (entitlements enforced
by AccountData) and returns rows, totals over the whole match set, facet counts
and, for lists, the Webb job-kit library.

Every filter is rule-based and cheap; a bare vertical word ("invoices", "my
orders") returns the full history, newest first, paged.
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
import pandas as pd

from webbsearch.retrieval.account_data import AccessDenied
from webbsearch.retrieval.analyzer import normalize, parts_only
from webbsearch.retrieval.entitlements import Principal

VERTICALS = ("orders", "invoices", "lists", "content")
MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
MONTHS["sept"] = 9
MONTH_RE = r"(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec)"
STREET = r"(?!\s+(?:st|street|rd|road|ave|avenue|ln|lane|dr|drive|ct|court|way|pl|place|blvd|hill|farm|brook|pond|valley)\b)"
SEASONS = {"spring": (3, 5), "summer": (6, 8), "fall": (9, 11), "autumn": (9, 11), "winter": (12, 2)}

INV_STATUS = {"PAST_DUE": "Past due", "OPEN": "Open, not yet due", "PAID": "Paid"}
ORD_STATUS = {"OPEN": "Open", "SHIPPED": "Shipped", "BACKORDERED": "Backordered", "INVOICED": "Invoiced"}
CHANNELS = {"WEB": "Web", "COUNTER": "Counter", "PHONE": "Phone", "EDI": "EDI"}
CTYPES = {  # content type -> (label, trigger words)
    "spec_sheet": ("Spec sheets", r"spec(?:ification)?s?(?: sheets?)?|submittals?|data ?sheets?|cut sheets?"),
    "install_manual": ("Install manuals", r"install(?:ation)? (?:manuals?|guides?|instructions?)|manuals?|iom"),
    "buying_guide": ("Buying guides", r"buying guides?|how to choose|which .* to buy"),
    "how_to": ("How-to", r"how[- ]to|how do i|step by step"),
    "troubleshooting": ("Troubleshooting", r"troubleshoot(?:ing)?|error codes?|fault codes?|won'?t|not working"),
    "article": ("Articles", r"articles?|news"),
    "brand": ("Brands", r"brand (?:pages?|overview)"),
    "branch": ("Branches", r"branch(?:es)?|hours|locations?|store hours|open on"),
    "promo": ("Promotions", r"promos?|promotions?|rebates?|deals?|sales?|specials?|discounts?"),
    "event": ("Events & training", r"events?|training|classes|webinars?|ce credits?"),
    "faq": ("FAQ", r"faqs?|policy|policies|returns?|credit application|tax exempt"),
}
STOP = set("""my our all show me list the a an for from of on in at with what which did do does we i us find get see view any
invoices invoice bills bill statements statement orders order purchases purchase po pos job jobs project projects placed made
have has had are is were was that please and to by how many much total give pull up containing contain contains include
includes including items item stuff things anything everything account company ours mine you can could would where when
whats it its there some lists list saved content docs documents documentation library our's
buy pay spend spent much ever".""".split())


@dataclass
class AccountQuery:
    vertical: str
    raw: str = ""
    status: list = field(default_factory=list)
    start: date | None = None
    end: date | None = None
    due_start: date | None = None
    due_end: date | None = None
    min_days_late: int | None = None
    min_amount: float | None = None
    max_amount: float | None = None
    jobs: list = field(default_factory=list)
    po: str | None = None
    ids: list = field(default_factory=list)
    channel: str | None = None
    branch_id: str | None = None
    placed_by: str | None = None
    contains: str | None = None
    text: str | None = None
    sort: str = "date_desc"
    top_n: int | None = None
    ctype: str | None = None
    scope: str | None = None
    chips: list = field(default_factory=list)

    def chip(self, key: str, label: str, span: str = ""):
        self.chips.append({"key": key, "label": label, "span": span.strip()})


def _money(x: float) -> str:
    return f"${x:,.0f}" if x == int(x) else f"${x:,.2f}"


def _amount(s: str, k: str | None) -> float:
    v = float(s.replace(",", ""))
    return v * 1000 if k else v


def _fmt_range(a: date, b: date) -> str:
    if a == b:
        return f"{a:%b} {a.day}, {a.year}"
    if a.year == b.year and a.month == b.month:
        return f"{a:%b} {a.day}–{b.day}, {a.year}"
    if a.year == b.year:
        return f"{a:%b} {a.day} – {b:%b} {b.day}, {a.year}"
    return f"{a:%b} {a.day}, {a.year} – {b:%b} {b.day}, {b.year}"


def _month_range(y: int, m: int) -> tuple[date, date]:
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def _quarter(y: int, q: int) -> tuple[date, date]:
    m0 = 3 * (q - 1) + 1
    return date(y, m0, 1), _month_range(y, m0 + 2)[1]


def parse_period(t: str, today: date):
    """Lower-cased text -> (start, end, label, matched span) or None. Street names are not seasons or months."""
    def ret(a, b, label, m):
        return a, min(b, today) if b >= a else b, f"{label} · {_fmt_range(a, min(b, today))}", m.group(0)

    m = re.search(r"\b(?:in the |over the |for the |within the )?(?:last|past|previous|prior)\s+(\d{1,3})\s+(day|week|month|year)s?\b", t)
    if m:
        n, u = int(m.group(1)), m.group(2)
        days = {"day": 1, "week": 7, "month": 30, "year": 365}[u] * n
        return ret(today - timedelta(days=days - 1 if u == "day" else days), today, f"Last {n} {u}{'s' if n > 1 else ''}", m)
    m = re.search(r"\b(last|previous|prior|past)\s+(week|month|quarter|year)(?:'?s)?\b", t)
    if m:
        u = m.group(2)
        if u == "week":
            a = today - timedelta(days=today.weekday() + 7)
            return ret(a, a + timedelta(days=6), "Last week", m)
        if u == "month":
            end = today.replace(day=1) - timedelta(days=1)
            return ret(end.replace(day=1), end, "Last month", m)
        if u == "quarter":
            q = (today.month - 1) // 3 + 1
            y, q = (today.year, q - 1) if q > 1 else (today.year - 1, 4)
            a, b = _quarter(y, q)
            return ret(a, b, f"Q{q} {y}", m)
        return ret(date(today.year - 1, 1, 1), date(today.year - 1, 12, 31), f"{today.year - 1}", m)
    m = re.search(r"\b(this|current)\s+(week|month|quarter|year)\b|\b(ytd|year to date|mtd|month to date)\b", t)
    if m:
        u = m.group(2) or {"ytd": "year", "year to date": "year", "mtd": "month", "month to date": "month"}[m.group(3)]
        a = {"week": today - timedelta(days=today.weekday()), "month": today.replace(day=1),
             "quarter": date(today.year, 3 * ((today.month - 1) // 3) + 1, 1), "year": date(today.year, 1, 1)}[u]
        return ret(a, today, f"This {u}", m)
    m = re.search(r"\btoday'?s?\b", t)
    if m:
        return ret(today, today, "Today", m)
    m = re.search(r"\byesterday'?s?\b", t)
    if m:
        d = today - timedelta(days=1)
        return ret(d, d, "Yesterday", m)
    m = re.search(r"\bq([1-4])(?:\s*(?:of\s*)?(20\d\d))?\b", t)
    if m:
        y = int(m.group(2) or today.year)
        a, b = _quarter(y, int(m.group(1)))
        if a > today and not m.group(2):
            a, b = _quarter(y - 1, int(m.group(1)))
        return ret(a, b, f"Q{m.group(1)} {a.year}", m)
    m = re.search(r"\b(?:since|after)\s+" + MONTH_RE + STREET + r"(?:\s+(20\d\d))?\b", t)
    if m:
        mi = MONTHS[m.group(1)]
        y = int(m.group(2)) if m.group(2) else (today.year if date(today.year, mi, 1) <= today else today.year - 1)
        return ret(date(y, mi, 1), today, f"Since {calendar.month_abbr[mi]} {y}", m)
    m = re.search(r"\bsince\s+(20\d\d)\b", t)
    if m:
        return ret(date(int(m.group(1)), 1, 1), today, f"Since {m.group(1)}", m)
    m = re.search(r"\b(?:in|during|for|from)?\s*" + MONTH_RE + STREET + r"(?:\s+(20\d\d))?\b", t)
    if m and (m.group(1) != "may" or re.search(r"\b(in|during|for|from)\s+may\b|\bmay\s+20\d\d\b", t)) \
            and (m.group(1) not in ("mar", "jun", "jul", "oct", "dec", "jan", "feb", "apr", "aug", "sep", "nov")
                 or re.search(r"\b(in|during|for|from)\s+" + m.group(1) + r"\b|\b" + m.group(1) + r"\s+20\d\d\b", t)):
        mi = MONTHS[m.group(1)]
        y = int(m.group(2)) if m.group(2) else (today.year if date(today.year, mi, 1) <= today else today.year - 1)
        a, b = _month_range(y, mi)
        return ret(a, b, f"{calendar.month_name[mi]} {y}", m)
    for s, (a_m, b_m) in SEASONS.items():
        m = re.search(rf"\b(?:last\s+|this\s+)?{s}{STREET}(?:\s+(20\d\d))?\b", t)
        if m:
            y = int(m.group(1)) if m.group(1) else today.year
            if not m.group(1) and date(y, 2 if s == "winter" else a_m, 1) > today:
                y -= 1
            a = date(y - 1, 12, 1) if s == "winter" else date(y, a_m, 1)
            b = date(y, 2, calendar.monthrange(y, 2)[1]) if s == "winter" else _month_range(y, b_m)[1]
            return ret(a, b, f"{s.capitalize()} {y}", m)
    m = re.search(r"\b(?:in|during|for|from)?\s*(20[12]\d)\b(?!\s*(?:btu|gal|psi|ft|w\b))", t)
    if m and not re.search(r"\d{5,}", m.group(0)):
        y = int(m.group(1))
        if y <= today.year:
            return ret(date(y, 1, 1), date(y, 12, 31), f"{y}", m)
    return None


class AccountQueryParser:
    def __init__(self, engine):
        self.e = engine
        self.acc = engine.accounts
        b = self.acc.branches
        self.branch_by_city = {}
        for bid, n in sorted(zip(b["branch_id"], b["name"]), key=lambda t: t[0]):   # Springfield MA before VT
            self.branch_by_city.setdefault(str(n).split(",")[0].lower(), bid)

    def parse(self, q: str, vertical: str, p: Principal, today: date | None = None) -> AccountQuery:
        today = today or self.e.today
        raw = (q or "").strip()
        t = " " + raw.lower().replace("’", "'") + " "
        aq = AccountQuery(vertical=vertical, raw=raw)
        used: list[str] = []

        def take(span: str):
            nonlocal t
            if span:
                used.append(span)
                t = t.replace(span, " ", 1)

        # ids first: "order 1234567", "invoice 12345678", "po MER1001"
        for rx, kind in [(r"\b(?:order|so|sales order)\s*#?\s*(\d{6,9})\b", "order"),
                         (r"\binv(?:oice)?\s*#?\s*(\d{6,9})\b", "invoice"),
                         (r"\bp\.?\s?o\.?(?![a-z])\s*#?\s*(?=[\w-]*\d)([a-z0-9][\w-]{1,20})\b", "po")]:
            m = re.search(rx, t)
            if m:
                if kind == "po":
                    aq.po = m.group(1).upper()
                    aq.chip("po", f"PO {aq.po}", m.group(0))
                else:
                    aq.ids.append(m.group(1))
                    aq.chip("id", f"{kind.capitalize()} {m.group(1)}", m.group(0))
                take(m.group(0))
        # due windows (invoices)
        m = re.search(r"\b(?:coming )?due\s+(today|tomorrow|this week|next week|this month|next month|soon|"
                      r"in (?:the next )?(\d{1,3}) days|within (\d{1,3}) days|next (\d{1,3}) days)\b", t)
        if m and vertical in ("invoices", "all"):
            today_ = today
            k = m.group(1)
            n = int(m.group(2) or m.group(3) or m.group(4) or 0)
            win = {"today": (today_, today_), "tomorrow": (today_ + timedelta(1), today_ + timedelta(1)),
                   "this week": (today_, today_ + timedelta(days=6 - today_.weekday())),
                   "next week": (today_ + timedelta(days=7 - today_.weekday()), today_ + timedelta(days=13 - today_.weekday())),
                   "this month": (today_, _month_range(today_.year, today_.month)[1]),
                   "soon": (today_, today_ + timedelta(days=14))}.get(k)
            if k == "next month":
                nm = (today_.replace(day=28) + timedelta(days=4)).replace(day=1)
                win = _month_range(nm.year, nm.month)
            if win is None:
                win = (today_, today_ + timedelta(days=n))
            aq.due_start, aq.due_end = win
            aq.status = ["OPEN", "PAST_DUE"] if k in ("today",) else ["OPEN"]
            aq.chip("due", f"Due {_fmt_range(*win)}", m.group(0))
            take(m.group(0))
        # days late: "over 30 days past due", "60+ days late"
        m = re.search(r"\b(?:over|more than|at least)?\s*(\d{1,3})\+?\s*days?\s+(?:past due|late|overdue)\b", t)
        if m:
            aq.min_days_late = int(m.group(1))
            aq.status = ["PAST_DUE"]
            aq.chip("late", f"{aq.min_days_late}+ days past due", m.group(0))
            take(m.group(0))
        # amounts
        m = re.search(r"\bbetween\s*\$?\s*(\d[\d,]*(?:\.\d+)?)\s*(k)?\s*(?:and|to|-)\s*\$?\s*(\d[\d,]*(?:\.\d+)?)\s*(k)?\b", t)
        if m:
            aq.min_amount, aq.max_amount = _amount(m.group(1), m.group(2)), _amount(m.group(3), m.group(4))
            aq.chip("amount", f"{_money(aq.min_amount)}–{_money(aq.max_amount)}", m.group(0))
            take(m.group(0))
        m = re.search(r"(?:\b(?:over|above|more than|greater than|at least|min(?:imum)?(?: of)?)\s*|>=?\s*)\$?\s*(\d[\d,]*(?:\.\d+)?)\s*(k)?\b(?!\s*(?:days?|%|ton|gal))", t) \
            or re.search(r"\$\s*(\d[\d,]*(?:\.\d+)?)\s*(k)?\s*\+", t)
        if m:
            aq.min_amount = _amount(m.group(1), m.group(2))
            aq.chip("amount", f"Over {_money(aq.min_amount)}", m.group(0))
            take(m.group(0))
        m = re.search(r"(?:\b(?:under|below|less than|up to|max(?:imum)?(?: of)?)\s*|<=?\s*)\$?\s*(\d[\d,]*(?:\.\d+)?)\s*(k)?\b(?!\s*(?:days?|%|ton|gal))", t)
        if m:
            aq.max_amount = _amount(m.group(1), m.group(2))
            aq.chip("amount", f"Under {_money(aq.max_amount)}", m.group(0))
            take(m.group(0))
        # period
        per = parse_period(t, today)
        if per:
            aq.start, aq.end, label, span = per
            aq.chip("date", label, span)
            take(span)
        # "last 5 orders", "top 10 invoices": a count, not a date or a job
        m = re.search(r"\b(?:last|latest|recent|top|first|newest|oldest|largest|biggest)\s+(\d{1,3})\b(?!\s*(?:days?|weeks?|months?|years?|%))", t)
        if m:
            aq.top_n = int(m.group(1))
            aq.chip("top", f"{aq.top_n} {'most recent' if re.match(r'last|latest|recent|newest', m.group(0)) else 'shown'}", m.group(0))
            t = t.replace(m.group(0), m.group(0).replace(m.group(1), " "), 1)
        # sort
        for rx, s, label in [(r"\b(largest|biggest|highest|most expensive|top)\b", "amount_desc", "Largest first"),
                             (r"\b(smallest|lowest|cheapest)\b", "amount_asc", "Smallest first"),
                             (r"\b(most overdue|oldest (?:past due|unpaid))\b", "late_desc", "Most overdue first"),
                             (r"\b(oldest|earliest|first)\b", "date_asc", "Oldest first"),
                             (r"\b(newest|latest|most recent|recent|last)\b", "date_desc", "")]:
            m = re.search(rx, t)
            if m:
                aq.sort = s
                if label:
                    aq.chip("sort", label, m.group(0))
                take(m.group(0))
                break
        # status
        if vertical == "invoices":
            for rx, st, label in [(r"\b(past ?due|overdue|late|delinquent)\b", ["PAST_DUE"], "Past due"),
                                  (r"\b(not yet due|current|upcoming)\b", ["OPEN"], "Open, not yet due"),
                                  (r"\b(unpaid|open|outstanding|owe|owed|balance|due|not paid)\b", ["OPEN", "PAST_DUE"], "Unpaid"),
                                  (r"\b(paid|closed|settled)\b", ["PAID"], "Paid")]:
                m = re.search(rx, t)
                if m:
                    if not aq.status:
                        aq.status = st
                        aq.chip("status", label, m.group(0))
                    take(m.group(0))
                    break
        elif vertical == "orders":
            for rx, st, label in [(r"\b(back ?order(?:ed|s)?)\b", ["BACKORDERED"], "Backordered"),
                                  (r"\b(not shipped|unshipped|not yet shipped)\b", ["OPEN", "BACKORDERED"], "Not shipped"),
                                  (r"\b(shipped|in transit|on the way)\b", ["SHIPPED"], "Shipped"),
                                  (r"\b(open|pending|active|in progress|outstanding)\b", ["OPEN", "SHIPPED", "BACKORDERED"],
                                   "Open: not yet invoiced (open, shipped or backordered)"),
                                  (r"\b(invoiced|closed|complete|completed|delivered|done)\b", ["INVOICED"], "Invoiced")]:
                m = re.search(rx, t)
                if m:
                    aq.status = st
                    aq.chip("status", label, m.group(0))
                    take(m.group(0))
                    break
            m = re.search(r"\b(web|online|website|counter|will ?call|walk[- ]?in|phone|phoned|called in|edi)\b(?:\s+orders?)?", t)
            if m:
                w = m.group(1)
                aq.channel = "WEB" if w in ("web", "online", "website") else "PHONE" if w.startswith("phone") or w == "called in" \
                    else "EDI" if w == "edi" else "COUNTER"
                aq.chip("channel", f"{CHANNELS[aq.channel]} orders", m.group(0))
                take(m.group(0))
        elif vertical == "lists":
            for rx, sc, label in [(r"\b(shared|team|company)\b", "shared", "Shared lists"),
                                  (r"\b(private|personal|my own|mine only)\b", "mine", "My private lists"),
                                  (r"\b(library|webb kits?|job kits?|kits?|templates?)\b", "library", "Webb job-kit library")]:
                m = re.search(rx, t)
                if m:
                    aq.scope = sc
                    aq.chip("scope", label, m.group(0))
                    take(m.group(0))
                    break
        elif vertical == "content":
            for ct, (label, rx) in CTYPES.items():
                m = re.search(r"\b(?:" + rx + r")\b", t)
                if m:
                    aq.ctype = ct
                    aq.chip("ctype", label, m.group(0))
                    take(m.group(0))
                    for m2 in re.finditer(r"\b(?:" + rx + r")\b", t):   # "branch hours": both words say the same thing
                        take(m2.group(0))
                    break
        # who and where (orders / invoices)
        if vertical in ("orders", "invoices") and p.is_authenticated:
            users = self.acc.users[self.acc.users["account_id"] == p.account_id]
            for uid, fn in zip(users["user_id"], users["first_name"].astype(str)):
                m = re.search(rf"\b(?:by|from|placed by|ordered by)\s+{re.escape(fn.lower())}\b|\b{re.escape(fn.lower())}'s\b", t)
                if m and p.role != "tech":
                    aq.placed_by = uid
                    aq.chip("user", f"Placed by {fn}", m.group(0))
                    take(m.group(0))
                    break
            for city, bid in self.branch_by_city.items():
                m = re.search(rf"\b(?:at|from|in|picked up at)\s+(?:the\s+)?{re.escape(city)}(?:\s+branch)?\b", t)
                if m:
                    aq.branch_id = bid
                    aq.chip("branch", f"{self.acc.branches.loc[bid, 'name']} branch", m.group(0))
                    take(m.group(0))
                    break
        # what's left: a job, a PO, an order number, or product words
        words = [w for w in re.findall(r"[a-z0-9][a-z0-9'/.-]*", t)
                 if w not in STOP and w.strip("'") not in STOP and w.replace("'", "") not in STOP]     # "what's" = "whats"
        rest = " ".join(words).strip()
        if rest:
            self._resolve_rest(aq, rest, p)
        return aq

    def _resolve_rest(self, aq: AccountQuery, rest: str, p: Principal):
        v = aq.vertical
        if v in ("orders", "invoices") and p.is_authenticated and p.can(v):
            acc = self.acc
            o = acc.orders.loc[acc._order_ids(p)] if v == "orders" or p.can("orders") else \
                acc.orders.loc[acc.invoices.loc[acc.inv_by_acct.get(p.account_id, [])]["order_id"]]
            toks = rest.split()
            # PO number or order/invoice id typed bare
            up = rest.upper().replace(" ", "")
            if up and any(ch.isdigit() for ch in up):
                pos = o["po_number"].astype(str).str.upper()
                hit = pos[pos == up]
                if len(hit):
                    aq.po = up
                    aq.chip("po", f"PO {up}", rest)
                    return
                if up.isdigit() and len(up) >= 6:
                    aq.ids.append(up)
                    aq.chip("id", f"#{up}", rest)
                    return
            jobs = [j for j in o["job_name"].dropna().unique() if j]
            jl = {j: j.lower() for j in jobs}
            match = [j for j, low in jl.items() if all(re.search(rf"\b{re.escape(w)}", low) for w in toks)]
            if match:
                aq.jobs = sorted(match)
                aq.chip("job", f"Job: {match[0]}" if len(match) == 1 else f"Jobs matching “{rest}” ({len(match)})", rest)
                return
            aq.contains = rest
            aq.chip("contains", f"Contains “{rest}”", rest)
            return
        aq.text = rest
        if v in ("lists", "content"):
            aq.chip("text", f"“{rest}”", rest)


class AccountSearch:
    """Runs AccountQuery objects. Totals are computed over the whole match set, not the page."""

    def __init__(self, engine):
        self.e = engine
        self.parser = AccountQueryParser(engine)
        self._content = None

    # ------------------------------------------------------------ entry point
    def run(self, vertical: str, q: str, p: Principal, params: dict | None = None,
            offset: int = 0, limit: int = 50) -> dict:
        if vertical not in VERTICALS:
            raise ValueError(vertical)
        params = params or {}
        aq = self.parser.parse(q, vertical, p)
        self._apply_params(aq, params)

        out = {"vertical": vertical, "q": q, "chips": aq.chips, "rows": [], "total": 0, "offset": offset,
               "limit": limit, "summary": {}, "facets": {}, "denied": None, "signin": False,
               "sort": aq.sort, "principal_role": p.role,
               "asof": str(self.e.today)}      # the demo's data date: "last month" counts back from it
        if vertical in ("orders", "invoices"):
            if not p.is_authenticated:
                out["signin"] = True
                out["denied"] = f"Sign in to see your {vertical}."
                return out
            if not p.can(vertical):
                role = {"tech": "technician", "ap": "accounts payable"}.get(p.role, p.role)
                out["denied"] = (f"{vertical.capitalize()} aren't available to your role ({role}). "
                                 "The account owner or your accounts-payable contact can see them.")
                return out
            return (self._orders if vertical == "orders" else self._invoices)(aq, p, out, offset, limit)
        if vertical == "lists":
            return self._lists(aq, p, out, offset, limit)
        return self._content_run(aq, p, out, offset, limit)

    def _apply_params(self, aq: AccountQuery, params: dict):
        """Explicit filters from the page (facet clicks) win over what the text implied."""
        if params.get("status"):
            aq.status = [s for s in params["status"] if s]
            labels = {**INV_STATUS, **ORD_STATUS}
            aq.chips = [c for c in aq.chips if c["key"] != "status"]
            for s in aq.status:
                aq.chip("p:status", labels.get(s, s))
        if params.get("job"):
            aq.jobs = [params["job"]]
            aq.contains = None
            aq.chips = [c for c in aq.chips if c["key"] not in ("job", "contains")]
            aq.chip("p:job", f"Job: {params['job']}")
        if params.get("channel") in CHANNELS:
            aq.channel = params["channel"]
            aq.chip("p:channel", f"{CHANNELS[aq.channel]} orders")
        if params.get("ctype") in CTYPES:
            aq.ctype = params["ctype"]
            aq.chips = [c for c in aq.chips if c["key"] != "ctype"]
            aq.chip("p:ctype", CTYPES[aq.ctype][0])
        if params.get("scope") in ("shared", "mine", "library"):
            aq.scope = params["scope"]
        if params.get("sort") in ("date_desc", "date_asc", "amount_desc", "amount_asc", "late_desc", "due_asc"):
            aq.sort = params["sort"]

    # ------------------------------------------------------------ orders
    def _order_frame(self, p: Principal) -> pd.DataFrame:
        acc = self.e.accounts
        return acc.orders.loc[acc._order_ids(p)]

    def _contains_orders(self, o: pd.DataFrame, text: str) -> tuple[np.ndarray, dict]:
        """Orders with a line whose product title carries every word of `text`."""
        acc, cat = self.e.accounts, self.e.catalog
        want = [w for w in parts_only(normalize(text)) if w]
        if not want:
            return np.ones(len(o), bool), {}
        if getattr(self, "_line_skus", None) is None:
            self._line_skus = acc.lines["sku"].to_numpy()
        memo: dict[str, bool] = {}

        def hit(sku: str) -> bool:
            r = memo.get(sku)
            if r is None:
                i = cat.sku_to_idx.get(sku)
                tt = normalize(cat.title[i]) if i is not None else ()
                r = memo[sku] = all(any(x == w or (len(w) >= 3 and x.startswith(w)) for x in tt) for w in want)
            return r
        keep = []
        for oid in o["order_id"] if "order_id" in o.columns else o.index:
            pos = acc._line_pos.get(oid)
            keep.append(pos is not None and any(hit(s) for s in self._line_skus[pos]))
        return np.array(keep, bool), memo     # memo: sku -> matched, so the page can show the matching line

    def _order_mask(self, o: pd.DataFrame, aq: AccountQuery, skip: str | None = None) -> np.ndarray:
        m = np.ones(len(o), bool)
        if aq.start:
            m &= (o["order_date"] >= pd.Timestamp(aq.start)).to_numpy()
        if aq.end:
            m &= (o["order_date"] <= pd.Timestamp(aq.end) + pd.Timedelta(hours=23, minutes=59)).to_numpy()
        if aq.status and skip != "status":
            m &= o["status"].isin(aq.status).to_numpy()
        if aq.channel and skip != "channel":
            m &= (o["channel"] == aq.channel).to_numpy()
        if aq.branch_id:
            m &= (o["branch_id"] == aq.branch_id).to_numpy()
        if aq.placed_by:
            m &= (o["user_id"] == aq.placed_by).to_numpy()
        if aq.po:
            m &= (o["po_number"].astype(str).str.upper() == aq.po).to_numpy()
        if aq.ids:
            m &= o.index.isin(aq.ids)
        if aq.jobs and skip != "job":
            m &= o["job_name"].isin(aq.jobs).to_numpy()
        if aq.min_amount is not None:
            m &= (o["total"] >= aq.min_amount).to_numpy()
        if aq.max_amount is not None:
            m &= (o["total"] <= aq.max_amount).to_numpy()
        return m

    def _orders(self, aq, p, out, offset, limit):
        acc = self.e.accounts
        o = self._order_frame(p)
        memo = {}
        if aq.contains:
            mask, memo = self._contains_orders(o, aq.contains)
            o = o[mask]
        m = self._order_mask(o, aq)
        sel = o[m]
        key = {"date_desc": ("order_date", False), "date_asc": ("order_date", True),
               "amount_desc": ("total", False), "amount_asc": ("total", True)}.get(aq.sort, ("order_date", False))
        sel = sel.sort_values(key[0], ascending=key[1])
        if aq.top_n:
            sel = sel.head(aq.top_n)
        out["total"] = int(len(sel))
        rows = []
        for r in sel.iloc[offset:offset + limit].to_dict("records"):
            c = acc.order_card(r, p)
            pos = acc._line_pos.get(r["order_id"])
            if pos is not None:
                if getattr(self, "_line_skus", None) is None:
                    self._line_skus = acc.lines["sku"].to_numpy()
                skus = self._line_skus[pos]
                if aq.contains:
                    skus = [x for x in skus if memo.get(x)] + [x for x in skus if not memo.get(x)]
                c["preview"] = [self.e.catalog.title[self.e.catalog.sku_to_idx[s]] for s in skus[:3]
                                if s in self.e.catalog.sku_to_idx][:2]
            c["placed_by"] = str(acc.users.loc[r["user_id"], "first_name"]) if r["user_id"] in acc.users.index else ""
            rows.append({"type": "order", **c})
        out["rows"] = rows
        tot = float(sel["total"].sum())
        out["summary"] = {"count": int(len(sel)), "total": round(tot, 2) if p.sees_prices else None,
                          "open": int(sel["status"].isin(["OPEN", "SHIPPED", "BACKORDERED"]).sum()),
                          "backordered": int((sel["status"] == "BACKORDERED").sum()),
                          "first": str(sel["order_date"].min().date()) if len(sel) else None,
                          "last": str(sel["order_date"].max().date()) if len(sel) else None}
        ms = o[self._order_mask(o, aq, skip="status")]
        vc = ms["status"].value_counts()
        out["facets"]["status"] = [{"value": s, "label": ORD_STATUS[s], "count": int(vc.get(s, 0)),
                                    "selected": s in aq.status} for s in ORD_STATUS]
        mc = o[self._order_mask(o, aq, skip="channel")]["channel"].value_counts()
        out["facets"]["channel"] = [{"value": c, "label": CHANNELS[c], "count": int(mc.get(c, 0)),
                                     "selected": aq.channel == c} for c in CHANNELS if mc.get(c, 0)]
        mj = o[self._order_mask(o, aq, skip="job")]
        jc = mj.loc[mj["job_name"] != "", "job_name"].value_counts().head(10)
        out["facets"]["job"] = [{"value": j, "count": int(n), "selected": j in aq.jobs} for j, n in jc.items()]
        return out

    # ------------------------------------------------------------ invoices
    def _inv_frame(self, p: Principal) -> pd.DataFrame:
        acc = self.e.accounts
        inv = acc.invoices.loc[acc.inv_by_acct.get(p.account_id, [])].copy()
        o = acc.orders.loc[inv["order_id"]]
        inv["job_name"] = o["job_name"].to_numpy()
        inv["po_number"] = o["po_number"].astype(str).to_numpy()
        inv["channel"] = o["channel"].to_numpy()
        inv["branch_id"] = o["branch_id"].to_numpy()
        inv["user_id"] = o["user_id"].to_numpy()
        inv["days_late"] = np.where(inv["status"] == "PAST_DUE",
                                    (pd.Timestamp(self.e.today) - inv["due_date"]).dt.days, 0)
        return inv

    def _inv_mask(self, inv: pd.DataFrame, aq: AccountQuery, skip: str | None = None) -> np.ndarray:
        m = np.ones(len(inv), bool)
        if aq.start:
            m &= (inv["invoice_date"] >= pd.Timestamp(aq.start)).to_numpy()
        if aq.end:
            m &= (inv["invoice_date"] <= pd.Timestamp(aq.end)).to_numpy()
        if aq.due_start:
            m &= (inv["due_date"] >= pd.Timestamp(aq.due_start)).to_numpy()
        if aq.due_end:
            m &= (inv["due_date"] <= pd.Timestamp(aq.due_end)).to_numpy()
        if aq.status and skip != "status":
            m &= inv["status"].isin(aq.status).to_numpy()
        if aq.min_days_late is not None:
            m &= (inv["days_late"] >= aq.min_days_late).to_numpy()
        if aq.min_amount is not None:
            m &= (inv["amount"] >= aq.min_amount).to_numpy()
        if aq.max_amount is not None:
            m &= (inv["amount"] <= aq.max_amount).to_numpy()
        if aq.po:
            m &= (inv["po_number"].str.upper() == aq.po).to_numpy()
        if aq.ids:
            m &= (inv.index.isin(aq.ids) | inv["order_id"].isin(aq.ids))
        if aq.jobs and skip != "job":
            m &= inv["job_name"].isin(aq.jobs).to_numpy()
        if aq.placed_by:
            m &= (inv["user_id"] == aq.placed_by).to_numpy()
        if aq.branch_id:
            m &= (inv["branch_id"] == aq.branch_id).to_numpy()
        if aq.channel:
            m &= (inv["channel"] == aq.channel).to_numpy()
        return m

    def _invoices(self, aq, p, out, offset, limit):
        acc = self.e.accounts
        inv = self._inv_frame(p)
        if aq.contains:
            o = acc.orders.loc[inv["order_id"]]
            inv = inv[self._contains_orders(o, aq.contains)[0]]
        sel = inv[self._inv_mask(inv, aq)]
        key = {"date_desc": ("invoice_date", False), "date_asc": ("invoice_date", True),
               "amount_desc": ("amount", False), "amount_asc": ("amount", True),
               "late_desc": ("days_late", False), "due_asc": ("due_date", True)}.get(aq.sort, ("invoice_date", False))
        sel = sel.sort_values(key[0], ascending=key[1])
        if aq.top_n:
            sel = sel.head(aq.top_n)
        out["total"] = int(len(sel))
        out["rows"] = [{"type": "invoice", **acc.invoice_card(r)} for r in sel.iloc[offset:offset + limit].to_dict("records")]
        pdv = sel[sel["status"] == "PAST_DUE"]
        out["summary"] = {"count": int(len(sel)), "amount": round(float(sel["amount"].sum()), 2),
                          "balance": round(float(sel["balance"].sum()), 2),
                          "past_due_count": int(len(pdv)), "past_due_balance": round(float(pdv["balance"].sum()), 2),
                          "oldest_due": str(pdv["due_date"].min().date()) if len(pdv) else None}
        ms = inv[self._inv_mask(inv, aq, skip="status")]
        vc = ms["status"].value_counts()
        out["facets"]["status"] = [{"value": s, "label": INV_STATUS[s], "count": int(vc.get(s, 0)),
                                    "selected": s in aq.status} for s in INV_STATUS]
        mj = inv[self._inv_mask(inv, aq, skip="job")]
        jc = mj.loc[mj["job_name"] != "", "job_name"].value_counts().head(10)
        out["facets"]["job"] = [{"value": j, "count": int(n), "selected": j in aq.jobs} for j, n in jc.items()]
        return out

    # ------------------------------------------------------------ lists
    def _lists(self, aq, p, out, offset, limit):
        acc, kits = self.e.accounts, self.e.kits
        mine = []
        if p.is_authenticated and p.can("lists") and aq.scope != "library":
            L = acc.visible_lists(p)
            L = L[L["list_type"] == "CUSTOMER"]
            if aq.scope == "shared":
                L = L[L["shared"]]
            elif aq.scope == "mine":
                L = L[L["owner_user_id"] == p.user_id]
            if aq.text:
                want = [w for w in parts_only(normalize(aq.text)) if w]
                items = acc.list_items[acc.list_items["list_id"].isin(L.index)]
                hit_items: dict[str, list] = {}
                for lid, sku in zip(items["list_id"], items["sku"]):
                    i = self.e.catalog.sku_to_idx.get(sku)
                    if i is None:
                        continue
                    tt = normalize(self.e.catalog.title[i])
                    if all(any(x == w or (len(w) >= 3 and x.startswith(w)) for x in tt) for w in want):
                        hit_items.setdefault(lid, []).append(self.e.catalog.title[i])
                name_hit = [lid for lid, n in zip(L.index, L["name"]) if all(w in normalize(n) for w in want)]
                keep = list(dict.fromkeys(name_hit + list(hit_items)))
                L = L.loc[keep]
            else:
                hit_items = {}
            L = L.sort_values("updated_at", ascending=False)
            for lid, r in L.iterrows():
                c = acc.list_card(lid)
                owner = acc.users.loc[r["owner_user_id"], "first_name"] if r["owner_user_id"] in acc.users.index else ""
                c.update({"type": "list", "owner": str(owner), "yours": r["owner_user_id"] == p.user_id,
                          "matches": hit_items.get(lid, [])[:2]})
                pos = acc.list_items[acc.list_items["list_id"] == lid].sort_values("sort")["sku"].head(3)
                c["preview"] = [self.e.catalog.title[self.e.catalog.sku_to_idx[s]] for s in pos if s in self.e.catalog.sku_to_idx]
                mine.append(c)
        elif not p.is_authenticated:
            out["signin"] = True
        out["rows"] = mine
        out["total"] = len(mine)
        # the library: Webb job kits (all of them, or the ones the words describe)
        if aq.scope in (None, "library"):
            if aq.text:
                lib = kits.search(aq.text, k=24, min_score=0.35)
                out["library_note"] = f"{len(lib)} job kits match “{aq.text}”"
            else:
                lib = kits.browse()
                out["library_note"] = f"{len(lib)} job kits · every part for a standard job, priced by brand"
            if p.is_authenticated and not p.sees_prices:
                lib = [k | {"min_total": None, "max_total": None} for k in lib]
            out["library"] = lib
            out["library_categories"] = kits.categories()
        out["summary"] = {"count": len(mine), "shared": sum(1 for c in mine if c["shared"]),
                          "yours": sum(1 for c in mine if c.get("yours")), "library": len(out.get("library", []))}
        return out

    # ------------------------------------------------------------ content
    def _content_df(self):
        if self._content is None:
            from webbsearch.config import settings
            c = pd.read_parquet(settings.canonical_dir / "content.parquet")
            self._content = c.set_index("content_id", drop=False)
        return self._content

    def _content_run(self, aq, p, out, offset, limit):
        c = self._content_df()
        vc = c["content_type"].value_counts()
        if aq.text:
            toks = [t for t in normalize(aq.text) if t]
            acl = p.acl("content")
            kinds = None
            res = self.e.backend.search_vertical("content", toks, acl, 400)
            ids = [d for d, _ in res if d in c.index]
            sub = c.loc[ids]
        else:
            sub = c if aq.ctype else c[c["content_type"] != "spec_sheet"]
            sub = sub.sort_values("published_at", ascending=False)
        tc = sub["content_type"].value_counts()
        out["facets"]["ctype"] = [{"value": k, "label": CTYPES[k][0], "count": int(tc.get(k, 0)) if (aq.text or aq.ctype) else int(vc.get(k, 0)),
                                   "selected": aq.ctype == k} for k in CTYPES if (tc.get(k, 0) or vc.get(k, 0))]
        if aq.ctype:
            sub = sub[sub["content_type"] == aq.ctype]
        out["total"] = int(len(sub))
        rows = []
        for r in sub.iloc[offset:offset + limit].to_dict("records"):
            body = str(r["body"] or "")
            rows.append({"type": "content", "content_id": r["content_id"], "content_type": r["content_type"],
                         "type_label": CTYPES.get(r["content_type"], (r["content_type"],))[0], "title": r["title"],
                         "snippet": body[:180] + ("…" if len(body) > 180 else ""), "url": r["url"],
                         "date": str(r["published_at"])[:10] if r["published_at"] is not None else ""})
        out["rows"] = rows
        out["summary"] = {"count": int(len(sub)), "library": int(len(c))}
        return out

    # ------------------------------------------------------------ suggestions
    def suggest(self, vertical: str, prefix: str, p: Principal, k: int = 8) -> dict:
        pre = (prefix or "").strip().lower()
        acc = self.e.accounts
        smart = {
            "invoices": ["past due invoices", "unpaid invoices", "invoices last month", "invoices this month",
                         "invoices due this week", "invoices over $1,000", "paid invoices this year",
                         "largest unpaid invoices", "60+ days past due", "invoices last 90 days"],
            "orders": ["open orders", "backordered orders", "orders last month", "orders this week",
                       "web orders last 30 days", "counter orders this month", "orders over $1,000",
                       "shipped orders", "orders last 90 days"],
            "lists": ["shared lists", "my private lists", "job kits", "water heater kits", "boiler kits", "toilet install kit"],
            "content": ["install manuals", "spec sheets", "how-to guides", "troubleshooting", "promotions", "events & training",
                        "branch hours", "buying guides", "FAQ returns policy"],
        }.get(vertical, [])
        out = {"smart": [], "records": []}
        words = pre.split()
        out["smart"] = [s for s in smart if not pre or all(w in s.lower() for w in words)][:k]
        if vertical in ("orders", "invoices") and p.is_authenticated and p.can(vertical):
            o = acc.orders.loc[acc._order_ids(p)] if vertical == "orders" else \
                acc.orders.loc[acc.invoices.loc[acc.inv_by_acct.get(p.account_id, [])]["order_id"]]
            if pre:
                jobs = [j for j in o["job_name"].unique() if j and all(w in j.lower() for w in words)]
                out["records"] += [{"label": j, "q": f"{vertical} for {j}", "kind": "Job"} for j in sorted(jobs)[:5]]
                pos = [x for x in o["po_number"].astype(str).unique() if x and x.lower().startswith(pre.replace(' ', ''))]
                out["records"] += [{"label": f"PO {x}", "q": f"po {x}", "kind": "PO"} for x in sorted(pos)[:4]]
                digits = re.sub(r"\D", "", pre)
                if len(digits) >= 3:
                    ids = (o.index if vertical == "orders" else acc.inv_by_acct.get(p.account_id, []))
                    hits = [str(x) for x in ids if str(x).startswith(digits)][:4]
                    out["records"] += [{"label": f"{vertical[:-1].capitalize()} {x}", "q": f"{vertical[:-1]} {x}",
                                        "kind": "#"} for x in hits]
            else:
                recent = o[o["job_name"] != ""].sort_values("order_date", ascending=False)["job_name"].drop_duplicates().head(4)
                out["records"] = [{"label": j, "q": f"{vertical} for {j}", "kind": "Recent job"} for j in recent]
        elif vertical == "lists":
            if p.is_authenticated and p.can("lists"):
                L = acc.visible_lists(p)
                L = L[L["list_type"] == "CUSTOMER"]
                names = [n for n in L["name"] if not pre or all(w in n.lower() for w in words)]
                out["records"] += [{"label": n, "q": n, "kind": "Your list"} for n in names[:5]]
            if pre:
                ks = self.e.kits.search(pre, k=4, min_score=0.35)
                out["records"] += [{"label": f"{x['name']} · {x['variant']}", "kit": x["kit_id"], "kind": "Job kit"} for x in ks]
        elif vertical == "content" and pre:
            c = self._content_df()
            res = self.e.backend.search_vertical("content", [t for t in normalize(pre)], p.acl("content"), 6)
            out["records"] = [{"label": c.loc[d, "title"], "q": c.loc[d, "title"],
                               "kind": CTYPES.get(c.loc[d, "content_type"], ("Doc",))[0]} for d, _ in res if d in c.index][:6]
        return out
