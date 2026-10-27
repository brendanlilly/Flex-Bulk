"""Offline evaluation on held-out sessions (the last 30 days).

    python -m webbsearch.eval.run_eval [--limit 3000] [--claude 200]

Configurations (each adds one layer):
  bm25          lexical lane only, BM25 order
  lex+exact     + exact part-number lane (RRF)
  hybrid        + dense lane (RRF)                      <- candidate generator
  ltr           + LambdaMART, no account signals
  ltr+personal  + account history, brand loyalty, cohort priors
  +claude       + Claude rerank on hard queries (only with ANTHROPIC_API_KEY)

Metrics: NDCG@10 (gain 7 for the wanted SKU, 3 for spec-matching items),
MRR and Hit@1 / Hit@10 of the wanted SKU, candidate Recall@200, zero-result rate,
and server-side latency.
"""
from __future__ import annotations

import argparse
import json
import math
import time
from collections import defaultdict
from datetime import date

import numpy as np
import pandas as pd

from webbsearch.config import settings
from webbsearch.rank.train import load_judgments
from webbsearch.retrieval.service import Engine

CONFIGS = ["bm25", "lex+exact", "hybrid", "ltr", "ltr+personal"]


def ndcg10(ranked: list[str], rel: dict[str, int], n_grade2: int) -> float:
    gain = {3: 7.0, 2: 3.0}
    dcg = sum(gain.get(rel.get(s, 0), 0.0) / math.log2(r + 2) for r, s in enumerate(ranked[:10]))
    ideal = [7.0] + [3.0] * min(9, n_grade2)
    idcg = sum(g / math.log2(r + 2) for r, g in enumerate(ideal))
    return dcg / idcg if idcg else 0.0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=3000)
    ap.add_argument("--claude", type=int, default=0, help="evaluate Claude rerank on N hard queries")
    ap.add_argument("--out", default=str(settings.data_dir / "eval"))
    a = ap.parse_args(argv)
    meta = json.loads((settings.data_dir / "sim" / "meta.json").read_text())
    engine = Engine(asof=date.fromisoformat(meta["feature_asof"]))
    sess = pd.read_parquet(settings.data_dir / "sim" / "sessions.parquet")
    te = sess[sess["SPLIT"] == "test"].head(a.limit)
    judg = load_judgments()
    jr = pd.read_parquet(settings.data_dir / "sim" / "judgments.parquet")
    n2 = dict(zip(jr.loc[jr["ITEM_NO"] < 0, "SESSION_ID"], jr.loc[jr["ITEM_NO"] < 0, "GRADE"]))
    cat = engine.catalog
    rows = []
    lat = defaultdict(list)
    t_all = time.time()
    for n, s in enumerate(te.itertuples(), 1):
        p = engine.accounts.principal(str(s.CUST_NO), str(s.USER_ID))
        rel = judg.get(int(s.SESSION_ID), {})
        want = str(s.INTENT_SKU)
        res = {}
        # bm25 only
        t = time.perf_counter()
        pq = engine.parser.parse(s.QUERY); engine.service._flag_unknown_vocab(pq)
        fuzzy = {x for x in pq.tokens if len(x) >= 5 and x.isalpha() and not engine.backend.term_exists(x)}
        li, _ = engine.backend.lexical(pq.tokens, 200, fuzzy_tokens=fuzzy)
        res["bm25"] = [str(cat.sku[i]) for i in li]
        lat["bm25"].append((time.perf_counter() - t) * 1000)
        c2 = engine.retriever.retrieve(pq, lanes=("exact", "lexical"))
        res["lex+exact"] = [str(cat.sku[i]) for i in c2.idx]
        t = time.perf_counter()
        cands = engine.retriever.retrieve(pq, lanes=("exact", "lexical", "dense"))
        lat["hybrid"].append((time.perf_counter() - t) * 1000)
        res["hybrid"] = [str(cat.sku[i]) for i in cands.idx]
        X = engine.fb.build(pq, cands, None, personal=False)
        sc = engine.ranker.score(X, personal=False) + 100.0 * X[:, 6]
        res["ltr"] = [str(cat.sku[i]) for i in cands.idx[np.argsort(-sc, kind="stable")]]
        t = time.perf_counter()
        pc = engine.retriever.retrieve(pq, principal=p)       # + account-history lane
        Xp = engine.fb.build(pq, pc, p, personal=True)
        sc = engine.ranker.score(Xp, personal=True) + 100.0 * Xp[:, 6]
        res["ltr+personal"] = [str(cat.sku[i]) for i in pc.idx[np.argsort(-sc, kind="stable")]]
        lat["ltr+personal"].append((time.perf_counter() - t) * 1000)
        for cfg, ranked in res.items():
            r_want = ranked.index(want) + 1 if want in ranked else 0
            rows.append({"session": s.SESSION_ID, "config": cfg, "style": s.STYLE, "kind": s.INTENT_KIND,
                         "ndcg10": ndcg10(ranked, rel, int(n2.get(s.SESSION_ID, 0))),
                         "mrr": 1.0 / r_want if r_want else 0.0, "hit1": float(r_want == 1),
                         "hit10": float(0 < r_want <= 10), "recall200": float(r_want > 0),
                         "zero": float(len(ranked) == 0)})
        if n % 500 == 0:
            print(f"  evaluated {n:,}/{len(te):,} ({time.time() - t_all:.0f}s)", flush=True)
    df = pd.DataFrame(rows)
    summary = df.groupby("config")[["ndcg10", "mrr", "hit1", "hit10", "recall200", "zero"]].mean().loc[CONFIGS]
    by_style = df.pivot_table(index="style", columns="config", values="ndcg10", aggfunc="mean")[CONFIGS]
    by_kind = df.pivot_table(index="kind", columns="config", values="hit1", aggfunc="mean")[CONFIGS]
    style_n = df[df.config == "bm25"].groupby("style").size()
    latency = {k: {"p50": round(float(np.percentile(v, 50)), 1), "p95": round(float(np.percentile(v, 95)), 1)}
               for k, v in lat.items()}
    out = {"n_sessions": int(len(te)), "summary": summary.round(4).to_dict(orient="index"),
           "ndcg10_by_style": by_style.round(4).to_dict(orient="index"), "style_counts": style_n.to_dict(),
           "hit1_by_intent": by_kind.round(4).to_dict(orient="index"), "latency_ms": latency,
           "importance": engine.ranker.importance()}
    if a.claude and engine.claude.enabled:
        out["claude"] = eval_claude(engine, te, judg, n2, a.claude)
    from pathlib import Path
    Path(a.out).mkdir(parents=True, exist_ok=True)
    Path(a.out, "results.json").write_text(json.dumps(out, indent=2, default=str))
    df.to_parquet(Path(a.out, "per_query.parquet"), index=False)
    pd.set_option("display.width", 200)
    print("\n== Summary (held-out sessions: %d) ==" % len(te))
    print(summary.round(3).to_string())
    print("\n== NDCG@10 by query style ==")
    print(by_style.round(3).to_string())
    print("\n== Hit@1 by intent (rebuy vs new product) ==")
    print(by_kind.round(3).to_string())
    print("\n== Latency (ms) ==", json.dumps(latency))
    return out


def eval_claude(engine, te, judg, n2, n_max):
    """LTR+personal vs LTR+personal+Claude rerank on natural / uncertain queries."""
    svc = engine.service
    rows = []
    for s in te.itertuples():
        if len(rows) >= n_max:
            break
        pq = engine.parser.parse(s.QUERY)
        if pq.style != "natural" and s.STYLE not in ("typo", "nl"):
            continue
        p = engine.accounts.principal(str(s.CUST_NO), str(s.USER_ID))
        rel = judg.get(int(s.SESSION_ID), {})
        base = svc.search_products(engine.parser.parse(s.QUERY), p, llm_on=False, size=10)
        t = time.perf_counter()
        pq2 = engine.parser.parse(s.QUERY)
        out = engine.qu.interpret(s.QUERY)
        if out:
            engine.qu.apply(pq2, out)
        withc = svc.search_products(pq2, p, llm_on=True, llm_mode="force", size=10)
        ms = (time.perf_counter() - t) * 1000
        for name, r in (("ltr+personal", base), ("+claude", withc)):
            ranked = [c["sku"] for c in r["items"]]
            want = str(s.INTENT_SKU)
            rows.append({"config": name, "ndcg10": ndcg10(ranked, rel, int(n2.get(s.SESSION_ID, 0))),
                         "hit1": float(ranked[:1] == [want]), "ms": ms if name == "+claude" else 0})
    d = pd.DataFrame(rows)
    return {"n": int(len(d) // 2), "by_config": d.groupby("config").mean().round(4).to_dict(orient="index"),
            "llm_stats": engine.claude.stats()}


if __name__ == "__main__":
    main()
