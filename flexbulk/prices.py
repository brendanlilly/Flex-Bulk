"""Prices from the price file: the list price, and any price levels it has (Contractor, Net...) that staff choose per
order. A level with no price for an item falls back to the list price, and says so."""
from __future__ import annotations

import numpy as np
import pandas as pd

from webbsearch.config import settings


class Prices:
    def __init__(self, path=None):
        f = path or settings.canonical_dir / "prices.parquet"
        self.levels: list[str] = []
        self.table: dict[str, dict[str, float]] = {}
        self.list: dict[str, float] = {}
        try:
            df = pd.read_parquet(f)
        except (OSError, ValueError):
            return
        self.levels = [c for c in df.columns if c not in ("sku", "list_price")]
        self.list = {s: float(v) for s, v in zip(df["sku"], df["list_price"]) if v and v > 0}
        for lv in self.levels:
            col = df[lv].to_numpy(dtype=float)
            self.table[lv] = {s: float(v) for s, v in zip(df["sku"], col) if not np.isnan(v) and v > 0}

    def price(self, sku: str, level: str | None) -> tuple[float | None, str]:
        """(price, basis): the level's price, else the list price."""
        if level and level in self.table:
            v = self.table[level].get(sku)
            if v is not None:
                return round(v, 2), level
        v = self.list.get(sku)
        return (round(v, 2), "list") if v is not None else (None, "none")

    def apply(self, card: dict, level: str | None) -> None:
        """Price one match card for the order's level (cards of items not on the list are left alone)."""
        if card.get("kind") not in (None, "catalog"):
            return
        p, basis = self.price(card.get("sku", ""), level)
        lp = self.list.get(card.get("sku", ""))
        card["price"] = p
        card["price_basis"] = basis
        card["list_price"] = round(lp, 2) if lp else None
