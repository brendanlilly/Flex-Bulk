"""Serial numbers -> manufacture date, by brand rule (the rules are the library's; synthetic for now)."""
from __future__ import annotations

import datetime as dt
import re

from webbsearch.knowledge.library import SERIAL_RULES

MONTHS = "ABCDEFGHJKLM"          # month letters, I skipped
# the value must hold a digit, so "where is the serial number" doesn't read NUMBER as the serial
_MODEL = re.compile(r"\bMODEL\s*(?:NO\.?|NUMBER|#)?\s*[:#.]?\s*((?=[A-Z0-9-]*\d)[A-Z0-9][A-Z0-9-]{3,})", re.I)
_SERIAL = re.compile(r"\bSERIAL\s*(?:NO\.?|NUMBER|#)?\s*[:#.]?\s*((?=[A-Z0-9-]*\d)[A-Z0-9][A-Z0-9-]{5,})", re.I)


def _week_to_month(year: int, week: int) -> int:
    d = dt.date(year, 1, 1) + dt.timedelta(days=(max(1, min(week, 53)) - 1) * 7 + 3)
    return d.month if d.year == year else 12


def decode(serial: str):
    s = re.sub(r"\s+", "", (serial or "").upper())
    for brand, (prefix, pat, how, _) in SERIAL_RULES.items():
        m = re.match(pat, s)
        if not m:
            continue
        g = m.groups()
        try:
            if how == "yy_ww":
                y, mo = 2000 + int(g[0]), _week_to_month(2000 + int(g[0]), int(g[1]))
            elif how == "ww_yy":
                y, mo = 2000 + int(g[1]), _week_to_month(2000 + int(g[1]), int(g[0]))
            elif how == "yy_mm":
                y, mo = 2000 + int(g[0]), int(g[1])
            elif how == "yyyy_mm":
                y, mo = int(g[0]), int(g[1])
            elif how == "m_yy":
                y, mo = 2000 + int(g[1]), MONTHS.index(g[0]) + 1
            else:
                continue
        except ValueError:
            continue
        if not (1 <= mo <= 12) or not (1990 <= y <= dt.date.today().year):
            continue
        return {"brand": brand, "year": y, "month": mo, "serial": s}
    return None


def example(brand: str, year: int, month: int, n: int = 12345) -> str:
    prefix, _, how, _ = SERIAL_RULES[brand]
    yy, week = year % 100, dt.date(year, month, 15).isocalendar()[1]
    return {"yy_ww": f"{prefix}{yy:02d}{week:02d}{n:06d}", "ww_yy": f"{prefix}{week:02d}{yy:02d}{n:06d}",
            "yy_mm": f"{prefix}{yy:02d}{month:02d}{n:05d}" if prefix in ("BW",) else
                     (f"{prefix}{yy:02d}{month:02d}-{n:05d}" if prefix == "BO" else f"{prefix}{yy:02d}{month:02d}{n:06d}"),
            "yyyy_mm": f"{prefix}{year:04d}{month:02d}{n:05d}", "m_yy": f"{prefix}{MONTHS[month - 1]}{yy:02d}{n:06d}"}[how]


def nameplate(text: str) -> dict:
    t = text or ""
    m, s = _MODEL.search(t), _SERIAL.search(t)
    out = {"model": m.group(1).upper() if m else None, "serial": s.group(1).upper() if s else None}
    if not out["serial"]:
        for tok in re.findall(r"[A-Z0-9-]{8,}", t.upper()):
            if decode(tok):
                out["serial"] = tok
                break
    return out


def add_months(d: dt.date, n: int) -> dt.date:
    y, m = divmod(d.month - 1 + n, 12)
    return dt.date(d.year + y, m + 1, 1)


def age_label(made: dt.date, today: dt.date) -> str:
    months = (today.year - made.year) * 12 + today.month - made.month
    y, m = divmod(max(0, months), 12)
    return f"{y} yr{'s' if y != 1 else ''} {m} mo" if y else f"{m} mo"
