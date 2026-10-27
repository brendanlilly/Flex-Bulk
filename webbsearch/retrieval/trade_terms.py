"""Trade terms that name a standard or a connection, not a model number.

"C900", "DR18" and "SDR35" look like part numbers to a tokenizer, and "MJ" is two letters the coverage check
ignores. Each one decides what kind of product can answer the search: an MJ elbow is a waterworks fitting, never a
sheet-metal duct elbow; SDR 35 is PVC sewer pipe. When no product carries the term, the page keeps only the
product families the term can belong to (and says what is missing); with none of those, the catalog doesn't carry it.

`families` is the set of catalog families that can hold the term (empty: none in this catalog). Production: this
list moves to the search dictionary as its own entry kind so the eCommerce team can extend it.
"""
from __future__ import annotations

import re

PVC_PRESSURE_PIPE: set[str] = set()          # the synthetic catalog has no PVC pressure pipe
SEWER = {"dwv_fitting"}
WATERWORKS = {"waterworks", "gate_check_valve"}

TERMS: list[tuple[str, str, str, set[str]]] = [
    # (pattern on the lower-cased query, key, label, families)
    (r"\bc[-\s]?900\b", "c900", "AWWA C900 PVC pressure pipe", PVC_PRESSURE_PIPE),
    (r"\bc[-\s]?905\b", "c905", "AWWA C905 PVC pressure pipe", PVC_PRESSURE_PIPE),
    (r"\bc[-\s]?909\b", "c909", "AWWA C909 PVCO pressure pipe", PVC_PRESSURE_PIPE),
    (r"\bdr[-\s]?(?:14|18|21|25|26|32\.5|41)\b", "dr", "DR (pressure class)", PVC_PRESSURE_PIPE),
    (r"\bsdr[-\s]?(?:26|35)\b", "sdr_sewer", "SDR 35 / SDR 26 PVC sewer", SEWER),
    (r"\bd[-\s]?3034\b", "d3034", "ASTM D3034 PVC sewer", SEWER),
    (r"\bmj\b|\bmechanical[-\s]+joint\b", "mj", "MJ (mechanical joint)", WATERWORKS),
    (r"\bc[-\s]?1(?:10|53)\b", "c110", "AWWA C110/C153 ductile iron fittings", {"waterworks"}),
    (r"\bc[-\s]?5(?:09|15)\b", "c509", "AWWA C509/C515 resilient wedge gate valve", WATERWORKS),
]
_TERMS = [(re.compile(p), k, label, fams) for p, k, label, fams in TERMS]
MATERIAL = {"sdr_sewer": "PVC", "d3034": "PVC", "c900": "PVC", "c905": "PVC", "c909": "PVC", "dr": "PVC"}
# a pipe standard names the pipe; a coupling or adapter for that pipe is still the right kind of product.
# A connection code (MJ) or a fitting standard (C110) is the product's own ends or body
KIND = {"c900": "pipe", "c905": "pipe", "c909": "pipe", "dr": "pipe", "sdr_sewer": "pipe", "d3034": "pipe",
        "mj": "connection", "c110": "fitting", "c509": "valve"}
# the tokens these become after the analyzer, so model-number and coverage checks can recognize them
TOKEN_RE = re.compile(r"^(?:c9\d\d|c1(?:10|53)|c5(?:09|15)|dr\d{2}|sdr\d{2}|d3034|mj)$")


def find(raw: str) -> list[dict]:
    """The trade terms in a query, in the order they appear."""
    low = (raw or "").lower()
    out, seen = [], set()
    for rx, key, label, fams in _TERMS:
        m = rx.search(low)
        if m and key not in seen:
            seen.add(key)
            out.append({"key": key, "text": m.group(0).upper().replace(" ", ""), "label": label,
                        "families": sorted(fams), "pos": m.start(), "material": MATERIAL.get(key),
                        "kind": KIND.get(key)})
    return sorted(out, key=lambda t: t["pos"])


def is_term(chunk: str) -> bool:
    """A typed chunk that is a standard / connection code, not a model number ("C900", "DR18", "SDR35")."""
    return bool(TOKEN_RE.match(re.sub(r"[^a-z0-9]", "", chunk.lower())))
