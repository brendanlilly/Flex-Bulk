"""The historical search log (data/canonical/search_log.parquet: ts, account, user, query, clicked item) as
insights, and the relevance audit.

Insights: volume by week, the queries people repeat, which item they end up clicking for each (and how
concentrated those clicks are), what families and brands searches lead to, and how each trade searches.

Relevance audit: replay logged searches through today's ranking, as the account that searched, and see where
the item that person clicked lands now. With draft settings it runs both and reports what the draft would
change before anyone saves it. Runs in a background thread; the last result is kept in data/merch/audit.json.
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections import Counter
from pathlib import Path

import pandas as pd

from webbsearch.merch.store import norm_q

PARTISH = re.compile(r"^[a-z]*-?\d[\w./-]*$")


class History:
    def __init__(self, engine, path: Path):
        self.e = engine
        self.path = Path(path)
        self._df = None
        self._summary = None
        self.lock = threading.Lock()

    @property
    def ok(self) -> bool:
        return self.path.exists()

    def df(self) -> pd.DataFrame:
        with self.lock:
            if self._df is None:
                d = pd.read_parquet(self.path)
                d["ts"] = pd.to_datetime(d["ts"])
                d["qn"] = d["query"].map(norm_q)
                cat = self.e.catalog
                ix = d["clicked_sku"].map(cat.sku_to_idx)
                ok = ix.notna()
                d["family"], d["brand"] = None, None
                d.loc[ok, "family"] = [str(cat.cat3[int(i)]) for i in ix[ok]]
                d.loc[ok, "brand"] = [str(cat.brand[int(i)]) for i in ix[ok]]
                d["segment"] = d["account_id"].map(self.e.accounts.accounts["segment"]).fillna("unknown")
                self._df = d
            return self._df

    def title(self, sku: str) -> str:
        i = self.e.catalog.sku_to_idx.get(str(sku))
        return str(self.e.catalog.title[i]) if i is not None else str(sku)

    # ------------------------------------------------------------------ insights
    def summary(self) -> dict:
        if self._summary is not None:
            return self._summary
        d = self.df()
        n = len(d)
        qc = d["qn"].value_counts()
        wk = d.set_index("ts").resample("W-MON").agg({"query": "size", "account_id": "nunique"})
        top = []
        for qn, cnt in qc.head(60).items():
            g = d[d["qn"] == qn]
            clicks = g["clicked_sku"].value_counts()
            sku = str(clicks.index[0])
            top.append({"q": g["query"].iloc[0], "qn": qn, "n": int(cnt), "accounts": int(g["account_id"].nunique()),
                        "top_sku": sku, "top_title": self.title(sku), "top_share": round(float(clicks.iloc[0] / len(g)), 3),
                        "distinct_clicked": int(len(clicks)), "last": str(g["ts"].max().date())})

        def share(col, k=15):
            vc = d[col].dropna().value_counts()
            return [{"name": str(a), "n": int(b), "share": round(float(b / n), 4)} for a, b in vc.head(k).items()]
        segs = []
        for sg, g in d.groupby("segment"):
            segs.append({"segment": sg, "n": int(len(g)), "accounts": int(g["account_id"].nunique()),
                         "top": g["qn"].value_counts().head(5).index.tolist()})
        once = qc[qc == 1]
        partish = d["qn"].map(lambda q: bool(PARTISH.match(q)) and " " not in q)
        self._summary = {
            "source": "demo search log (data/canonical/search_log.parquet)",
            "from": str(d["ts"].min().date()), "to": str(d["ts"].max().date()), "searches": n,
            "accounts": int(d["account_id"].nunique()), "users": int(d["user_id"].nunique()),
            "unique_queries": int(len(qc)), "one_off_share": round(float(once.sum() / n), 3),
            "top60_share": round(float(qc.head(60).sum() / n), 3),
            "part_number_share": round(float(partish.mean()), 3),
            "weekly": [{"week": str(ix.date()), "searches": int(r["query"]), "accounts": int(r["account_id"])}
                       for ix, r in wk.iterrows()],
            "top_queries": top, "families": share("family"), "brands": share("brand"), "segments":
                sorted(segs, key=lambda s: -s["n"]),
        }
        return self._summary

    def query(self, q: str) -> dict:
        d = self.df()
        g = d[d["qn"] == norm_q(q)]
        if not len(g):
            return {"q": q, "n": 0}
        clicks = g["clicked_sku"].value_counts().head(12)
        by_m = g.set_index("ts").resample("MS").size()
        return {"q": q, "n": int(len(g)), "accounts": int(g["account_id"].nunique()),
                "first": str(g["ts"].min().date()), "last": str(g["ts"].max().date()),
                "monthly": [{"month": str(ix.date())[:7], "n": int(v)} for ix, v in by_m.items()],
                "segments": [{"segment": s, "n": int(v)} for s, v in g["segment"].value_counts().items()],
                "clicked": [{"sku": str(s), "title": self.title(s), "n": int(v), "share": round(float(v / len(g)), 3)}
                            for s, v in clicks.items()]}


class Audit:
    """Where does the item the customer clicked land today? (optionally: with the draft settings too)"""

    def __init__(self, engine, history: History, path: Path):
        self.e = engine
        self.h = history
        self.path = Path(path)
        self.state = {"running": False, "done": 0, "total": 0}
        self.lock = threading.Lock()
        self.last = None
        if self.path.exists():
            try:
                self.last = json.loads(self.path.read_text())
            except (OSError, json.JSONDecodeError):
                self.last = None

    def status(self) -> dict:
        with self.lock:
            out = {**self.state, "result": self.last}
        r = self.last
        if r:      # v2.4 (review G13): a saved run on older settings or an older dictionary isn't today's baseline
            try:
                sv = int(self.e.merch.get().get("version", 0))
            except Exception:  # noqa: BLE001
                sv = None
            voc = getattr(self.e, "vocab", None)
            dv = getattr(voc, "version", None)
            why = []
            if sv is not None and r.get("settings_version") is not None and r["settings_version"] != sv:
                why.append(f"settings v{r['settings_version']} (now v{sv})")
            if dv is not None and r.get("dictionary_version") is not None and r["dictionary_version"] != dv:
                why.append(f"dictionary v{r['dictionary_version']} (now v{dv})")
            out["stale"] = why
        return out

    def sample(self, n: int, mode: str = "top") -> pd.DataFrame:
        """top: the most-searched queries (one logged search each, the latest); recent: the latest searches."""
        d = self.h.df().sort_values("ts", ascending=False)
        if mode == "recent":
            return d.head(n)
        return d.drop_duplicates("qn").set_index("qn").loc[d["qn"].value_counts().head(n).index].reset_index()

    def start(self, n: int = 150, mode: str = "top", draft: dict | None = None, who: str = "") -> bool:
        with self.lock:
            if self.state["running"]:
                return False
            self.state = {"running": True, "done": 0, "total": n, "started": time.strftime("%H:%M:%S"), "mode": mode,
                          "with_draft": draft is not None}
        threading.Thread(target=self._run, args=(n, mode, draft, who), daemon=True).start()
        return True

    def _pos(self, r: dict, sku: str) -> int | None:
        for k, it in enumerate(r.get("items") or []):
            if it["sku"] == sku:
                return k + 1
        return None

    def _run(self, n, mode, draft, who):
        from webbsearch.retrieval.entitlements import ANONYMOUS
        t0 = time.time()
        rows = []
        try:
            live = self.e.merch.get()
            qn_counts = self.h.df()["qn"].value_counts()
            s = self.sample(n, mode)
            with self.lock:
                self.state["total"] = len(s)
            for r in s.itertuples():
                try:
                    p = self.e.accounts.principal(str(r.account_id), str(r.user_id))
                except Exception:
                    p = ANONYMOUS
                sku = str(r.clicked_sku)
                res = self.e.results.results(r.query, p, settings=live, llm="off")
                row = {"q": r.query, "qn": r.qn, "searches": int(qn_counts.get(r.qn, 1)), "account": str(r.account_id),
                       "sku": sku, "title": self.h.title(sku), "total": int(res.get("total") or 0),
                       "pos": self._pos(res, sku)}
                if draft is not None:
                    row["pos_draft"] = self._pos(self.e.results.results(r.query, p, settings=draft, llm="off"), sku)
                rows.append(row)
                with self.lock:
                    self.state["done"] = len(rows)
            out = {"at": time.strftime("%Y-%m-%d %H:%M"), "by": who or "", "mode": mode, "n": len(rows),
                   "seconds": round(time.time() - t0, 1), "settings_version": live.get("version", 0),
                   "dictionary_version": getattr(getattr(self.e, "vocab", None), "version", None),
                   "live": metrics([r["pos"] for r in rows])}
            if draft is not None:
                out["draft"] = metrics([r["pos_draft"] for r in rows])
                out["moved"] = {"better": sum(1 for r in rows if _better(r["pos_draft"], r["pos"])),
                                "worse": sum(1 for r in rows if _better(r["pos"], r["pos_draft"]))}
            key = lambda r: (-(r["pos"] or 999), -r["searches"])
            out["misses"] = sorted([r for r in rows if not r["pos"] or r["pos"] > 10], key=key)[:60]
            # by customer discipline, for tuning per-discipline weights (brand affinity)
            seg_of = self.e.accounts.accounts["segment"].to_dict()
            by = {}
            for r in rows:
                by.setdefault(seg_of.get(r["account"], "unknown"), []).append(r)
            out["by_segment"] = [{"segment": sg, "n": len(rs), "live": metrics([r["pos"] for r in rs]),
                                  **({"draft": metrics([r["pos_draft"] for r in rs])} if draft is not None else {})}
                                 for sg, rs in sorted(by.items(), key=lambda kv: -len(kv[1]))]
            out["rows"] = rows
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(out))
            with self.lock:
                self.last = out
        except Exception as ex:  # keep the dashboard usable; say what failed
            with self.lock:
                self.state["error"] = f"{type(ex).__name__}: {ex}"
        finally:
            with self.lock:
                self.state["running"] = False


def _better(a, b) -> bool:
    """position a is better than position b (None = not on the page)"""
    return a is not None and (b is None or a < b)


def metrics(pos: list) -> dict:
    n = len(pos) or 1
    hit = lambda k: round(sum(1 for p in pos if p and p <= k) / n, 3)
    return {"hit1": hit(1), "hit3": hit(3), "hit10": hit(10), "hit24": hit(24),
            "missing": round(sum(1 for p in pos if not p) / n, 3),
            "mrr": round(sum(1 / p for p in pos if p) / n, 3),
            "positions": dict(Counter("1" if p == 1 else "2-3" if p and p <= 3 else "4-10" if p and p <= 10 else
                                      "11-24" if p and p <= 24 else "25+" if p else "not found" for p in pos))}
