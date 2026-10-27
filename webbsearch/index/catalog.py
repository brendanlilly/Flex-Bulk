"""Columnar in-memory view of the product catalog, addressed by ordinal.

The search engines return ordinals; everything that needs product fields for
ranking or display (family, brand, price, stock tier, attributes) gathers from
these arrays instead of hitting a document store per hit.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

from webbsearch.retrieval.analyzer import compact_id

STOCK_RANK = {"stocked": 3, "regional": 2, "dc": 1, "special": 0}


class Catalog:
    def __init__(self, products: pd.DataFrame, keep_text: bool = False):
        p = products.reset_index(drop=True)
        if not keep_text:  # descriptions are only needed at index-build time
            p = p.drop(columns=["description"], errors="ignore")
        self.df = p if keep_text else None   # arrays below are the serving representation
        self.n = len(p)
        hi = lambda c: np.array(p[c].tolist(), dtype=object)
        # low-cardinality columns share one str object per distinct value
        lo = lambda c: np.asarray(pd.Categorical(p[c]), dtype=object)
        self.sku = hi("sku")
        self.title = hi("title")
        self.mfr_part = hi("mfr_part")
        self._attrs_raw = hi("attrs")
        self.brand = lo("brand")
        self.family = lo("family")
        self.cat1 = lo("cat1")
        self.cat2 = lo("cat2")
        self.cat3 = lo("cat3")
        self.status = lo("status")
        self.uom = lo("uom")
        self.stock_tier = lo("stock_tier")
        self.list_price = p["list_price"].to_numpy(np.float32)
        self.stock_rank = p["stock_tier"].map(STOCK_RANK).fillna(0).to_numpy(np.int8)
        self.sku_to_idx = {s: i for i, s in enumerate(self.sku)}
        # family-level price medians -> relative price feature
        med = p.groupby("family")["list_price"].median()
        self.rel_price = (p["list_price"] / p["family"].map(med)).fillna(1.0).to_numpy(np.float32)
        # exact-id lane: compact key -> ordinal (int), collisions spill to a small overflow map.
        # (A dict of lists for 2.25M keys costs ~5x the memory.)
        self._id1: dict[str, int] = {}
        self._idn: dict[str, list[int]] = {}
        for i, (mp, up, sk) in enumerate(zip(self.mfr_part, p["upc"].fillna(""), self.sku)):
            for key in {compact_id(mp), compact_id(str(up)), compact_id(str(sk))}:
                if len(key) < 4:
                    continue
                j = self._id1.get(key)
                if j is None and key not in self._idn:
                    self._id1[key] = i
                elif j is not None:
                    self._idn[key] = [j, i]
                    del self._id1[key]
                else:
                    self._idn[key].append(i)
        self._sorted_ids = np.array(sorted(k for k in self.mfr_compact_keys()))

    def mfr_compact_keys(self):
        return {compact_id(m) for m in self.mfr_part if len(compact_id(m)) >= 5}

    def id_lookup(self, key: str) -> list[int]:
        j = self._id1.get(key)
        if j is not None:
            return [j]
        return self._idn.get(key, [])

    def attrs(self, i: int) -> dict:
        return _parse(self._attrs_raw[i])

    def id_prefix(self, prefix: str, limit: int = 20) -> list[int]:
        """Partial part-number lookup (>=5 chars): 'LF25AU' -> LF25AUB..."""
        if len(prefix) < 5:
            return []
        lo = np.searchsorted(self._sorted_ids, prefix, "left")
        hi = np.searchsorted(self._sorted_ids, prefix + "￿", "left")
        out = []
        for k in self._sorted_ids[lo:min(hi, lo + limit)]:
            out.extend(self.id_lookup(str(k)))
        return out[:limit]

    def card(self, i: int) -> dict:
        return {"sku": self.sku[i], "title": self.title[i], "brand": self.brand[i], "mfr_part": self.mfr_part[i],
                "category": [self.cat1[i], self.cat2[i], self.cat3[i]], "family": self.family[i],
                "list_price": float(self.list_price[i]), "uom": self.uom[i], "stock_tier": self.stock_tier[i],
                "status": self.status[i], "attrs": self.attrs(i)}

    @classmethod
    def load(cls, canonical_dir: Path) -> "Catalog":
        import pyarrow.parquet as pq
        cols = ["sku", "brand", "mfr_part", "upc", "title", "cat1", "cat2", "cat3", "family", "attrs",
                "list_price", "uom", "stock_tier", "status"]
        t = pq.read_table(canonical_dir / "products.parquet", columns=cols)
        df = t.to_pandas()
        del t
        c = cls(df)
        del df
        import gc; gc.collect()
        try:
            import pyarrow as pa
            pa.default_memory_pool().release_unused()
        except Exception:
            pass
        return c


@lru_cache(maxsize=50_000)
def _parse(s: str) -> dict:
    try:
        return json.loads(s) if s else {}
    except Exception:
        return {}
