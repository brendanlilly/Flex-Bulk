"""Special and custom orders, tier 4 (design §7): never ranked with items. A search about one of the four families
shows a separate "Request a quote" row; the request goes to the branch's inside sales through the expert desk, with a
2-business-day answer target.

Families (decided): configured fixtures and trim; cut-to-length and fabricated; engineered HVAC and boiler systems;
pump packages and controls."""
from __future__ import annotations

import re

from webbsearch.extended.tiers import add_business_days

SLA_DAYS = 2
FAMILIES = {
    "fixtures": {"label": "Configured fixtures and trim",
                 "families": {"faucet", "sink", "toilet", "faucet_parts"},
                 "words": r"\b(configur\w*|custom (finish|trim|color)|special finish|trim kit|rough[- ]?in)\b",
                 "asks": ["Model or series", "Finish", "Configuration (holes, spout, handle)", "Quantity", "Need-by date"]},
    "cut": {"label": "Cut-to-length and fabricated",
            "families": {"cu_pipe", "pex_tubing", "hangers", "duct", "buttweld", "flange"},
            "words": r"\b(cut[- ]to[- ]length|cut to size|fabricat\w*|thread(ed)? (to|and) cut|custom length|spool)\b",
            "asks": ["Material and size", "Lengths and quantities", "End prep (threaded, grooved, beveled)",
                     "Drawings, if any", "Need-by date"]},
    "engineered": {"label": "Engineered HVAC and boiler systems",
                   "families": {"boiler", "ac_condenser", "furnace", "minisplit", "wh_indirect", "steam"},
                   "words": r"\b(engineer\w*|sizing|load calc\w*|system design|packaged system|skid|submittal package)\b",
                   "asks": ["Building and load (or the engineer's schedule)", "Fuel and venting", "Equipment tags",
                            "Submittals needed", "Need-by date"]},
    "pumps": {"label": "Pump packages and controls",
              "families": {"pumps", "circulator", "well", "mixing_valve", "radiant"},
              "words": r"\b(booster|duplex|triplex|pump package|lift station|control panel|vfd|packaged pump)\b",
              "asks": ["Flow and head (or duty point)", "Power (voltage, phase)", "Controls", "Enclosure",
                       "Need-by date"]},
}
TRIGGER = re.compile(r"\b(special order|custom order|quote|made to order|build to order)\b", re.I)


def family_for(q: str, dominant_family: str | None) -> str | None:
    """The special-order family a search is about: its own words first, else the page's main product family when
    the search asks for something custom."""
    t = (q or "").lower()
    for k, f in FAMILIES.items():
        if re.search(f["words"], t):
            return k
    if dominant_family and TRIGGER.search(t):
        for k, f in FAMILIES.items():
            if dominant_family in f["families"]:
                return k
    return None


def row(q: str, dominant_family: str | None, branch_name: str) -> dict | None:
    k = family_for(q, dominant_family)
    if k is None:
        return None
    f = FAMILIES[k]
    return {"family": k, "label": f["label"], "asks": f["asks"], "tier": "4",
            "text": f"Need it {('configured' if k == 'fixtures' else 'cut or fabricated' if k == 'cut' else 'engineered' if k == 'engineered' else 'packaged')}?",
            "detail": f"Request a quote. {branch_name} inside sales answers within {SLA_DAYS} business days.",
            "returns": "Non-returnable unless defective"}


def open_quote(desk, *, family: str, details: dict, principal, branch: str, today) -> dict:
    """A quote request on the expert desk (channel "quote"), due in 2 business days."""
    if family not in FAMILIES:
        raise ValueError("unknown special-order family")
    clean = {str(k)[:60]: str(v)[:1000] for k, v in (details or {}).items() if str(v).strip()}
    if not clean:
        raise ValueError("tell us what you need")
    q = f"Quote request: {FAMILIES[family]['label']}. " + "; ".join(f"{k}: {v}" for k, v in clean.items())
    due = add_business_days(today, SLA_DAYS)
    case = desk.open(question=q[:2000], conversation=[], sku=None, line=None, tier=4, channel="quote",
                     principal=principal, branch=branch,
                     draft={"kind": "quote", "family": family, "details": clean, "due": str(due)})
    return case | {"due": str(due), "family": family, "label": FAMILIES[family]["label"]}
