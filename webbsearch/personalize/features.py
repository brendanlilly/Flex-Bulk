"""Build and serve personalization features.

Build (offline, hourly in prod):
    python -m webbsearch.personalize.features --asof 2026-07-27

Serve: FeatureStore loads the feature tables into memory keyed by product
ordinal, so per-request feature assembly for ~200 candidates is a few dict
lookups and numpy gathers (<2 ms). In production the same tables are pushed
from Snowflake to a low-latency KV (Redis/DynamoDB) keyed by account_id; the
FeatureStore interface is the seam.
"""
from __future__ import annotations

import argparse
import re
import time
from collections import defaultdict
from datetime import date
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from webbsearch.config import ROOT, settings

SQL_FILE = ROOT / "sql" / "features.sql"
TABLES = ["products", "orders", "order_lines", "accounts", "branches", "events", "search_log"]


def split_sql(text: str) -> dict[str, str]:
    out, name, buf = {}, None, []
    for line in text.splitlines():
        m = re.match(r"--\s*name:\s*(\w+)", line)
        if m:
            if name:
                out[name] = "\n".join(buf).strip().rstrip(";")
            name, buf = m.group(1), []
        elif name:
            buf.append(line)
    if name:
        out[name] = "\n".join(buf).strip().rstrip(";")
    return out


def build(asof: date, canonical: Path | None = None, out: Path | None = None) -> Path:
    canonical = canonical or settings.canonical_dir
    out = (out or settings.features_dir) / str(asof)
    out.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    for t in TABLES:
        con.execute(f"CREATE VIEW {t} AS SELECT * FROM read_parquet('{canonical / (t + '.parquet')}')")
    stmts = split_sql(SQL_FILE.read_text())
    for name, sql in stmts.items():
        t0 = time.time()
        q = sql.replace("{{ASOF}}", f"DATE '{asof}'")
        con.execute(f"COPY ({q}) TO '{out / (name + '.parquet')}' (FORMAT PARQUET)")
        n = con.execute(f"SELECT COUNT(*) FROM read_parquet('{out / (name + '.parquet')}')").fetchone()[0]
        print(f"  {name:28s} {n:>10,} rows  {time.time() - t0:5.1f}s")
    return out


class FeatureStore:
    """In-memory feature lookups keyed by product ordinal."""

    def __init__(self, fdir: Path, sku_to_idx: dict[str, int], n_products: int, asof: date):
        self.asof = asof
        self.n = n_products
        rd = lambda n: pd.read_parquet(fdir / f"{n}.parquet")
        pp = rd("product_popularity")
        idx = pp["sku"].map(sku_to_idx)
        ok = idx.notna()
        ii = idx[ok].astype(int).to_numpy()
        self.pop_365 = np.zeros(n_products, np.float32)
        self.pop_90 = np.zeros(n_products, np.float32)
        self.pop_accts = np.zeros(n_products, np.float32)
        self.pop_365[ii] = pp.loc[ok, "orders_365d"].to_numpy()
        self.pop_90[ii] = pp.loc[ok, "orders_90d"].to_numpy()
        self.pop_accts[ii] = pp.loc[ok, "accounts_365d"].to_numpy()

        ap = rd("account_product")
        ap["idx"] = ap["sku"].map(sku_to_idx)
        ap = ap.dropna(subset=["idx"])
        ap["days"] = (pd.Timestamp(asof) - pd.to_datetime(ap["last_date"])).dt.days
        self.acct_prod: dict[str, dict[int, tuple[float, float, float]]] = defaultdict(dict)
        for a, i, n, q, d in zip(ap["account_id"], ap["idx"].astype(int), ap["n_orders"], ap["qty"], ap["days"]):
            self.acct_prod[a][i] = (float(n), float(q), float(d))

        af = rd("account_family")
        self.acct_fam: dict[str, dict[str, float]] = defaultdict(dict)
        for a, f, s in zip(af["account_id"], af["family"], af["share"]):
            self.acct_fam[a][f] = float(s)
        ab = rd("account_brand")
        self.acct_brand: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
        for a, f, b, s, nl in zip(ab["account_id"], ab["family"], ab["brand"], ab["share"], ab["n_lines"]):
            self.acct_brand[(a, f)][b] = float(s)
        # brand affinity across categories: each brand's share of all the account's lines
        tot = ab.groupby("account_id")["n_lines"].sum()
        self.acct_lines: dict[str, float] = {k: float(v) for k, v in tot.items()}
        self.acct_brand_all: dict[str, dict[str, float]] = defaultdict(dict)
        for (a, b), n in ab.groupby(["account_id", "brand"])["n_lines"].sum().items():
            if tot.get(a):
                self.acct_brand_all[a][b] = float(n) / float(tot[a])
        sf = rd("segment_family")
        self.seg_fam: dict[str, dict[str, float]] = defaultdict(dict)
        for s_, f, sh in zip(sf["segment"], sf["family"], sf["share"]):
            self.seg_fam[s_][f] = float(sh)
        sp = rd("segment_product")
        sp["idx"] = sp["sku"].map(sku_to_idx)
        sp = sp.dropna(subset=["idx"])
        self.seg_prod: dict[str, dict[int, float]] = defaultdict(dict)
        for s_, i, n in zip(sp["segment"], sp["idx"].astype(int), sp["n_accounts"]):
            self.seg_prod[s_][i] = float(n)
        av = rd("account_views")
        av["idx"] = av["sku"].map(sku_to_idx)
        av = av.dropna(subset=["idx"])
        self.acct_views: dict[str, dict[int, float]] = defaultdict(dict)
        for a, i, v in zip(av["account_id"], av["idx"].astype(int), av["views_90d"]):
            self.acct_views[a][i] = float(v)
        self.query_pop = rd("query_popularity")

    # convenience views for UI / explanations
    def top_families(self, account_id: str, k: int = 5) -> list[tuple[str, float]]:
        d = self.acct_fam.get(account_id, {})
        return sorted(d.items(), key=lambda x: -x[1])[:k]

    def preferred_brand(self, account_id: str, family: str) -> tuple[str, float] | None:
        d = self.acct_brand.get((account_id, family))
        if not d:
            return None
        b = max(d, key=d.get)
        return b, d[b]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", type=str, required=True)
    a = ap.parse_args(argv)
    build(date.fromisoformat(a.asof))


if __name__ == "__main__":
    main()
