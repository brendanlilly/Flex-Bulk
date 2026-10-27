"""Offline Claude jobs (Message Batches API: ~50% of on-demand price, 24h SLA).

    python -m webbsearch.llm.enrich catalog   --limit 2000 [--dry-run]   # attributes + synonyms per SKU
    python -m webbsearch.llm.enrich vocab     [--dry-run]                # trade vocabulary per category
    python -m webbsearch.llm.enrich rewrites  --limit 5000 [--dry-run]   # query-log tail -> QU cache
    python -m webbsearch.llm.enrich collect   --batch-id msgbatch_...    # fetch results -> parquet

Outputs land in data/enrichment/ (Snowflake: SEARCH.PRODUCT_ENRICHMENT /
SEARCH.QUERY_REWRITES) with review_status='pending'. Nothing reaches the index
until a product manager approves it (sampled review queue), because a
confidently wrong attribute is worse for search than a missing one.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import pandas as pd

from webbsearch.config import settings
from webbsearch.llm.client import PRICES
from webbsearch.llm.query_understanding import SCHEMA as QU_SCHEMA
from webbsearch.retrieval.analyzer import analyze

OUT = settings.data_dir / "enrichment"

CATALOG_SYSTEM = (
    "You normalize product records for F.W. Webb, a plumbing/HVAC/industrial PVF distributor. From the title and "
    "description, extract attributes that buyers search by, and list the trade terms, abbreviations and common "
    "misspellings contractors use for this exact item. Only extract what the text states; use null when absent. "
    "Sizes use nominal inch notation (1/2, 3/4, 1-1/4). Never invent part numbers or ratings."
)
CATALOG_SCHEMA = {
    "type": "object",
    "properties": {
        "normalized_title": {"type": "string"},
        "attributes": {"type": "object", "properties": {
            "nominal_size": {"type": ["string", "null"]}, "connection": {"type": ["string", "null"]},
            "material": {"type": ["string", "null"]}, "pressure_rating": {"type": ["string", "null"]},
            "fuel": {"type": ["string", "null"]}, "capacity": {"type": ["string", "null"]},
            "voltage": {"type": ["string", "null"]}, "lead_free": {"type": ["boolean", "null"]},
            "product_type": {"type": ["string", "null"]}}},
        "search_terms": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
        "confidence": {"type": "number"}},
    "required": ["normalized_title", "attributes", "search_terms", "confidence"],
}
VOCAB_SYSTEM = (
    "You are building the search vocabulary for a plumbing/HVAC/industrial distributor. For the product category "
    "given, list how trade contractors actually type queries for it: abbreviations, slang, brand-as-generic names, "
    "and common misspellings, each mapped to the canonical term. Be specific to the category."
)
VOCAB_SCHEMA = {"type": "object", "properties": {"mappings": {"type": "array", "items": {
    "type": "object", "properties": {"user_term": {"type": "string"}, "canonical": {"type": "string"}},
    "required": ["user_term", "canonical"]}, "maxItems": 40}}, "required": ["mappings"]}


def _req(custom_id: str, system: str, user: str, tool: str, schema: dict, model: str, max_tokens: int) -> dict:
    return {"custom_id": custom_id, "params": {
        "model": model, "max_tokens": max_tokens,
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": user}],
        "tools": [{"name": tool, "description": f"Return {tool}.", "input_schema": schema}],
        "tool_choice": {"type": "tool", "name": tool}}}


def estimate(reqs: list[dict], out_tokens_each: int) -> dict:
    model = reqs[0]["params"]["model"] if reqs else settings.model_fast
    pi, po = PRICES.get(model, (3.0, 15.0))
    sys_toks = len(reqs[0]["params"]["system"][0]["text"]) / 3.6 if reqs else 0
    user_toks = sum(len(r["params"]["messages"][0]["content"]) for r in reqs) / 3.6
    tool_toks = 350  # schema + tool framing per request
    # Prompt caching only applies above a model-specific minimum prefix length
    # (4,096 tokens for Haiku 4.5), so assume the prefix is billed in full.
    min_cache = 4096 if "haiku" in model else 1024
    prefix = sys_toks + tool_toks
    per_req_prefix = prefix * 0.1 if prefix >= min_cache else prefix
    inp = user_toks + prefix + per_req_prefix * max(len(reqs) - 1, 0)
    out = out_tokens_each * len(reqs)
    cost = (inp * pi + out * po) / 1e6 * 0.5  # batch discount
    return {"requests": len(reqs), "model": model, "est_input_tokens": int(inp), "est_output_tokens": int(out),
            "est_cost_usd_batch": round(cost, 2)}


def build_catalog(limit: int | None, only_missing: bool) -> list[dict]:
    p = pd.read_parquet(settings.canonical_dir / "products.parquet", columns=["sku", "title", "description", "attrs"])
    if only_missing:
        p = p[p["attrs"].isin(["{}", "", None])]
    if limit:
        p = p.sample(min(limit, len(p)), random_state=7)
    return [_req(f"sku-{r.sku}", CATALOG_SYSTEM, f"Title: {r.title}\nDescription: {r.description}",
                 "product_record", CATALOG_SCHEMA, settings.model_fast, 350) for r in p.itertuples()]


def build_vocab() -> list[dict]:
    p = pd.read_parquet(settings.canonical_dir / "products.parquet", columns=["cat1", "cat2", "cat3", "title"])
    reqs, idmap = [], {}
    for n, ((c1, c2, c3), g) in enumerate(p.groupby(["cat1", "cat2", "cat3"])):
        ex = "\n".join(g["title"].sample(min(15, len(g)), random_state=7))
        idmap[f"cat-{n}"] = c3   # custom_id must match ^[a-zA-Z0-9_-]{1,64}$
        reqs.append(_req(f"cat-{n}", VOCAB_SYSTEM, f"Category: {c1} > {c2} > {c3}\nExample items:\n{ex}",
                         "vocabulary", VOCAB_SCHEMA, settings.model_smart, 1200))
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "vocab_ids.json").write_text(json.dumps(idmap))
    return reqs


def build_rewrites(limit: int) -> list[dict]:
    """Query-log tail: rare and zero-result-prone queries. In production, select by
    zero results / low CTR / reformulation rate from SEARCH.SEARCH_LOG."""
    from webbsearch.llm.query_understanding import build_system_prompt
    from webbsearch.index.catalog import Catalog
    log = pd.read_parquet(settings.canonical_dir / "search_log.parquet")
    q = log["query"].str.lower().str.strip().value_counts()
    tail = q[q <= 2].index.tolist()[:limit]
    system = build_system_prompt(Catalog.load(settings.canonical_dir))
    today = pd.Timestamp.today().date()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "rewrites_queries.json").write_text(json.dumps({f"q-{i}": x for i, x in enumerate(tail)}))
    return [_req(f"q-{i}", system, f"Today is {today}. Query: {x}", "interpretation", QU_SCHEMA,
                 settings.model_fast, 300) for i, x in enumerate(tail)]


def submit(reqs: list[dict], name: str) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    ids = []
    for s in range(0, len(reqs), 50_000):  # batch limits: 100k requests / 256 MB
        b = client.messages.batches.create(requests=reqs[s:s + 50_000])
        ids.append(b.id)
        print(f"submitted {b.id} ({len(reqs[s:s + 50_000]):,} requests)")
    (OUT / f"{name}_batches.json").write_text(json.dumps(ids))
    return ",".join(ids)


def collect(batch_id: str):
    import anthropic
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    while True:
        b = client.messages.batches.retrieve(batch_id)
        if b.processing_status == "ended":
            break
        print(f"{batch_id}: {b.processing_status} {b.request_counts}")
        time.sleep(60)
    rows = []
    for r in client.messages.batches.results(batch_id):
        if r.result.type != "succeeded":
            rows.append({"custom_id": r.custom_id, "ok": False, "output": None}); continue
        tool = next((c.input for c in r.result.message.content if c.type == "tool_use"), None)
        rows.append({"custom_id": r.custom_id, "ok": True, "output": json.dumps(tool)})
    df = pd.DataFrame(rows)
    df["review_status"] = "pending"
    df.to_parquet(OUT / f"results_{batch_id}.parquet", index=False)
    # review queue: 2% random sample + everything under 0.6 confidence
    conf = df["output"].map(lambda s: (json.loads(s) or {}).get("confidence", 1.0) if s else 0.0)
    review = df[(conf < 0.6) | (df.index.to_series().sample(frac=0.02, random_state=7).reindex(df.index).notna())]
    review.to_csv(OUT / f"review_{batch_id}.csv", index=False)
    print(f"collected {len(df):,} results; {len(review):,} queued for review")


def load_rewrites_into_cache(path: Path):
    """Approved rewrites -> the same cache keys the online QU uses, so serving is a free cache hit."""
    from webbsearch.llm.client import ClaudeClient
    from webbsearch.llm.query_understanding import qu_cache_key
    c = ClaudeClient(api_key="offline-load")
    df = pd.read_parquet(path)
    qmap = json.loads((OUT / "rewrites_queries.json").read_text())
    n = 0
    for r in df.itertuples():
        if r.ok and r.review_status == "approved":
            q = qmap.get(r.custom_id)
            if q:
                c._persist(qu_cache_key(q, pd.Timestamp.today().date()), json.loads(r.output))
                n += 1
    print(f"loaded {n} rewrites")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("job", choices=["catalog", "vocab", "rewrites", "collect"])
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--only-missing", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--batch-id")
    a = ap.parse_args(argv)
    OUT.mkdir(parents=True, exist_ok=True)
    if a.job == "collect":
        return collect(a.batch_id)
    if a.job == "catalog":
        reqs, out_each = build_catalog(a.limit, a.only_missing), 220
    elif a.job == "vocab":
        reqs, out_each = build_vocab(), 900
    else:
        reqs, out_each = build_rewrites(a.limit or 5000), 180
    est = estimate(reqs, out_each)
    path = OUT / f"{a.job}_requests.jsonl"
    with open(path, "w") as fh:
        for r in reqs:
            fh.write(json.dumps(r) + "\n")
    print(json.dumps(est, indent=2))
    print(f"requests written to {path}")
    if a.dry_run or not settings.anthropic_api_key:
        if not a.dry_run:
            print("ANTHROPIC_API_KEY not set: dry run only")
        return est
    submit(reqs, a.job)


if __name__ == "__main__":
    main()
