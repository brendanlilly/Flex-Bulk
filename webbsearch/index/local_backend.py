"""Embedded serving backend: Tantivy (BM25, Rust) + HNSW (hnswlib).

hnswlib is optional (it has no prebuilt Windows wheels). Without it the dense
lane falls back to an exact dot product over data/models/product_vectors.npy:
same results, ~20-40 ms instead of ~1 ms at 750k x 128.

Mirrors the OpenSearch deployment 1:1 in shape:
  products      -> lexical (multi-field BM25, per-token dis_max) + dense (HNSW) lanes
  orders/invoices/lists/content -> lexical with document-level ACL tokens

ACL model (same as OpenSearch DLS / a terms filter on `visible_to`):
  every account-scoped document carries tokens such as `acct:200123`,
  `user:50044`, `public`; the entitlement layer computes the tokens a caller
  may see and the backend ANDs them into every query. Retrieval can therefore
  never return another account's order/invoice, whatever the query text or the
  LLM asks for.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import numpy as np
import pandas as pd
import tantivy
from tantivy import Occur, Query

try:
    import hnswlib
except ImportError:  # pragma: no cover - platform dependent
    hnswlib = None

from webbsearch.config import settings
from webbsearch.index.catalog import Catalog
from webbsearch.retrieval.analyzer import analyze, compact_id, expand_concepts, word_spans

FIELD_BOOSTS = {"title": 3.0, "brand": 2.0, "cat": 1.5, "attrs": 1.4, "syn": 0.9, "desc": 0.35, "ids": 8.0}
NO_BRAND = {f: b for f, b in FIELD_BOOSTS.items() if f != "brand"}


def field_boosts(tok: str, brand_tokens: set[str] | None, field_w: dict | None = None) -> dict[str, float]:
    """`field_w` (v2.44): the merchandising multipliers per field (Relevance by field), 1.0 = the built-in weight."""
    base = FIELD_BOOSTS if brand_tokens is None or tok in brand_tokens else NO_BRAND
    if not field_w:
        return base
    return {f: b * float(field_w.get(f, 1.0)) for f, b in base.items() if b * float(field_w.get(f, 1.0)) > 0}
DOC_FIELDS = {"title": 3.0, "body": 1.0, "ids": 6.0}


def _schema_products():
    sb = tantivy.SchemaBuilder()
    sb.add_integer_field("idx", stored=True, fast=True, indexed=True)
    for f in FIELD_BOOSTS:
        sb.add_text_field(f, stored=False, tokenizer_name="whitespace")
    for f in ("f_brand", "f_cat3", "f_family", "f_stock", "f_status"):
        sb.add_text_field(f, stored=False, tokenizer_name="raw")
    return sb.build()


def _schema_docs():
    sb = tantivy.SchemaBuilder()
    sb.add_text_field("doc_id", stored=True, tokenizer_name="raw")
    for f in DOC_FIELDS:
        sb.add_text_field(f, stored=False, tokenizer_name="whitespace")
    sb.add_text_field("visible_to", stored=False, tokenizer_name="raw")
    sb.add_text_field("kind", stored=False, tokenizer_name="raw")
    return sb.build()


KEEP_IN_INDEX_DIR = ("photos",)      # built separately (scripts/build_photo_index.py); not part of the search index


def clear_index_dir(index_dir: Path) -> None:
    """Empty the search index folder for a rebuild, keeping what other builds put there (the photo index)."""
    index_dir.mkdir(parents=True, exist_ok=True)
    for p in index_dir.iterdir():
        if p.name in KEEP_IN_INDEX_DIR:
            continue
        shutil.rmtree(p) if p.is_dir() else p.unlink()


class LocalBackend:
    VERTICALS = ("orders", "invoices", "lists", "content")

    def __init__(self, index_dir: Path):
        self.dir = index_dir
        self.schema_p = _schema_products()
        self.schema_d = _schema_docs()
        self.products = tantivy.Index(self.schema_p, path=str(index_dir / "products"))
        self.docs = {v: tantivy.Index(self.schema_d, path=str(index_dir / v)) for v in self.VERTICALS}
        self.hnsw, self.vecs = None, None
        if hnswlib is not None and (index_dir / "dense.hnsw").exists():
            self.hnsw = hnswlib.Index(space="ip", dim=json.loads((index_dir / "dense.json").read_text())["dim"])
            self.hnsw.load_index(str(index_dir / "dense.hnsw"))
            self.hnsw.set_ef(96)
        else:
            self.vecs = np.ascontiguousarray(np.load(settings.models_dir / "product_vectors.npy"),
                                             dtype=np.float32)
        self._searchers = {"products": self.products.searcher(),
                           **{v: ix.searcher() for v, ix in self.docs.items()}}
        self._vocab_cache: dict[str, bool] = {}

    # ------------------------------------------------------------------ build
    @staticmethod
    def build(index_dir: Path, catalog: Catalog, embeddings: np.ndarray, verticals: dict[str, pd.DataFrame],
              threads: int = 2, log=print):
        clear_index_dir(index_dir)
        t0 = time.time()
        # products -------------------------------------------------------
        (index_dir / "products").mkdir()
        ix = tantivy.Index(_schema_products(), path=str(index_dir / "products"))
        w = ix.writer(heap_size=1_000_000_000, num_threads=threads)
        df = catalog.df
        for i in range(catalog.n):
            title = catalog.title[i]
            attrs = catalog.attrs(i)
            attr_text = " ".join(str(v) for v in attrs.values())
            low = (title + " " + catalog.cat3[i]).lower()
            ids = {compact_id(catalog.mfr_part[i]).lower(), str(catalog.sku[i]).lower()}
            upc = df.at[i, "upc"]
            if upc:
                ids.add(compact_id(str(upc)).lower())
            w.add_document(tantivy.Document(
                idx=i, title=analyze(title), brand=analyze(catalog.brand[i]),
                cat=analyze(f"{catalog.cat1[i]} {catalog.cat2[i]} {catalog.cat3[i]}"),
                attrs=analyze(attr_text), syn=analyze(expand_concepts(low)),
                desc=analyze(df.at[i, "description"] or ""), ids=" ".join(ids),
                f_brand=catalog.brand[i], f_cat3=catalog.cat3[i], f_family=catalog.family[i],
                f_stock=catalog.stock_tier[i], f_status=catalog.status[i]))
            if i and i % 150_000 == 0:
                log(f"    products indexed {i:,} ({time.time() - t0:.0f}s)")
        w.commit()
        w.wait_merging_threads()
        log(f"  products lexical: {catalog.n:,} docs, {time.time() - t0:.0f}s")
        # dense ------------------------------------------------------------
        t1 = time.time()
        (index_dir / "dense.json").write_text(json.dumps({"dim": int(embeddings.shape[1])}))
        if hnswlib is not None:
            h = hnswlib.Index(space="ip", dim=embeddings.shape[1])
            h.init_index(max_elements=len(embeddings), ef_construction=120, M=16)
            h.set_num_threads(threads)
            h.add_items(embeddings, np.arange(len(embeddings)))
            h.save_index(str(index_dir / "dense.hnsw"))
            log(f"  products dense (HNSW M=16): {time.time() - t1:.0f}s")
        else:
            log("  products dense: hnswlib not installed, serving will use exact search over product_vectors.npy")
        # account-scoped + content verticals ------------------------------
        LocalBackend.build_verticals(index_dir, verticals, threads, log)

    @staticmethod
    def build_verticals(index_dir: Path, verticals: dict[str, pd.DataFrame], threads: int = 2, log=print) -> None:
        """(Re)build account and content vertical indexes; an existing one is replaced."""
        for v, vdf in verticals.items():
            t2 = time.time()
            if (index_dir / v).exists():
                shutil.rmtree(index_dir / v)
            (index_dir / v).mkdir()
            vix = tantivy.Index(_schema_docs(), path=str(index_dir / v))
            vw = vix.writer(heap_size=300_000_000, num_threads=threads)
            for r in vdf.itertuples(index=False):
                vw.add_document(tantivy.Document(
                    doc_id=str(r.doc_id), title=analyze(r.title), body=analyze(r.body),
                    ids=" ".join(compact_id(x).lower() for x in str(r.ids).split() if x),
                    visible_to=list(r.visible_to), kind=str(r.kind)))
            vw.commit()
            vw.wait_merging_threads()
            log(f"  {v}: {len(vdf):,} docs, {time.time() - t2:.0f}s")

    # ----------------------------------------------------------------- search
    def _token_query(self, schema, tok: str, boosts: dict[str, float], fuzzy: bool) -> Query:
        subs = [Query.boost_query(Query.term_query(schema, f, tok), b) for f, b in boosts.items()]
        if fuzzy:
            subs.append(Query.boost_query(
                Query.fuzzy_term_query(schema, "title", tok, distance=1, transposition_cost_one=True, prefix=False),
                1.2))
        return Query.disjunction_max_query(subs, tie_breaker=0.25)

    def lexical(self, tokens: list[str], k: int, filters: dict[str, list[str]] | None = None,
                fuzzy_tokens: set[str] | None = None, id_tokens: list[str] | None = None,
                brand_tokens: set[str] | None = None, field_w: dict | None = None):
        """`brand_tokens`: the words of brands the query names. Other words don't score the brand field, so a
        generic word inside a brand name ("valve" in Legend Valve, "pipe" in Charlotte Pipe) doesn't pull that
        brand's whole line to the top; titles carry the brand name anyway. None keeps every field."""
        if not tokens and not id_tokens:
            return np.zeros(0, np.int64), np.zeros(0, np.float32)
        fuzzy_tokens = fuzzy_tokens or set()

        def tq(t):
            return self._token_query(self.schema_p, t, field_boosts(t, brand_tokens, field_w), t in fuzzy_tokens)
        should = []
        seen: set = set()
        for g in word_spans(list(tokens)):
            if len(g) == 1:                      # a word
                if g[0] not in seen:
                    should.append((Occur.Should, tq(g[0])))
            else:                                # a hyphenated word: its parts or its joined form, not both
                parts = Query.boolean_query([(Occur.Should, tq(t)) for t in dict.fromkeys(g[:-1])])
                should.append((Occur.Should, Query.disjunction_max_query([parts, tq(g[-1])], tie_breaker=0.0)))
            seen.update(g)
        for t in id_tokens or []:
            should.append((Occur.Should, Query.boost_query(Query.term_query(self.schema_p, "ids", t),
                                                           20.0 * float((field_w or {}).get("ids", 1.0)) or 0.01)))
        q = Query.boolean_query(should)
        if filters:
            clauses = [(Occur.Must, q)]
            for f, vals in filters.items():
                if vals:
                    clauses.append((Occur.Must, Query.boolean_query(
                        [(Occur.Should, Query.term_query(self.schema_p, f"f_{f}", v)) for v in vals])))
            q = Query.boolean_query(clauses)
        se = self._searchers["products"]
        res = se.search(q, k)
        if not res.hits:
            return np.zeros(0, np.int64), np.zeros(0, np.float32)
        idx = np.fromiter((se.doc(a)["idx"][0] for _, a in res.hits), np.int64, len(res.hits))
        sc = np.fromiter((s for s, _ in res.hits), np.float32, len(res.hits))
        return idx, sc

    def dense(self, vec: np.ndarray, k: int):
        if self.hnsw is None:  # exact fallback
            sims = self.vecs @ np.asarray(vec, np.float32).ravel()
            k = min(k, len(sims))
            top = np.argpartition(-sims, k - 1)[:k]
            top = top[np.argsort(-sims[top])]
            return top.astype(np.int64), sims[top].astype(np.float32)
        labels, dist = self.hnsw.knn_query(vec.reshape(1, -1), k=k)
        return labels[0].astype(np.int64), (1.0 - dist[0]).astype(np.float32)

    def vectors(self, idx: list[int]) -> np.ndarray:
        if self.hnsw is None:
            return self.vecs[np.asarray(idx, np.int64)]
        return np.asarray(self.hnsw.get_items(idx), np.float32)

    def search_vertical(self, vertical: str, tokens: list[str], acl: list[str], k: int,
                        id_tokens: list[str] | None = None, kinds: list[str] | None = None):
        if not acl:
            return []
        sch = self.schema_d
        should = [(Occur.Should, self._token_query(sch, t, DOC_FIELDS, False)) for t in dict.fromkeys(tokens)]
        for t in id_tokens or []:
            should.append((Occur.Should, Query.boost_query(Query.term_query(sch, "ids", t), 25.0)))
        if not should:
            return []
        clauses = [(Occur.Must, Query.boolean_query(should)),
                   (Occur.Must, Query.boolean_query([(Occur.Should, Query.term_query(sch, "visible_to", a))
                                                     for a in acl]))]
        if kinds:
            clauses.append((Occur.Must, Query.boolean_query([(Occur.Should, Query.term_query(sch, "kind", x))
                                                             for x in kinds])))
        se = self._searchers[vertical]
        res = se.search(Query.boolean_query(clauses), k)
        return [(se.doc(a)["doc_id"][0], float(s)) for s, a in res.hits]

    def term_df(self, tok: str) -> int:
        """Products whose title, brand, category or specs contain this (normalized) word."""
        se = self._searchers["products"]
        return max(se.doc_freq(f, tok) for f in ("title", "brand", "cat", "attrs"))

    def term_exists(self, tok: str) -> bool:
        """Is this token in the product vocabulary? (drives fuzzy expansion)."""
        hit = self._vocab_cache.get(tok)
        if hit is None:
            se = self._searchers["products"]
            q = Query.boolean_query([(Occur.Should, Query.term_query(self.schema_p, f, tok))
                                     for f in ("title", "brand", "cat", "attrs", "syn")])
            hit = bool(se.search(q, 1).hits)
            if len(self._vocab_cache) < 500_000:
                self._vocab_cache[tok] = hit
        return hit
