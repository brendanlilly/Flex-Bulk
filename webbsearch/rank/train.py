"""Train the LambdaMART rankers from labelled search sessions.

    python -m webbsearch.rank.train

Features are computed with the feature store as-of FEATURE_ASOF (before every
labelled session) so the model never sees the future.
"""
from __future__ import annotations

import json
import time
from datetime import date

import numpy as np
import pandas as pd

from webbsearch.config import settings
from webbsearch.rank.features import FEATURES
from webbsearch.rank.ltr import mask_personal, train
from webbsearch.retrieval.service import Engine


def load_judgments() -> dict[int, dict[str, int]]:
    j = pd.read_parquet(settings.data_dir / "sim" / "judgments.parquet")
    j = j[j["ITEM_NO"] >= 0]
    out: dict[int, dict[str, int]] = {}
    for s, i, g in zip(j["SESSION_ID"], j["ITEM_NO"].astype(str), j["GRADE"]):
        out.setdefault(int(s), {})[i] = int(g)
    return out


def session_candidates(engine: Engine, row, lanes=("exact", "lexical", "dense", "history")):
    p = engine.accounts.principal(str(row.CUST_NO), str(row.USER_ID))
    pq = engine.parser.parse(row.QUERY)
    engine.service._flag_unknown_vocab(pq)
    cands = engine.retriever.retrieve(pq, lanes=lanes, principal=p)
    return p, pq, cands


def build_matrix(engine: Engine, sessions: pd.DataFrame, judg: dict, log_every=1000, personal: bool = True):
    """personal=True: candidates include the account-history lane (signed-in traffic).
    personal=False: no history lane (anonymous traffic / no-personalization baseline)."""
    Xs, ys, groups, meta = [], [], [], []
    t0 = time.time()
    lanes = ("exact", "lexical", "dense", "history") if personal else ("exact", "lexical", "dense")
    for n, row in enumerate(sessions.itertuples(), 1):
        p, pq, cands = session_candidates(engine, row, lanes)
        if len(cands) == 0:
            continue
        X = engine.fb.build(pq, cands, p, personal=True)
        rel = judg.get(int(row.SESSION_ID), {})
        y = np.array([rel.get(str(engine.catalog.sku[i]), 0) for i in cands.idx], np.int32)
        Xs.append(X); ys.append(y); groups.append(len(y)); meta.append(int(row.SESSION_ID))
        if n % log_every == 0:
            print(f"  features {n:,}/{len(sessions):,} sessions ({time.time() - t0:.0f}s)", flush=True)
    return np.vstack(Xs), np.concatenate(ys), groups, meta


def main():
    meta = json.loads((settings.data_dir / "sim" / "meta.json").read_text())
    asof = date.fromisoformat(meta["feature_asof"])
    engine = Engine(asof=asof)
    sessions = pd.read_parquet(settings.data_dir / "sim" / "sessions.parquet")
    tr = sessions[sessions["SPLIT"] == "train"].sample(frac=1.0, random_state=7)
    judg = load_judgments()
    t = time.time()
    models = {}
    for name, personal in (("personal", True), ("anonymous", False)):
        X, y, g, sids = build_matrix(engine, tr, judg, personal=personal)
        if not personal:
            X = mask_personal(X)
        keep, off = [], 0
        for gi in g:
            keep.append(y[off:off + gi].max() > 0); off += gi
        print(f"[{name}] groups: {len(g):,} ({sum(keep):,} with a relevant candidate), rows {len(y):,}")
        cut = int(len(g) * 0.85)
        bounds = np.cumsum([0] + g)

        def sub(a, b):
            idx = [k for k in range(a, b) if keep[k]]
            rows = np.concatenate([np.arange(bounds[k], bounds[k + 1]) for k in idx])
            return X[rows], y[rows], [g[k] for k in idx]

        Xt, yt, gt = sub(0, cut)
        Xv, yv, gv = sub(cut, len(g))
        m = train(Xt, yt, gt, Xv, yv, gv)
        m.save_model(str(settings.models_dir / f"ltr_{name}.txt"))
        models[name] = m
        print(f"[{name}] best iteration {m.best_iteration}, valid ndcg@10 {m.best_score['valid_1']['ndcg@10']:.4f}")
        del X, y
    mp = models["personal"]
    print(f"trained in {time.time() - t:.0f}s")
    gain = mp.feature_importance("gain")
    for f, v in sorted(zip(FEATURES, gain / gain.sum()), key=lambda x: -x[1])[:12]:
        print(f"  {f:22s} {v:.3f}")


if __name__ == "__main__":
    main()
