"""Branch availability: what's on the shelf where the customer picks up.

Flex Bulk: on-hand comes from the stock file (flexbulk.loader -> data/canonical/stock.parquet), held as a sparse
item x branch table. Without a stock file every item has none on hand (lines say "ask the branch"). The synthetic
on-hand FlexSearch used for its demo is kept only for tests that build an Availability without a stock table.
"""
from __future__ import annotations


import numpy as np
import pandas as pd

# Fallback coordinates for the synthetic data's town names, used only when the branch master
# (config/fwwebb_branches.json, see branches.py) is missing. Production: the branch master's lat/lon.
BRANCH_GEO = {
    "Bedford": (42.49, -71.28), "Boston": (42.36, -71.06), "Worcester": (42.26, -71.80),
    "Springfield": (42.10, -72.59), "Lowell": (42.63, -71.32), "Brockton": (42.08, -71.02),
    "Framingham": (42.28, -71.42), "Hyannis": (41.65, -70.28), "New Bedford": (41.64, -70.93),
    "Pittsfield": (42.45, -73.25), "Peabody": (42.53, -70.93), "Woburn": (42.48, -71.15),
    "Plymouth": (41.96, -70.67), "Fitchburg": (42.58, -71.80), "Newton": (42.34, -71.21),
    "Manchester": (42.99, -71.46), "Nashua": (42.77, -71.47), "Concord": (43.21, -71.54),
    "Portsmouth": (43.07, -70.76), "Keene": (42.93, -72.28), "Lebanon": (43.64, -72.25),
    "Laconia": (43.53, -71.47), "Portland": (43.66, -70.26), "Bangor": (44.80, -68.77),
    "Augusta": (44.31, -69.78), "Lewiston": (44.10, -70.21), "Presque Isle": (46.68, -68.02),
    "Ellsworth": (44.54, -68.42), "Burlington": (44.48, -73.21), "Rutland": (43.61, -72.97),
    "Barre": (44.20, -72.50), "Brattleboro": (42.85, -72.56), "St. Johnsbury": (44.42, -72.02),
    "Hartford": (41.76, -72.68), "New Haven": (41.31, -72.92), "Waterbury": (41.56, -73.05),
    "Stamford": (41.05, -73.54), "Norwich": (41.52, -72.08), "Danbury": (41.39, -73.45),
    "Providence": (41.82, -71.41), "Warwick": (41.70, -71.42), "Albany": (42.65, -73.76),
    "Syracuse": (43.05, -76.15), "Rochester": (43.16, -77.61), "Buffalo": (42.89, -78.88),
    "Utica": (43.10, -75.23), "Binghamton": (42.10, -75.92), "Plattsburgh": (44.70, -73.45),
    "Watertown": (43.97, -75.91), "Kingston": (41.93, -74.00), "Poughkeepsie": (41.70, -73.92),
    "Glens Falls": (43.31, -73.64), "Ithaca": (42.44, -76.50), "Elmira": (42.09, -76.81),
    "Erie": (42.13, -80.09),
}
TIER_P = {"stocked": 0.9, "regional": 0.32, "dc": 0.0, "special": 0.0}


def _miles(lat1, lon1, lat2, lon2):
    r = 3958.8
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dp, dl = p2 - p1, np.radians(lon2 - lon1)
    a = np.sin(dp / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return 2 * r * np.arcsin(np.sqrt(a))


class Availability:
    def __init__(self, catalog, branches: pd.DataFrame, pop_365: np.ndarray, default_branch: str | None = None,
                 stock: pd.DataFrame | None = None):
        self.cat = catalog
        b = branches.reset_index(drop=True)
        self.ids = b["branch_id"].astype(str).to_numpy()
        self.names = b["name"].to_numpy()
        self.city = b["city"].to_numpy()
        self.state = b["state"].to_numpy() if "state" in b else np.array([""] * len(b))
        self.showroom = b["showroom"].fillna(False).astype(bool).to_numpy() if "showroom" in b else np.zeros(len(b), bool)
        self.region = b["region"].fillna("").astype(str).to_numpy() if "region" in b else np.array([""] * len(b))
        # wholesale counters: the only pickup, stock and transfer locations (a showroom-only location has none)
        self.counter = b["counter"].fillna(True).astype(bool).to_numpy() if "counter" in b else np.ones(len(b), bool)
        # coordinates from the branch master (config/fwwebb_branches.json); the table below only as a fallback
        geo = [(la, lo) if "lat" in b and pd.notna(la) and pd.notna(lo) else BRANCH_GEO.get(c, (42.4, -71.4))
               for c, la, lo in zip(self.city, b["lat"] if "lat" in b else [None] * len(b), b["lon"] if "lon" in b else [None] * len(b))]
        self.lat = np.array([g[0] for g in geo], dtype=float)
        self.lon = np.array([g[1] for g in geo], dtype=float)
        self.pos = {bid: i for i, bid in enumerate(self.ids)}
        self.dist = _miles(self.lat[:, None], self.lon[:, None], self.lat[None, :], self.lon[None, :])
        self.counters = np.flatnonzero(self.counter)
        self.default = default_branch if default_branch and self.counter[self.pos.get(str(default_branch), 0)] else self.ids[self.counters[0]]
        pn = np.log1p(pop_365)
        self.pop_norm = (pn / max(float(pn.max()), 1.0)).astype(np.float32)
        tiers = catalog.stock_tier
        self.p_tier = np.array([TIER_P.get(t, 0.0) for t in tiers], np.float32)
        self.cheap = (catalog.list_price < 15).astype(np.int32)
        self.table = None               # the stock file: sparse (item, branch) on-hand
        if stock is not None:
            from scipy.sparse import csr_matrix
            r = stock["sku"].map(catalog.sku_to_idx)
            c = stock["branch_id"].astype(str).map(self.pos)
            ok = r.notna() & c.notna()
            self.table = csr_matrix((stock.loc[ok, "qty"].to_numpy(np.float64).round().astype(np.int64),
                                     (r[ok].astype(int).to_numpy(), c[ok].astype(int).to_numpy())),
                                    shape=(catalog.n, len(self.ids)))

    def branch_index(self, branch_id: str | None) -> int:
        """The pickup counter for an id; a showroom-only id resolves to the nearest counter."""
        i = self.pos.get(str(branch_id), self.pos.get(str(self.default), 0))
        if not self.counter[i]:
            i = int(self.counters[np.argmin(self.dist[i, self.counters])])
        return i

    def near_counters(self, bi: int, radius: float) -> np.ndarray:
        """Counters within `radius` miles of branch index `bi`, excluding it."""
        return np.flatnonzero((self.dist[bi] <= radius) & self.counter & (np.arange(len(self.ids)) != bi))

    def counters_by_distance(self, bi: int) -> np.ndarray:
        """Counter indexes, nearest to `bi` first (bi itself first)."""
        return self.counters[np.argsort(self.dist[bi, self.counters], kind="stable")]

    def branch_info(self, branch_id: str | None) -> dict:
        i = self.branch_index(branch_id)
        return {"branch_id": self.ids[i], "name": self.names[i], "lat": float(self.lat[i]), "lon": float(self.lon[i]),
                "state": str(self.state[i])}

    def all_branches(self) -> list[dict]:
        """Pickup locations: wholesale counters only."""
        return [{"branch_id": self.ids[i], "name": self.names[i], "lat": float(self.lat[i]), "lon": float(self.lon[i]),
                 "state": str(self.state[i])} for i in self.counters.tolist()]

    def _qty(self, idx: np.ndarray, bidx: np.ndarray) -> np.ndarray:
        """On-hand, shape (len(idx), len(bidx)): the stock file's, else FlexSearch's synthetic demo numbers."""
        if self.table is not None:
            if not len(idx) or not len(bidx):
                return np.zeros((len(idx), len(bidx)), np.int64)
            q = self.table[np.asarray(idx, np.int64)][:, np.asarray(bidx, np.int64)].toarray().astype(np.int64)
            return np.where(self.counter[np.asarray(bidx, np.int64)][None, :], q, 0)
        i = idx.astype(np.uint64)[:, None]
        b = bidx.astype(np.uint64)[None, :]
        h = (i * np.uint64(2654435761) + b * np.uint64(40503) + np.uint64(97)) % np.uint64(2 ** 32)
        h2 = (h * np.uint64(2246822519) + np.uint64(3266489917)) % np.uint64(2 ** 32)
        u = (h % np.uint64(10000)).astype(np.float32) / 10000.0
        p = self.p_tier[idx][:, None] * (0.55 + 0.45 * self.pop_norm[idx][:, None])
        have = u < p
        base = 2 + (self.pop_norm[idx] * 30).astype(np.int64)[:, None]
        q = 1 + (h2 % base.astype(np.uint64)).astype(np.int64)
        q = q * (1 + 9 * self.cheap[idx][:, None])  # fittings sit in bins of dozens
        return np.where(have & self.counter[bidx.astype(np.int64)][None, :], q, 0)

    def summary(self, idx: np.ndarray, branch_id: str | None, radius: float = 25.0) -> dict:
        """Per-candidate availability at the pickup branch and within `radius` miles."""
        bi = self.branch_index(branch_id)
        near = self.near_counters(bi, radius)
        cols = np.concatenate([[bi], near])
        q = self._qty(np.asarray(idx, np.int64), cols) if len(idx) else np.zeros((0, len(cols)), np.int64)
        at = q[:, 0]
        nq = q[:, 1:].sum(axis=1) if len(near) else np.zeros(len(idx), np.int64)
        nn = (q[:, 1:] > 0).sum(axis=1) if len(near) else np.zeros(len(idx), np.int64)
        tiers = self.cat.stock_tier[np.asarray(idx, np.int64)] if len(idx) else []
        eta = []
        for a, n, t in zip(at.tolist(), nq.tolist(), tiers):
            if a > 0:
                eta.append("Pickup today")
            elif n > 0:
                eta.append("Transfer tomorrow")
            elif t == "special":
                eta.append("Special order 5-10 days")
            elif t == "dc":
                eta.append("Ships from CD in 1-2 days")
            else:
                eta.append("Ships in 2-3 days")
        return {"at_branch": at, "nearby_qty": nq, "nearby_branches": nn, "eta": eta,
                "radius": radius, "n_nearby_branches": int(len(near))}

    def map_all(self, i: int, branch_id: str | None) -> list[dict]:
        bi = self.branch_index(branch_id)
        q = self._qty(np.array([i]), self.counters)[0]
        return [{"branch_id": self.ids[b], "name": self.names[b], "qty": int(q[r]), "lat": float(self.lat[b]),
                 "lon": float(self.lon[b]), "home": b == bi, "miles": round(float(self.dist[bi, b]), 1)}
                for r, b in enumerate(self.counters.tolist())]

    def by_branch(self, i: int, branch_id: str | None, k: int = 8) -> list[dict]:
        bi = self.branch_index(branch_id)
        order = self.counters_by_distance(bi)
        q = self._qty(np.array([i]), order)[0]
        out = []
        for r, b in enumerate(order.tolist()):
            if r < k or q[r] > 0 and len(out) < k + 4:
                out.append({"branch_id": self.ids[b], "name": self.names[b], "miles": round(float(self.dist[bi, b]), 1),
                            "qty": int(q[r]), "lat": float(self.lat[b]), "lon": float(self.lon[b]), "home": b == bi})
        return out

    def all_qty(self, i: int) -> np.ndarray:
        """On-hand for one SKU at every branch (branch order = self.ids)."""
        return self._qty(np.array([i]), np.arange(len(self.ids)))[0]


def fulfil(need: int, qty: np.ndarray, av: "Availability", branch_id: str | None, fallback: str = "dc") -> dict:
    """How a line of `need` units gets to the pickup branch: on the shelf, part shelf + transfer, all transfer
    (nearest branches first) or from the CD / special order. `qty` is on-hand at every branch."""
    bi = av.branch_index(branch_id)
    home = str(av.names[bi]).split(",")[0]
    at = int(qty[bi])
    need = max(1, int(need))
    out = {"need": need, "at_branch": at, "branch": home, "plan": [], "short": max(0, need - at)}
    if at >= need:
        return out | {"status": "pickup", "eta": "Pickup today", "text": f"{at} on hand at {home}"}
    rem = need - at
    for b in av.counters_by_distance(bi).tolist():
        if b == bi or qty[b] <= 0:
            continue
        take = int(min(qty[b], rem))
        out["plan"].append({"branch": str(av.names[b]).split(",")[0], "branch_id": str(av.ids[b]), "qty": take,
                            "miles": round(float(av.dist[bi, b]), 1)})
        rem -= take
        if rem == 0 or len(out["plan"]) >= 3:
            break
    moved = need - at - rem
    far = max((x["miles"] for x in out["plan"]), default=0)
    when = "tomorrow" if far <= 60 else "in 2 days" if far <= 150 else "in 3-4 days"
    src = ", ".join(f"{x['qty']} from {x['branch']} ({x['miles']:g} mi)" for x in out["plan"])
    parts = [f"{at} on hand at {home}" if at else f"None at {home}"]
    if moved:
        parts.append(f"{moved} by transfer: {src}, arriving {when}")
    if rem:
        parts.append(f"{rem} " + ("special order, 5-10 days" if fallback == "special" else "from the CD, 1-2 days"))
    status = "partial" if at and moved and not rem else "transfer" if moved and not rem else fallback
    eta = "Pickup today" if status == "pickup" else f"Transfer {when}" if not rem else \
        ("Special order 5-10 days" if fallback == "special" else "Ships from CD in 1-2 days")
    return out | {"status": status, "eta": eta, "text": "; ".join(parts), "short": need - at}

