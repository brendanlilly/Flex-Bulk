"""Error codes as people say them: "E05", "error 3", "code 11", "4 flashes", "blinking green 3 times"."""
from __future__ import annotations

import re

_FLASH1 = re.compile(r"\b(\d{1,2})\s*(?:x\s*)?(?:flash(?:es)?|blinks?|blinking)\b", re.I)
_FLASH2 = re.compile(r"\b(?:flash(?:es|ing)?|blink(?:s|ing)?)\s+(?:\w+\s+)?(\d{1,2})\s*(?:times|x)\b", re.I)
_E = re.compile(r"\b[Ee]\s?-?\s?0*(\d{1,3})\b")
_CODE = re.compile(r"\b(?:error|err|fault|code)\s*(?:code\s*)?#?\s*0*(\d{1,3})\b", re.I)
ASKS = re.compile(r"\b(?:error|err|fault|code|flash\w*|blink\w*|e\s?-?\d{1,3})\b", re.I)


def parse(q: str):
    q = q or ""
    for rx, kind in ((_FLASH1, "flash"), (_FLASH2, "flash"), (_E, "E"), (_CODE, "code")):
        m = rx.search(q)
        if m:
            return kind, int(m.group(1))
    return None


def lookup(rows: list[dict], parsed):
    if not parsed:
        return None
    kind, n = parsed
    for r in rows:
        if r["n"] == n and (kind == "code" or r["kind"] == kind):
            return r
    return None


def label(parsed) -> str:
    kind, n = parsed
    return f"{n} flash{'es' if n != 1 else ''}" if kind == "flash" else (f"E{n:02d}" if kind == "E" else f"code {n}")
