"""Each branch's same-day zone, region, serving CD and transfer days (design §2, decisions #1, #2, #12).

Same-day zone: counters within about a 45-minute drive. Drive time is estimated from straight-line miles with a road
factor and an average speed until Webb's routing data replaces it. Region: the branch master's region, which stands in
for the serving CD's delivery territory. CD: one per region; the prototype designates the counter nearest the region's
centre unless config/fwwebb_cds.json names one (a demo designation, not Webb's real CD list)."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

ROAD_FACTOR = 1.25       # road miles per straight-line mile
MPH = 40.0               # average speed for branch-to-branch trips
CD_FILE = Path(__file__).resolve().parents[2] / "config" / "fwwebb_cds.json"


def drive_minutes(miles: np.ndarray | float) -> np.ndarray | float:
    return np.asarray(miles) * ROAD_FACTOR / MPH * 60.0


class BranchMap:
    def __init__(self, av, zone_minutes: float = 45.0):
        self.av = av
        self.zone_minutes = float(zone_minutes)
        self.regions = sorted({r for r, c in zip(av.region, av.counter) if r and c})
        self.region_of = {i: av.region[i] for i in range(len(av.ids))}
        names = json.loads(CD_FILE.read_text()) if CD_FILE.exists() else {}
        self.cd: dict[str, int] = {}
        for r in self.regions:
            members = [i for i in av.counters.tolist() if av.region[i] == r]
            pick = av.pos.get(str(names.get(r))) if names.get(r) else None
            if pick is None:
                la, lo = np.mean(av.lat[members]), np.mean(av.lon[members])
                pick = min(members, key=lambda i: (av.lat[i] - la) ** 2 + (av.lon[i] - lo) ** 2)
            self.cd[r] = int(pick)
        self.cd_regions = list(self.cd)               # CD column order (index into cd_regions)

    def with_zone(self, minutes: float) -> "BranchMap":
        if abs(minutes - self.zone_minutes) < 1e-9:
            return self
        b = object.__new__(BranchMap)
        b.__dict__.update(self.__dict__)
        b.zone_minutes = float(minutes)
        return b

    def zone(self, bi: int) -> np.ndarray:
        """Counters in the same-day zone of branch index `bi`, the branch itself first."""
        av = self.av
        near = av.counters[(drive_minutes(av.dist[bi, av.counters]) <= self.zone_minutes) & (av.counters != bi)]
        return np.concatenate([[bi], near]).astype(np.int64)

    def region_counters(self, bi: int) -> np.ndarray:
        r = self.av.region[bi]
        return np.array([i for i in self.av.counters.tolist() if self.av.region[i] == r], np.int64)

    def region(self, bi: int) -> str:
        return str(self.av.region[bi])

    def cd_name(self, region: str) -> str:
        i = self.cd.get(region)
        return f"{region} CD" if i is None else f"{region} CD ({str(self.av.names[i]).split(',')[0]})"

    def transfer_days(self, bi: int, src: int) -> int:
        """Business days to move stock from counter `src` to `bi`: same region next day; otherwise by distance."""
        if self.av.region[bi] and self.av.region[bi] == self.av.region[src]:
            return 1
        miles = float(self.av.dist[bi, src])
        return 2 if miles <= 150 else 3

    def transfer_row(self, bi: int) -> np.ndarray:
        """Transfer business days from every branch to `bi` (index = branch index); 0 for `bi` itself."""
        av = self.av
        same = (av.region == av.region[bi]) & (av.region != "")
        td = np.where(same, 1, np.where(av.dist[bi] <= 150, 2, 3)).astype(np.int64)
        td[bi] = 0
        return td

    def describe(self, bi: int) -> dict:
        av = self.av
        z = self.zone(bi)
        return {"branch": str(av.names[bi]), "region": self.region(bi), "cd": self.cd_name(self.region(bi)),
                "zone_minutes": self.zone_minutes,
                "same_day": [str(av.names[i]) for i in z[1:].tolist()]}
