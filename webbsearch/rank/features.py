"""Feature assembly for learning-to-rank.

One row per (query, account, candidate product). Groups:
  retrieval  - per-lane scores/ranks from the hybrid retriever
  match      - token coverage, spec (digit) coverage, brand agreement, family prior
  product    - popularity (Snowflake-computed), stock tier, status, relative price
  personal   - this account's purchase/brand/family/view history      <- zeroed for the anonymous model
  cohort     - what the account's trade segment buys (cold-start prior)
"""
from __future__ import annotations

import math
from collections import Counter

import numpy as np

from webbsearch.retrieval.analyzer import normalize, parts_only, word_groups
from webbsearch.retrieval.entitlements import Principal
from webbsearch.retrieval.hybrid import Candidates
from webbsearch.retrieval.query_parser import ParsedQuery

FEATURES = [
    "bm25", "bm25_norm", "lex_rank_recip", "dense_sim", "dense_rank_recip", "rrf", "exact_id",
    "coverage", "title_coverage", "spec_coverage", "brand_match", "family_prior", "n_query_tokens", "is_part_query",
    "pop_365", "pop_90", "pop_accts", "stock_rank", "status_active", "rel_price",
    "acct_bought_n", "acct_bought_recency", "acct_family_share", "acct_brand_share", "acct_views",
    "seg_family_share", "seg_prod_accts",
]
PERSONAL = ["acct_bought_n", "acct_bought_recency", "acct_family_share", "acct_brand_share", "acct_views"]
COHORT = ["seg_family_share", "seg_prod_accts"]
PERSONAL_IDX = [FEATURES.index(f) for f in PERSONAL]
COHORT_IDX = [FEATURES.index(f) for f in COHORT]


class FeatureBuilder:
    def __init__(self, catalog, store):
        self.cat = catalog
        self.fs = store
        self._tok_cache: dict[int, tuple[frozenset, frozenset]] = {}

    def doc_tokens(self, i: int) -> tuple[frozenset, frozenset]:
        t = self._tok_cache.get(i)
        if t is None:
            title = frozenset(normalize(self.cat.title[i]))
            a = self.cat.attrs(i)
            allt = title | frozenset(normalize(" ".join(map(str, a.values())) + " " + self.cat.brand[i] + " "
                                               + self.cat.cat3[i]))
            t = (title, allt)
            if len(self._tok_cache) > 300_000:
                self._tok_cache.clear()
            self._tok_cache[i] = t
        return t

    def build(self, pq: ParsedQuery, cands: Candidates, principal: Principal | None,
              personal: bool = True) -> np.ndarray:
        n = len(cands)
        X = np.zeros((n, len(FEATURES)), np.float32)
        if n == 0:
            return X
        idx = cands.idx
        # a hyphenated word counts once, covered by its parts or its joined form; repeated words count once
        groups = list({frozenset(g): g for g in word_groups(pq.tokens)}.values())
        qt = [t for t in parts_only(pq.tokens)]
        qset = set(qt)
        qdig = {t for t in qset if any(c.isdigit() for c in t)}
        fams = self.cat.family[idx]
        brands = self.cat.brand[idx]
        # family prior from the raw lexical top list (pseudo-relevance query classification)
        top = cands.lex_top_idx
        fam_prior = Counter(self.cat.family[top].tolist()) if len(top) else Counter()
        tot = max(1, sum(fam_prior.values()))
        bmax = float(cands.bm25.max()) or 1.0
        X[:, 0] = cands.bm25
        X[:, 1] = cands.bm25 / bmax
        X[:, 2] = np.where(cands.lex_rank > 0, 1.0 / (1.0 + cands.lex_rank), 0.0)
        X[:, 3] = cands.dense_sim
        X[:, 4] = np.where(cands.dense_rank > 0, 1.0 / (1.0 + cands.dense_rank), 0.0)
        X[:, 5] = cands.rrf * 100
        X[:, 6] = cands.exact
        qbrands = set(pq.brands)
        for r, i in enumerate(idx.tolist()):
            title_t, all_t = self.doc_tokens(i)
            if groups:
                X[r, 7] = sum(1 for g in groups if g & all_t) / len(groups)
                X[r, 8] = sum(1 for g in groups if g & title_t) / len(groups)
            if qdig:
                X[r, 9] = len(qdig & all_t) / len(qdig)
            else:
                X[r, 9] = -1.0
            if qbrands:
                X[r, 10] = 1.0 if brands[r] in qbrands else -1.0
            X[r, 11] = fam_prior.get(fams[r], 0) / tot
        X[:, 12] = len(qt)
        X[:, 13] = 1.0 if pq.style == "part" else 0.0
        X[:, 14] = np.log1p(self.fs.pop_365[idx])
        X[:, 15] = np.log1p(self.fs.pop_90[idx])
        X[:, 16] = np.log1p(self.fs.pop_accts[idx])
        X[:, 17] = self.cat.stock_rank[idx]
        st = self.cat.status[idx]
        X[:, 18] = np.where(st == "A", 1.0, np.where(st == "S", 0.5, 0.0))
        X[:, 19] = np.log(np.maximum(self.cat.rel_price[idx], 1e-3))
        if principal is not None and principal.account_id:
            a = principal.account_id
            if personal:
                ap = self.fs.acct_prod.get(a, {})
                af = self.fs.acct_fam.get(a, {})
                av = self.fs.acct_views.get(a, {})
                for r, i in enumerate(idx.tolist()):
                    h = ap.get(i)
                    if h:
                        X[r, 20] = math.log1p(h[0])
                        X[r, 21] = math.exp(-h[2] / 120.0)
                    f = fams[r]
                    X[r, 22] = af.get(f, 0.0)
                    bb = self.fs.acct_brand.get((a, f))
                    if bb:
                        X[r, 23] = bb.get(brands[r], 0.0)
                    v = av.get(i)
                    if v:
                        X[r, 24] = math.log1p(v)
            seg = principal.segment
            if seg:
                sf = self.fs.seg_fam.get(seg, {})
                sp = self.fs.seg_prod.get(seg, {})
                for r, i in enumerate(idx.tolist()):
                    X[r, 25] = sf.get(fams[r], 0.0)
                    X[r, 26] = math.log1p(sp.get(i, 0.0))
        return X
