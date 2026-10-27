"""The Extended Catalog, as the engine sees it (v2.3).

  coded_adjust()  availability tiers move coded results (tiers 1-2); called by results() on the candidate pool
  coded_tiers()   tier, label and delivery estimate for coded items on a page
  section()       the Extended Catalog section for a search: -EC items, after the coded results, from their own
                  index, in their own request (/api/extended) so the coded results never wait for it
  product()       one -EC item (quick view, cart, checkout)

The -EC build lives at <data>/extended/builds/<name> with <data>/extended/CURRENT naming the live one. A missing build
never blocks search: the section says "not available" and coded search runs as before."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

from webbsearch.config import settings
from webbsearch.extended import gaps, ranking
from webbsearch.extended.branchmap import BranchMap
from webbsearch.extended.build import FLAG_COMPLY_NEED, FLAG_HAZMAT, FLAG_PUBLISHED
from webbsearch.extended.suppliers import WAREHOUSES, Suppliers
from webbsearch.extended.tiers import Tiering
from webbsearch.retrieval.analyzer import clean_dashes, compact_id, normalize, word_groups

RETURNS = {"1": "Returnable", "2": "Returnable within 30 days; 25% restocking fee",
           "3": "Non-returnable unless defective", "4": "Non-returnable unless defective"}
SHIP_NOTE = "Additional shipping charges may apply"


def ec_dir() -> Path:
    return Path(os.environ.get("WEBB_EC_DIR") or settings.data_dir / "extended")


class _Snap:
    """One consistent view: a build and the suppliers and tiers that go with it. Swapped whole when a new build
    lands, so a search that started on the old build finishes on it (its files stay open until nothing uses it)."""

    def __init__(self, store, name, sup, tiering, sup_of_store, built_ts):
        self.store, self.name, self.sup, self.tiering = store, name, sup, tiering
        self.sup_of_store, self.built_ts = sup_of_store, built_ts

    def aged_h(self) -> float:
        return max(0.0, (time.time() - self.built_ts) / 3600.0) if self.built_ts else 0.0


class Extended:
    def __init__(self, e, log=print):
        self.e = e
        self.log = log
        self.bm = BranchMap(e.availability)
        brands = set(map(str, e.catalog.brand)) | {g["brand"] for g in gaps.ITEMS}
        sup = Suppliers(brands)
        self.snap = _Snap(None, None, sup, Tiering(e.availability, self.bm, sup, e.catalog, e.today), None, 0.0)
        self._zones: dict = {}
        self.dir = ec_dir()
        self._lock = threading.Lock()
        self._build_lock = threading.Lock()
        self._checked = 0.0
        self.build_proc = None
        self._open()
        if self.store is None and os.environ.get("WEBB_EC_AUTOBUILD") == "1":
            self.start_build(int(os.environ.get("WEBB_EC_SIZE") or 1_000_000))

    # the live view's parts
    store = property(lambda self: self.snap.store)
    store_name = property(lambda self: self.snap.name)
    sup = property(lambda self: self.snap.sup)
    tiering = property(lambda self: self.snap.tiering)
    _sup_of_store = property(lambda self: self.snap.sup_of_store)

    # ------------------------------------------------------------ the build
    def _open(self) -> None:
        cur = self.dir / "CURRENT"
        if not cur.exists():
            return
        name = cur.read_text().strip()
        if name == self.snap.name:
            return
        path = self.dir / "builds" / name
        if not (path / "manifest.json").exists():
            return
        from webbsearch.extended.store import ECStore
        try:
            st = ECStore(path)
        except Exception as ex:  # noqa: BLE001  (a bad build never stops search)
            self.log(f"Extended Catalog: build {name} not opened ({type(ex).__name__}: {ex})")
            return
        sup, tiering = self.snap.sup, self.snap.tiering
        if [b for b in st.brands if b not in sup.id]:     # suppliers for -EC brands the core catalog doesn't carry
            sup = Suppliers(set(sup.brands) | set(st.brands))
            tiering = Tiering(self.e.availability, self.bm, sup, self.e.catalog, self.e.today)
            self._zones = {}
        try:
            built = time.mktime(time.strptime(st.man.get("built", ""), "%Y-%m-%dT%H:%M:%S"))
        except (ValueError, TypeError):
            built = 0.0
        self.snap = _Snap(st, name, sup, tiering, np.array([sup.id[b] for b in st.brands], np.int64), built)
        self.log(f"Extended Catalog: {st.man['published']:,} items ({name})")
        # the old build is not closed here: a search may still be reading it; its files close when it's collected

    def refresh(self) -> None:
        """Pick up a finished build (checked at most every 5 seconds)."""
        if time.time() - self._checked < 5:
            return
        with self._lock:
            self._checked = time.time()
            self._open()

    def start_build(self, n: int, shards: int = 1) -> dict:
        """Build -EC items in a separate, low-priority process; search keeps running and picks it up when done."""
        with self._build_lock:
            if self.build_proc is not None and self.build_proc.poll() is None:
                return self.status()
            out = self.dir / "builds" / f"ec-{time.strftime('%Y%m%d-%H%M%S')}"
            out.parent.mkdir(parents=True, exist_ok=True)
            for old in out.parent.glob("*.tmp"):          # a build that was stopped part way
                shutil.rmtree(old, ignore_errors=True)
            cmd = [sys.executable, "-m", "webbsearch.extended.build", "--n", str(int(n)), "--out", str(out),
                   "--shards", str(int(shards)), "--heap", "100000000"]
            pre = (lambda: os.nice(15)) if hasattr(os, "nice") else None
            with open(self.dir / "build.log", "a") as logf:
                self.build_proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT, preexec_fn=pre)
            self.log(f"Extended Catalog: building {n:,} items in the background (pid {self.build_proc.pid})")
        return self.status()

    def status(self) -> dict:
        self.refresh()
        building = self.build_proc is not None and self.build_proc.poll() is None
        st = self.store
        return {"ready": st is not None, "building": building, "build": self.store_name,
                "items": int(st.man["published"]) if st else 0, "held_back": int(st.man["unpublished"]) if st else 0,
                "built": st.man.get("built") if st else None, "build_seconds": st.man.get("seconds") if st else None,
                "shards": len(st.shards) if st else 0}

    # ------------------------------------------------------------ settings
    def cfg(self, ms: dict | None = None) -> dict:
        ms = ms if ms is not None else self.e.merch.get()
        return ranking.merged((ms or {}).get("availability"), {})

    def visible(self, p, cfg: dict) -> bool:
        """Signed-in contractor accounts of the chosen segments only (design decision #9)."""
        return bool(cfg["ec"]["enabled"] and p.is_authenticated and getattr(p, "segment", None) in cfg["ec"]["segments"])

    # ------------------------------------------------------------ coded items
    def coded_tiers(self, idx, branch, need: int = 1, cfg: dict | None = None, held: int = 0) -> dict:
        cfg = cfg or self.cfg()
        base = self.tiering
        tg = base
        zm = int(cfg["zone_minutes"])
        if zm != int(self.bm.zone_minutes):           # another same-day zone (a draft): one Tiering per zone, cached
            tg = self._zones.get((zm, id(base)))
            if tg is None:
                tg = Tiering(self.e.availability, self.bm.with_zone(zm), base.sup, self.e.catalog, self.e.today,
                             brand_id=base._brand_id)
                self._zones = {(zm, id(base)): tg, **{k: v for k, v in list(self._zones.items())[:7]}}
        return tg.coded(np.asarray(idx, np.int64), branch, need, cfg["stale_hours"], held)

    def coded_adjust(self, s: np.ndarray, idx: np.ndarray, offset_free: np.ndarray, branch, need: int = 1,
                     cfg: dict | None = None) -> tuple[np.ndarray, dict]:
        """Scores moved by availability: s + A x (relevance spread of the best matches)."""
        cfg = cfg or self.cfg()
        if not cfg["enabled"] or not len(idx):
            return s, {}
        t = self.coded_tiers(idx, branch, need, cfg)
        brands = self.e.catalog.brand[np.asarray(idx, np.int64)]
        a = ranking.adjustment(cfg, t["tier"], t["hi"], brands=brands)
        span = ranking.relevance_span(s, offset_free, cfg["relevance_window"])
        return s + a * span, t       # "block" applies to the Extended Catalog only; coded items are never hidden here

    def card(self, tier: str, lo: int, hi: int, brand: str, branch_name: str, at: int, need: int,
             long_lead: bool = False) -> dict:
        lab = self.tiering.label(tier, int(lo), int(hi), brand, {"branch": branch_name, "at_branch": int(at),
                                                                  "need": need, "long_lead": bool(long_lead)})
        return {"tier": tier, **lab, "days": [int(lo), int(hi)] if hi >= 0 else None,
                "arrives": self.tiering.arrives(int(hi)) if hi >= 0 else None,
                "returns": RETURNS.get(tier[:1], "")}

    # ------------------------------------------------------------ -EC section
    def _coded_wins(self, part: str) -> bool:
        """A coded item with this part number (the catalog, or a real F.W. Webb item) always wins over an -EC copy."""
        cp = compact_id(part)
        wi = getattr(self.e, "webb_items", None)
        return bool(self.e.catalog.id_lookup(cp)) or (wi is not None and cp in wi.by_code)

    def section(self, q: str, p, branch=None, need: int = 1, limit: int | None = None,
                ms: dict | None = None) -> dict:
        t0 = time.perf_counter()
        self.refresh()
        cfg = self.cfg(ms)
        out = {"q": q, "items": [], "total": 0, "status": "ok", "label": "Extended Catalog",
               "note": "Ships from the manufacturer. Lead times are manufacturer's estimates."}
        if not self.visible(p, cfg):
            return out | {"status": "hidden"}
        snap = self.snap                     # one consistent build for the whole request
        st = snap.store
        if st is None:
            return out | {"status": "building" if self.status()["building"] else "unavailable"}
        e = self.e
        q = clean_dashes(q or "").strip()[:200]
        if not q:
            return out | {"status": "empty"}
        dic = getattr(e, "vocab", None)
        if dic is not None:
            try:
                q, _ = dic.apply(q, None)
            except Exception:  # noqa: BLE001
                pass
        pq = e.parser.parse(q)
        brand_toks = {t for b in pq.brands for t in normalize(b)}
        rows = st.search(pq.tokens, pq.id_chunks, brand_toks or None, k=80)
        groups = [g for g in word_groups(pq.tokens)]
        ids = {c.upper() for c in pq.id_chunks}
        keep, cov, bm25, exact = [], [], [], []
        top = rows[0][1] if rows else 1.0
        for r, sc in rows:
            if not st.published(r):
                continue
            sku, part, title = st.strings(r)
            if self._coded_wins(part):
                continue
            cp = compact_id(part)
            ex = cp in ids or compact_id(sku) in ids
            c = 1.0 if ex else st.coverage(r, groups)
            if c < cfg["ec"]["min_coverage"]:
                continue
            keep.append(r); cov.append(c); bm25.append(sc / max(top, 1e-6)); exact.append(ex)
        if not keep:
            return out | {"ms": round((time.perf_counter() - t0) * 1000, 1)}
        rows_a = np.array(keep, np.int64)
        tier = snap.tiering.extended(st, rows_a, branch, need, cfg["stale_hours"], snap.sup_of_store, snap.aged_h())
        c = st.cols
        price_ok = (np.asarray(c["price_c"][rows_a]) >= 0) & (np.asarray(c["conf"][rows_a]) >= cfg["ec"]["price_confidence"])
        ship_ok = np.asarray(c["wt"][rows_a]) > 0
        brands = np.array([st.brands[int(b)] for b in np.asarray(c["brand"][rows_a])])
        blk = ranking.blocked(cfg, brands)
        a = ranking.adjustment(cfg, tier["tier"], tier["hi"], ship_ok, price_ok, brands)
        rel = 0.6 * np.array(cov) + 0.4 * np.array(bm25)
        score = rel + a + 10.0 * np.array(exact)
        # G5: nothing with both an unknown price and an unknown delivery estimate near the top
        g5 = (~price_ok) & (tier["hi"] < 0)
        order = sorted(range(len(keep)), key=lambda k: (bool(blk[k]), bool(g5[k]), -score[k]))
        order = [k for k in order if not blk[k]]
        maxn = int(limit if limit is not None else cfg["ec"]["max_items"])
        bi = self.e.availability.branch_index(branch)
        bname = str(self.e.availability.names[bi]).split(",")[0]
        items = []
        for k in order[:maxn]:
            items.append(self._ec_item(snap, int(rows_a[k]), tier["tier"][k], int(tier["lo"][k]), int(tier["hi"][k]),
                                       bool(price_ok[k]), bname, need, bool(tier["long_lead"][k]), p)
                         | {"exact": bool(exact[k]), "coverage": round(float(cov[k]), 2),
                            "score": round(float(score[k]), 3)})
        out.update(items=items, total=len(order), more=len(rows) >= 80, exact=any(it["exact"] for it in items),
                   ms=round((time.perf_counter() - t0) * 1000, 1))
        return out

    def _ec_item(self, snap, row: int, tier: str, lo: int, hi: int, price_ok: bool, bname: str, need: int,
                 long_lead: bool, p) -> dict:
        r = snap.store.row(row)
        sup = snap.sup.list[int(snap.sup_of_store[r["brand"]])]
        lab = self.card(tier, lo, hi, r["brand_name"], bname, 0, need, long_lead)
        ship = None
        if r["wt"] > 0 and min(r["dl"], r["dw"], r["dh"]) > 0:
            ship = {"weight_lb": round(r["wt"] / 10, 1),
                    "dims_in": [round(r["dl"] / 10, 1), round(r["dw"] / 10, 1), round(r["dh"] / 10, 1)],
                    "freight_class": int(r["fclass"]) or None, "method": "LTL" if r["wt"] / 10 * need > 150 else "UPS"}
        o = int(r["origin"])
        tm = self.e.catalog
        t = int(r["tmpl"])
        price = round(r["price_c"] / 100.0, 2) if price_ok and sees_prices(p) else None
        return {"sku": r["sku"], "ec": True, "title": r["title"], "brand": r["brand_name"], "mfr_part": r["part"],
                "family": r["family"], "cat3": tm.cat3[t], "cat2": tm.cat2[t], "attrs": {},
                "price": price, "price_basis": "list" if price is not None else None,
                "price_note": None if price is not None else
                ("Price hidden for your role" if not sees_prices(p) else "Price confirmed at order"),
                "uom": "EA", "availability": lab, "shipping": ship,
                "shipping_note": None if ship else SHIP_NOTE,
                "ships_from": f"{WAREHOUSES[o][0]}, {WAREHOUSES[o][1]}" if 0 <= o < len(WAREHOUSES) else None,
                "freight": {"ffa_min": sup.ffa_min, "vendor_code": sup.code},
                "hazmat": bool(r["flags"] & FLAG_HAZMAT), "compliance": bool(r["flags"] & FLAG_COMPLY_NEED),
                "returns": RETURNS["3"]}

    def _row_for(self, snap, sku: str) -> int | None:
        st = snap.store
        if st is None:
            return None
        row = st.by_sku(sku)
        if row is None or not st.published(row) or self._coded_wins(st.strings(row)[1]):
            return None
        return row

    def product(self, sku: str, p, branch=None, need: int = 1) -> dict | None:
        self.refresh()
        snap = self.snap
        cfg = self.cfg()
        if snap.store is None or not self.visible(p, cfg):
            return None
        row = self._row_for(snap, sku)
        if row is None:
            return None
        st = snap.store
        tier = snap.tiering.extended(st, np.array([row]), branch, need, cfg["stale_hours"], snap.sup_of_store,
                                     snap.aged_h())
        c = st.cols
        price_ok = bool(c["price_c"][row] >= 0 and c["conf"][row] >= cfg["ec"]["price_confidence"])
        bi = self.e.availability.branch_index(branch)
        it = self._ec_item(snap, row, tier["tier"][0], int(tier["lo"][0]), int(tier["hi"][0]), price_ok,
                           str(self.e.availability.names[bi]).split(",")[0], need, bool(tier["long_lead"][0]), p)
        sup = snap.sup.list[int(snap.sup_of_store[int(c["brand"][row])])]
        it["supplier"] = sup.to_dict()
        it["erp"] = {"line_type": "NonStock", "ec_number": it["sku"], "vendor_code": sup.code}
        return it

    def lookup(self, sku: str, p) -> tuple[float | None, dict | None, object]:
        """(price, row, snapshot) for the cart and checkout. Price None when it's confirmed at order or the role
        doesn't see prices; row None when the item isn't orderable here (not published, or coded since)."""
        snap = self.snap
        row = self._row_for(snap, sku)
        if row is None:
            return None, None, snap
        r = snap.store.row(row)
        ok = r["price_c"] >= 0 and r["conf"] >= self.cfg()["ec"]["price_confidence"] and sees_prices(p)
        return (round(r["price_c"] / 100.0, 2) if ok else None), r, snap


def sees_prices(p) -> bool:
    """Signed-out shoppers see list prices; a signed-in role may not (a tech, for instance)."""
    return not getattr(p, "is_authenticated", False) or bool(getattr(p, "sees_prices", True))
