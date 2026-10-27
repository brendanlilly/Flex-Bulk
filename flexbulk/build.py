"""Load your files and build everything the matcher serves from.

    python -m flexbulk.build                 # load the files in data/incoming/, then index
    python -m flexbulk.build --skip-load     # index the tables already loaded

Steps:
  1. load        the item, stock and price files (flexbulk.loader, config/bulk_sources.yaml)
  2. features    product features; with no sales history, an item's popularity is read from its stock: how many
                 branches carry it and how many they hold (what a branch stocks is what sells there). A sales file
                 can replace this later without changing anything else.
  3. index       the keyword (Tantivy) and meaning (embedding) indexes over the item list
  4. ranker      FlexSearch's trained ranking model, as the starting model (data/models/)

Restart the app afterwards (it reads the new tables at start-up). About 2 minutes for 40,000 items; a 750,000-item
list takes 20-30 minutes, mostly step 3.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from webbsearch.config import settings

MODELS = Path(__file__).parent / "models"


def popularity_from_stock(asof: date, log=print) -> None:
    """product_popularity from branch stock: the retrieval prior and the ranker's popularity features."""
    cdir = settings.canonical_dir
    st = pd.read_parquet(cdir / "stock.parquet") if (cdir / "stock.parquet").exists() else None
    out = settings.features_dir / str(asof) / "product_popularity.parquet"
    if st is None or st.empty:
        log("popularity: no stock file, every item starts equal")
        return
    p = pd.read_parquet(cdir / "products.parquet", columns=["sku", "list_price"]).set_index("sku")
    g = st.groupby("sku").agg(branches=("branch_id", "nunique"), qty=("qty", "sum"))
    est = np.round(g["branches"] * 1.5 + np.log1p(g["qty"]) * 2).astype(int)
    df = pd.DataFrame({"sku": g.index, "orders_90d": (est // 4).to_numpy(), "orders_365d": est.to_numpy(),
                       "accounts_365d": g["branches"].to_numpy(), "qty_365d": g["qty"].to_numpy(dtype=float),
                       "revenue_365d": (g["qty"] * g.index.map(p["list_price"]).fillna(0)).to_numpy(dtype=float)})
    df.to_parquet(out, index=False)
    log(f"popularity: from stock at {st['branch_id'].nunique()} branches for {len(df):,} items")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Load the item, stock and price files and build the matcher's indexes")
    ap.add_argument("--skip-load", action="store_true", help="index the tables already loaded")
    a = ap.parse_args(argv)
    t0 = time.time()
    log = lambda m: print(f"[{time.time() - t0:5.0f}s] {m}", flush=True)
    if not a.skip_load:
        log("1/4 load")
        from flexbulk.loader import load
        load(log=log)
    import json
    meta = json.loads((settings.data_dir / "sim" / "meta.json").read_text())
    asof = date.fromisoformat(meta["today"])
    log(f"2/4 features as of {asof}")
    from webbsearch.personalize.features import build as build_features
    build_features(asof)
    popularity_from_stock(asof, log)
    log("3/4 index")
    from webbsearch.index import build as index_build
    argv0 = sys.argv
    sys.argv = [argv0[0], "--reembed"]
    try:
        index_build.main()
    finally:
        sys.argv = argv0
    log("4/4 ranker")
    settings.models_dir.mkdir(parents=True, exist_ok=True)
    for f in MODELS.glob("ltr_*.txt"):
        dst = settings.models_dir / f.name
        if not dst.exists():
            shutil.copyfile(f, dst)
            log(f"  starting ranker: {f.name}")
    log("done. Restart the app to serve the new list.")


if __name__ == "__main__":
    main()
