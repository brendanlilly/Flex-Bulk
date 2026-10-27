"""Demo branch stock for real Webb items from the cross-reference lake.

The lake knows which Webb items exist (4,388 parts, 3,783 Control #s) but not
what is on the shelf. Out-of-stock swaps need an on-hand number, so this
module derives a deterministic one per (Webb item, branch) the same way
availability.py does for the synthetic catalog. Every response carries
`demo: True` and the UI says "demo stock".

Production: replace `_qty` with the branch on-hand feed keyed by Control #.
Nothing else changes: the equivalents logic in xref.py only asks "how many at
this branch, how many nearby".
"""
from __future__ import annotations

import zlib

import numpy as np

# share of branches that carry the class (repair parts: capacitors and igniters sit on
# every counter; motors, gas valves and boards are thinner)
CARRY = {"capacitor": 0.78, "contactor": 0.72, "igniter": 0.7, "flame_sensor": 0.7, "relay": 0.62,
         "thermocouple": 0.66, "fuse": 0.8, "transformer": 0.55, "pressure_control": 0.5, "gas_valve": 0.48,
         "motor": 0.42, "draft_inducer": 0.35, "control_board": 0.34, "ignition_control": 0.46,
         "motor_accessory": 0.7, "press_fitting": 0.75, "thermostat": 0.6, "compressor": 0.2}
DEPTH = {"capacitor": 12, "contactor": 8, "fuse": 20, "motor_accessory": 10, "press_fitting": 40, "igniter": 8,
         "flame_sensor": 8, "thermocouple": 10}


class DemoStock:
    def __init__(self, availability):
        self.av = availability

    def _qty(self, item: str, cls: str, bidx: np.ndarray) -> np.ndarray:
        seed = np.uint64(zlib.crc32(str(item).encode()))
        b = np.asarray(bidx, np.uint64)
        h = (seed * np.uint64(2654435761) + b * np.uint64(40503) + np.uint64(7)) % np.uint64(2 ** 32)
        h2 = (h * np.uint64(2246822519) + np.uint64(3266489917)) % np.uint64(2 ** 32)
        u = (h % np.uint64(10000)).astype(np.float32) / 10000.0
        have = u < CARRY.get(cls or "", 0.5)
        q = 1 + (h2 % np.uint64(DEPTH.get(cls or "", 5))).astype(np.int64)
        return np.where(have & self.av.counter[np.asarray(bidx, np.int64)], q, 0)     # showroom-only: no stock

    def summary(self, item: str, cls: str, branch_id: str | None, radius: float = 25.0, k: int = 0) -> dict:
        av = self.av
        bi = av.branch_index(branch_id)
        near = av.near_counters(bi, radius)
        at = int(self._qty(item, cls, np.array([bi]))[0])
        nq = self._qty(item, cls, near) if len(near) else np.zeros(0, np.int64)
        out = {"at_branch": at, "nearby_qty": int(nq.sum()), "nearby_branches": int((nq > 0).sum()),
               "branch": str(av.names[bi]).split(",")[0], "radius": radius, "demo": True}
        out["eta"] = "Pickup today" if at else "Transfer tomorrow" if out["nearby_qty"] else "Ships from CD in 1-2 days"
        if k:
            order = av.counters_by_distance(bi)
            q = self._qty(item, cls, order)
            rows = []
            for r, b in enumerate(order.tolist()):
                if r < k or (q[r] > 0 and len(rows) < k + 3):
                    rows.append({"branch_id": str(av.ids[b]), "name": str(av.names[b]), "qty": int(q[r]),
                                 "miles": round(float(av.dist[bi, b]), 1), "home": b == bi})
            out["by_branch"] = rows
        return out

    def all_qty(self, item: str, cls: str) -> np.ndarray:
        """Demo on-hand for one Webb item at every branch (branch order = availability ids)."""
        return self._qty(item, cls, np.arange(len(self.av.ids)))

