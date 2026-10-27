"""Candidate generation: exact-ID, lexical (BM25) and dense lanes fused with RRF.

Reciprocal-rank fusion is used for *candidate generation* only; the final
order comes from the learning-to-rank model (webbsearch/rank), which sees the
per-lane scores as features alongside behavior and personalization signals.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from webbsearch.config import settings
from webbsearch.retrieval.analyzer import compact_id, normalize
from webbsearch.retrieval.query_parser import ParsedQuery

GATHER_FIELDS = ("syn", "desc", "ids")
LANE_WEIGHTS = {"exact": 4.0, "partial": 2.0, "lexical": 1.0, "dense": 0.4, "history": 1.0}
DENSE_FLOOR = 0.6     # cosine; dense-only candidates below this are dropped
LEX_POOL = 1000        # BM25 hits fetched before the popularity prior picks the lexical lane
PRIOR_WEIGHT = 0.12    # static-rank prior (log popularity), like a rank_feature boost in OpenSearch


@dataclass
class Candidates:
    idx: np.ndarray            # product ordinals
    rrf: np.ndarray
    bm25: np.ndarray           # 0 if not retrieved lexically
    lex_rank: np.ndarray       # 0 if absent, else 1-based
    dense_sim: np.ndarray      # cosine for every candidate
    dense_rank: np.ndarray
    exact: np.ndarray          # 1 if exact id match
    lex_top_idx: np.ndarray    # raw lexical top list (for family prior)

    def __len__(self):
        return len(self.idx)

    def take(self, order: np.ndarray) -> "Candidates":
        return Candidates(self.idx[order], self.rrf[order], self.bm25[order], self.lex_rank[order],
                          self.dense_sim[order], self.dense_rank[order], self.exact[order], self.lex_top_idx)


class HybridRetriever:
    def __init__(self, backend, catalog, embedder, prior: np.ndarray | None = None, token_fn=None,
                 history_fn=None):
        self.b = backend
        self.cat = catalog
        self.emb = embedder
        self.prior = prior            # [n_products] in 0..1
        self.token_fn = token_fn      # idx -> (title_tokens, all_tokens)
        self.history_fn = history_fn  # principal -> {idx: (n_orders, qty, days)}
        from webbsearch.retrieval.idmatch import IdMatch
        self.idmatch = IdMatch(catalog) if catalog is not None else None   # v2.43: near and partial part numbers

    def history_lane(self, pq: ParsedQuery, principal, k: int = 60) -> list[int]:
        """Personalized retrieval: products this account already buys that match the query.
        Broad queries ("30 gal water heater") match hundreds of SKUs; the one this
        contractor always buys must reach the ranker even if BM25 ties bury it."""
        if principal is None or not getattr(principal, "account_id", None) or not self.history_fn \
                or not self.token_fn or pq.style == "part":
            return []
        hist = self.history_fn(principal) or {}
        q = set(pq.tokens)
        if not q:
            return []
        qd = {t for t in q if any(c.isdigit() for c in t)}
        scored = []
        for i, (n, _, days) in hist.items():
            _, toks = self.token_fn(i)
            cov = len(q & toks) / len(q)
            if cov >= 0.6 and qd <= toks:
                scored.append((cov, n, i))
        scored.sort(reverse=True)
        return [i for _, _, i in scored[:k]]

    def exact_ids(self, pq: ParsedQuery) -> list[int]:
        """Products whose part number, item number or UPC is exactly a typed one. A part number that only starts
        or contains what was typed is a partial match (partial_ids), never exact: "VI36142-07L" is not
        VI36142-07LF (v2.43)."""
        # a part number typed in pieces ("WI80385 LF", "VI 36142 07 LF"): the longest run of 2-4 pieces that is a part
        # number wins over a piece that happens to be another one ("GE35510 LF" is GE35510LF, not GE35510) (v2.43)
        ch = (pq.raw or "").split()
        for n in (4, 3, 2):
            joined: list[int] = []
            for i in range(len(ch) - n + 1):
                k = compact_id("".join(ch[i:i + n]))
                if len(k) >= 6 and any(c_.isdigit() for c_ in k) and any(c_.isdigit() for c_ in ch[i]):
                    joined.extend(self.cat.id_lookup(k))
            if joined:
                return list(dict.fromkeys(joined))
        out: list[int] = []
        for c in pq.id_chunks:
            out.extend(self.cat.id_lookup(c))
        return list(dict.fromkeys(out))

    def partial_ids(self, pq: ParsedQuery) -> list[int]:
        """Part numbers that start with or contain a typed one (5+ characters), when nothing matches it exactly."""
        if not pq.id_chunks or any(self.cat.id_lookup(c) for c in pq.id_chunks):
            return []
        out: list[int] = []
        for c in pq.id_chunks:
            if len(c) >= 5 and not c.isdigit():
                out.extend(self.cat.id_prefix(c, 10))
        if self.idmatch is not None:
            for c in pq.id_chunks:
                out.extend(self.idmatch.contains(c, 30)[0])
        return list(dict.fromkeys(out))[:40]

    def retrieve(self, pq: ParsedQuery, filters: dict | None = None, k_lex: int | None = None,
                 k_dense: int | None = None, lanes=("exact", "lexical", "dense", "history"),
                 principal=None, limit: int | None = None, history_k: int = 60,
                 field_w: dict | None = None) -> Candidates:
        limit = limit or settings.candidates_rerank
        k_lex = k_lex or max(settings.candidates_lexical, limit)
        k_dense = k_dense or settings.candidates_dense
        tokens = pq.tokens
        if pq.rewritten:  # Claude rewrite: union of original and rewritten tokens
            tokens = list(dict.fromkeys(list(tokens) + list(normalize(pq.rewritten))))
        ranked: dict[str, list[int]] = {}
        bm25: dict[int, float] = {}
        lex_rank: dict[int, int] = {}
        exact = self.exact_ids(pq) if "exact" in lanes else []
        if exact:
            ranked["exact"] = exact
        partial = self.partial_ids(pq) if "exact" in lanes and not exact else []
        if partial:
            ranked["partial"] = partial
        lex_idx = np.zeros(0, np.int64)
        if "lexical" in lanes:
            fuzzy = {t for t in tokens if len(t) >= 5 and t.isalpha() and not self.b.term_exists(t)}
            id_toks = [c.lower() for c in pq.id_chunks]
            pool = LEX_POOL if self.prior is not None else k_lex
            brand_toks = {t for br in pq.brands for t in normalize(br)}
            # v2.44 field weights: title, brand, category and specs act on the final order (fieldrel.py), where each
            # move is explained; here they only drop a field set to 0. Synonyms, description and part numbers are
            # weighted here, in what's gathered and the text score the ranker reads.
            fw = {f: (float(v) if f in GATHER_FIELDS or float(v) == 0 else 1.0) for f, v in (field_w or {}).items()}
            kw = {"field_w": fw} if any(v != 1.0 for v in fw.values()) else {}
            lex_idx, lex_sc = self.b.lexical(tokens, pool, filters=filters, fuzzy_tokens=fuzzy, id_tokens=id_toks,
                                             brand_tokens=brand_toks, **kw)
            for r, (i, s) in enumerate(zip(lex_idx.tolist(), lex_sc.tolist()), 1):
                bm25[i] = s
                lex_rank[i] = r
            if self.prior is not None and len(lex_idx):
                adj = lex_sc / max(float(lex_sc.max()), 1e-6) + PRIOR_WEIGHT * self.prior[lex_idx]
                o = np.argsort(-adj, kind="stable")[:k_lex]
                lex_idx = lex_idx[o]
            ranked["lexical"] = lex_idx.tolist()
        qvec = None
        dense_rank: dict[int, int] = {}
        if "dense" in lanes and (pq.tokens or pq.rewritten):
            qtext = pq.rewritten or pq.raw
            qvec = self.emb.encode_one(qtext)
            d_idx, _ = self.b.dense(qvec, k_dense)
            d_list = d_idx.tolist()
            if filters:
                d_list = [i for i in d_list if self._passes(i, filters)]
            ranked["dense"] = d_list
            for r, i in enumerate(d_list, 1):
                dense_rank[i] = r
        if "history" in lanes and filters is None:
            hl = self.history_lane(pq, principal, k=history_k)
            if hl:
                ranked["history"] = hl
        # RRF
        fused: dict[int, float] = {}
        k = settings.rrf_k
        for lane, lst in ranked.items():
            w = LANE_WEIGHTS[lane]
            for r, i in enumerate(lst, 1):
                fused[i] = fused.get(i, 0.0) + w / (k + r)
        if not fused:
            z = np.zeros(0)
            return Candidates(z.astype(np.int64), z, z, z, z, z, z, lex_idx)
        items = sorted(fused.items(), key=lambda x: -x[1])[:limit]
        idx = np.array([i for i, _ in items], np.int64)
        rrf = np.array([s for _, s in items], np.float32)
        if qvec is not None:
            vecs = self.b.vectors(idx.tolist())
            dsim = vecs @ qvec
        else:
            dsim = np.zeros(len(idx), np.float32)
        ex = set(exact)
        hist_set = set(ranked.get("history", []))
        # Evidence floor: a candidate found only by the dense lane must be semantically close.
        part_set = set(partial)
        floor_ok = np.array([(i in bm25) or (i in ex) or (i in hist_set) or (i in part_set) or (d >= DENSE_FLOOR)
                             for i, d in zip(idx.tolist(), dsim.tolist())], bool)
        idx, rrf, dsim = idx[floor_ok], rrf[floor_ok], dsim[floor_ok]
        return Candidates(
            idx=idx, rrf=rrf,
            bm25=np.array([bm25.get(i, 0.0) for i in idx.tolist()], np.float32),
            lex_rank=np.array([lex_rank.get(i, 0) for i in idx.tolist()], np.float32),
            dense_sim=dsim.astype(np.float32),
            dense_rank=np.array([dense_rank.get(i, 0) for i in idx.tolist()], np.float32),
            exact=np.array([1.0 if i in ex else 0.0 for i in idx.tolist()], np.float32),
            lex_top_idx=lex_idx[:50],
        )

    def _passes(self, i: int, filters: dict) -> bool:
        m = {"brand": self.cat.brand, "cat3": self.cat.cat3, "family": self.cat.family,
             "stock": self.cat.stock_tier, "status": self.cat.status}
        for f, vals in filters.items():
            if vals and m[f][i] not in vals:
                return False
        return True
