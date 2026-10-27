"""SearchService: the single entry point the API, the assistant and the
evaluation harness call.

Request flow (products):
  parse (rules) -> [Claude QU if flagged, cached] -> hybrid retrieval (exact | BM25 | dense, RRF)
  -> feature assembly (retrieval + match + product + personal + cohort)
  -> LambdaMART -> [Claude rerank on top-N if hard query] -> price + badges + reasons

Account verticals (orders, invoices, lists) are searched with ACL tokens from
the Principal; content is public. "All" federates the verticals and orders the
blocks by detected intent.
"""
from __future__ import annotations

import bisect
import json
import os
import math
import re
import time
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

import numpy as np

from webbsearch.config import settings
from webbsearch.index.catalog import Catalog
from webbsearch.index.embed import LSAEmbedder
from webbsearch.index.local_backend import LocalBackend
from webbsearch.llm.client import ClaudeClient
from webbsearch.llm.query_understanding import ClaudeQueryUnderstanding
from webbsearch.llm.rerank import ClaudeReranker, blend
from webbsearch.personalize.features import FeatureStore
from webbsearch.rank.features import FEATURES, FeatureBuilder
from webbsearch.rank.ltr import Ranker
from webbsearch.retrieval.branches import load_branches
from webbsearch.retrieval.account_data import AccessDenied, AccountData
from webbsearch.retrieval.analyzer import analyze, clean_dashes, compact_id, normalize
from webbsearch.retrieval.entitlements import ANONYMOUS, Principal
from webbsearch.retrieval.hybrid import HybridRetriever
from webbsearch.retrieval.query_parser import QUESTION_RE, ParsedQuery, QueryParser

F = {f: i for i, f in enumerate(FEATURES)}
VTYPE = {"orders": "order", "invoices": "invoice", "lists": "list", "content": "content"}
SEGMENT_LABEL = {"plumbing_contractor": "plumbing contractors", "hvac_contractor": "HVAC contractors",
                 "mechanical_contractor": "mechanical contractors", "facilities_mro": "facilities teams",
                 "builder_remodeler": "builders", "industrial": "industrial accounts",
                 "water_works": "water works utilities"}


class Engine:
    """Loads every serving component once per process."""

    def __init__(self, asof: date | None = None, today: date | None = None, claude: ClaudeClient | None = None,
                 log=print):
        t0 = time.time()
        meta = json.loads((settings.data_dir / "sim" / "meta.json").read_text()) \
            if (settings.data_dir / "sim" / "meta.json").exists() else {}
        self.today = today or date.fromisoformat(meta.get("today", str(date.today())))
        self.asof = asof or self.today
        self.catalog = Catalog.load(settings.canonical_dir)
        if settings.backend == "opensearch":
            from webbsearch.index.opensearch_backend import OpenSearchBackend
            self.backend = OpenSearchBackend()
        else:
            self.backend = LocalBackend(settings.index_dir)
        self.embedder = LSAEmbedder.load(settings.models_dir / "embedder.pkl", serving=True)
        self.parser = QueryParser(sorted(set(self.catalog.brand)), self.today)
        from webbsearch.retrieval.brands import Brands
        self.brands = Brands(self.catalog)          # v2.42: aliases, covers, brands not carried, product lines
        self.parser.master = self.brands
        fdir = settings.features_dir / str(self.asof)
        self.features = FeatureStore(fdir, self.catalog.sku_to_idx, self.catalog.n, self.asof)
        self.fb = FeatureBuilder(self.catalog, self.features)
        self.ranker = Ranker(settings.models_dir)
        pop = np.log1p(self.features.pop_365)
        self.retriever = HybridRetriever(self.backend, self.catalog, self.embedder,
                                         prior=(pop / max(float(pop.max()), 1.0)).astype(np.float32),
                                         token_fn=self.fb.doc_tokens,
                                         history_fn=lambda p: self.features.acct_prod.get(p.account_id))
        self.accounts = AccountData(settings.canonical_dir, self.catalog, self.today)
        self.claude = claude or ClaudeClient()
        self.qu = ClaudeQueryUnderstanding(self.claude, self.catalog, self.today)
        self.reranker = ClaudeReranker(self.claude)
        self.service = SearchService(self)
        from webbsearch.retrieval.availability import Availability
        from webbsearch.retrieval.results import ResultsService
        import pandas as _pd
        sf = settings.canonical_dir / "stock.parquet"        # Flex Bulk: the stock file (flexbulk.loader)
        self.availability = Availability(self.catalog, load_branches(settings.canonical_dir),
                                         self.features.pop_365, stock=_pd.read_parquet(sf) if sf.exists() else None)
        from webbsearch.retrieval.hvac import HVAC
        from webbsearch.retrieval.kits import KitStore
        from webbsearch.retrieval.webb_items import WebbItems
        self.kits = KitStore()
        # Flex Bulk: FlexSearch's real Webb items (job kits, fwwebb.com) and the OEM cross-reference lake point at Webb
        # item numbers, which a new item list may not use. Off unless FLEXBULK_XREF=1 (item numbers are Webb's).
        xref_on = os.environ.get("FLEXBULK_XREF", "0") == "1"
        self.webb_items = WebbItems() if xref_on else WebbItems(kits_path=Path("/nonexistent"), seed_path=Path("/nonexistent"))
        self.hvac = HVAC(self.catalog, self.today)
        from webbsearch.retrieval.account_query import AccountSearch
        from webbsearch.retrieval.xref import XRef
        self.account_search = AccountSearch(self)
        from webbsearch.retrieval.compare import Compare
        self.compare = Compare(self)          # two products side by side (v2.02)
        from webbsearch.knowledge.answer import Knowledge
        self.knowledge = Knowledge(self)      # the Knowledge Center: answers from product documents (v2.1)
        self.xref = XRef()
        if not xref_on:
            self.xref.ok = False
        if self.xref.ok:
            from webbsearch.retrieval.webb_stock import DemoStock
            self.xref.attach_stock(DemoStock(self.availability))   # demo on-hand for real Webb items (labeled)
            log(f"cross-reference lake: {self.xref.stats['crosses']:,} relationships, "
                f"{self.xref.stats['webb_items']:,} Webb items ({self.xref.path})")
        self.results = ResultsService(self)
        from webbsearch.retrieval.fieldrel import FieldRel
        self.fieldrel = FieldRel(self.catalog)              # v2.44 relevance by field and attribute
        from webbsearch.merch.store import MerchStore
        self.merch = MerchStore(os.environ.get("WEBB_MERCH_DIR") or settings.data_dir / "merch")
        # the search dictionary (trade vocabulary the eCommerce team maintains), checked against this catalog
        from webbsearch.merch.dictionary import Dictionary, Lint
        from webbsearch.retrieval.analyzer import CANON
        self.vocab = Dictionary(os.environ.get("WEBB_MERCH_DIR") or settings.data_dir / "merch")
        from webbsearch.retrieval import lexicon
        lexicon.warm()                      # trade words in 12 languages (v2.4)
        # v2.43: the speller's and the part-number matcher's tables, built now rather than on someone's first search
        try:
            self.results._catalog_words()
            self.results._head_families()
            self.results._brand_words()
            self.results._real_words()
            self.results._english()
            if getattr(self.retriever, "idmatch", None) is not None:
                self.retriever.idmatch._sorted()
            self.fieldrel.warm()
            if getattr(self, "service", None) is not None:
                self.service.suggest("copper")          # the type-ahead's first products and kits (v2.44)
        except Exception:  # noqa: BLE001
            pass
        brands = set(map(str, self.catalog.brand.unique() if hasattr(self.catalog.brand, "unique") else set(self.catalog.brand)))
        if self.xref.ok:
            try:
                brands |= {r[0] for r in self.xref._db().execute("select name from brand where n >= 5")}
            except Exception:
                pass
        self.vocab.lint = Lint(df=getattr(self.backend, "term_df", None), brands=brands, canon=CANON)
        seed = Path(__file__).resolve().parents[2] / "config" / "dictionary" / "webb-dictionary.json"
        if not self.vocab.entries() and self.vocab.version == 0 and seed.exists():
            d = json.loads(seed.read_text())
            r = self.vocab.import_rows(d["entries"], who="starting dictionary", source="")
            log(f"search dictionary: loaded the starting dictionary ({r['imported']:,} entries, "
                f"{self.vocab.counts()['live_rules']} live rules)")
        try:          # the release's dictionary changes, once per copy (merch/dictionary_releases.py)
            from webbsearch.merch.dictionary_releases import apply_releases
            apply_releases(self.vocab, self.vocab.root, log=log)
        except Exception as ex:  # noqa: BLE001  (never blocks start-up)
            log(f"search dictionary: release changes not applied ({type(ex).__name__}: {ex})")
        try:                                      # the Extended Catalog (v2.3): tiers, -EC items, freight
            from webbsearch.extended.service import Extended
            self.extended = Extended(self, log=log)
        except Exception as ex:  # noqa: BLE001  (never blocks start-up; search runs as in v2.2)
            self.extended = None
            log(f"Extended Catalog: off ({type(ex).__name__}: {ex})")
        log(f"engine ready in {time.time() - t0:.1f}s (features as-of {self.asof}, claude "
            f"{'on' if self.claude.enabled else 'off: rule-based fallback'})")


class SearchService:
    def __init__(self, e: Engine):
        self.e = e
        self._build_suggestions()

    # ================================================================ search
    def search(self, q: str, principal: Principal = ANONYMOUS, vertical: str = "all",
               filters: dict | None = None, page: int = 1, size: int = 24, debug: bool = False,
               llm: str = "auto", personalize: bool = True, skip_products: bool = False) -> dict:
        e = self.e
        q = clean_dashes(q)                  # en / em dashes and the minus sign read as hyphens (1–1/4 = 1-1/4)
        timings = {}
        page, size = max(1, int(page)), max(1, min(int(size), 100))
        filters = {k: v for k, v in (filters or {}).items() if k in ("brand", "cat3", "stock", "family", "status")}
        t = time.perf_counter()
        pq = e.parser.parse(q[:500])
        self._flag_unknown_vocab(pq)
        timings["parse_ms"] = _ms(t)
        llm_on = llm != "off" and e.claude.enabled
        llm_info = {"available": e.claude.enabled, "qu": None, "rerank": None}
        if llm_on and (pq.needs_llm or llm == "force"):
            t = time.perf_counter()
            out = e.qu.interpret(q)
            timings["claude_qu_ms"] = _ms(t)
            if out:
                e.qu.apply(pq, out)
                llm_info["qu"] = out
        resp = {"query": q, "parsed": pq.to_dict(), "vertical": vertical, "blocks": [], "llm": llm_info}
        # ---------------------------------------------------------- products
        # On "All", an account question ("past due invoices", "orders last month") should not
        # also return a grid of loosely matching products.
        acct_score = max(pq.intent_scores.get("orders", 0), pq.intent_scores.get("invoices", 0)) \
            if pq.intent == "question" else pq.intent_scores.get(pq.intent, 0)
        account_q = pq.intent in ("orders", "invoices", "question") and \
            acct_score >= pq.intent_scores.get("products", 0) + 0.5 and (not (pq.brands or pq.sizes) or pq.account_phrase)
        want_products = (vertical == "products" or (vertical == "all" and not account_q)) and not skip_products
        resp["account_question"] = bool(account_q)
        if want_products and principal.can("products") and (pq.tokens or pq.id_chunks):
            t = time.perf_counter()
            prod = self.search_products(pq, principal, filters=filters, page=page, size=size, debug=debug,
                                        llm_on=llm_on, llm_mode=llm, personalize=personalize, timings=timings)
            resp["products"] = prod
            timings["products_ms"] = _ms(t)
        # ---------------------------------------------------------- verticals
        t = time.perf_counter()
        prods = (resp.get("products") or {}).get("items") or []
        for v in ("orders", "invoices", "lists", "content"):
            if vertical not in ("all", v):
                continue
            if vertical == "all" and account_q and v in ("orders", "invoices") and pq.intent in ("orders", "invoices") \
                    and v != pq.intent:
                continue
            if vertical == "all" and v in ("orders", "invoices") and not account_q and not (
                    pq.order_no or pq.invoice_no or pq.po or pq.intent == v):
                # product query on "All": show where this account bought the top results, not text matches
                if v == "orders" and principal.can("orders") and prods:
                    bought = [c for c in prods[:6] if c.get("history")][:2]
                    rows = []
                    for c in bought:
                        for o in e.accounts.find_orders(principal, sku=c["sku"], limit=2):
                            rows.append({"type": "order", **o, "contains": c["title"], "exact": False})
                    if rows:
                        resp["blocks"].append({"vertical": "orders", "label": "Where you bought these", "hits": rows[:4]})
                continue
            if not principal.can(v):
                # say so when the user asked for this vertical, directly or as an account question on All,
                # instead of an empty "nothing matched"
                if principal.is_authenticated and (vertical == v or (vertical == "all" and account_q and pq.intent == v)):
                    role = {"tech": "technician", "ap": "accounts payable"}.get(principal.role, principal.role)
                    resp["denied"] = (f"{v.capitalize()} aren't available to your role ({role}). "
                                      "The account owner or your accounts-payable contact can see them.")
                continue
            k = 5 if vertical == "all" else size
            # On "All", lists/content must match most of the query in their title, not just one token
            min_cov = 0.6 if (vertical == "all" and v in ("lists", "content") and pq.intent != v) else 0.0
            hits = self.search_vertical(v, pq, principal, k, min_coverage=min_cov)
            if hits:
                resp["blocks"].append({"vertical": v, "hits": hits})
        timings["verticals_ms"] = _ms(t)
        resp["blocks"].sort(key=lambda b: -self._block_priority(b, pq))
        resp["top_answer"] = self._top_answer(resp, pq)
        resp["timings"] = timings
        resp["timings"]["total_ms"] = round(sum(v for k, v in timings.items() if k.endswith("_ms")
                                                and k in ("parse_ms", "claude_qu_ms", "products_ms", "verticals_ms")), 1)
        return resp

    def search_products(self, pq: ParsedQuery, principal: Principal, filters=None, page=1, size=24,
                        debug=False, llm_on=False, llm_mode="auto", personalize=True, timings=None,
                        return_scores=False) -> dict:
        e = self.e
        timings = timings if timings is not None else {}
        t = time.perf_counter()
        f = {k: v for k, v in (filters or {}).items() if v}
        cands = e.retriever.retrieve(pq, filters=f or None,
                                     principal=principal if (personalize and principal.is_authenticated) else None)
        timings["retrieve_ms"] = _ms(t)
        t = time.perf_counter()
        personal = personalize and principal.is_authenticated
        X = e.fb.build(pq, cands, principal if principal.is_authenticated else None, personal=personal)
        s = e.ranker.score(X, personal=personal)
        # exact part-number hits always lead
        s = s + 100.0 * X[:, F["exact_id"]]
        if getattr(e, "merch", None):       # the same ranking weights as the results page (rules stay on that page)
            from webbsearch.merch.rules import adjust_scores
            acct = ({"segment": principal.segment, "lines": e.features.acct_lines.get(principal.account_id, 0),
                     "brand_share": e.features.acct_brand_all.get(principal.account_id, {})}
                    if principal.is_authenticated else None)
            s = adjust_scores(s, X, cands.idx, e.catalog, F, e.merch.get(), personal, [], acct=acct)
        order = np.argsort(-s, kind="stable")
        timings["rank_ms"] = _ms(t)
        idx = cands.idx[order]
        Xo = X[order]
        so = s[order]
        ids = [str(e.catalog.sku[i]) for i in idx]
        reasons_llm = {}
        rerank_used = False
        if llm_on and len(idx) > 1 and (llm_mode == "force" or pq.style == "natural" or self._uncertain(so)):
            t = time.perf_counter()
            n = min(settings.llm_rerank_window, len(idx))
            items = [self._llm_item(i, principal) for i in idx[:n]]
            ctx = self._account_ctx(principal)
            res = e.reranker.rerank(pq.raw, items, ctx)
            timings["claude_rerank_ms"] = _ms(t)
            if res:
                new = blend(ids[:n], res)
                pos = {sku: j for j, sku in enumerate(ids)}
                head = [pos[sku] for sku in new]
                perm = np.array(head + list(range(n, len(ids))))
                idx, Xo, so = idx[perm], Xo[perm], so[perm]
                ids = [ids[j] for j in perm]
                reasons_llm = {k: v[2] for k, v in res.items() if v[2]}
                rerank_used = True
                # exact part-number hits stay pinned above anything Claude reorders
                ex = np.flatnonzero(Xo[:, F["exact_id"]] > 0)
                if len(ex):
                    perm2 = np.concatenate([ex, np.flatnonzero(Xo[:, F["exact_id"]] <= 0)])
                    idx, Xo, so = idx[perm2], Xo[perm2], so[perm2]
                    ids = [ids[j] for j in perm2]
        if return_scores:
            return {"idx": idx, "scores": so}
        facets = self._facets(idx[:100])
        start = (page - 1) * size
        cards = []
        for r in range(start, min(start + size, len(idx))):
            i = int(idx[r])
            c = e.catalog.card(i)
            c["price"] = e.accounts.price(principal, i)
            c["history"] = e.accounts.history_badge(principal, i)
            c["reasons"] = self._reasons(Xo[r], i, principal, pq)
            if ids[r] in reasons_llm:
                c["claude_reason"] = reasons_llm[ids[r]]
            if debug:
                c["debug"] = {"score": round(float(so[r]), 4),
                              "features": {FEATURES[j]: round(float(Xo[r, j]), 4) for j in range(len(FEATURES))}}
            cards.append(c)
        return {"total": int(len(idx)), "page": page, "size": size, "items": cards, "facets": facets,
                "reranked_by_claude": rerank_used, "model": "personal" if personal else "anonymous"}

    # ================================================================ verticals
    def search_vertical(self, v: str, pq: ParsedQuery, principal: Principal, k: int,
                        min_coverage: float = 0.0) -> list[dict]:
        e = self.e
        acl = principal.acl(v)
        if not acl:
            return []
        ids = [c.lower() for c in pq.id_chunks]
        for x in (pq.order_no, pq.invoice_no, pq.po):
            if x:
                ids.append(compact_id(x).lower())
        tokens = [t for t in pq.tokens if t not in ("order", "invoice", "po", "list", "past", "due", "open")]
        hits: list[dict] = []
        try:
            if v in ("orders", "invoices") and (pq.date_range or pq.status) and not ids:
                # structured account question: filter, don't text-match
                a, b = pq.date_range or (None, None)
                if v == "orders":
                    rows = e.accounts.find_orders(principal, start=a, end=b,
                                                  status=pq.status if pq.status in ("OPEN", "SHIPPED", "BACKORDERED") else None,
                                                  limit=k)
                else:
                    rows = e.accounts.find_invoices(principal, status=pq.status, start=a, end=b, limit=k)
                return [{"type": VTYPE[v], **r, "match": "filter"} for r in rows]
            res = e.backend.search_vertical(v, tokens, acl, k, id_tokens=ids)
        except AccessDenied:
            return []
        for doc_id, score in res:
            if v == "orders":
                card = e.accounts.order_card(e.accounts.orders.loc[doc_id].to_dict(), principal)
            elif v == "invoices":
                card = e.accounts.invoice_card(e.accounts.invoices.loc[doc_id].to_dict())
            elif v == "lists":
                card = e.accounts.list_card(doc_id)
                if card["list_type"] == "WEBB_KIT" and getattr(e, "kits", None) and e.kits.ok:
                    continue  # superseded by the real job kits (retrieval/kits.py)
            else:
                card = self._content_card(doc_id)
            exact = any(x and str(x) in (doc_id, card.get("po", "")) for x in (pq.order_no, pq.invoice_no, pq.po)) \
                or doc_id in pq.id_chunks
            if min_coverage and not exact and tokens:
                title_toks = set(normalize(card.get("title") or card.get("name") or ""))
                if len(set(tokens) & title_toks) / len(set(tokens)) < min_coverage:
                    continue
            hits.append({"type": VTYPE[v], "score": round(score, 3), "exact": bool(exact), **card})
        return hits

    def _content_card(self, cid: str) -> dict:
        if not hasattr(self, "_content"):
            import pandas as pd
            self._content = pd.read_parquet(settings.canonical_dir / "content.parquet").set_index("content_id", drop=False)
        r = self._content.loc[cid]
        return {"content_id": cid, "title": r["title"], "content_type": r["content_type"], "url": r["url"],
                "snippet": r["body"][:180]}

    def _block_priority(self, b: dict, pq: ParsedQuery) -> float:
        base = pq.intent_scores.get(b["vertical"], 0.0)
        if any(h.get("exact") for h in b["hits"]):
            base += 10
        return base

    def _top_answer(self, resp, pq):
        for b in resp["blocks"]:
            for h in b["hits"]:
                if h.get("exact"):
                    return {"vertical": b["vertical"], **h}
        prods = resp.get("products", {}).get("items") or []
        if prods and pq.style == "part" and "Exact part number match" in prods[0]["reasons"]:
            return {"vertical": "products", **prods[0]}
        return None

    # ================================================================ helpers
    def _flag_unknown_vocab(self, pq: ParsedQuery):
        if pq.style == "part":
            return
        unknown = [t for t in pq.tokens if t.isalpha() and len(t) >= 3 and not self.e.backend.term_exists(t)]
        pq.unknown_tokens = unknown  # type: ignore[attr-defined]
        if unknown:
            pq.needs_llm = True

    @staticmethod
    def _uncertain(scores: np.ndarray) -> bool:
        if len(scores) < 3:
            return False
        return float(scores[0] - scores[2]) < 0.15

    def _llm_item(self, i: int, p: Principal) -> dict:
        c = self.e.catalog
        hb = self.e.accounts.history_badge(p, i)
        return {"id": str(c.sku[i]), "title": c.title[i], "brand": c.brand[i], "attrs": c.attrs(i),
                "stock": c.stock_tier[i], "bought": hb["times"] if hb else 0}

    def _account_ctx(self, p: Principal) -> str:
        if not p.is_authenticated:
            return "anonymous visitor"
        fams = self.e.features.top_families(p.account_id, 4)
        fam_txt = ", ".join(f for f, _ in fams)
        br = []
        for f, _ in fams:
            pb = self.e.features.preferred_brand(p.account_id, f)
            if pb and pb[1] >= 0.5:
                br.append(f"{pb[0]} for {f}")
        return (f"{SEGMENT_LABEL.get(p.segment, p.segment)}; buys mostly {fam_txt}"
                + (f"; brand loyalty: {', '.join(br)}" if br else ""))

    def _reasons(self, x: np.ndarray, i: int, p: Principal, pq: ParsedQuery) -> list[str]:
        out = []
        c = self.e.catalog
        if x[F["exact_id"]] > 0:
            out.append("Exact part number match")
        merch = getattr(self.e, "merch", None)
        if p.is_authenticated and (merch is None or merch.get()["personalization"]["reasons"]):
            hb = self.e.accounts.history_badge(p, i)
            if hb and x[F["acct_bought_n"]] > 0:
                out.append(f"You've bought this {hb['times']}x (last {hb['last_date']})")
            elif x[F["acct_brand_share"]] >= 0.5:
                out.append(f"Your usual brand for {c.cat3[i].lower()}")
            if x[F["seg_prod_accts"]] >= math.log1p(4) and not hb:
                out.append(f"Popular with {SEGMENT_LABEL.get(p.segment, 'similar')} accounts")
        if x[F["spec_coverage"]] >= 1.0 and any(ch.isdigit() for ch in pq.raw):
            specs = [t for t in pq.tokens if any(ch.isdigit() for ch in t)]
            out.append("Matches " + " ".join(specs[:3]))
        tier = c.stock_tier[i]
        out.append({"stocked": "Stocked at branches", "regional": "Stocked regionally",
                    "dc": "Ships from distribution center", "special": "Special order"}.get(tier, tier))
        if c.status[i] == "D":
            out.append("Discontinued - see replacements")
        return out

    def _facets(self, idx: np.ndarray) -> dict:
        c = self.e.catalog
        if len(idx) == 0:
            return {}
        out = {"brand": Counter(c.brand[idx].tolist()).most_common(12),
               "cat3": Counter(c.cat3[idx].tolist()).most_common(10),
               "stock": Counter(c.stock_tier[idx].tolist()).most_common()}
        sizes = Counter()
        for i in idx[:120].tolist():
            v = c.attrs(i).get("size")
            if v:
                sizes[v] += 1
        if sizes:
            out["size"] = sizes.most_common(12)
        return {k: [{"value": v, "count": n} for v, n in lst] for k, lst in out.items()}

    # ================================================================ typeahead
    def _build_suggestions(self):
        e = self.e
        entries: dict[str, tuple[str, float, str]] = {}
        qp = e.features.query_pop
        for q, n in zip(qp["query"], qp["n"]):
            key = analyze(q)
            if len(key) < 3 or any(ch.isdigit() for ch in key.replace("/", "").replace("-", "")) and len(key) > 18:
                continue
            if QUESTION_RE.match(q.strip()):  # conversational queries make poor suggestions
                continue
            # never suggest misspellings: every word must exist in the catalog vocabulary
            if any(t.isalpha() and len(t) >= 3 and not e.backend.term_exists(t) for t in key.split()):
                continue
            if key not in entries or entries[key][1] < n:
                entries[key] = (q, float(n), "query")
        for c3, n in Counter(e.catalog.cat3.tolist()).items():
            entries[analyze(c3)] = (c3, 50.0 + math.log1p(n), "category")
        for b, n in Counter(e.catalog.brand.tolist()).items():
            entries[analyze(b)] = (b, 40.0 + math.log1p(n), "brand")
        keys = []
        for key, (disp, sc, typ) in entries.items():
            words = key.split()
            for j in range(len(words)):
                keys.append((" ".join(words[j:]), disp, sc - (0.5 * j), typ))
        keys.sort(key=lambda x: x[0])
        self._sug_keys = [k[0] for k in keys]
        self._sug = keys

    def suggest(self, prefix: str, principal: Principal = ANONYMOUS, k: int = 8) -> dict:
        """Type-ahead (v2.44 layout): searches people run, categories and brands, products, job kits, what's popular in
        the category the prefix lands in, and the account's own past purchases that match (shown first)."""
        t = time.perf_counter()
        e = self.e
        key = analyze(prefix)
        out = {"queries": [], "categories": [], "brands": [], "products": [], "kits": [], "popular": None,
               "buy_again": [], "orders": [], "completion": prefix.strip()}
        if key:
            lo = bisect.bisect_left(self._sug_keys, key)
            hi = bisect.bisect_left(self._sug_keys, key + "\uffff")
            seen = set()
            # suggestions that start with what was typed come before ones that only contain it ("copper" before
            # "1/2 type m copper"), then the more searched
            hits = sorted(self._sug[lo:min(hi, lo + 600)],
                          key=lambda x: (not analyze(x[1]).startswith(key), -x[2]))
            for _, disp, sc, typ in hits:
                if disp.lower() in seen:
                    continue
                seen.add(disp.lower())
                bucket = {"query": "queries", "category": "categories", "brand": "brands"}[typ]
                if len(out[bucket]) < k:
                    out[bucket].append(disp)
        hist = {}
        if principal.is_authenticated:
            words = normalize(prefix)
            if words:
                if principal.role == "tech":  # technicians: only what they bought themselves
                    uh = e.accounts.user_history(principal.user_id)
                    ap = {e.catalog.sku_to_idx[s]: (v["times"], v["qty"], 0) for s, v in uh.items()
                          if s in e.catalog.sku_to_idx}
                else:
                    ap = e.features.acct_prod.get(principal.account_id, {})
                top = sorted(ap.items(), key=lambda kv: -kv[1][0])[:600]
                for i, (n, q, d) in top:
                    tt = normalize(e.catalog.title[i])
                    if all(any(w.startswith(x) for w in tt) for x in words):
                        c = self._ta_card(int(i), principal)
                        c["times"] = int(n)
                        out["buy_again"].append(c)
                        hist[int(i)] = True
                        if len(out["buy_again"]) >= 6:
                            break
            digits = compact_id(prefix)
            if principal.can("orders") and digits.isdigit() and len(digits) >= 4:
                ids = [x for x in e.accounts._order_ids(principal) if str(x).startswith(digits)][:3]
                out["orders"] = [e.accounts.order_card(e.accounts.orders.loc[x].to_dict(), principal) for x in ids]
        # products: what the prefix completes to (the top suggestion when the last word is still being typed)
        toks = [w for w in normalize(prefix) if w]
        comp = prefix.strip()
        typed = " ".join(prefix.lower().split())
        starts = [d.lower() for d in out["queries"] + out["categories"] if d.lower().startswith(typed)]
        ext = Counter(re.match(re.escape(typed) + r"\S*", d).group(0) for d in starts)
        if ext and ext.most_common(1)[0][0] != typed and (not toks or not e.backend.term_exists(toks[-1])
                                                          or ext.most_common(1)[0][1] > ext.get(typed, 0)):
            # what's typed is the start of a longer word in what people search ("water heat" -> "water heater")
            comp = ext.most_common(1)[0][0]
            toks = [w for w in normalize(comp) if w]
        elif toks and not e.backend.term_exists(toks[-1]):
            # the word being typed: its most common completion among the suggestions ("copp" -> "copper")
            raw = prefix.strip().split()
            last = raw[-1].lower() if raw else ""
            words = Counter(w for d in out["queries"] + out["categories"] for w in d.lower().split()
                            if w.startswith(last) and len(w) > len(last))
            if words:
                comp = " ".join(raw[:-1] + [words.most_common(1)[0][0]])
            elif out["queries"]:
                comp = out["queries"][0]
            toks = [w for w in normalize(comp) if w]
        last_w = (comp.split() or [""])[-1]
        if comp and (any(c.isalpha() for c in last_w) or "/" in last_w) and \
                all(e.backend.term_exists(w) for w in normalize(comp) if w.isalpha()) and \
                comp.lower() not in (x.lower() for x in out["queries"]):
            out["queries"] = [comp] + out["queries"][:k - 1]          # what was typed (completed) comes first
        out["completion"] = comp
        if toks and len(prefix.strip()) >= 2 and principal.can("products"):
            try:
                idx, sc = e.backend.lexical(toks, 300, brand_tokens=set())
            except Exception:  # noqa: BLE001
                idx, sc = np.zeros(0, np.int64), np.zeros(0, np.float32)
            if len(idx):
                prior = getattr(e.retriever, "prior", None)
                need = set(toks)
                from webbsearch.retrieval.results import title_head
                full = np.array([need <= set(normalize(e.catalog.title[int(i)])) for i in idx])
                # the product the words name ("water heater": heaters, not water heater connectors)
                head = np.array([title_head(str(e.catalog.title[int(i)])) in need for i in idx])
                score = (0.3 * sc / max(float(sc.max()), 1e-6) + 1.0 * full + 0.8 * head
                         + (1.0 * prior[idx] if prior is not None else 0))
                order = idx[np.argsort(-score, kind="stable")]
                picked, per = [], Counter()          # at most two of one brand, unless the search names it
                named = set(normalize(comp))
                for i in order:
                    i = int(i)
                    b = str(e.catalog.brand[i])
                    if i in hist or (per[b] >= 2 and not set(normalize(b)) <= named):
                        continue
                    picked.append(i)
                    per[b] += 1
                    if len(picked) >= 7:
                        break
                out["products"] = [self._ta_card(i, principal) for i in picked]
                # the category most of the matches are in, and what sells most there among them
                fam = Counter(str(e.catalog.cat3[int(i)]) for i in order[:60])
                if fam:
                    c3, _ = fam.most_common(1)[0]
                    out["top_category"] = c3
                    out["_fam"] = fam
                    pool = [int(i) for i in idx if str(e.catalog.cat3[int(i)]) == c3 and int(i) not in hist
                            and int(i) not in set(picked[:3])]
                    if prior is not None:
                        pool.sort(key=lambda i: -float(prior[i]))
                    pc, pool_ = Counter(), []
                    for i in pool:                    # two of a brand at most here too
                        b = str(e.catalog.brand[i])
                        if pc[b] < 2 or set(normalize(b)) <= named:
                            pool_.append(i)
                            pc[b] += 1
                    pool = pool_
                    if len(pool) >= 2:
                        out["popular"] = {"category": c3, "q": c3,
                                          "items": [self._ta_card(i, principal) for i in pool[:4]]}
        if getattr(e, "kits", None) is not None and e.kits.ok and comp:
            try:
                out["kits"] = [{"kit_id": c["kit_id"], "name": c["name"], "variant": c.get("variant_label") or c.get("variant"),
                                "primary": c.get("primary"), "lines": c.get("lines")}
                               for c in e.kits.search(comp, k=5, min_score=0.45)]
            except Exception:  # noqa: BLE001
                out["kits"] = []
        # v2.46: the top suppliers for the category the search lands in, and the jump-to rows under Ask Webb
        # (categories, job kits, my orders, events, related content) with a count and a preview line each
        try:
            sc = self.ta_scopes
            digits = compact_id(prefix)
            # the category the words name ("ball valve" -> Ball Valves) over the one most matches sit in
            fam = out.pop("_fam", None) or Counter()
            cats = out.get("categories") or []
            sup_cat = max(cats, key=lambda c: (fam.get(c, 0), -cats.index(c))) if cats else out.get("top_category")
            out["suppliers"] = [] if (digits.isdigit() or not sup_cat) else sc.suppliers(sup_cat, prefix)
            out["scopes"] = sc.rows(out, principal, prefix) if key else {}
        except Exception:  # noqa: BLE001  (the type-ahead never fails over its extras)
            out.pop("_fam", None)
            out.setdefault("suppliers", [])
            out.setdefault("scopes", {})
        out["ms"] = _ms(t)
        return out

    @property
    def ta_scopes(self):
        if getattr(self, "_ta_scopes", None) is None:
            from webbsearch.retrieval.ta_scopes import TypeaheadScopes
            self._ta_scopes = TypeaheadScopes(self.e)
        return self._ta_scopes

    def suggest_scope(self, prefix: str, scope: str, principal: Principal = ANONYMOUS) -> dict:
        """The type-ahead's right column for one jump-to row (v2.46): what the row counted, with a View all."""
        from webbsearch.retrieval.ta_scopes import SCOPES
        if scope not in SCOPES:
            return {"scope": scope, "items": [], "error": "unknown scope"}
        s = self.suggest(prefix, principal)
        return self.ta_scopes.detail(scope, s, principal, prefix, lambda i: self._ta_card(i, principal))

    def _ta_card(self, i: int, p: Principal) -> dict:
        cat = self.e.catalog
        pr = self.e.accounts.price(p, i)
        a = cat.attrs(i)
        hb = self.e.accounts.history_badge(p, i) if p.is_authenticated else None
        return {"sku": str(cat.sku[i]), "title": str(cat.title[i]), "brand": str(cat.brand[i]),
                "mfr_part": str(cat.mfr_part[i]), "cat3": str(cat.cat3[i]), "family": str(cat.family[i]),
                "material": a.get("material"), "attrs": {"type": a.get("type", ""), "color": a.get("color", "")},
                "price": pr["price"], "price_basis": pr["basis"], "list_price": float(cat.list_price[i]),
                "uom": str(cat.uom[i]), "history": hb}


def _ms(t: float) -> float:
    return round((time.perf_counter() - t) * 1000, 2)
