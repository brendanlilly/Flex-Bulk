"""Availability tiers and delivery estimates (design §2, §3, §6).

  1a  in stock in the branch's same-day zone      1b  in the region (or its CD), next business day
  1c  elsewhere at Webb, a 2-3 day transfer        2a/2b/2c  coded non-stock: factory stock / lead time / unknown
  3a/3b/3c  the same for Extended Catalog (-EC) items          4  special order (a separate row, special.py)

A tier is computed per item, branch and quantity. Factory stock older than the staleness limit counts as unknown stock
(a falls to b). Lead times are manufacturer's estimates: the delivery estimate adds the manufacturer's average lateness,
transit from the ship-from warehouse and a day of handling, and the page says it is an estimate."""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np

from webbsearch.extended.suppliers import WH_LAT, WH_LON, WAREHOUSES, Suppliers, unif
from webbsearch.retrieval.availability import _miles

TIERS = ("1a", "1b", "1c", "2a", "2b", "2c", "3a", "3b", "3c")
LONG_LEAD = 120          # business days; beyond this a lead time shows as "long lead time, confirmed at order"
CD_SHARE = 0.4           # share of CD-tier (dc) items the regional CDs hold (synthetic; production: CD on-hand)


def add_business_days(d: date, n: int) -> date:
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def transit_days(miles: np.ndarray, ltl: np.ndarray | bool = False) -> np.ndarray:
    """Ground transit business days by distance; LTL a day more."""
    m = np.asarray(miles, float)
    d = np.select([m <= 250, m <= 600, m <= 1000, m <= 1800], [1, 2, 3, 4], 5)
    return d + np.asarray(ltl, int)


class Tiering:
    def __init__(self, av, bmap, suppliers: Suppliers, catalog, today: date, brand_id: np.ndarray | None = None):
        self.av, self.bm, self.sup, self.cat, self.today = av, bmap, suppliers, catalog, today
        if brand_id is not None:                 # shared with another Tiering over the same suppliers (no 750k loop)
            self._brand_id = brand_id
        else:
            self._brand_id = np.array([suppliers.id.get(str(b), 0) for b in catalog.brand], np.int32) \
                if catalog is not None else np.zeros(0, np.int32)
        reg = list(bmap.cd_regions)
        self.cd_idx = np.array([bmap.cd[r] for r in reg], np.int64)
        self.cd_region = reg

    # ------------------------------------------------------------ helpers
    def wh_miles(self, origin: np.ndarray, bi: int) -> np.ndarray:
        o = np.clip(np.asarray(origin, np.int64), 0, len(WAREHOUSES) - 1)
        return _miles(WH_LAT[o], WH_LON[o], self.av.lat[bi], self.av.lon[bi])

    def cd_qty(self, idx: np.ndarray) -> np.ndarray:
        """On-hand at each region's CD, shape (len(idx), n_regions). Only CD-tier items the CDs hold."""
        idx = np.asarray(idx, np.int64)
        held = (self.cat.stock_tier[idx] == "dc") & (unif(idx, 31) < CD_SHARE)
        out = np.zeros((len(idx), len(self.cd_idx)), np.int64)
        for j in range(len(self.cd_idx)):
            have = held & (unif(idx * 7 + j, 32) < 0.75)
            out[:, j] = np.where(have, 4 + (unif(idx * 11 + j, 33) * 56).astype(np.int64), 0)
        return out

    def _supplier_tier(self, fq, age, lead, need, stale_h, late, transit, letter_base):
        """Tier letters (a/b/c) and delivery days (lo, hi; -1 unknown) from factory stock and lead time."""
        fresh = (fq >= need) & (age <= stale_h)
        lead = np.where(lead == 0, 1, lead)                 # under a business day counts as one
        lead = np.where(lead > LONG_LEAD, -1, lead)
        t = np.where(fresh, "a", np.where(lead > 0, "b", "c"))
        lo = np.where(fresh, 1 + transit, np.where(lead > 0, lead + transit + 1, -1))
        hi = np.where(fresh, 2 + transit, np.where(lead > 0, np.ceil(lead * (1 + late)) + transit + 1, -1))
        return np.char.add(letter_base, t.astype(str)), lo.astype(np.int64), hi.astype(np.int64), None

    # ------------------------------------------------------------ coded items
    def coded(self, idx: np.ndarray, branch_id: str | None, need: int = 1, stale_h: float = 48.0,
              held: int = 0) -> dict:
        """Tiers for catalog items `idx` at a pickup branch for `need` units (vectorized). `held`: units of the
        pickup branch's own shelf already promised (a split cart line), so they aren't counted twice."""
        av, bm = self.av, self.bm
        idx = np.asarray(idx, np.int64)
        need = max(1, int(need))
        bi = av.branch_index(branch_id)
        cols = av.counters
        q = av._qty(idx, cols) if len(idx) else np.zeros((0, len(cols)), np.int64)
        zone = np.isin(cols, bm.zone(bi))
        region = av.region[cols] == av.region[bi]
        td = bm.transfer_row(bi)[cols]
        cdq = self.cd_qty(idx) if len(idx) else np.zeros((0, len(self.cd_idx)), np.int64)
        own = np.array([r == av.region[bi] for r in self.cd_region])
        if bi in set(cols.tolist()):
            ci = list(cols).index(bi)
            if held:
                q = q.copy()
                q[:, ci] = np.maximum(0, q[:, ci] - int(held))
            at = q[:, ci]
        else:
            at = np.zeros(len(idx), np.int64)
        zq = q[:, zone].sum(1)
        rq = zq + q[:, region & ~zone].sum(1) + (cdq[:, own].sum(1) if own.any() else 0)
        aq = q.sum(1) + cdq.sum(1)
        tier = np.full(len(idx), "", dtype="<U2")
        lo = np.full(len(idx), -1, np.int64)
        hi = np.full(len(idx), -1, np.int64)
        tier[aq >= need] = "1c"
        tier[rq >= need] = "1b"
        tier[zq >= need] = "1a"
        lo[tier == "1a"], hi[tier == "1a"] = 0, 0
        lo[tier == "1b"], hi[tier == "1b"] = 1, 1
        far = np.where(q > 0, td[None, :], 99).min(1) if len(idx) else np.zeros(0, np.int64)
        cd_far = np.where(cdq > 0, np.array([bm.transfer_row(bi)[c] for c in self.cd_idx])[None, :], 99).min(1) \
            if len(idx) else np.zeros(0, np.int64)
        d1c = np.clip(np.minimum(far, cd_far), 2, 3)
        lo[tier == "1c"], hi[tier == "1c"] = d1c[tier == "1c"], d1c[tier == "1c"]
        rest = tier == ""
        long_lead = np.zeros(len(idx), bool)
        if rest.any():
            bid = self._brand_id[idx[rest]]
            f = self.sup.feeds(idx[rest], bid, salt=7)
            long_lead[rest] = f["lead_days"] > LONG_LEAD
            tr = transit_days(self.wh_miles(f["origin"], bi))
            t2, l2, h2, _ = self._supplier_tier(f["factory_qty"], f["feed_age_h"], f["lead_days"], need, stale_h,
                                                self.sup.a_late[bid], tr, "2")
            tier[rest], lo[rest], hi[rest] = t2, l2, h2
        return {"tier": tier, "lo": lo, "hi": hi, "at_branch": at, "zone_qty": zq, "region_qty": rq, "all_qty": aq,
                "need": need, "branch": int(bi), "long_lead": long_lead}

    # ------------------------------------------------------------ -EC items
    def extended(self, store, rows: np.ndarray, branch_id: str | None, need: int = 1, stale_h: float = 48.0,
                 sup_of_store: np.ndarray | None = None, aged_h: float = 0.0) -> dict:
        """`sup_of_store`: the store's brand ids -> these suppliers' ids. `aged_h`: hours since the build, added to the
        feed ages so factory stock goes stale on time."""
        rows = np.asarray(rows, np.int64)
        bi = self.av.branch_index(branch_id)
        need = max(1, int(need))
        c = store.cols
        bid = np.asarray(c["brand"][rows], np.int64)
        fq = np.asarray(c["fqty"][rows], np.int64)
        age = np.asarray(c["fage"][rows], float) + max(0.0, float(aged_h))
        lead = np.asarray(c["lead"][rows], np.int64)
        wt = np.asarray(c["wt"][rows], float) / 10.0
        ltl = wt * need > 150
        tr = transit_days(self.wh_miles(np.asarray(c["origin"][rows]), bi), ltl)
        if sup_of_store is not None:
            bid = np.asarray(sup_of_store, np.int64)[bid]
        late = self.sup.a_late[np.clip(bid, 0, len(self.sup.a_late) - 1)] if len(self.sup.a_late) else 0.0
        t3, lo, hi, _ = self._supplier_tier(fq, age, lead, need, stale_h, late, tr, "3")
        return {"tier": t3, "lo": lo, "hi": hi, "need": need, "branch": int(bi),
                "long_lead": (lead > LONG_LEAD), "stale": (fq >= need) & (age > stale_h)}

    # ------------------------------------------------------------ words
    def label(self, tier: str, lo: int, hi: int, brand: str = "", where: dict | None = None) -> dict:
        """What a card says: a short line, a detail and whether it is a manufacturer's estimate."""
        where = where or {}
        br = where.get("branch", "your branch")
        if tier == "1a":
            if where.get("at_branch", 0) >= where.get("need", 1):
                return {"text": f"In stock at {br}", "detail": "Pickup today", "estimate": False}
            return {"text": "In stock nearby", "detail": f"Same-day from a branch near {br}", "estimate": False}
        if tier == "1b":
            return {"text": "In stock in your region", "detail": "Next business day", "estimate": False}
        if tier == "1c":
            return {"text": "In stock at Webb", "detail": f"Transfer, {lo} business days", "estimate": False}
        src = brand or "the manufacturer"
        if tier.endswith("a"):
            return {"text": f"Ships from {src}", "detail": f"In stock at the factory; about {lo}-{hi} business days (manufacturer's estimate)",
                    "estimate": True}
        if tier.endswith("b"):
            rng = f"{lo}-{hi}" if hi > lo else f"{lo}"
            return {"text": f"Ships from {src}", "detail": f"About {rng} business days (manufacturer's estimate)",
                    "estimate": True}
        if where.get("long_lead"):
            return {"text": f"Ships from {src}", "detail": "Long lead time; confirmed at order", "estimate": True}
        return {"text": f"Ships from {src}", "detail": "Lead time confirmed at order", "estimate": True}

    def arrives(self, hi: int) -> str | None:
        if hi is None or hi < 0:
            return None
        return add_business_days(self.today, int(hi)).strftime("%a %b %-d")
