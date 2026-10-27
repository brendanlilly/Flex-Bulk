"""LambdaMART ranker (LightGBM) over hybrid candidates.

Two models:
  personal   - all features; used for signed-in accounts
  anonymous  - personal and cohort features zeroed; used for logged-out traffic
               and as the "no personalization" baseline in evaluation

Labels in this prototype come from the simulator's ground truth (3 = the SKU
the buyer wanted, 2 = matches every spec they typed). In production they come
from logged outcomes: purchase=3, add-to-cart=2, click=1, with position-bias
correction (randomized top-k swaps or a click model) and weekly retraining;
see SPEC.md section 7.
"""
from __future__ import annotations

from pathlib import Path

import sklearn  # noqa: F401  Windows: loads the OpenMP and C++ runtimes lib_lightgbm.dll needs (no VC++ redistributable)
import lightgbm as lgb  # noqa: I001
import numpy as np

from webbsearch.rank.features import COHORT_IDX, FEATURES, PERSONAL_IDX


def mask_personal(X: np.ndarray) -> np.ndarray:
    X = X.copy()
    X[:, PERSONAL_IDX + COHORT_IDX] = 0.0
    return X


class Ranker:
    def __init__(self, models_dir: Path):
        self.models = {}
        for name in ("personal", "anonymous"):
            f = models_dir / f"ltr_{name}.txt"
            if f.exists():
                self.models[name] = lgb.Booster(model_file=str(f))

    @property
    def ready(self) -> bool:
        return "personal" in self.models

    def score(self, X: np.ndarray, personal: bool) -> np.ndarray:
        if len(X) == 0:
            return np.zeros(0)
        if personal and "personal" in self.models:
            return self.models["personal"].predict(X)
        if "anonymous" in self.models:
            return self.models["anonymous"].predict(mask_personal(X))
        return X[:, FEATURES.index("rrf")]  # before training: fused retrieval score

    def importance(self) -> dict:
        out = {}
        for n, m in self.models.items():
            g = m.feature_importance("gain")
            out[n] = sorted(zip(FEATURES, (g / max(g.sum(), 1e-9)).round(4).tolist()), key=lambda x: -x[1])
        return out


def train(X: np.ndarray, y: np.ndarray, groups: list[int], Xv=None, yv=None, gv=None, seed: int = 7) -> lgb.Booster:
    params = dict(objective="lambdarank", metric="ndcg", ndcg_eval_at=[10], learning_rate=0.05, num_leaves=31,
                  min_data_in_leaf=40, feature_fraction=0.9, bagging_fraction=0.8, bagging_freq=1,
                  lambdarank_truncation_level=20, label_gain=[0, 1, 3, 7], seed=seed, verbose=-1)
    dtr = lgb.Dataset(X, y, group=groups, feature_name=FEATURES)
    valid = [dtr]
    if Xv is not None:
        valid.append(lgb.Dataset(Xv, yv, group=gv, feature_name=FEATURES))
    cb = [lgb.early_stopping(40, verbose=False)] if Xv is not None else []
    return lgb.train(params, dtr, num_boost_round=600, valid_sets=valid, callbacks=cb)
