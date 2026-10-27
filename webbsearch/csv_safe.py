"""CSV cells that can't run as spreadsheet formulas. Search words, feedback comments and dictionary rows are typed
by customers or staff; Excel and Sheets run a cell that starts with = + - @ (or a tab or carriage return) as a
formula. Such a cell gets a leading apostrophe, unless it is a plain number like -5 or +3."""
from __future__ import annotations

import re

_NUMBER = re.compile(r"^[+-]?\d+(?:\.\d+)?$")


def csv_safe(v):
    if isinstance(v, str) and v and v[0] in "=+-@\t\r" and not _NUMBER.match(v):
        return "'" + v
    return v


def csv_safe_row(row) -> list:
    return [csv_safe(v) for v in row]
