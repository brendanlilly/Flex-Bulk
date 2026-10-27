"""Claude listwise reranking + explanations for hard queries.

Trigger policy (set in SearchService): natural-language queries, or queries
where the LTR model is unsure (small score margin among the top results), or
an explicit "refine with Claude" action. The top-N LTR results are sent with
compact attributes and the account's buying context; Claude returns an order
and a one-line reason per item. Its ranking is blended with LTR through RRF
rather than replacing it, so a bad LLM call degrades gracefully.
"""
from __future__ import annotations

from webbsearch.config import settings
from webbsearch.llm.client import ClaudeClient

SCHEMA = {
    "type": "object",
    "properties": {
        "ranking": {
            "type": "array",
            "items": {"type": "object",
                      "properties": {"id": {"type": "string"},
                                     "relevance": {"type": "integer", "minimum": 0, "maximum": 3},
                                     "reason": {"type": "string",
                                                "description": "<= 14 words, factual; only for the top 8, else empty"}},
                      "required": ["id", "relevance"]},
            "description": "Every candidate id, best first."},
    },
    "required": ["ranking"],
}

SYSTEM = (
    "You rank product search results for F.W. Webb, a plumbing/HVAC/industrial wholesale distributor. "
    "Given the buyer's query, their account context, and candidate products, order the candidates by how well "
    "each satisfies the query. Specs the buyer stated (size, connection, fuel, capacity, brand, part number) are "
    "hard requirements: a product that contradicts a stated spec is relevance 0. Among products that satisfy "
    "the query, prefer what this account already buys (brand/SKU history), then stocked items. Give every id "
    "exactly once. Give a reason only for your top 8 (leave the rest empty) to keep the response short. "
    "Reasons must be grounded in the candidate data; never invent specs."
)


class ClaudeReranker:
    def __init__(self, client: ClaudeClient):
        self.c = client

    def rerank(self, query: str, items: list[dict], account_ctx: str, timeout: float | None = None) -> dict | None:
        """items: [{'id','title','brand','attrs','stock','bought'}]. Returns {id: (rank, relevance, reason)}."""
        if not items:
            return None
        lines = []
        for it in items:
            a = "; ".join(f"{k}={v}" for k, v in (it.get("attrs") or {}).items() if v)[:220]
            extra = f" | bought {it['bought']}x" if it.get("bought") else ""
            lines.append(f"[{it['id']}] {it['title']} | brand={it['brand']} | {a} | stock={it['stock']}{extra}")
        user = f"Query: {query}\nAccount: {account_ctx}\nCandidates:\n" + "\n".join(lines)
        # key includes per-item account signals so one account never sees another's cached reasons
        k = ClaudeClient.key("rerank-v2", query, account_ctx, [(it["id"], it.get("bought", 0)) for it in items])
        out = self.c.structured(model=settings.model_fast, system=SYSTEM, user=user, tool_name="ranking",
                                schema=SCHEMA, timeout=timeout or settings.rerank_timeout_s,
                                max_tokens=150 + 25 * len(items) + 30 * 8, cache_key=k, persist=False)
        if not out:
            return None
        valid = {it["id"] for it in items}
        res = {}
        for r, row in enumerate(out.get("ranking") or [], 1):
            i = str(row.get("id"))
            if i in valid and i not in res:
                res[i] = (r, int(row.get("relevance", 0)), str(row.get("reason", ""))[:140])
        return res or None


def blend(ltr_order: list[str], llm: dict, w_llm: float = 1.2, k: int = 10) -> list[str]:
    """RRF blend of LTR order and Claude order; relevance-0 items drop to the bottom."""
    score = {}
    for r, i in enumerate(ltr_order, 1):
        score[i] = 1.0 / (k + r)
    for i, (r, rel, _) in llm.items():
        score[i] = score.get(i, 0) + w_llm / (k + r) - (1.0 if rel == 0 else 0.0)
    return sorted(score, key=lambda i: -score[i])
