"""Freight: a ShipHawk-shaped rater (UPS and 12 LTL services) and free-freight (FFA) by manufacturer (design §4).

Production: ShipHawk, Webb's rating aggregator, rates UPS and Webb's 12 LTL services ZIP to ZIP. This module has the same
request and response shape over synthetic rate tables, so swapping in the real call changes one method (`rate`).

Rules kept here:
  * UPS when every package is <= 150 lb and length + girth <= 165 in; otherwise LTL.
  * Job sites default to liftgate + limited access (removable at checkout).
  * Hazmat ships ground only.
  * No weight or dimensions: no rate; the cart says "confirmed after you order", never $0.
  * Free freight when a manufacturer's PO (all ship-tos) reaches its FFA minimum; under it, the carrier cost."""
from __future__ import annotations

import math

UPS_MAX_LB = 150.0
UPS_MAX_LG = 165.0           # length + girth, inches
DIM_DIVISOR = 139.0          # UPS daily-rates dimensional weight
LTL_SERVICES = [f"LTL service {k}" for k in range(1, 13)]   # Webb's 12 LTL contracts (named in ShipHawk)
ACCESSORIALS = {"liftgate": 85.0, "limited_access": 95.0, "residential": 90.0, "appointment": 45.0,
                "inside_delivery": 125.0}
JOBSITE_DEFAULT = ("liftgate", "limited_access")
CLASS_FACTOR = {50: 0.55, 55: 0.6, 60: 0.65, 65: 0.7, 70: 0.75, 77: 0.8, 85: 0.88, 92: 0.95, 100: 1.0, 110: 1.08,
                125: 1.2, 150: 1.4, 175: 1.6, 200: 1.8, 250: 2.2, 300: 2.6, 400: 3.2, 500: 3.8}


def ups_zone(miles: float) -> int:
    for z, m in ((2, 150), (3, 300), (4, 600), (5, 1000), (6, 1400), (7, 1800)):
        if miles <= m:
            return z
    return 8


def parcel_ok(lb: float, dims: list[float]) -> bool:
    if lb <= 0 or len(dims) != 3 or min(dims) <= 0:
        return False
    L, W, H = sorted(dims, reverse=True)
    return lb <= UPS_MAX_LB and L + 2 * (W + H) <= UPS_MAX_LG


def _transit(miles: float, ltl: bool) -> int:
    d = 1 if miles <= 250 else 2 if miles <= 600 else 3 if miles <= 1000 else 4 if miles <= 1800 else 5
    return d + (1 if ltl else 0)


class ShipHawkSim:
    """rate(request) -> {"mode", "rates": [...], "missing": [...]}. Request (ShipHawk-shaped):
    {"origin": {"zip", "miles_to_destination"}, "destination": {"type": "branch"|"jobsite"|"residence"},
     "items": [{"sku", "weight_lb", "dims_in": [l, w, h], "freight_class", "hazmat", "quantity"}],
     "accessorials": [...]}"""

    def rate(self, req: dict) -> dict:
        items = req.get("items") or []
        miles = float((req.get("origin") or {}).get("miles_to_destination") or 0)
        dest = (req.get("destination") or {}).get("type", "branch")
        acc = [a for a in (req.get("accessorials") or []) if a in ACCESSORIALS]
        missing = [it.get("sku") for it in items if not it.get("weight_lb") or float(it.get("weight_lb") or 0) <= 0
                   or len(it.get("dims_in") or []) != 3 or min(float(x) for x in it["dims_in"]) <= 0]
        if missing:
            return {"mode": "unknown", "rates": [], "missing": missing}
        hazmat = any(it.get("hazmat") for it in items)
        parcel = all(parcel_ok(float(it["weight_lb"]), list(it["dims_in"])) for it in items)
        total_lb = sum(float(it["weight_lb"]) * int(it.get("quantity", 1)) for it in items)
        if parcel and total_lb <= 500:
            z = ups_zone(miles)
            cost = 0.0
            for it in items:
                L, W, H = it["dims_in"]
                bill = max(float(it["weight_lb"]), math.ceil(L * W * H / DIM_DIVISOR))
                cost += int(it.get("quantity", 1)) * (8.9 + bill * (0.38 + 0.11 * z))
            if dest == "residence":
                cost += 5.6
            if hazmat:
                cost += 41.0
            rates = [{"carrier": "UPS", "service": "UPS Ground", "mode": "parcel", "price": round(cost, 2),
                      "transit_days": _transit(miles, False)}]
            if not hazmat:
                rates.append({"carrier": "UPS", "service": "UPS 2nd Day Air", "mode": "parcel",
                              "price": round(cost * 2.6, 2), "transit_days": 2})
            return {"mode": "parcel", "rates": rates, "missing": []}
        # LTL: class-weighted cwt rate by distance, a minimum charge, fuel, accessorials; 12 services quoted
        cls = max((int(it.get("freight_class") or 100) for it in items), default=100)
        cwt = max(total_lb / 100.0, 1.0)
        base = cwt * CLASS_FACTOR.get(cls, 1.0) * (18.0 + 0.045 * miles)
        extras = sum(ACCESSORIALS[a] for a in acc)
        rates = []
        for k, name in enumerate(LTL_SERVICES):
            disc = 0.55 + 0.02 * ((k * 7) % 11)            # each contract's discount
            price = max(165.0, base * (1 - disc + 0.6)) * 1.24 + extras
            rates.append({"carrier": name, "service": "LTL standard", "mode": "ltl", "price": round(price, 2),
                          "transit_days": _transit(miles, True) + (k % 3 == 2)})
        rates.sort(key=lambda r: (r["price"], r["transit_days"]))
        return {"mode": "ltl", "rates": rates[:5], "missing": [], "accessorials": acc, "freight_class": cls}


def ffa_status(subtotal: float, ffa_min: float | None, vendor: str) -> dict:
    """Free-freight status for one manufacturer's PO (all ship-tos)."""
    if ffa_min is None:
        return {"free": False, "min": None, "short": None, "text": f"Freight added on {vendor} orders"}
    if subtotal >= ffa_min:
        return {"free": True, "min": ffa_min, "short": 0.0, "text": f"Free freight on {vendor}: this order qualifies"}
    short = round(ffa_min - subtotal, 2)
    return {"free": False, "min": ffa_min, "short": short,
            "text": f"{vendor} ships free over ${ffa_min:,.0f}: you're ${short:,.2f} away"}
