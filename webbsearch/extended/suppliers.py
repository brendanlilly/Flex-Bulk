"""Manufacturer (supplier) profiles and the feeds behind tiers 2 and 3 (design §3, §5, §6).

Production: Snowflake holds each manufacturer's inventory feed (factory stock with a timestamp), lead-time file, on-time
record from Webb's receipts, ship-from warehouses and the free-freight (FFA) minimum from the vendor agreement. This
copy derives a deterministic, plausible profile per brand from its name, so every run sees the same numbers.

Everything here is vectorized over numpy arrays: a profile is looked up per brand id, and per-item feeds are hashed from
(item key, brand) so 10M items cost a few array operations, not a Python loop."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

import numpy as np

# Origin warehouses (city, state, zip, lat, lon). Production: the manufacturer's ship-from addresses.
WAREHOUSES = [
    ("Allentown", "PA", "18106", 40.60, -75.49), ("Edison", "NJ", "08817", 40.52, -74.41),
    ("Columbus", "OH", "43217", 39.96, -82.99), ("Indianapolis", "IN", "46241", 39.77, -86.16),
    ("Chicago", "IL", "60638", 41.88, -87.63), ("Memphis", "TN", "38118", 35.15, -90.05),
    ("Atlanta", "GA", "30336", 33.75, -84.39), ("Charlotte", "NC", "28214", 35.23, -80.84),
    ("Dallas", "TX", "75261", 32.78, -96.80), ("Kansas City", "MO", "64161", 39.10, -94.58),
    ("Reno", "NV", "89506", 39.53, -119.81), ("Ontario", "CA", "91761", 34.06, -117.65),
    ("Worcester", "MA", "01607", 42.26, -71.80), ("Syracuse", "NY", "13211", 43.05, -76.15),
]
WH_LAT = np.array([w[3] for w in WAREHOUSES])
WH_LON = np.array([w[4] for w in WAREHOUSES])
FFA_LEVELS = (500.0, 750.0, 1000.0, 1500.0, 2500.0)
MAX_WH = 3


def _h(*parts) -> int:
    return int.from_bytes(hashlib.blake2b("|".join(map(str, parts)).encode(), digest_size=8).digest(), "little")


def _u(*parts) -> float:
    return (_h(*parts) % 1_000_000) / 1_000_000.0


def mix(a: np.ndarray, salt: int) -> np.ndarray:
    """A 64-bit hash of an integer array (splitmix64), for deterministic per-item draws."""
    with np.errstate(over="ignore"):
        x = a.astype(np.uint64) + np.uint64((salt * 0x9E3779B97F4A7C15) & 0xFFFFFFFFFFFFFFFF)
        x = (x ^ (x >> np.uint64(30))) * np.uint64(0xBF58476D1CE4E5B9)
        x = (x ^ (x >> np.uint64(27))) * np.uint64(0x94D049BB133111EB)
        return x ^ (x >> np.uint64(31))


def unif(a: np.ndarray, salt: int) -> np.ndarray:
    return (mix(a, salt) >> np.uint64(11)).astype(np.float64) / float(1 << 53)


def vendor_code(brand: str, taken: set[str]) -> str:
    """Webb's three-letter vendor code (synthetic): the brand's first letters, made unique."""
    letters = re.sub(r"[^A-Z]", "", brand.upper()) or "XXX"
    base = (letters + "XXX")[:3]
    if base not in taken:
        return base
    for c in letters[3:] + "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        cand = base[:2] + c
        if cand not in taken:
            return cand
    k = 2
    while f"{base[:2]}{k}" in taken:
        k += 1
    return f"{base[:2]}{k}"


@dataclass
class Supplier:
    brand: str
    code: str
    stock_feed: bool            # sends factory inventory
    lead_feed: bool             # sends lead times per part
    lead_mean: float            # business days, quoted
    lateness: float             # average lateness vs quote (0.12 = 12% late)
    feed_age_h: float           # typical age of the inventory feed, hours
    warehouses: list[int] = field(default_factory=list)
    ffa_min: float | None = None        # free freight at or above this PO value; None: freight is always added
    ship_data: float = 0.8      # share of items with weight and dimensions
    cortex_conf: float = 0.9    # typical Cortex pricing-group confidence

    def to_dict(self) -> dict:
        return {"brand": self.brand, "vendor_code": self.code, "stock_feed": self.stock_feed,
                "lead_feed": self.lead_feed, "lead_days_typical": round(self.lead_mean, 1),
                "on_time_lateness": round(self.lateness, 3), "feed_age_hours": round(self.feed_age_h, 1),
                "ships_from": [f"{WAREHOUSES[w][0]}, {WAREHOUSES[w][1]} {WAREHOUSES[w][2]}" for w in self.warehouses],
                "ffa_min": self.ffa_min}


def make_supplier(brand: str, code: str) -> Supplier:
    u = lambda k: _u(brand, k)  # noqa: E731
    nwh = 1 + int(u("nwh") * MAX_WH)
    whs = sorted({_h(brand, "wh", j) % len(WAREHOUSES) for j in range(nwh)})
    stale = u("stale") < 0.18
    return Supplier(
        brand=brand, code=code,
        stock_feed=u("stock") < 0.6, lead_feed=u("lead") < 0.8,
        lead_mean=2.0 + 26.0 * u("mean") ** 1.6,
        lateness=0.35 * u("late") ** 2,
        feed_age_h=(60.0 + 140.0 * u("age")) if stale else (2.0 + 28.0 * u("age")),
        warehouses=whs,
        ffa_min=FFA_LEVELS[_h(brand, "ffa") % len(FFA_LEVELS)] if u("ffa?") < 0.45 else None,
        ship_data=0.55 + 0.42 * u("ship"),
        cortex_conf=0.72 + 0.26 * u("conf"))


class Suppliers:
    """Profiles for every brand, in a stable order (brand id = position)."""

    def __init__(self, brands):
        self.brands = sorted(set(map(str, brands)))
        taken: set[str] = set()
        self.list: list[Supplier] = []
        for b in self.brands:
            c = vendor_code(b, taken)
            taken.add(c)
            self.list.append(make_supplier(b, c))
        self.id = {b: i for i, b in enumerate(self.brands)}
        self.by_code = {s.code: s for s in self.list}
        L = self.list
        self.a_stock = np.array([s.stock_feed for s in L])
        self.a_lead = np.array([s.lead_feed for s in L])
        self.a_mean = np.array([s.lead_mean for s in L])
        self.a_late = np.array([s.lateness for s in L])
        self.a_age = np.array([s.feed_age_h for s in L])
        self.a_ship = np.array([s.ship_data for s in L])
        self.a_conf = np.array([s.cortex_conf for s in L])
        self.a_wh = np.full((len(L), MAX_WH), -1, np.int16)
        for i, s in enumerate(L):
            self.a_wh[i, :len(s.warehouses)] = s.warehouses
        self.a_nwh = np.array([len(s.warehouses) for s in L])

    def __getitem__(self, brand: str) -> Supplier:
        return self.list[self.id[brand]]

    def ids(self, brands) -> np.ndarray:
        return np.array([self.id.get(str(b), 0) for b in brands], np.int32)

    def feeds(self, key: np.ndarray, bid: np.ndarray, salt: int = 0) -> dict:
        """Per-item factory stock, feed age and quoted lead time (business days) for items keyed by `key` (any
        stable integer) of brand ids `bid`. -1 = not reported."""
        u1, u2, u3, u4, u5 = (unif(key, salt * 10 + k) for k in range(1, 6))
        has_stock = self.a_stock[bid]
        qty = np.where(u1 < 0.28, 0, (1 + (u2 ** 2.2) * 240).astype(np.int64))
        qty = np.where(has_stock, qty, -1)
        age = self.a_age[bid] * (0.5 + u3)
        lead = np.maximum(1, np.round(self.a_mean[bid] * (0.4 + 1.3 * u4))).astype(np.int64)
        lead = np.where(u4 > 0.985, 121 + (u5 * 60).astype(np.int64), lead)      # a few long-lead parts
        known = self.a_lead[bid] & (u5 < 0.93)
        lead = np.where(known, lead, -1)
        wh = self.a_wh[bid, (mix(key, salt + 77) % self.a_nwh[bid].astype(np.uint64)).astype(np.int64)]
        return {"factory_qty": qty, "feed_age_h": age, "lead_days": lead, "origin": wh.astype(np.int64)}
