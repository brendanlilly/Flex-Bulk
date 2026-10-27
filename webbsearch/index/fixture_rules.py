"""Catalog records a tradesperson would call impossible (see scripts/validate_fixtures.py).

The synthetic catalog has a few (1.2%): a wax ring sold by the gallon, a dual-value (45/5 MFD) capacitor
labeled "Start", a street elbow with two cup ends. Search still returns them; recommendations (Bulk order)
skip them when anything else fits, because a recommendation is a claim that the item is right.
"""
from __future__ import annotations

import re

RULES = [
    # (name, why it's wrong, title must match, and must also match / must not match)
    ("wax_ring_units", "Wax rings are sold each, not by the gallon or pound",
     r"\bwax ring\b", r"\bgallon\b|\b\d+ lb\b", None),
    ("dual_cap_start", "A dual value (45/5 MFD) is a dual run capacitor; start capacitors are single-value",
     r"\b\d+(?:\.\d+)?/\d+(?:\.\d+)? mfd\b", r"\bstart capacitor\b", None),
    ("street_elbow_ends", "A street elbow has one fitting (FTG) end; CxC / CxFIP / CxMIP contradict 'street'",
     r"\bstreet (?:90|45)\b", r"\bsweat (?:cxc|cxfip|cxmip)\b", None),
    ("nozzle_no_rating", "An oil burner nozzle needs flow (GPH), spray angle and pattern to be orderable",
     r"\bnozzle\b", None, r"\bgph\b|°|\bdeg\b"),
    ("pump_na_spec", "'N/A' in a product title is a missing attribute, not a spec",
     r"\bpump\b", r"\bN/A\b", None),
]
_C = [(n, re.compile(m, re.I), re.compile(a, re.I) if a else None, re.compile(nv, re.I) if nv else None) for n, _, m, a, nv in RULES]


def implausible(title: str) -> str | None:
    """The rule a title breaks, or None."""
    t = title or ""
    for name, m, also, never in _C:
        if m.search(t) and (also is None or also.search(t)) and (never is None or not never.search(t)):
            return name
    return None
