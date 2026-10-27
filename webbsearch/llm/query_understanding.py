"""Claude query understanding.

Where it runs:
  1. Online, synchronously, only for queries the rule parser flags
     (conversational / long / unknown vocabulary): ~5-10% of traffic, with a
     hard timeout. On timeout the search proceeds on rules, and the call is
     finished in the background so the next identical query hits the cache.
  2. Offline, nightly: the query-log tail (zero-result and low-CTR queries) is
     batch-rewritten (Message Batches API, 50% cost) and loaded into the same
     cache. Most "LLM-understood" queries at serving time are cache hits that
     cost nothing and add no latency.

Output is a structured interpretation, never free text that goes to the user.
The taxonomy/brand list goes in a cached system prompt.
"""
from __future__ import annotations

import threading
from datetime import date

from webbsearch.config import settings
from webbsearch.llm.client import ClaudeClient
from webbsearch.retrieval.analyzer import analyze
from webbsearch.retrieval.query_parser import ParsedQuery

SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": ["products", "orders", "invoices", "lists", "content", "question"],
                   "description": "Primary vertical the user wants."},
        "rewritten_query": {"type": "string",
                            "description": "Concise keyword query for the product index using distributor vocabulary "
                                           "(sizes like 3/4, 1-1/4; nouns like elbow, ball valve, water heater). "
                                           "Empty if not a product search."},
        "categories": {"type": "array", "items": {"type": "string"},
                       "description": "Up to 3 category names, copied exactly from the provided taxonomy."},
        "brands": {"type": "array", "items": {"type": "string"}, "description": "Brands named or clearly implied."},
        "attributes": {"type": "object", "additionalProperties": {"type": "string"},
                       "description": "Specs such as size, gallons, btu, fuel, connection, material, voltage."},
        "part_numbers": {"type": "array", "items": {"type": "string"}},
        "account_filters": {
            "type": "object",
            "properties": {"date_from": {"type": "string", "description": "YYYY-MM-DD"},
                           "date_to": {"type": "string", "description": "YYYY-MM-DD"},
                           "status": {"type": "string", "enum": ["OPEN", "PAST_DUE", "PAID", "SHIPPED",
                                                                 "BACKORDERED", "INVOICED", ""]},
                           "job_or_po_text": {"type": "string"},
                           "order_no": {"type": "string"}, "invoice_no": {"type": "string"}}},
        "confidence": {"type": "number", "description": "0-1 confidence in this interpretation."},
    },
    "required": ["intent", "rewritten_query", "confidence"],
}


def build_system_prompt(catalog) -> str:
    import pandas as pd
    df = pd.DataFrame({"cat1": catalog.cat1, "cat2": catalog.cat2, "cat3": catalog.cat3, "brand": catalog.brand})
    fam = df.groupby(["cat1", "cat2", "cat3"])["brand"].agg(lambda s: ", ".join(s.value_counts().index[:6]))
    lines = [f"- {c1} > {c2} > {c3}  (brands: {b})" for (c1, c2, c3), b in fam.items()]
    return (
        "You interpret search queries typed into F.W. Webb's B2B e-commerce site. Webb is a Northeast US "
        "wholesale distributor of plumbing, HVAC, hydronic heating, water works and industrial PVF supplies. "
        "Users are mostly trade contractors, facilities staff and purchasing agents. They type trade shorthand "
        "(\"3/4 cxc 90\" = 3/4 inch copper sweat 90-degree elbow; \"hsi\" = hot surface igniter; \"prv\" = pressure "
        "reducing valve; \"45/5 cap\" = 45/5 MFD dual run capacitor; \"combi\" = condensing combi boiler), part "
        "numbers, and sometimes questions about their own account (orders, invoices, purchase lists).\n\n"
        "Return a structured interpretation with the tool. Rules:\n"
        "- rewritten_query: short keywords the product index will match. Expand abbreviations, keep sizes and "
        "part numbers verbatim, drop filler words. For account questions, leave it empty unless a product is named.\n"
        "- categories: copy names exactly from the taxonomy below; omit if unsure.\n"
        "- Never invent part numbers, brands, order or invoice numbers.\n"
        "- Dates: resolve relative phrases against today's date given in the message.\n\n"
        "Taxonomy (L1 > L2 > L3 with common brands):\n" + "\n".join(lines)
    )


def qu_cache_key(q: str, today: date) -> str:
    """Shared by the online path and the nightly batch loader. The date is part of
    the key only when the query contains a relative date phrase, so ordinary
    product rewrites stay valid across days."""
    from webbsearch.retrieval.query_parser import parse_dates
    norm = analyze(q) or q.lower().strip()
    return ClaudeClient.key("qu-v1", norm, str(today) if parse_dates(q, today) else "")


class ClaudeQueryUnderstanding:
    def __init__(self, client: ClaudeClient, catalog, today: date):
        self.c = client
        self.today = today
        self.system = build_system_prompt(catalog)
        self.valid_cats = set(catalog.cat3)
        self.valid_brands = set(catalog.brand)
        self._inflight: set[str] = set()

    def _key(self, q: str) -> str:
        return qu_cache_key(q, self.today)

    def interpret(self, q: str, timeout: float | None = None) -> dict | None:
        k = self._key(q)
        hit = self.c.cached(k)
        if hit is not None:
            return hit
        timeout = timeout or settings.qu_timeout_s
        out = self.c.structured(model=settings.model_fast, system=self.system,
                                user=f"Today is {self.today}. Query: {q}", tool_name="interpretation",
                                schema=SCHEMA, timeout=timeout, max_tokens=500, cache_key=k)
        if out is None and self.c.enabled and k not in self._inflight:
            # finish in the background so the next identical query is a cache hit
            self._inflight.add(k)
            threading.Thread(target=self._bg, args=(q, k), daemon=True).start()
        return out

    def _bg(self, q, k):
        try:
            self.c.structured(model=settings.model_fast, system=self.system, user=f"Today is {self.today}. Query: {q}",
                              tool_name="interpretation", schema=SCHEMA, timeout=10, max_tokens=500, cache_key=k)
        finally:
            self._inflight.discard(k)

    def apply(self, pq: ParsedQuery, out: dict) -> ParsedQuery:
        """Merge a Claude interpretation into the parsed query. Claude only adds
        signal; it cannot widen access (account filters still pass through the
        entitlement layer) and invalid categories/brands are dropped."""
        pq.source = "rules+claude"
        rq = (out.get("rewritten_query") or "").strip()
        if rq:
            pq.rewritten = rq
        cats = [c for c in out.get("categories") or [] if c in self.valid_cats]
        brands = [b for b in out.get("brands") or [] if b in self.valid_brands]
        pq.llm_filters = {"cat3": cats, "brands": brands, "attributes": out.get("attributes") or {},
                          "confidence": out.get("confidence")}
        for b in brands:
            if b not in pq.brands:
                pq.brands.append(b)
        if out.get("intent") and (out.get("confidence") or 0) >= 0.5:
            pq.intent = out["intent"]
        af = out.get("account_filters") or {}
        try:
            if af.get("date_from") and af.get("date_to"):
                pq.date_range = (date.fromisoformat(af["date_from"]), date.fromisoformat(af["date_to"]))
        except ValueError:
            pass
        if af.get("status"):
            pq.status = af["status"]
        for k_src, k_dst in (("order_no", "order_no"), ("invoice_no", "invoice_no")):
            if af.get(k_src) and af[k_src] in pq.raw:  # must literally appear: no invented ids
                setattr(pq, k_dst, af[k_src])
        return pq
