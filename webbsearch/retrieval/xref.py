"""Cross-reference: any OEM or competitor part number -> the Webb item that replaces it.

Source: the Webb xRef lake (~/Developer/webb-crossref, `assets/field.sqlite`):
48,443 parts, 61,730 relationships, 4,388 of the parts are Webb-stocked items
with their Control # (= Webb item #). Each relationship carries a WMCR score and
band (Verified / Strong / Probable / Review / Weak), its type (competitive,
supersession, in-family), attribute-by-attribute comparisons and the sources
that asserted it.

Lookup uses the lake's own forgiving matcher (webbsearch/xref/matcher.py,
vendored from the xRef reference implementation: brand + part, O/0 and other
confusables, transpositions, one wrong character), so "wr 50a55843" and
"White-Rodgers 50A55-843" land on the same part. A Webb item # ("1192087",
"#1192087", "webb 1192087") lands on that item.

v1.1 adds, on the same lake:
  * `crosses()`      every cross for a part, paged, for "show all" in place
  * `item()`         a Webb item's detail: specs, the part numbers it replaces
                     (by brand, with bands), equivalents and demo branch stock
  * `equivalents()`  Webb items that can stand in for a Webb item: direct
                     Webb-to-Webb crosses first, then items that replace the
                     same OEM part (the evidence is named on each)
  * `oem_index()`    Webb item -> OEM numbers it replaces, for the search index
  * stock on every Webb card (webb_stock.DemoStock, labeled demo)

The lake is optional. It is found at $WEBB_XREF_DB, data/xref/field.sqlite, or
the sibling checkout ../webb-crossref/assets/field.sqlite; without it the
feature is simply off.
"""
from __future__ import annotations

import os
import re
import sqlite3
import threading
from collections import Counter, defaultdict
from pathlib import Path

from webbsearch.config import ROOT

SHOW_BANDS = ("Verified", "Strong", "Probable", "Review")   # Weak crosses are not offered as replacements
INDEX_BANDS = ("Verified", "Strong", "Probable")             # what a Webb item "replaces" on its page / in the index
BAND_RANK = {"Verified": 0, "Strong": 1, "Probable": 2, "Review": 3, "Weak": 4}
CONFIDENT = {"upc", "exact", "brand_part", "part_only", "skeleton", "webb_item"}
CLOSE = {"swap", "edit1"}
TYPE_LABEL = {"COMPETITIVE": "Competitive cross", "SUPERSESSION": "Supersedes", "IN_FAMILY": "Same family",
              "OEM": "OEM replacement", "UNIVERSAL": "Universal replacement", "DIRECT": "Direct replacement",
              "FUNCTIONAL": "Functional equivalent", "ALIAS": "Same part, other number"}
WEBB_ITEM_RE = re.compile(r"^\s*(?:(?:f\.?\s*w\.?\s*)?webb\s*(?:item|control)?\s*(?:#|no\.?|number)?\s*|#)?(\d{3,8})\s*$", re.I)
NOBRAND = re.compile(r"^\(?brand not stated\)?$", re.I)


def find_db() -> Path | None:
    # the full lake wins over the trimmed extract that ships in the coworker zip (scripts/make_xref_extract.py)
    for c in (os.environ.get("WEBB_XREF_DB"), ROOT / "data" / "xref" / "field.sqlite",
              ROOT.parent / "webb-crossref" / "assets" / "field.sqlite", ROOT / "config" / "xref" / "field.sqlite"):
        if c and Path(c).exists():
            return Path(c)
    return None


def _brand(b: str | None) -> str:
    b = (b or "").strip()
    return "" if NOBRAND.match(b) else b


class XRef:
    def __init__(self, path: Path | None = None):
        self.path = path or find_db()
        self.ok = self.path is not None
        self._local = threading.local()
        self.url_tpl = "https://www.fwwebb.com/product/{item}"
        self.stats = {}
        self.stock = None
        if self.ok:
            db = self._db()
            r = db.execute("select v from config where k='webb_product_url'").fetchone()
            if r:
                self.url_tpl = r[0]
            ex = db.execute("select v from config where k='extract'").fetchone()
            self.stats = {"source": "extract" if ex else "full lake",
                          "parts": db.execute("select count(*) from part").fetchone()[0],
                          "crosses": db.execute("select count(*) from cross_ref").fetchone()[0],
                          "webb_items": db.execute("select count(*) from part where webb_item<>''").fetchone()[0]}
            self.labels = {a: l for a, l in db.execute("select attr, label from spec_label")}

    def attach_stock(self, stock) -> None:
        """webb_stock.DemoStock (or, in production, the branch on-hand feed)."""
        self.stock = stock

    def _db(self):
        db = getattr(self._local, "db", None)
        if db is None:
            db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False)
            db.row_factory = sqlite3.Row
            from webbsearch.xref.matcher import Matcher
            self._local.db, self._local.m = db, Matcher(db)
        return db

    def _matcher(self):
        self._db()
        return self._local.m

    def _stock(self, item: str, cls: str, branch: str | None, k: int = 0) -> dict | None:
        return self.stock.summary(item, cls, branch, k=k) if self.stock else None

    # ------------------------------------------------------------ resolve a typed number to one lake part
    def webb_part(self, item: str):
        """The lake row for a Webb item # (the best-connected one when a Control # has two spellings)."""
        if not self.ok or not item:
            return None
        return self._db().execute("select * from part where webb_item=? order by ncross desc, id limit 1",
                                  (str(item).strip(),)).fetchone()

    def resolve(self, q: str) -> dict | None:
        """{row, how, why, close, ambiguous} for the part a query names, or None."""
        if not self.ok or not q or len(q) > 80:
            return None
        m = WEBB_ITEM_RE.match(q)
        if m:
            p = self.webb_part(m.group(1))
            if p:
                return {"row": p, "how": "webb_item", "why": "Webb item # matched", "close": False, "ambiguous": []}
        hits = self._matcher().search(q, limit=5)
        if not hits:
            return None
        top = hits[0]
        if top["how"] not in CONFIDENT | CLOSE or top["score"] < 70:
            return None
        p = self._db().execute("select * from part where id=?", (top["id"],)).fetchone()
        # an ambiguous bare number ("50") is several different parts: say so rather than guess
        same = [h for h in hits[1:] if h["how"] == top["how"] and h["score"] >= top["score"] - 3
                and h["part"].replace(" ", "") == top["part"].replace(" ", "")]
        return {"row": p, "how": top["how"], "why": top["why"], "close": top["how"] in CLOSE,
                "ambiguous": [{"brand": h["brand"], "part": h["part"]} for h in same][:3]}

    # ------------------------------------------------------------ lookup
    def lookup(self, q: str, limit: int = 6, branch: str | None = None) -> dict | None:
        """The part the query names and the Webb items that replace it, strongest evidence first."""
        r = self.resolve(q)
        if not r:
            return None
        p = r["row"]
        db = self._db()
        out = {"part": {"id": p["id"], "brand": _brand(p["brand"]), "part": p["part"], "cls": p["cls"],
                        "noun": p["noun"] or "", "how": r["how"], "why": r["why"], "close": r["close"]},
               "ambiguous": r["ambiguous"],
               "self": self._webb_card(p, branch) if p["webb_item"] else None, "replacements": [], "more": 0}
        rows = [x for x in self._webb_rows(p["id"]) if x["band"] in SHOW_BANDS and x["webb_item"] != p["webb_item"]]
        others = db.execute(
            """select p.brand, p.part, p.noun, p.cls, c.band, c.wmcr
                 from cross_ref c join part p on p.id = case when c.a = ? then c.b else c.a end
                where (c.a = ? or c.b = ?) and c.a <> c.b and (p.webb_item is null or p.webb_item = '')
                  and c.band in ('Verified','Strong','Probable')
                order by c.wmcr desc, p.ncross desc limit 4""", (p["id"], p["id"], p["id"])).fetchall()
        out["others"] = [dict(o) | {"brand": _brand(o["brand"])} for o in others]
        seen = set()
        for x in rows:
            if x["webb_item"] in seen:
                continue
            seen.add(x["webb_item"])
            if len(out["replacements"]) >= limit:
                out["more"] += 1
                continue
            out["replacements"].append(self._cross_card(x, flip=x["a"] != p["id"], branch=branch))
        out["total_crosses"] = db.execute("select count(*) from cross_ref where (a=? or b=?) and a<>b",
                                          (p["id"], p["id"])).fetchone()[0]
        # out-of-stock swaps: the strongest replacement may not be on this branch's shelf
        reps = out["replacements"]
        if reps and self.stock and not reps[0]["stock"]["at_branch"]:
            pick = next((c for c in reps[1:] if c["stock"]["at_branch"]), None)
            if pick:
                out["in_stock_pick"] = {"webb_item": pick["webb_item"], "instead_of": reps[0]["webb_item"]}
        if out["self"] and self.stock and not out["self"]["stock"]["at_branch"]:
            out["self"]["swaps"] = self.equivalents(p["webb_item"], branch, limit=3, in_stock_only=True)
        return out

    def _webb_rows(self, pid: int):
        return self._db().execute(
            """select c.id, c.wmcr, c.band, c.types, c.tier, c.a, c.cap, c.override,
                      p.id pid, p.brand, p.part, p.cls, p.noun, p.webb_item, p.mpn
                 from cross_ref c join part p on p.id = case when c.a = ? then c.b else c.a end
                where (c.a = ? or c.b = ?) and c.a <> c.b and p.webb_item <> ''
                order by c.wmcr desc,
                         case when exists (select 1 from spec_cmp s where s.rel = c.id) then 0 else 1 end,
                         p.brand, p.part""", (pid, pid, pid)).fetchall()

    def _webb_card(self, p, branch: str | None = None) -> dict:
        noun = p["noun"] or (p["cls"] or "").replace("_", " ")
        card = {"webb_item": p["webb_item"], "brand": _brand(p["brand"]), "part": p["part"], "noun": noun,
                "cls": p["cls"], "mpn": p["mpn"] or p["part"], "url": self.url_tpl.format(item=p["webb_item"]),
                "sku": "webb-" + p["webb_item"]}
        card["title"] = " ".join(x for x in (card["brand"], card["part"], noun) if x)
        card["stock"] = self._stock(p["webb_item"], p["cls"], branch)
        card["img"] = f"/api/img/webb/{p['webb_item']}" if self._has_photo(p["webb_item"]) else None
        return card

    def _has_photo(self, item: str) -> bool:
        """fwwebb.com's product photo, fetched into data/images/webb/ for photo search (v1.6)."""
        if getattr(self, "_photos", None) is None:
            from webbsearch.vision.index import images_dir
            d = images_dir()
            self._photos = {f.stem for f in d.glob("*.jpg")} if d.exists() else set()
        return str(item) in self._photos

    def _cross_card(self, r, flip: bool, branch: str | None = None) -> dict:
        db = self._db()
        specs = db.execute("select attr, lval, rval, verdict, note from spec_cmp where rel=? "
                           "order by case verdict when 'mismatch' then 0 when 'form' then 1 when 'uprate' then 2 "
                           "when 'unknown' then 3 else 4 end, attr", (r["id"],)).fetchall()
        rows = []
        for s in specs:
            if s["attr"].startswith("_") or self.labels.get(s["attr"], s["attr"]) is None:
                continue
            left, right = (s["rval"], s["lval"]) if flip else (s["lval"], s["rval"])
            rows.append({"attr": self.labels.get(s["attr"]) or s["attr"].replace("_", " "), "yours": left,
                         "webb": right, "verdict": s["verdict"], "note": s["note"] or ""})
        agree = sum(1 for s in rows if s["verdict"] in ("match", "equal", "same", "ok", "agree"))
        differ = [s for s in rows if s["verdict"] in ("mismatch", "form")]
        src = db.execute("select label, url, tier from src where rel=? order by tier limit 2", (r["id"],)).fetchall()
        return {**self._webb_card(r, branch), "wmcr": r["wmcr"], "band": r["band"], "tier": r["tier"],
                "types": self._types(r["types"]),
                "specs": rows[:6], "agree": agree, "compared": len(rows), "differ": differ[:3],
                "note": r["override"] or r["cap"] or "",
                "sources": [{"label": s["label"], "url": s["url"]} for s in src]}

    @staticmethod
    def _types(t: str | None) -> list[str]:
        return [TYPE_LABEL.get(x, x.replace("_", " ").title()) for x in (t or "").split(";") if x]

    # ------------------------------------------------------------ every cross for a part ("show all", in place)
    def crosses(self, pid: int, branch: str | None = None, offset: int = 0, limit: int = 50) -> dict:
        """All Webb items that cross to a part (Review or better), then the non-Webb crosses, paged."""
        if not self.ok:
            return {"webb": [], "others": [], "total_webb": 0, "total_others": 0, "weak": 0}
        db = self._db()
        p = db.execute("select * from part where id=?", (pid,)).fetchone()
        if p is None:
            return {"webb": [], "others": [], "total_webb": 0, "total_others": 0, "weak": 0}
        seen, webb, weak = set(), [], 0
        for x in self._webb_rows(pid):
            if x["webb_item"] == p["webb_item"] or x["webb_item"] in seen:
                continue
            if x["band"] not in SHOW_BANDS:
                weak += 1
                continue
            seen.add(x["webb_item"])
            webb.append(x)
        page = [self._cross_card(x, flip=x["a"] != pid, branch=branch) for x in webb[offset:offset + limit]]
        others = db.execute(
            """select p.brand, p.part, p.noun, p.cls, c.band, c.wmcr, c.types
                 from cross_ref c join part p on p.id = case when c.a = ? then c.b else c.a end
                where (c.a = ? or c.b = ?) and c.a <> c.b and (p.webb_item is null or p.webb_item = '')
                order by c.wmcr desc, p.brand, p.part""", (pid, pid, pid)).fetchall()
        return {"part": {"id": pid, "brand": _brand(p["brand"]), "part": p["part"]},
                "webb": page, "total_webb": len(webb), "offset": offset, "weak": weak,
                "others": [{"brand": _brand(o["brand"]), "part": o["part"], "noun": o["noun"] or (o["cls"] or "").replace("_", " "),
                            "band": o["band"], "wmcr": o["wmcr"], "types": self._types(o["types"])}
                           for o in others[:200]],
                "total_others": len(others)}

    # ------------------------------------------------------------ equivalents (out-of-stock swaps)
    def equivalents(self, webb_item: str, branch: str | None = None, limit: int = 6,
                    in_stock_only: bool = False) -> list[dict]:
        """Webb items that can stand in for this one.

        Direct: a Webb-to-Webb cross in the lake (Review or better), scored by its own WMCR.
        Shared: both Webb items replace the same OEM part at Probable or better, same part class;
        scored by the weaker of the two crosses, and the shared OEM part is named so the counter
        can check it. Direct evidence ranks first."""
        if not self.ok:
            return []
        db = self._db()
        ids = [r["id"] for r in db.execute("select id from part where webb_item=?", (str(webb_item),))]
        if not ids:
            return []
        me = db.execute("select * from part where id=?", (ids[0],)).fetchone()
        cands: dict[str, dict] = {}
        for pid in ids:
            for x in self._webb_rows(pid):
                if x["webb_item"] == webb_item or x["band"] not in SHOW_BANDS:
                    continue
                c = cands.get(x["webb_item"])
                if c is None or (c["kind"] == "direct" and x["wmcr"] > c["score"]):
                    cands[x["webb_item"]] = {"row": x, "kind": "direct", "score": x["wmcr"], "band": x["band"],
                                             "types": self._types(x["types"]), "via": None, "shared": 0}
        # shared OEM parts
        q = """select c.id rel, c.wmcr, o.id oid, o.brand obrand, o.part opart
                 from cross_ref c join part o on o.id = case when c.a = ? then c.b else c.a end
                where (c.a = ? or c.b = ?) and c.a <> c.b and (o.webb_item is null or o.webb_item = '')
                  and c.band in ('Verified','Strong','Probable')"""
        oems = [r for pid in ids for r in db.execute(q, (pid, pid, pid))]
        shared: dict[str, dict] = {}
        for o in oems[:400]:
            for x in self._webb_rows(o["oid"]):
                if x["webb_item"] == webb_item or x["band"] not in INDEX_BANDS or x["cls"] != me["cls"]:
                    continue
                s = min(o["wmcr"], x["wmcr"])
                cur = shared.setdefault(x["webb_item"], {"row": x, "score": s, "n": 0, "via": None, "vs": -1})
                cur["n"] += 1
                if s > cur["vs"]:
                    cur["vs"], cur["via"] = s, f"{_brand(o['obrand'])} {o['opart']}".strip()
                cur["score"] = max(cur["score"], s)
        for wi, s in shared.items():
            if wi in cands:
                cands[wi]["shared"] = s["n"]
                continue
            cands[wi] = {"row": s["row"], "kind": "shared", "score": s["score"], "band": None, "types": [],
                         "via": s["via"], "shared": s["n"]}
        ranked = sorted(cands.values(), key=lambda c: (c["kind"] != "direct", -(c["shared"] if c["kind"] == "shared" else 0),
                                                       -c["score"], c["row"]["brand"] or "", c["row"]["part"]))
        out = []
        for c in ranked:
            card = self._webb_card(c["row"], branch)
            if in_stock_only and not (card["stock"] and card["stock"]["at_branch"]):
                continue
            if c["kind"] == "direct":
                card["how"] = f"Direct cross · {c['band']} {c['score']}"
            else:
                card["how"] = (f"Both replace {c['via']}" + (f" and {c['shared'] - 1} more part number{'s' if c['shared'] > 2 else ''}"
                                                              if c["shared"] > 1 else ""))
            card.update({"kind": c["kind"], "score": c["score"], "band": c["band"], "types": c["types"],
                         "shared": c["shared"], "via": c["via"]})
            out.append(card)
            if len(out) >= limit:
                break
        return out

    # ------------------------------------------------------------ Webb item detail (drawer / product page)
    def item(self, webb_item: str, branch: str | None = None) -> dict | None:
        if not self.ok:
            return None
        db = self._db()
        rows = db.execute("select * from part where webb_item=? order by ncross desc, id", (str(webb_item),)).fetchall()
        if not rows:
            return None
        p = rows[0]
        ids = [r["id"] for r in rows]
        card = self._webb_card(p, None)
        card["stock"] = self._stock(p["webb_item"], p["cls"], branch, k=6)
        card["line"] = p["webb_line"] or ""
        card["upc"] = p["upc"] or ""
        card["aliases"] = [f"{_brand(r['brand'])} {r['part']}".strip() for r in rows[1:]]
        specs, seen = [], set()
        for s in db.execute(f"select attr, val from part_spec where part in ({','.join('?' * len(ids))})", ids):
            lab = self.labels.get(s["attr"], s["attr"].replace("_", " ").capitalize())
            if s["attr"].startswith("_") or lab is None or s["attr"] in seen or not s["val"]:
                continue
            seen.add(s["attr"])
            specs.append({"attr": lab, "val": s["val"]})
        card["specs"] = specs
        rep = self.replaces(webb_item, bands=SHOW_BANDS)
        card["replaces"] = rep
        card["equivalents"] = self.equivalents(webb_item, branch, limit=6)
        # where the evidence came from, across all of this item's crosses
        srcs = Counter()
        for pid in ids:
            for s in db.execute("""select s.label from src s join cross_ref c on c.id = s.rel
                                    where (c.a = ? or c.b = ?) and c.a <> c.b""", (pid, pid)):
                srcs[s["label"]] += 1
        card["sources"] = [{"label": k, "n": n} for k, n in srcs.most_common(5)]
        # what the search index and the product page would carry for this item (use 4)
        idx = [x for g in rep["by_brand"] for x in g["parts"] if x["band"] in INDEX_BANDS]
        card["index_terms"] = {"n": len(idx), "sample": [f"{x['brand']} {x['part']}".strip() for x in idx[:12]]}
        return card

    def replaces(self, webb_item: str, bands=INDEX_BANDS) -> dict:
        """The non-Webb part numbers a Webb item replaces, grouped by brand."""
        db = self._db()
        ids = [r["id"] for r in db.execute("select id from part where webb_item=?", (str(webb_item),))]
        best: dict[int, dict] = {}
        for pid in ids:
            for o in db.execute(
                    """select o.id, o.brand, o.part, o.noun, o.cls, c.band, c.wmcr, c.types
                         from cross_ref c join part o on o.id = case when c.a = ? then c.b else c.a end
                        where (c.a = ? or c.b = ?) and c.a <> c.b and (o.webb_item is null or o.webb_item = '')""",
                    (pid, pid, pid)):
                if o["band"] not in bands:
                    continue
                if o["id"] not in best or o["wmcr"] > best[o["id"]]["wmcr"]:
                    best[o["id"]] = {"brand": _brand(o["brand"]), "part": o["part"], "band": o["band"],
                                     "wmcr": o["wmcr"], "types": self._types(o["types"])}
        groups = defaultdict(list)
        for x in best.values():
            groups[x["brand"] or "Brand not stated"].append(x)
        by_brand = [{"brand": b, "n": len(xs), "parts": sorted(xs, key=lambda x: (BAND_RANK[x["band"]], -x["wmcr"], x["part"]))}
                    for b, xs in groups.items()]
        by_brand.sort(key=lambda g: (-g["n"], g["brand"]))
        bands_n = Counter(x["band"] for x in best.values())
        idx = [x for x in best.values() if x["band"] in INDEX_BANDS]
        return {"total": len(best), "bands": {b: bands_n.get(b, 0) for b in SHOW_BANDS if bands_n.get(b)},
                "brands": len(by_brand), "by_brand": by_brand,
                # "replaces" as the index and Ask count it: Probable or better
                "indexed": len(idx), "indexed_brands": len({x["brand"] or "Brand not stated" for x in idx})}

    def oem_index(self):
        """Yield one record per Webb item: the OEM numbers it replaces (Probable or better).
        This is what goes into the `replaces_*` fields of the products index (deploy/opensearch)."""
        db = self._db()
        for (wi,) in db.execute("select distinct webb_item from part where webb_item<>'' order by webb_item").fetchall():
            rep = self.replaces(wi)
            if not rep["total"]:
                continue
            parts = [x for g in rep["by_brand"] for x in g["parts"]]
            yield {"webb_item": wi,
                   "replaces_pn": sorted({re.sub(r"[^A-Z0-9]", "", x["part"].upper()) for x in parts}),
                   "replaces_brand_pn": sorted({f"{x['brand']} {x['part']}".strip() for x in parts}),
                   "replaces_brand": sorted({x["brand"] for x in parts if x["brand"]}),
                   "replaces_count": rep["total"],
                   "replaces_best_band": min((x["band"] for x in parts), key=lambda b: BAND_RANK[b])}

    # ------------------------------------------------------------ coverage (truck-stock idea)
    def coverage(self, cls: str | None = None, top: int = 10) -> list[dict]:
        """Webb items ranked by how many other part numbers they replace (Probable or better)."""
        if not self.ok:
            return []
        db = self._db()
        q = """with x as (select case when pa.webb_item<>'' then pa.id else pb.id end w,
                                 case when pa.webb_item<>'' then pb.id else pa.id end o
                            from cross_ref c join part pa on pa.id=c.a join part pb on pb.id=c.b
                           where (pa.webb_item<>'') <> (pb.webb_item<>'') and c.band in ('Verified','Strong','Probable'))
               select p.webb_item, p.brand, p.part, p.noun, p.cls, count(distinct x.o) n
                 from x join part p on p.id=x.w {w} group by x.w order by n desc limit ?"""
        rows = db.execute(q.format(w="where p.cls = ?" if cls else ""), ((cls, top) if cls else (top,))).fetchall()
        return [dict(r) | {"url": self.url_tpl.format(item=r["webb_item"])} for r in rows]


_MODEL = re.compile(r"(?=[\w-]*\d)[A-Za-z0-9][\w-]{2,}")


def summary(r: dict) -> str:
    """One paragraph for the Ask panel and the page notice."""
    p = r["part"]
    name = f"{p['brand']} {p['part']}".strip()
    what = f" ({p['noun'] or p['cls'].replace('_', ' ')})"
    if r["self"]:
        s = f"{name}{what} is a Webb stock item, #{r['self']['webb_item']}."
        if r["replacements"]:
            n = len(r["replacements"]) + r["more"]
            s += f" The cross-reference also lists {n} Webb-stocked equivalent{'s' if n > 1 else ''}."
        return s
    if r["replacements"]:
        top = r["replacements"][0]
        n = len(r["replacements"]) + r["more"]
        return (f"{name}{what} isn't a Webb stock item. The cross-reference has {n} "
                f"Webb replacement{'s' if n > 1 else ''}; the strongest is "
                f"{top['brand']} {top['part']} (#{top['webb_item']}), {top['band'].lower()} match, score {top['wmcr']}.")
    s = f"{name}{what} is in the cross-reference, but no Webb-stocked part replaces it"
    if r.get("others"):
        s += f"; it crosses to {', '.join((o['brand'] + ' ' + o['part']).strip() for o in r['others'][:2])}, which Webb doesn't stock"
    return s + "."


# pressure classes, schedules, ASTM grades, lengths: "150LB", "300#", "SCH80", "A106", "10'6\"" describe an item
SPEC_WORD = re.compile(r"\d{2,4}(?:lbs?|#|wog|psi|4b)|s(?:ch)?\d{2,3}|[ab]\d{2,3}|\d{1,2}'\d{0,2}\"?", re.I)


def worth_looking_up(q: str) -> bool:
    """Only part-number-shaped queries go to the lake: "3/4 ball valve" is not a part number."""
    toks = q.split()
    if not toks or len(toks) > 5:
        return False
    units = {"btu", "btuh", "mbh", "gal", "gallon", "gallons", "gpm", "gph", "hp", "ton", "tons", "psi", "cfm", "v", "volt",
             "volts", "watt", "watts", "w", "amp", "amps", "ft", "feet", "lb", "lbs", "seer", "seer2", "mfd", "va", "rpm"}
    for k, t in enumerate(toks):
        t = t.strip(",;:()[]#")
        # a plain number before a unit is a rating, not a part number ("18000 btu mini split", v2.42)
        if t.isdigit() and k + 1 < len(toks) and toks[k + 1].strip(",.").lower() in units:
            continue
        if "/" in t or re.fullmatch(r"\d{1,3}(?:[.,]\d+)?", t) or SPEC_WORD.fullmatch(t):
            continue
        # a range is a spec: "pressure gauge 0-100", "25-75 psi" (v2.44); "36142-07" counts down, a part number
        rg = re.fullmatch(r"(\d{1,4})-(\d{1,4})", t)
        if rg and int(rg.group(1)) < int(rg.group(2)):
            continue
        if _MODEL.fullmatch(t) and any(c.isdigit() for c in t) and len(re.sub(r"\W", "", t)) >= 4:
            return True
    return False
