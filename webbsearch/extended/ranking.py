"""How availability moves results (design §8-§10, revised v0.5): the dashboard's "Availability ranking" settings.

Coded items (tiers 1-2) always come first. Within them, Score = R + A: R is the ranker's relevance, A the availability
adjustment (tier weight, delivery days, data quality), A scaled to the spread of relevance among the page's best matches
so a tier weight of 0.30 means "30% of the gap between the best and the 100th best match". Extended Catalog (-EC) items
are a separate section after every relevant coded item; within it the same A orders tiers 3a/3b/3c."""
from __future__ import annotations

import copy

import numpy as np

SEGMENTS = ["plumbing_contractor", "hvac_contractor", "mechanical_contractor", "builder_remodeler", "facilities_mro",
            "industrial", "water_works"]

DEFAULTS = {
    "enabled": True,                       # availability moves coded results
    "tiers": {"1a": 0.30, "1b": 0.24, "1c": 0.16, "2a": 0.08, "2b": 0.04, "2c": 0.0,
              "3a": -0.02, "3b": -0.06, "3c": -0.20},
    "per_day": 0.01,                       # minus this per business day of the delivery estimate
    "day_cap": 0.10,                       # at most this much for slow delivery
    "data": {"ship_complete": 0.02, "ship_missing": -0.03, "no_price": -0.05},
    "stale_hours": 48,                     # factory stock older than this counts as unknown
    "zone_minutes": 45,                    # the same-day zone: drive time from the pickup branch
    "relevance_window": 100,               # A is scaled to the relevance spread of this many best matches
    "brands": {},                          # brand -> {"mode": "boost"|"bury"|"block", "value": 0.05}
    "ffa_boost": 0.0,                      # off: items that help reach a manufacturer's free-freight minimum
    "ec": {
        "enabled": True,                   # the Extended Catalog section
        "segments": list(SEGMENTS),        # who sees it: signed-in accounts of these segments
        "min_coverage": 0.75,              # share of the searched words an -EC item must carry
        "max_items": 24,                   # items in the section
        "budget_ms": 400,                  # the section gives up (and says so) after this long
        "price_confidence": 85,            # Cortex pricing-group confidence needed to show a price
    },
}

LIMITS = {"tier": (-1.0, 1.0), "per_day": (0.0, 0.1), "day_cap": (0.0, 0.5), "data": (-0.5, 0.5),
          "stale_hours": (1, 720), "zone_minutes": (10, 120), "relevance_window": (10, 600),
          "brand_value": (0.0, 1.0), "ffa_boost": (0.0, 0.2), "min_coverage": (0.3, 1.0), "max_items": (0, 60),
          "budget_ms": (100, 3000), "price_confidence": (50, 100)}


class SettingsError(ValueError):
    pass


def _num(v, lo, hi, name):
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise SettingsError(f"{name}: a number")
    if not (lo <= x <= hi) or x != x:
        raise SettingsError(f"{name}: between {lo:g} and {hi:g}")
    return x


def _bool(v, name):
    if isinstance(v, bool):
        return v
    if v in (0, 1):
        return bool(v)
    raise SettingsError(f"{name}: true or false")


def merged(cur: dict | None, patch: dict | None) -> dict:
    """The current availability settings with `patch` applied, validated. Unknown keys are refused."""
    out = copy.deepcopy(cur or DEFAULTS)
    for k, v in DEFAULTS.items():
        out.setdefault(k, copy.deepcopy(v))
    for k, v in DEFAULTS["ec"].items():
        out["ec"].setdefault(k, copy.deepcopy(v))
    p = patch or {}
    if not isinstance(p, dict):
        raise SettingsError("availability: an object")
    for k, v in p.items():
        if k == "enabled":
            out["enabled"] = _bool(v, "enabled")
        elif k == "tiers":
            if not isinstance(v, dict):
                raise SettingsError("tiers: an object")
            for t, w in v.items():
                if t not in DEFAULTS["tiers"]:
                    raise SettingsError(f"tiers: unknown tier {t}")
                out["tiers"][t] = _num(w, *LIMITS["tier"], f"tier {t}")
        elif k in ("per_day", "day_cap", "ffa_boost", "stale_hours", "zone_minutes", "relevance_window"):
            x = _num(v, *LIMITS[k], k)
            out[k] = int(x) if k in ("stale_hours", "zone_minutes", "relevance_window") else x
        elif k == "data":
            if not isinstance(v, dict):
                raise SettingsError("data: an object")
            for d, w in v.items():
                if d not in DEFAULTS["data"]:
                    raise SettingsError(f"data: unknown adjustment {d}")
                out["data"][d] = _num(w, *LIMITS["data"], d)
        elif k == "brands":
            if not isinstance(v, dict) or len(v) > 200:
                raise SettingsError("brands: an object of up to 200 brands")
            nb = {}
            for b, r in v.items():
                if not isinstance(r, dict) or r.get("mode") not in ("boost", "bury", "block"):
                    raise SettingsError(f"brands: {b}: mode boost, bury or block")
                nb[str(b)[:80]] = {"mode": r["mode"],
                                   "value": _num(r.get("value", 0.05), *LIMITS["brand_value"], f"{b} value")}
            out["brands"] = nb
        elif k == "ec":
            if not isinstance(v, dict):
                raise SettingsError("ec: an object")
            for e, w in v.items():
                if e == "enabled":
                    out["ec"]["enabled"] = _bool(w, "ec.enabled")
                elif e == "segments":
                    if not isinstance(w, list) or any(s not in SEGMENTS for s in w):
                        raise SettingsError(f"ec.segments: a list of {', '.join(SEGMENTS)}")
                    out["ec"]["segments"] = list(dict.fromkeys(w))
                elif e in ("min_coverage", "max_items", "budget_ms", "price_confidence"):
                    x = _num(w, *LIMITS[e], f"ec.{e}")
                    out["ec"][e] = x if e == "min_coverage" else int(x)
                else:
                    raise SettingsError(f"ec: unknown setting {e}")
        else:
            raise SettingsError(f"availability: unknown setting {k}")
    if out["day_cap"] < out["per_day"]:
        raise SettingsError("day_cap: at least per_day")
    return out


def adjustment(cfg: dict, tier: np.ndarray, hi_days: np.ndarray, ship_known: np.ndarray | None = None,
               price_known: np.ndarray | None = None, brands: np.ndarray | None = None) -> np.ndarray:
    """A per item: tier weight, minus per-day delivery (capped), plus data-quality and brand rules."""
    w = cfg["tiers"]
    a = np.array([w.get(str(t), 0.0) for t in tier], np.float64)
    days = np.asarray(hi_days, np.float64)
    tier_s = np.array([str(t) for t in tier])
    pen = np.minimum(np.where(days >= 0, days, 0) * cfg["per_day"], cfg["day_cap"])
    # a delivery nobody can estimate (tiers 2c, 3c) is never better than a slow one Webb can quote: the full cap
    off_shelf = np.array([t[:1] in ("2", "3") for t in tier_s], bool)
    pen = np.where((days < 0) & off_shelf, cfg["day_cap"], pen)
    a -= pen
    if ship_known is not None:
        a += np.where(ship_known, cfg["data"]["ship_complete"], cfg["data"]["ship_missing"])
    if price_known is not None:
        a += np.where(price_known, 0.0, cfg["data"]["no_price"])
    if brands is not None and cfg.get("brands"):
        for b, r in cfg["brands"].items():
            m = brands == b
            if r["mode"] == "boost":
                a[m] += r["value"]
            elif r["mode"] == "bury":
                a[m] -= r["value"]
    return a


def blocked(cfg: dict, brands) -> np.ndarray:
    bl = {b for b, r in (cfg.get("brands") or {}).items() if r["mode"] == "block"}
    return np.array([str(b) in bl for b in brands], bool) if bl else np.zeros(len(brands), bool)


def relevance_span(s: np.ndarray, offset_free: np.ndarray, window: int) -> float:
    """The spread of relevance among the `window` best matches without the exact-id / compliance offsets."""
    base = np.sort(s[offset_free])[::-1][:max(2, int(window))]
    if len(base) < 2:
        return 1.0
    return max(float(base[0] - base[-1]), 1e-3)
