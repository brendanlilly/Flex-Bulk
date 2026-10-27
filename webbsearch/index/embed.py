"""Dense embeddings for the semantic retrieval lane.

Local default: character n-gram TF-IDF -> truncated SVD (LSA), 128 dims, over
the trade-analyzed text. It runs anywhere with no model download, is robust to
typos, and learns co-occurrence ("combi" ~ "condensing boiler"), but it is not
a modern semantic encoder.

Production: swap in a hosted embedding model (Voyage `voyage-3.5-lite` or a
self-hosted bge/e5 model registered in OpenSearch ML Commons) behind the same
`encode()` interface; the OpenSearch backend then uses a neural ingest
pipeline and a `neural`/`knn` query. Nothing else changes.
"""
from __future__ import annotations

import pickle
from pathlib import Path

import numpy as np
from sklearn.decomposition import TruncatedSVD
from sklearn.feature_extraction.text import HashingVectorizer, TfidfTransformer
from sklearn.preprocessing import normalize as l2norm

from webbsearch.retrieval.analyzer import analyze


class LSAEmbedder:
    dim = 128

    def __init__(self, dim: int = 128, seed: int = 7):
        self.dim = dim
        self.hv = HashingVectorizer(analyzer="char_wb", ngram_range=(3, 5), n_features=2 ** 20,
                                    alternate_sign=False, norm=None)
        self.tfidf = TfidfTransformer(sublinear_tf=True)
        self.svd = TruncatedSVD(n_components=dim, random_state=seed, algorithm="randomized", n_iter=4)

    @staticmethod
    def prep(texts):
        return [analyze(t) for t in texts]

    def fit(self, texts: list[str], sample: int = 120_000, seed: int = 7) -> "LSAEmbedder":
        rng = np.random.default_rng(seed)
        if len(texts) > sample:
            texts = [texts[i] for i in rng.choice(len(texts), sample, replace=False)]
        X = self.hv.transform(self.prep(texts))
        X = self.tfidf.fit_transform(X)
        self.svd.fit(X)
        return self

    def encode(self, texts: list[str], batch: int = 50_000) -> np.ndarray:
        out = []
        for s in range(0, len(texts), batch):
            X = self.tfidf.transform(self.hv.transform(self.prep(texts[s:s + batch])))
            out.append(l2norm(self.svd.transform(X)).astype(np.float32))
        return np.vstack(out) if out else np.zeros((0, self.dim), np.float32)

    def encode_one(self, text: str) -> np.ndarray:
        """Online path: gather only the SVD rows for the query's non-zero n-grams
        (~50 of 1M), instead of a full sparse x dense product. ~0.2 ms."""
        if not hasattr(self, "_W"):
            self.compact()
        x = self.tfidf.transform(self.hv.transform(self.prep([text]))).tocsr()
        if x.nnz == 0:
            return np.zeros(self.dim, np.float32)
        v = x.data.astype(np.float32) @ self._W[x.indices].astype(np.float32)
        n = np.linalg.norm(v)
        return (v / n).astype(np.float32) if n else v.astype(np.float32)

    def compact(self):
        """Serving mode: keep a float16 row-major copy of the projection (268 MB)
        and drop the float64 SVD matrix (1 GB). Batch encode() is unavailable after."""
        self._W = np.array(self.svd.components_.T, dtype=np.float16, order="C")
        self.svd.components_ = None

    def save(self, path: Path):
        with open(path, "wb") as fh:
            pickle.dump({"tfidf": self.tfidf, "svd": self.svd, "dim": self.dim}, fh)

    @classmethod
    def load(cls, path: Path, serving: bool = False) -> "LSAEmbedder":
        with open(path, "rb") as fh:
            d = pickle.load(fh)
        e = cls(d["dim"])
        e.tfidf, e.svd = d["tfidf"], d["svd"]
        if serving:
            e.compact()
        return e
