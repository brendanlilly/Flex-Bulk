"""Type-ahead scopes (v2.46): the "jump to" rows under Ask Webb and what opens on the right when one is chosen.

    Categories · Job kits · My orders · Events · Related content

suggest() puts a count and a preview line on each row (only rows with something behind them); /api/suggest/scope
returns the right column's results for one row, with a "View all" target. Also the top suppliers for the category a
search lands in ("ball valve by Apollo · NIBCO · Watts"): the category's best sellers by order dollars, unless the
merchandising settings pin a list for the category (`supplier_pins`: {category: [brands]}; no dashboard page yet).

A row's words match the way past purchases do in the type-ahead: every word typed is the start of a word in the title
("ball valv" matches "Ball Valve"), so counts stay exact and cheap (no full search behind the box).
"""
from __future__ import annotations

from collections import Counter

import numpy as np
import pandas as pd

from webbsearch.config import settings
from webbsearch.retrieval.analyzer import normalize

SCOPES = ("categories", "kits", "orders", "events", "content")
SCOPE_TITLE = {"categories": "Categories", "kits": "Job kits", "orders": "Your orders", "events": "Events",
               "content": "Related content"}
CONTENT_LABEL = {"how_to": "How-to", "buying_guide": "Buying guide", "troubleshooting": "Troubleshooting",
                 "article": "Article", "spec_sheet": "Spec sheet", "install_manual": "Install manual", "faq": "FAQ",
                 "promo": "Promo", "brand": "Brand", "branch": "Branch", "event": "Event"}
CONTENT_PLURAL = {"how_to": "how-tos", "buying_guide": "buying guides", "troubleshooting": "troubleshooting",
                  "article": "articles", "spec_sheet": "spec sheets", "install_manual": "install manuals", "faq": "FAQs",
                  "promo": "promos", "brand": "brand pages"}
# content shown under Related content: what a buyer reads, not branch pages
CONTENT_KINDS = ("how_to", "buying_guide", "troubleshooting", "article", "spec_sheet", "install_manual", "faq",
                 "promo", "brand")


def words_match(words: tuple, title_words) -> bool:
    return bool(words) and all(any(w.startswith(x) for w in title_words) for x in words)


class TypeaheadScopes:
    def __init__(self, engine):
        self.e = engine
        self._ready = False

    # ------------------------------------------------------------------ data, built on first use
    def _build(self):
        if self._ready:
            return
        cat = self.e.catalog
        cat3 = pd.Series(cat.cat3, dtype=object)
        self.cat_count = Counter(cat3.tolist())
        self.brand_words = {normalize(str(b)) for b in set(cat.brand)}
        lines = self.e.accounts.lines
        idx = lines["sku"].map(cat.sku_to_idx)
        ok = idx.notna()
        li = idx[ok].astype(int).to_numpy()
        df = pd.DataFrame({"i": li, "cat3": np.asarray(cat.cat3, dtype=object)[li],
                           "brand": np.asarray(cat.brand, dtype=object)[li],
                           "usd": lines.loc[ok, "ext_price"].to_numpy(dtype=float),
                           "qty": lines.loc[ok, "qty"].to_numpy(dtype=float)})
        # brands by order dollars within each category; best sellers by quantity
        b = df.groupby(["cat3", "brand"])["usd"].sum().reset_index().sort_values(["cat3", "usd"], ascending=[True, False])
        self.cat_brands = {c: g["brand"].tolist() for c, g in b.groupby("cat3", sort=False)}
        s = df.groupby(["cat3", "i"])["qty"].sum().reset_index().sort_values(["cat3", "qty"], ascending=[True, False])
        self.cat_top = {c: g["i"].head(12).tolist() for c, g in s.groupby("cat3", sort=False)}
        c = pd.read_parquet(settings.canonical_dir / "content.parquet")
        c["w"] = [normalize(t) for t in c["title"]]
        self.content = c
        self._title_words: dict[int, tuple] = {}
        self._acct_lines: dict[str, pd.DataFrame] = {}
        self._ready = True

    def title_words(self, i: int) -> tuple:
        w = self._title_words.get(i)
        if w is None:
            w = self._title_words[i] = normalize(self.e.catalog.title[i])
        return w

    # ------------------------------------------------------------------ suppliers
    def suppliers(self, category: str | None, prefix: str, k: int = 3) -> list[str]:
        """Top suppliers for the category a search lands in; none when the search already names a brand."""
        self._build()
        if not category:
            return []
        typed = set(normalize(prefix))
        merch = getattr(self.e, "merch", None)
        pins = ((merch.get() if merch else {}) or {}).get("supplier_pins", {}) or {}
        pool = list(pins.get(category) or []) + [b for b in self.cat_brands.get(category, []) if b not in (pins.get(category) or [])]
        if any(bw and set(bw) <= typed for bw in self.brand_words):     # the search already names a brand
            return []
        return pool[:k]

    # ------------------------------------------------------------------ orders
    def _lines_for(self, p) -> pd.DataFrame:
        key = f"{p.account_id}|{p.user_id if p.role == 'tech' else ''}"
        d = self._acct_lines.get(key)
        if d is None:
            acc = self.e.accounts
            ids = set(acc._order_ids(p))
            lo = acc._lo[acc._lo["order_id"].isin(ids)]
            lo = lo.assign(i=lo["sku"].map(self.e.catalog.sku_to_idx)).dropna(subset=["i"])
            d = self._acct_lines[key] = lo.assign(i=lo["i"].astype(int))
        return d

    def orders(self, p, prefix: str) -> list[dict]:
        """The account's orders with a line matching what's typed, newest first: order id, date, matched titles."""
        self._build()
        if not (p.is_authenticated and p.can("orders")):
            return []
        words = normalize(prefix)
        if not words:
            return []
        lo = self._lines_for(p)
        hit = lo[[words_match(words, self.title_words(int(i))) for i in lo["i"].to_numpy()]] if len(lo) else lo
        if hit.empty:
            return []
        out = []
        for oid, g in hit.sort_values("order_date", ascending=False).groupby("order_id", sort=False):
            out.append({"order_id": oid, "titles": [self.e.catalog.title[int(i)] for i in g["i"].tolist()]})
        order = {o: d for o, d in zip(hit["order_id"], hit["order_date"])}
        out.sort(key=lambda r: order[r["order_id"]], reverse=True)
        return out

    # ------------------------------------------------------------------ content and events
    def content_rows(self, prefix: str, events: bool) -> pd.DataFrame:
        self._build()
        words = normalize(prefix)
        if not words:
            return self.content.iloc[0:0]
        c = self.content
        c = c[c["content_type"] == "event"] if events else c[c["content_type"].isin(CONTENT_KINDS)]
        m = [words_match(words, w) for w in c["w"]]
        return c[m]

    # ------------------------------------------------------------------ the rows under Ask Webb
    def rows(self, s: dict, p, prefix: str) -> dict:
        """Counts and a preview line per scope, for suggest(). Only scopes with something behind them."""
        self._build()
        out = {}
        cats = list(dict.fromkeys(s.get("categories") or ([s["top_category"]] if s.get("top_category") else [])))
        if cats:
            out["categories"] = {"n": len(cats), "sub": " · ".join(cats[:2])}
        if s.get("kits"):
            out["kits"] = {"n": len(s["kits"]), "sub": s["kits"][0]["name"]}
        o = self.orders(p, prefix)
        if o:
            r = self.e.accounts.orders.loc[o[0]["order_id"]]
            d = pd.Timestamp(r["order_date"])
            job = str(r["job_name"] or "").strip()
            out["orders"] = {"n": len(o), "sub": f"Last: {d:%b} {d.day}" + (f" · {job}" if job and job != "nan" else "")}
        ev = self.content_rows(prefix, events=True)
        if len(ev):
            out["events"] = {"n": int(len(ev)), "sub": str(ev.iloc[0]["title"]).replace(" training at ", " training · ")}
        co = self.content_rows(prefix, events=False)
        if len(co):
            kinds = [CONTENT_PLURAL.get(k, k) for k in co["content_type"].value_counts().index[:3]]
            sub = ", ".join(kinds)
            out["content"] = {"n": int(len(co)), "sub": sub[:1].upper() + sub[1:]}
        return out

    # ------------------------------------------------------------------ the right column for one scope
    def detail(self, scope: str, s: dict, p, prefix: str, card) -> dict:
        self._build()
        comp = s.get("completion") or prefix
        res = {"scope": scope, "title": SCOPE_TITLE.get(scope, scope), "items": [], "view_all": None}
        if scope == "categories":
            cats = list(dict.fromkeys(s.get("categories") or ([s["top_category"]] if s.get("top_category") else [])))
            res["items"] = [{"kind": "category", "name": c, "count": int(self.cat_count.get(c, 0))} for c in cats]
            if cats:
                top = [i for i in self.cat_top.get(cats[0], [])][:5]
                res["top"] = {"category": cats[0], "items": [card(int(i)) for i in top]}
                res["view_all"] = {"label": f"View all {self.cat_count.get(cats[0], 0):,}", "q": cats[0], "tab": "products"}
        elif scope == "kits":
            kits = s.get("kits") or []
            if kits and getattr(self.e, "kits", None) is not None:
                d = self.e.kits.detail(kits[0]["kit_id"])
                if d:
                    res["kit"] = {"kit_id": d["kit_id"], "name": d["name"], "variant": d.get("variant"),
                                  "brand": d.get("brand"), "lines": d.get("lines"), "total": d.get("total"),
                                  "items": [{"qty": l.get("qty"), "desc": l.get("desc"), "mpn": l.get("mpn")}
                                            for l in d.get("items", [])][:8]}
            res["items"] = [{"kind": "kit", "kit_id": k["kit_id"], "name": k["name"], "lines": k.get("lines")} for k in kits]
            res["view_all"] = {"label": "All job kits", "q": comp, "tab": "kits"}
        elif scope == "orders":
            o = self.orders(p, prefix)
            acc = self.e.accounts
            for r in o[:5]:
                c = acc.order_card(acc.orders.loc[r["order_id"]].to_dict(), p)
                more = len(r["titles"]) - 1
                c["match"] = r["titles"][0] + (f" + {more} more" if more > 0 else "")
                res["items"].append(c)
            res["n"] = len(o)
            res["view_all"] = {"label": f"View all {len(o)}", "q": comp, "tab": "orders"}
        elif scope in ("events", "content"):
            rows = self.content_rows(prefix, events=scope == "events")
            for r in rows.head(7).itertuples():
                res["items"].append({"kind": r.content_type, "label": CONTENT_LABEL.get(r.content_type, r.content_type),
                                     "content_id": r.content_id, "title": r.title, "url": r.url,
                                     "snippet": str(r.body)[:160]})
            res["n"] = int(len(rows))
            res["view_all"] = {"label": f"View all {len(rows)}", "q": comp, "tab": "content"}
        return res
