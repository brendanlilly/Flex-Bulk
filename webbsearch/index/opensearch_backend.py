"""OpenSearch serving backend (production target).

Same interface as LocalBackend, so HybridRetriever, the ranker and the API are
unchanged when WEBB_BACKEND=opensearch. Text is pre-analyzed by the shared
Python analyzer and indexed into `whitespace`-analyzed fields, which keeps
query-time and index-time normalization identical across both backends and
the evaluation harness. (deploy/opensearch/products_template.json also defines
an OpenSearch-native trade analyzer for teams that prefer server-side analysis.)

Lanes:
  lexical  bool.should of per-token dis_max(term^boost over fields) + optional fuzzy
  dense    knn on `vec` (faiss HNSW, inner product); swap to `neural` with an ML Commons model
  prior    rank_feature on `pop` (log popularity) mirrors LocalBackend + PRIOR_WEIGHT

Security: account-scoped indexes carry `visible_to` keyword ACL tokens and every
query adds a terms filter from the Principal. Configure OpenSearch
document-level security on the same field for the service role as defense in
depth (see SPEC.md section 8).

Not exercised live in the prototype workspace (no cluster reachable); query
bodies are unit-tested in tests/test_opensearch_backend.py and the bulk loader
is `python -m webbsearch.index.opensearch_backend --load`.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from webbsearch.config import ROOT, settings
from webbsearch.index.local_backend import DOC_FIELDS, FIELD_BOOSTS
from webbsearch.retrieval.analyzer import analyze, compact_id, expand_concepts

TEMPLATES = ROOT / "deploy" / "opensearch"


def _token_clause(tok: str, boosts: dict[str, float], fuzzy: bool) -> dict:
    qs = [{"term": {f: {"value": tok, "boost": b}}} for f, b in boosts.items()]
    if fuzzy:
        qs.append({"fuzzy": {"title": {"value": tok, "fuzziness": 1, "transpositions": True, "boost": 1.2}}})
    return {"dis_max": {"queries": qs, "tie_breaker": 0.25}}


def lexical_body(tokens: list[str], k: int, filters: dict | None = None, fuzzy_tokens=(), id_tokens=(),
                 prior_weight: float = 0.0, brand_tokens: set | None = None, field_w: dict | None = None) -> dict:
    """brand_tokens: see LocalBackend.lexical (generic words don't score the brand field)."""
    from webbsearch.index.local_backend import field_boosts
    should = [_token_clause(t, field_boosts(t, brand_tokens, field_w), t in set(fuzzy_tokens)) for t in dict.fromkeys(tokens)]
    should += [{"term": {"ids": {"value": t, "boost": 20.0}}} for t in id_tokens]
    q: dict = {"bool": {"should": should, "minimum_should_match": 1}}
    if filters:
        q["bool"]["filter"] = [{"terms": {f"f_{f}": v}} for f, v in filters.items() if v]
    if prior_weight:
        q["bool"]["should"].append({"rank_feature": {"field": "pop", "log": {"scaling_factor": 1}, "boost": prior_weight}})
    return {"size": k, "_source": ["idx"], "track_total_hits": False, "query": q}


def dense_body(vec: np.ndarray, k: int) -> dict:
    return {"size": k, "_source": ["idx"], "query": {"knn": {"vec": {"vector": vec.tolist(), "k": k}}}}


def vertical_body(tokens: list[str], acl: list[str], k: int, id_tokens=(), kinds=None) -> dict:
    should = [_token_clause(t, DOC_FIELDS, False) for t in dict.fromkeys(tokens)]
    should += [{"term": {"ids": {"value": t, "boost": 25.0}}} for t in id_tokens]
    flt = [{"terms": {"visible_to": acl}}]
    if kinds:
        flt.append({"terms": {"kind": kinds}})
    return {"size": k, "_source": ["doc_id"],
            "query": {"bool": {"should": should, "minimum_should_match": 1, "filter": flt}}}


class OpenSearchBackend:
    VERTICALS = ("orders", "invoices", "lists", "content")

    def __init__(self, url: str | None = None, prefix: str | None = None, client=None):
        if client is None:
            from opensearchpy import OpenSearch
            client = OpenSearch(url or settings.opensearch_url, timeout=5)
        self.os = client
        self.p = prefix or settings.opensearch_prefix
        self._vocab: dict[str, bool] = {}

    def ix(self, name: str) -> str:
        return f"{self.p}-{name}"

    def lexical(self, tokens, k, filters=None, fuzzy_tokens=None, id_tokens=None, brand_tokens=None, field_w=None):
        if not tokens and not id_tokens:
            return np.zeros(0, np.int64), np.zeros(0, np.float32)
        r = self.os.search(index=self.ix("products"),
                           body=lexical_body(tokens, k, filters, fuzzy_tokens or (), id_tokens or (),
                                             brand_tokens=brand_tokens, field_w=field_w))
        hits = r["hits"]["hits"]
        return (np.array([h["_source"]["idx"] for h in hits], np.int64),
                np.array([h["_score"] for h in hits], np.float32))

    def dense(self, vec, k):
        r = self.os.search(index=self.ix("products"), body=dense_body(vec, k))
        hits = r["hits"]["hits"]
        return np.array([h["_source"]["idx"] for h in hits], np.int64), np.array([h["_score"] for h in hits], np.float32)

    def vectors(self, idx: list[int]) -> np.ndarray:
        r = self.os.mget(index=self.ix("products"), body={"ids": [str(i) for i in idx]}, _source=["vec"])
        return np.array([d["_source"]["vec"] for d in r["docs"]], np.float32)

    def search_vertical(self, vertical, tokens, acl, k, id_tokens=None, kinds=None):
        if not acl:
            return []
        body = vertical_body(tokens, acl, k, id_tokens or (), kinds)
        if not body["query"]["bool"]["should"]:
            return []
        r = self.os.search(index=self.ix(vertical), body=body)
        return [(h["_source"]["doc_id"], float(h["_score"])) for h in r["hits"]["hits"]]

    def term_df(self, tok: str):
        return None                                   # not needed on the cluster; the dictionary's lint skips the check

    def term_exists(self, tok: str) -> bool:
        if tok not in self._vocab:
            body = {"size": 0, "query": {"multi_match": {"query": tok, "fields": ["title", "brand", "cat", "attrs", "syn"],
                                                          "type": "best_fields", "analyzer": "whitespace"}},
                    "terminate_after": 1}
            self._vocab[tok] = self.os.search(index=self.ix("products"), body=body)["hits"]["total"]["value"] > 0
        return self._vocab[tok]

    # ------------------------------------------------------------------ load
    def create_indexes(self, dim: int):
        prod = json.loads((TEMPLATES / "products_template.json").read_text())
        prod["mappings"]["properties"]["vec"]["dimension"] = dim
        for name, body in [("products", prod)] + [(v, json.loads((TEMPLATES / "account_docs_template.json").read_text()))
                                                 for v in self.VERTICALS]:
            if self.os.indices.exists(index=self.ix(name)):
                self.os.indices.delete(index=self.ix(name))
            self.os.indices.create(index=self.ix(name), body=body)

    def bulk_products(self, catalog, vectors: np.ndarray, popularity: np.ndarray, descriptions, chunk: int = 2000):
        from opensearchpy.helpers import bulk

        def gen():
            for i in range(catalog.n):
                title = catalog.title[i]
                attrs = catalog.attrs(i)
                yield {"_index": self.ix("products"), "_id": str(i), "_source": {
                    "idx": i, "sku": str(catalog.sku[i]), "title": analyze(title), "brand": analyze(catalog.brand[i]),
                    "cat": analyze(f"{catalog.cat1[i]} {catalog.cat2[i]} {catalog.cat3[i]}"),
                    "attrs": analyze(" ".join(map(str, attrs.values()))),
                    "syn": analyze(expand_concepts((title + " " + catalog.cat3[i]).lower())),
                    "desc": analyze(descriptions[i] or ""),
                    "ids": " ".join({compact_id(catalog.mfr_part[i]).lower(), str(catalog.sku[i]).lower()}),
                    "f_brand": catalog.brand[i], "f_cat3": catalog.cat3[i], "f_family": catalog.family[i],
                    "f_stock": catalog.stock_tier[i], "f_status": catalog.status[i],
                    "pop": float(max(popularity[i], 1e-3)), "vec": vectors[i].tolist()}}
        return bulk(self.os, gen(), chunk_size=chunk, request_timeout=120)

    def bulk_vertical(self, name: str, df):
        from opensearchpy.helpers import bulk

        def gen():
            for r in df.itertuples(index=False):
                yield {"_index": self.ix(name), "_id": str(r.doc_id), "_source": {
                    "doc_id": str(r.doc_id), "title": analyze(r.title), "body": analyze(r.body),
                    "ids": " ".join(compact_id(x).lower() for x in str(r.ids).split() if x),
                    "visible_to": list(r.visible_to), "kind": str(r.kind)}}
        return bulk(self.os, gen(), chunk_size=5000, request_timeout=120)


def main():
    """Load canonical data + vectors into OpenSearch: python -m webbsearch.index.opensearch_backend --load"""
    import argparse
    from datetime import date

    import pandas as pd

    from webbsearch.index.build import vertical_docs
    from webbsearch.index.catalog import Catalog
    ap = argparse.ArgumentParser()
    ap.add_argument("--load", action="store_true")
    a = ap.parse_args()
    if not a.load:
        return
    c = {n: pd.read_parquet(settings.canonical_dir / f"{n}.parquet") for n in
         ["products", "orders", "order_lines", "invoices", "lists", "list_items", "content", "branches"]}
    cat = Catalog(c["products"], keep_text=True)
    vecs = np.load(settings.models_dir / "product_vectors.npy")
    pop = pd.read_parquet(settings.features_dir / str(date.today()) / "product_popularity.parquet") \
        if (settings.features_dir / str(date.today())).exists() else None
    popv = np.zeros(cat.n, np.float32)
    if pop is not None:
        m = pop["sku"].map(cat.sku_to_idx).dropna().astype(int)
        popv[m.to_numpy()] = pop.loc[m.index, "orders_365d"].to_numpy()
    b = OpenSearchBackend()
    b.create_indexes(vecs.shape[1])
    print(b.bulk_products(cat, vecs, np.log1p(popv), c["products"]["description"].tolist()))
    for name, df in vertical_docs(c, cat).items():
        print(name, b.bulk_vertical(name, df))


if __name__ == "__main__":
    main()
