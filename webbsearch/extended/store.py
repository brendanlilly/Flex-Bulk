"""Serving side of the Extended Catalog: a built set of -EC items, opened read-only and memory-mapped.

Nothing per item is loaded into RAM: the numeric columns and the strings are memory-mapped files and the search index is
on disk, so 10M items cost page cache, not process memory, and the 750k-item engine is untouched. A search reads only
the rows it returns."""
from __future__ import annotations

import json
import mmap
from pathlib import Path

import numpy as np

from webbsearch.extended.build import COLS, FLAG_PUBLISHED, _schema, part_pieces
from webbsearch.retrieval.analyzer import analyze, compact_id, looks_like_part_number, word_spans

FIELDS = {"title": 3.0, "brand": 2.0, "cat": 1.5, "syn": 0.9, "ids": 8.0, "pn": 1.2}
NO_BRAND = {f: b for f, b in FIELDS.items() if f != "brand"}


class ECStore:
    def __init__(self, path: Path):
        import tantivy
        self.path = Path(path)
        self.man = json.loads((self.path / "manifest.json").read_text())
        self.n = int(self.man["n"])
        self.cols = {c: np.load(self.path / "cols" / f"{c}.npy", mmap_mode="r") for c in COLS}
        self.off = np.load(self.path / "cols" / "str_off.npy", mmap_mode="r")
        self.families = self.man["families"]
        self.brands = self.man["brands"]
        self.schema = _schema()
        self.shards = []
        for s in self.man["shards"]:
            k, (a, b) = s["k"], s["rows"]
            f = open(self.path / f"strings-{k}.bin", "rb")
            blob = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) if s["bytes"] else b""
            ix = tantivy.Index(self.schema, path=str(self.path / f"index-{k}"))
            self.shards.append({"k": k, "a": a, "b": b, "bytes": s["bytes"], "blob": blob, "file": f,
                                "searcher": ix.searcher()})
        self._bounds = np.array([s["b"] for s in self.shards])

    # ---------------------------------------------------------------- rows
    def _shard_of(self, row: int) -> dict:
        return self.shards[int(np.searchsorted(self._bounds, row, side="right"))]

    def strings(self, row: int) -> tuple[str, str, str]:
        s = self._shard_of(row)
        a = int(self.off[row])
        b = s["bytes"] if row + 1 >= s["b"] else int(self.off[row + 1])
        sku, part, title = bytes(s["blob"][a:b]).decode().split("\x1f")
        return sku, part, title

    def row(self, row: int) -> dict:
        c = {k: v[row].item() for k, v in self.cols.items()}
        sku, part, title = self.strings(row)
        return c | {"row": int(row), "sku": sku, "part": part, "title": title,
                    "brand_name": self.brands[c["brand"]], "family": self.families[c["fam"]]}

    def published(self, row: int) -> bool:
        return bool(self.cols["flags"][row] & FLAG_PUBLISHED)

    def by_sku(self, sku: str) -> int | None:
        from tantivy import Query
        key = str(sku or "").strip().upper()
        if not key.endswith("-EC"):
            return None
        for s in self.shards:
            se = s["searcher"]
            res = se.search(Query.term_query(self.schema, "sku", key), 1)
            if res.hits:
                return int(se.doc(res.hits[0][1])["row"][0])
        return None

    # -------------------------------------------------------------- search
    def _token_query(self, tok: str, brand_tokens: set | None):
        from tantivy import Query
        fields = FIELDS if brand_tokens is None or tok in brand_tokens else NO_BRAND
        subs = [Query.boost_query(Query.term_query(self.schema, f, tok), b) for f, b in fields.items()]
        if len(tok) >= 5 and looks_like_part_number(tok) and tok.isalnum():
            # the start of a longer part number (RTH9585WF finds RTH9585WF1004)
            subs.append(Query.boost_query(Query.regex_query(self.schema, "ids", tok + "[a-z0-9]{1,10}"), 6.0))
        return Query.disjunction_max_query(subs, tie_breaker=0.25)

    def query(self, tokens: list[str], id_tokens: list[str] | None, brand_tokens: set | None, require_all: bool):
        from tantivy import Occur, Query
        occ = Occur.Must if require_all else Occur.Should
        clauses = []
        for g in word_spans(list(dict.fromkeys(tokens))):
            if len(g) == 1:
                clauses.append((occ, self._token_query(g[0], brand_tokens)))
            else:            # a hyphenated word: its parts or its joined form
                parts = Query.boolean_query([(Occur.Must, self._token_query(t, brand_tokens)) for t in g[:-1]])
                clauses.append((occ, Query.disjunction_max_query([parts, self._token_query(g[-1], brand_tokens)],
                                                                 tie_breaker=0.0)))
        q = Query.boolean_query(clauses) if clauses else None
        ids = [(Occur.Should, Query.boost_query(Query.term_query(self.schema, "ids", t.lower()), 20.0))
               for t in id_tokens or [] if t.isalnum()]
        if ids:
            q = Query.boolean_query(ids + ([(Occur.Should, q)] if q is not None else []))
        return q

    def search(self, tokens: list[str], id_tokens: list[str] | None = None, brand_tokens: set | None = None,
               k: int = 60) -> list[tuple[int, float]]:
        """Rows and BM25 scores, best first, across shards. Every word must match; if that finds nothing, the best
        partial matches (the caller checks how much of the search each one covers)."""
        out: list[tuple[float, int]] = []
        for require_all in (True, False):
            q = self.query(tokens, id_tokens, brand_tokens, require_all)
            if q is None:
                return []
            for s in self.shards:
                se = s["searcher"]
                res = se.search(q, k)
                out.extend((float(sc), int(se.doc(a)["row"][0])) for sc, a in res.hits)
            if out or not tokens or len(tokens) == 1:
                break
        out.sort(key=lambda x: -x[0])
        seen, rows = set(), []
        for sc, r in out:
            if r not in seen:
                seen.add(r)
                rows.append((r, sc))
        return rows[:k]

    def coverage(self, row: int, groups: list[set]) -> float:
        """Share of the searched words (word groups) this item's title, brand or category carries."""
        if not groups:
            return 1.0
        sku, part, title = self.strings(row)
        have = set(analyze(f"{title} {self.brands[int(self.cols['brand'][row])]}").split())
        have |= {compact_id(part).lower()} | set(part_pieces(part).split())
        def hit(g):
            return bool(g & have) or any(len(t) >= 5 and any(h.startswith(t) for h in have) for t in g)
        return sum(1 for g in groups if hit(g)) / len(groups)

    def close(self):
        for s in self.shards:
            try:
                if s["blob"]:
                    s["blob"].close()
                s["file"].close()
            except Exception:  # noqa: BLE001
                pass
