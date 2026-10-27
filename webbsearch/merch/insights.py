"""Insights and recommendations for the merchandising dashboard (the Recommendations page).

Two tiers:
  * report()  runs on every page load (about a second): trends in the historical and live logs, search gaps and
    misspellings, Bulk order signals, analytics health, checks on the settings themselves and on catalog data.
  * deep run  in the background (one to three minutes): replays logged searches, as the accounts that searched, to
    test candidate changes before recommending them: each ranking weight up and down, the brand cap, brand affinity
    per discipline, lifting the item customers pick for head searches, each active rule with and without it, and
    burying discontinued items.

Every recommendation carries its evidence, an estimate of the effect, and actions: most put the change into the
editor's draft (as validated settings objects), so it goes through Preview, the audit or an A/B test before Publish.
Dismissals are shared (data/merch/insights_state.json).
"""
from __future__ import annotations

import copy
import difflib
import hashlib
import json
import math
import re
import threading
import time
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

from webbsearch.merch.analytics import FEEDBACK_REASONS
from webbsearch.merch.history import _better, metrics
from webbsearch.merch.rules import rule_matches
from webbsearch.merch.store import (LIMITS, SEGMENTS, validate_brand_pref, validate_cross, validate_rewrite,
                                    validate_rule)

STD_SIZE = re.compile(r"^\d+(?:-\d+/\d+)?$|^\d+/\d+$")
PARTISH = re.compile(r"^[a-z]*-?\d[\w./-]*$")
WORD = re.compile(r"[A-Za-z][A-Za-z'-]{2,}")
TITLE_SIZE = re.compile(r"(?<![\d/-])(\d+-\d+/\d+|\d+/\d+|\d+) in\.")
CATS = ("ranking", "rules", "gaps", "bulk", "data", "trends", "cleanup")
CAT_LABEL = {"ranking": "Ranking & weights", "rules": "Rules", "gaps": "Search gaps", "bulk": "Bulk order",
             "data": "Data quality", "trends": "Trends", "cleanup": "Cleanup"}
# a candidate weight is tried at these values (one setting at a time, everything else live)
BASE = {"purchased": 0.8, "purchased_times": 0.35, "purchased_recent": 0.5, "popularity": 0.3, "stocked": 0.3, "branch_stock": 0.3}
WLABEL = {"purchased": "Previously purchased", "purchased_times": "… times bought", "purchased_recent": "… bought recently",
          "popularity": "Popularity", "stocked": "Stocked item", "branch_stock": "On the shelf at the pickup branch"}


def _rid(*parts) -> str:
    return hashlib.md5("|".join(str(p) for p in parts).encode()).hexdigest()[:12]


def _prio(score: float) -> str:
    return "high" if score >= 70 else "medium" if score >= 40 else "low"


def rec(kind: str, cat: str, score: float, title: str, detail: str, evidence=(), impact: str = "", actions=(),
        source: str = "", key=None, check: dict | None = None) -> dict:
    return {"id": _rid(kind, key if key is not None else title), "kind": kind, "cat": cat, "score": round(float(score), 1),
            "priority": _prio(score), "title": title, "detail": detail, "evidence": [e for e in evidence if e], "impact": impact,
            "actions": list(actions), "source": source, "check": check or {}}


def draft(label: str, *ops) -> dict:
    return {"type": "draft", "label": label, "ops": list(ops)}


def look(q: str, label: str = "Look") -> dict:
    return {"type": "query", "label": label, "q": q}


def go(page: str, label: str, **kw) -> dict:
    return {"type": "go", "label": label, "page": page, **kw}


def _z(a: int, b: int) -> float:
    """Poisson z for a change in counts between two equal windows."""
    return (a - b) / math.sqrt(a + b) if a + b else 0.0


def _ztest(x1, n1, x2, n2):
    from webbsearch.merch.analytics import _ztest as z
    return z(x1, n1, x2, n2)


def searches_needed(p: float, lift: float = 0.1) -> int:
    p = p if 0 < p < 1 else 0.3
    p2 = min(0.99, p * (1 + lift))
    return math.ceil((1.96 + 0.84) ** 2 * (p * (1 - p) + p2 * (1 - p2)) / (p2 - p) ** 2)


class Insights:
    def __init__(self, engine, analytics, history, audit, root: Path, learned=None):
        self.e, self.a, self.h, self.au = engine, analytics, history, audit
        self.root = Path(root)
        self.learned = learned                  # callable -> bulkprefs.Learned (or None)
        self.state_path = self.root / "insights_state.json"
        self.deep_path = self.root / "insights_deep.json"
        self.lock = threading.Lock()
        self.deep_state = {"running": False, "done": 0, "total": 0, "step": ""}
        self.deep = self._load(self.deep_path)
        self._vocab = None
        self._catalog = None
        self._brand_cat = None
        self._spell_cache: dict[str, str | None] = {}
        self._p_cache: dict = {}

    @staticmethod
    def _load(p: Path):
        try:
            return json.loads(p.read_text()) if p.exists() else None
        except (OSError, json.JSONDecodeError):
            return None

    # ------------------------------------------------------------------ shared
    def dismissed(self) -> dict:
        return (self._load(self.state_path) or {}).get("dismissed", {})

    def dismiss(self, rid: str, who: str = "", undo: bool = False) -> dict:
        with self.lock:
            st = self._load(self.state_path) or {"dismissed": {}}
            if undo:
                st["dismissed"].pop(rid, None)
            else:
                st["dismissed"][rid] = {"who": (who or "")[:60], "at": time.strftime("%Y-%m-%d %H:%M")}
            self.root.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(st))
        return st["dismissed"]

    def _principal(self, acct, user):
        from webbsearch.retrieval.entitlements import ANONYMOUS
        k = (str(acct), str(user))
        if k not in self._p_cache:
            try:
                self._p_cache[k] = self.e.accounts.principal(str(acct), str(user))
            except Exception:
                self._p_cache[k] = ANONYMOUS
        return self._p_cache[k]

    def _pos(self, q: str, p, sku: str, settings: dict):
        r = self.e.results.results(q, p, settings=settings, llm="off")
        for k, it in enumerate(r.get("items") or []):
            if it["sku"] == sku:
                return k + 1, r
        return None, r

    def _term_exists(self):
        return getattr(getattr(self.e.retriever, "b", None), "term_exists", None)

    def _brand_words(self) -> set:
        return {w for b in set(self.e.catalog.brand.tolist()) for w in re.findall(r"[a-z]+", str(b).lower())}

    def _brand_counts(self) -> Counter:
        if self._brand_cat is None:
            c = self.e.catalog
            self._brand_cat = Counter(zip((str(x).lower() for x in c.cat3), (str(x).lower() for x in c.brand)))
        return self._brand_cat

    # ------------------------------------------------------------------ report (page load)
    def report(self, days: int = 30) -> dict:
        t0 = time.time()
        s = self.e.merch.get()
        recs: list[dict] = []
        trends = self._trends(days)
        recs += trends.pop("recs")
        recs += self._spelling(s)
        recs += self._live(s, days)
        recs += self._bulk(s, days)
        recs += self._settings_health(s, days)
        checks = self._catalog_checks()
        recs += checks["recs"]
        deep = self.deep
        if deep:
            recs += [r for r in deep.get("recs", []) if self._still_open(r, s)]
        dis = self.dismissed()
        seen = set()
        out = []
        for r in sorted(recs, key=lambda r: -r["score"]):
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            r["dismissed"] = dis.get(r["id"])
            out.append(r)
        live = [r for r in out if not r["dismissed"]]
        counts = {c: sum(1 for r in live if r["cat"] == c) for c in CATS}
        counts["high"] = sum(1 for r in live if r["priority"] == "high")
        return {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "ms": round((time.time() - t0) * 1000), "days": days,
                "recs": out, "counts": counts, "labels": CAT_LABEL, "trends": trends, "checks": checks["rows"], "sampled": {"n": checks["sampled"], "of": checks["of"]},
                "deep": self._deep_summary(s)}

    def _still_open(self, r: dict, s: dict) -> bool:
        """A replay-tested recommendation that the live settings already carry, or no longer apply to, is dropped."""
        ch = r.get("check") or {}
        t = ch.get("type")
        if t == "weight":
            sect, k = ch["path"]
            return s[sect].get(k) != ch["value"]
        if t == "query_rule":
            return not any(x["match"]["type"] == "exact" and ch["q"] in x["match"]["q"].split(" | ") for x in s["rules"])
        if t == "rule":
            x = next((x for x in s["rules"] if x["id"] == ch["id"]), None)
            return bool(x and x["enabled"] and x["match"]["type"] == ch.get("match_type", x["match"]["type"]))
        if t == "affinity":
            return any(s["affinity"]["segments"][sg][k] != v for sg, w in ch["segments"].items() for k, v in w.items())
        if t == "status_bury":
            return not any(b.get("field") == "status" for x in s["rules"] if x["enabled"] for b in x.get("boost") or [])
        return True

    # ------------------------------------------------------------------ trends
    def _trends(self, days: int) -> dict:
        out = {"recs": [], "weekly": [], "categories": [], "queries": [], "live_queries": [], "kpi_shifts": [], "window": None}
        if self.h is not None and self.h.ok:
            import pandas as pd
            d = self.h.df()
            end = d["ts"].max().normalize()
            end = end - pd.Timedelta(days=int(end.weekday()))      # last full week ends on a Monday
            cut, start = end - pd.Timedelta(weeks=8), end - pd.Timedelta(weeks=16)
            now, before = d[(d.ts >= cut) & (d.ts < end)], d[(d.ts >= start) & (d.ts < cut)]
            out["window"] = {"now": [str(cut.date()), str(end.date())], "before": [str(start.date()), str(cut.date())],
                             "source": "historical log"}
            wk = d.set_index("ts").resample("W-MON").size()
            out["weekly"] = [{"week": str(ix.date()), "n": int(v)} for ix, v in wk.tail(30).items()]

            def movers(col, min_n):
                a, b = now[col].value_counts(), before[col].value_counts()
                rows = []
                for k in set(a.index) | set(b.index):
                    x, y = int(a.get(k, 0)), int(b.get(k, 0))
                    if x + y < min_n or not k:
                        continue
                    rows.append({"name": str(k), "now": x, "before": y, "change": round((x - y) / max(y, 1), 3), "z": round(_z(x, y), 2)})
                return sorted(rows, key=lambda r: -abs(r["z"]))
            cats = movers("family", 60)
            out["categories"] = cats[:14]
            qs = movers("qn", 16)
            out["queries"] = [r for r in qs if abs(r["z"]) >= 2][:12]
            top_q_by_cat = now.groupby("family")["query"].agg(lambda s_: s_.value_counts().index[0]) if len(now) else {}
            for r in cats:
                if abs(r["z"]) >= 2.5 and abs(r["change"]) >= 0.25:
                    up = r["change"] > 0
                    q = top_q_by_cat.get(r["name"]) if hasattr(top_q_by_cat, "get") else None
                    out["recs"].append(rec("cat_trend", "trends", 38 + min(abs(r["z"]) * 4, 20),
                        f"{r['name']}: searches {'up' if up else 'down'} {abs(round(r['change'] * 100))}% ({r['before']} → {r['now']})",
                        ("Rising demand: check stock at the branches that sell it, its top searches in Preview, and whether a campaign "
                         "rule or a job kit belongs here." if up else
                         "Falling demand: check whether customers stopped finding it (zero results, low click-through) or the season turned."),
                        [f"Last 8 weeks vs the 8 before ({out['window']['now'][0]} to {out['window']['now'][1]}); z = {r['z']}"],
                        actions=[look(q, f"Look at “{q[:30]}”")] if q else [], source="historical log", key=r["name"]))
            for r in out["queries"][:4]:
                if r["z"] >= 3 and r["change"] >= 0.5:
                    out["recs"].append(rec("query_trend", "trends", 30 + min(r["z"] * 3, 15), f"Rising search: “{r['name']}” ({r['before']} → {r['now']})",
                        "A search growing this fast is worth checking before it becomes a head query: is the right item first?",
                        [f"Last 8 weeks vs the 8 before; z = {r['z']}"], actions=[look(r["name"])], source="historical log", key=r["name"]))
        # live log: this period vs the previous one
        if self.a is not None:
            now_t = time.time()
            since = time.strftime("%Y-%m-%d", time.localtime(now_t - days * 86400))
            prev = time.strftime("%Y-%m-%d", time.localtime(now_t - 2 * days * 86400))
            cur, old = self.a._per_search(since), self.a._per_search(prev, since)
            out["live"] = {"now": len(cur), "before": len(old)}
            if len(cur) >= 30 and len(old) >= 30:
                for key, lab, lower in (("clicked", "Click-through", False), ("atc", "Add to cart", False), ("zero", "No results", True)):
                    x1, x2 = sum(r[key] for r in old), sum(r[key] for r in cur)
                    t = _ztest(x1, len(old), x2, len(cur))
                    if t["p"] is not None and t["p"] < 0.05:
                        worse = (t["b"] < t["a"]) != lower
                        contrib = self._contributors(cur, old, key)
                        out["kpi_shifts"].append({"metric": lab, "before": t["a"], "now": t["b"], "p": t["p"], "worse": worse})
                        out["recs"].append(rec("kpi_shift", "trends", 72 if worse else 25,
                            f"{lab} {'fell' if (t['b'] < t['a']) else 'rose'} from {t['a'] * 100:.1f}% to {t['b'] * 100:.1f}% (last {days} days vs the {days} before)",
                            ("Most of the change comes from the searches below; open one to see what changed." if worse else
                             "Worth knowing what drove it before the next change lands on top of it."),
                            [f"{len(old)} → {len(cur)} searches; p = {t['p']:.3f}"] + [f"“{c['q']}”: {c['what']}" for c in contrib[:3]],
                            actions=[look(c["q"]) for c in contrib[:2]], source="live log", key=lab))
            ca, cb = Counter(r["qn"] for r in cur), Counter(r["qn"] for r in old)
            lq = [{"name": q, "now": ca[q], "before": cb.get(q, 0), "z": round(_z(ca[q], cb.get(q, 0)), 2)}
                  for q in ca if ca[q] + cb.get(q, 0) >= 8]
            out["live_queries"] = sorted([r for r in lq if r["z"] >= 2], key=lambda r: -r["z"])[:10]
        return out

    @staticmethod
    def _contributors(cur, old, key):
        """Queries whose rate change, weighted by volume, explains most of a KPI shift."""
        def rate(rows):
            g = defaultdict(list)
            for r in rows:
                g[r["qn"]].append(r[key])
            return {q: (sum(v) / len(v), len(v)) for q, v in g.items()}
        a, b = rate(old), rate(cur)
        out = []
        for q in set(a) & set(b):
            (ra, na), (rb, nb) = a[q], b[q]
            if na >= 3 and nb >= 3:
                out.append({"q": q, "effect": (rb - ra) * nb, "what": f"{ra * 100:.0f}% → {rb * 100:.0f}% of {nb} searches"})
        return sorted(out, key=lambda r: r["effect"])

    # ------------------------------------------------------------------ misspellings and unknown words
    def _vocab_list(self) -> dict:
        if self._vocab is None:
            c = Counter()
            cat = self.e.catalog
            for t in cat.title[::4]:
                c.update(w.lower() for w in WORD.findall(str(t)))
            for x in set(cat.cat3.tolist()) | set(cat.brand.tolist()):
                c.update(w.lower() for w in WORD.findall(str(x)))
            by_first = defaultdict(list)
            parts = set()
            for w, n in c.items():
                if "-" in w or "'" in w:
                    parts.update(x for x in re.split(r"[-']", w) if x)
                elif n >= 3:
                    by_first[w[0]].append(w)
            self._vocab = {"count": c, "by_first": by_first, "parts": parts}
        return self._vocab

    def _correct(self, word: str) -> str | None:
        if word in self._spell_cache:
            return self._spell_cache[word]
        v = self._vocab_list()
        # a misspelling, not an abbreviation: no prefix relation ("prog" isn't "pro"), close in spelling
        cands = [w for w in v["by_first"].get(word[0], []) if abs(len(w) - len(word)) <= 2 and w != word and len(w) >= 4
                 and v["count"][w] >= 10 and re.search(r"[aeiouy]", w)
                 and not word.startswith(w) and (not w.startswith(word) or len(w) == len(word) + 1)]
        best = None
        for m in difflib.get_close_matches(word, cands, n=5, cutoff=0.8):
            score = (difflib.SequenceMatcher(None, word, m).ratio(), v["count"][m])
            if best is None or score > best[0]:
                best = (score, m)
        self._spell_cache[word] = best[1] if best else None
        return self._spell_cache[word]

    def _spelling(self, s: dict) -> list[dict]:
        from webbsearch.retrieval.analyzer import normalize
        from webbsearch.retrieval.shorthand import expand
        te = self._term_exists()
        if te is None:
            return []
        counts: Counter = Counter()
        if self.h is not None and self.h.ok:
            counts.update(self.h.df()["qn"].value_counts().head(20000).to_dict())
        zero_q = set()
        if self.a is not None:
            since = time.strftime("%Y-%m-%d", time.localtime(time.time() - 90 * 86400))
            for r in self.a._per_search(since):
                counts[r["qn"]] += 1
                if r["zero"]:
                    zero_q.add(r["qn"])
        have = {r["from"] for r in s.get("rewrites") or []}
        brand_words = self._brand_words()
        words: Counter = Counter()
        ex: dict = defaultdict(list)
        zero_w: Counter = Counter()
        vocab = self._vocab_list()
        parts, known = vocab["parts"], vocab["count"]
        for q, n in counts.items():
            # whole words only: the letters in front of a part number (LFAP2937915) aren't a word
            for w in {c.strip(".,;:()\"'!?").lower() for c in q.split()}:
                if (len(w) < 4 or not w.isalpha() or w in have or w in brand_words or w in parts or known[w] >= 3):
                    continue
                toks = normalize(w)
                if not toks or all(te(t) for t in toks) or expand(w).strip().lower() != w:
                    continue
                words[w] += n
                if len(ex[w]) < 3:
                    ex[w].append(q)
                if q in zero_q:
                    zero_w[w] += 1
        groups: dict = defaultdict(list)                 # correction -> [(word, searches)]
        for w, n in words.most_common(80):
            if n < 3 and not zero_w[w]:
                continue
            fix = self._correct(w)
            if not fix or not all(te(t) for t in normalize(fix)):
                continue
            groups[fix].append((w, n))
        out = []
        for fix, ws in sorted(groups.items(), key=lambda kv: -sum(n for _, n in kv[1]))[:12]:
            total = sum(n for _, n in ws)
            short = [w for w, _ in ws if len(w) < 5]
            zero = sum(zero_w[w] for w, _ in ws)
            ops = []
            for w, _ in ws[:10]:
                try:
                    ops.append({"op": "append", "list": "rewrites", "dedupe": "from", "value": validate_rewrite({"from": w, "to": fix})})
                except ValueError:
                    pass
            if not ops:
                continue
            score = 40 + min(math.log1p(total) * 6, 26) + (12 if short else 0) + (10 if zero else 0)
            spelled = ", ".join(f"“{w}” ({n})" for w, n in ws[:6]) + (f" and {len(ws) - 6} more" if len(ws) > 6 else "")
            out.append(rec("spelling", "gaps", score,
                f"{len(ws)} misspelling{'s' if len(ws) > 1 else ''} of “{fix}” in {total} searches" if len(ws) > 1 else f"Misspelling: “{ws[0][0]}” → “{fix}” ({total} searches)",
                (f"{', '.join('“' + w + '”' for w in short)}: words under 5 letters aren't fuzzy-matched, so those searches rest on their other words. " if short else "")
                + ("Fuzzy matching finds something for the longer ones today; a rewrite makes the match exact and the page says “Showing results for …”. "
                   if len(short) < len(ws) else "")
                + "One rewrite per spelling fixes every search containing it.",
                [spelled] + [f"e.g. “{ex[w][0]}”" for w, _ in ws[:2]] + ([f"{zero} searches with them found nothing in the live log"] if zero else []),
                impact=f"{total} searches in the logs", source="historical and live logs", key=fix,
                actions=[draft(f"Add {len(ops)} rewrite{'s' if len(ops) > 1 else ''} to draft", *ops), look(ex[ws[0][0]][0])]))
        return out

    # ------------------------------------------------------------------ live log, personalization, analytics health, A/B
    def _live(self, s: dict, days: int) -> list[dict]:
        if self.a is None:
            return []
        out = []
        ins = self.a.insights(days)
        have_rw = {r["from"] for r in s.get("rewrites") or []}
        for z in ins["zero_results"][:10]:
            if z["qn"] in have_rw:
                continue
            out.append(rec("zero", "gaps", 48 + min(z["zero"] * 6, 30), f"“{z['q']}” found nothing {z['zero']}×",
                "No product matched. If the catalog has it under other words, add a rewrite; if it doesn't, it's a catalog question.",
                [f"last searched {z['last']}"], actions=[go("synonyms", "Add a rewrite", prefill=z["qn"]), look(z["q"])],
                source="live log", key=z["qn"]))
        for z in ins["low_ctr"][:8]:
            out.append(rec("low_ctr", "gaps", 32 + min(z["n"] * 2, 25), f"“{z['q']}”: {round(z['ctr'] * 100)}% of {z['n']} searches clicked",
                "People search it and leave. Compare what they click with what's on top in the drill-down; lift the item they want.",
                [f"average clicked position {z.get('avg_pos') or '—'}"], actions=[look(z["q"])], source="live log", key=z["qn"]))
        # customer feedback (v1.9): searches marked not helpful, and a falling helpful rate
        fb = self.a.feedback_summary(days)
        for g in fb["top_down"][:8]:
            if g["down"] < 2 and not g["comments"]:
                continue
            said = [r for r in self.a.feedback_list(days, vote=-1, q=g["qn"], limit=20) if r["qn"] == g["qn"]]
            why = Counter(x for r in said for x in r["reasons"])
            quote = next((r["comment"] for r in said if r["comment"]), "")
            out.append(rec("feedback_down", "gaps", 50 + min(g["down"] * 8, 30), f"Customers marked “{g['q']}” not helpful {g['down']}×",
                (f"“{quote[:140]}” " if quote else "") + "Open the search to see what's on top and fix it with a rule, a rewrite or a dictionary entry.",
                [", ".join(f"{FEEDBACK_REASONS.get(k, k)}: {n}" for k, n in why.most_common(3)) if why else "", f"{g['up']} helpful votes" if g["up"] else ""],
                actions=[look(g["q"]), go("feedback", "Feedback")], source="customer feedback", key=g["qn"]))
        if fb["n"] >= 20 and fb["prev_n"] >= 20 and fb["prev_helpful_rate"] is not None and fb["helpful_rate"] is not None \
                and fb["helpful_rate"] + 0.10 <= fb["prev_helpful_rate"]:
            out.append(rec("feedback_rate", "trends", 72, f"Helpful votes fell to {fb['helpful_rate'] * 100:.0f}% from {fb['prev_helpful_rate'] * 100:.0f}%",
                "Customers rate results worse than the period before. Check the change log for what was published since.",
                [f"{fb['n']} votes now, {fb['prev_n']} before"], actions=[go("feedback", "Feedback"), go("changes", "Change log")],
                source="customer feedback", key="fbrate"))
        lift = ins.get("personalization") or {}
        pe, an = lift.get("personalized") or {}, lift.get("anonymous") or {}
        if (pe.get("n") or 0) >= 40 and (an.get("n") or 0) >= 20 and pe.get("ctr") is not None and an.get("ctr") is not None:
            if pe["ctr"] + 0.05 < an["ctr"]:
                out.append(rec("pers_worse", "ranking", 66, f"Signed-in customers click less than guests ({pe['ctr'] * 100:.0f}% vs {an['ctr'] * 100:.0f}%)",
                    "Personalization should help. Check the purchase weights and brand affinity in the audit; a recent weight change is the usual cause.",
                    [f"{pe['n']} signed-in and {an['n']} guest searches"], actions=[go("audit", "Relevance audit")], source="live log", key="pers"))
            elif pe.get("avg_pos") and an.get("avg_pos") and pe["avg_pos"] < an["avg_pos"]:
                out.append(rec("pers_ok", "trends", 12, f"Personalization is working: signed-in customers click at position {pe['avg_pos']} vs {an['avg_pos']} for guests",
                    "No action. This is the gap to protect when changing purchase weights or brand affinity.",
                    [f"{pe['n']} signed-in, {an['n']} guest searches; click-through {pe['ctr'] * 100:.0f}% vs {an['ctr'] * 100:.0f}%"], source="live log", key="pers_ok"))
        # analytics health
        since = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
        (ev, orphan), = self.a._q("SELECT COUNT(*), SUM(search_id IS NULL) FROM events WHERE day>=? AND kind IN "
                                  "('click','quick_view','expand','add_to_cart')", (since,))
        n_search = ins["kpis"].get("searches") or 0
        if n_search >= 50 and not ev:
            out.append(rec("no_clicks", "data", 80, f"{n_search} searches but no clicks logged in {days} days",
                "The results page isn't sending click events; every click-based number on the dashboard is empty until it does.",
                [], source="live log", key="noclicks"))
        elif ev and (orphan or 0) / ev > 0.25:
            out.append(rec("orphan_clicks", "data", 45, f"{(orphan or 0) / ev * 100:.0f}% of clicks carry no search id",
                "Clicks outside a results page (start page, cart) are expected; above a quarter, click-through undercounts.",
                [f"{orphan} of {ev} click events"], source="live log", key="orphans"))
        p95 = ins["kpis"].get("p95_ms")
        if p95 and p95 > 300:
            out.append(rec("slow", "data", 50, f"p95 response {round(p95)} ms", "Above 300 ms customers notice. Check the Live log's slow searches.",
                [], actions=[go("log", "Live search log")], source="live log", key="p95"))
        # A/B test ready to decide
        for x in s.get("experiments") or []:
            if x.get("status") != "running":
                continue
            rep = self.a.experiment_report(x)
            A, B = rep["arms"]["A"], rep["arms"]["B"]
            need = searches_needed(A.get("ctr") or 0.3)
            have = min(A.get("searches") or 0, B.get("searches") or 0)
            ctr = rep.get("ctr") or {}
            if have >= need:
                win = ctr.get("p") is not None and ctr["p"] < 0.05
                out.append(rec("ab_ready", "rules", 75 if win else 55,
                    f"A/B test “{x['name']}” has enough searches: {'B ' + ('wins' if ctr['lift'] > 0 else 'loses') if win else 'no significant difference'}",
                    "Decide now: publish B, or stop the test. Waiting longer and stopping on a good day overstates the result.",
                    [f"{have} searches per group (needed {need}); click-through A {A['ctr'] * 100:.1f}% vs B {B['ctr'] * 100:.1f}%"
                     + (f", p {ctr['p']:.3f}" if ctr.get("p") is not None else "")],
                    actions=[go("tests", "Open A/B tests")], source="A/B test", key=x["id"]))
            elif x.get("started") and (time.time() - time.mktime(time.strptime(x["started"][:10], "%Y-%m-%d"))) > 28 * 86400:
                out.append(rec("ab_slow", "cleanup", 30, f"A/B test “{x['name']}” has run 4+ weeks at {have} of {need} searches",
                    "At this traffic it won't reach a decision soon. Stop it, or raise the share of sessions in B.",
                    [], actions=[go("tests", "Open A/B tests")], source="A/B test", key=x["id"] + "slow"))
        return out

    # ------------------------------------------------------------------ Bulk order
    def _bulk(self, s: dict, days: int) -> list[dict]:
        if self.a is None:
            return []
        out = []
        br = self.a.bulk_report(days)
        lr = self.learned() if self.learned else None
        tbl = lr.table(self.e.catalog) if lr else {"items": [], "brands": []}
        crossed = {c["from_brand"].lower() for c in s.get("crosses") or [] if c.get("kind") == "brand"}
        carried = {str(b).lower() for b in set(self.e.catalog.brand.tolist())}
        for b in br["brands_asked"][:8]:
            if b["brand"].lower() in crossed:
                continue
            sw = [x for x in tbl["brands"] if x["from"] == b["brand"].lower()]
            to = sw[0]["to"] if sw and sw[0]["to"].lower() in carried else None
            acts = []
            if to:
                acts.append(draft(f"Add cross {b['brand']} → {to} to draft",
                                  {"op": "append", "list": "crosses", "value": validate_cross({"kind": "brand", "from_brand": b["brand"], "to_brand": to,
                                                                                               "note": "From Recommendations: the brand people take instead"})}))
            acts.append(go("crosses", "Open Crosses", prefill=b["brand"]))
            out.append(rec("brand_cross", "bulk", 55 + min(b["n"] * 2, 25), f"{b['n']} Bulk order lines ask for {b['brand']}, which Webb doesn't carry",
                "Without a cross those lines are matched on their other words. " + (f"People who changed the pick took {to} most often ({sw[0]['n']}×, {sw[0]['share'] * 100:.0f}%)." if to else "Pick the brand Webb sells instead."),
                [], actions=acts, source="Bulk order", key=b["brand"].lower()))
        have_x = {(c.get("from_text"), c.get("to_sku")) for c in s.get("crosses") or [] if c.get("kind") == "item"}
        off = set(s.get("learned_off") or [])
        for r in tbl["items"][:60]:
            if r["n"] >= 5 and r["share"] >= 0.7 and (r["key"], r["sku"]) not in have_x and f"{r['key']}::{r['sku']}" not in off:
                out.append(rec("learned", "bulk", 35 + min(r["n"] * 2, 25), f"“{r['key']}” is matched to one item {r['n']}× ({r['share'] * 100:.0f}%)",
                    "Make it an item cross so every account gets it first, not only the ones whose teams picked it.",
                    [f"{r['title'][:90]} (#{r['sku']}) · {r['accounts']} accounts"],
                    actions=[draft("Add item cross to draft", {"op": "append", "list": "crosses", "value": validate_cross(
                        {"kind": "item", "from_text": r["key"], "to_sku": r["sku"], "note": "From Recommendations: learned pick"})})],
                    source="Bulk order", key=f"{r['key']}::{r['sku']}"))
        # categories where people keep changing the pick -> a brand order from what they choose instead
        since = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
        fam: dict = defaultdict(lambda: {"n": 0, "changed": 0, "brands": Counter()})
        for (m,) in self.a._q("SELECT meta FROM events WHERE kind='bulk_choice' AND day>=?", (since,)):
            d = json.loads(m or "{}")
            f = (d.get("family") or "").lower()
            if not f or d.get("req_brand"):
                continue
            fam[f]["n"] += 1
            if (d.get("rank") or 0) > 0:
                fam[f]["changed"] += 1
                if d.get("brand"):
                    fam[f]["brands"][d["brand"]] += 1
        labels = lr._family_labels(self.e.catalog) if lr else {}
        prefs = {b["family"].lower(): b for b in s.get("brand_prefs") or [] if b.get("scope") == "company"}
        for f, v in fam.items():
            brands = [b for b, n in v["brands"].most_common(3) if n >= 2]
            if v["changed"] < 5 or v["changed"] / v["n"] < 0.4 or not brands:
                continue
            label = labels.get(f, f)
            cur = prefs.get(label.lower())
            if cur and [x.lower() for x in cur["brands"][:len(brands)]] == [x.lower() for x in brands]:
                continue
            try:
                bp = validate_brand_pref({"family": label, "brands": brands, "note": "From Recommendations: the brands people pick"})
            except ValueError:
                continue
            picked = ", ".join(f"{b_} {v['brands'][b_]}×" for b_ in brands)
            out.append(rec("brand_order", "bulk", 45 + min(v["n"], 20), f"{label}: people changed the first pick on {v['changed']} of {v['n']} lines",
                f"They picked {picked}. A brand order puts those first for lines that name no brand."
                + (f" Current order: {' > '.join(cur['brands'])}." if cur else ""),
                [], actions=[draft(f"Set order {' > '.join(brands)} in draft",
                                   {"op": "upsert", "list": "brand_prefs", "key": {"family": label, "scope": "company"}, "value": bp})],
                source="Bulk order", key=f))
        for g in br["not_in_catalog"][:6]:
            if g["n"] >= 3:
                out.append(rec("catalog_gap", "gaps", 30 + min(g["n"] * 2, 20), f"Bulk order: no {g['what']} in the catalog ({g['n']} lines)",
                    "Customers ask for it by name. A sourcing question for purchasing, not a search fix.",
                    [f"e.g. “{g['example'][:70]}”"], source="Bulk order", key=g["what"]))
        return out

    # ------------------------------------------------------------------ the settings themselves
    def _settings_health(self, s: dict, days: int) -> list[dict]:
        out = []
        cat = self.e.catalog
        today = str(date.today())
        rr = self.a.rule_report(days)["rules"] if self.a is not None else {}
        n_search = (self.a.counts() or {}).get("searches", 0) if self.a is not None else 0
        status = lambda sku: str(cat.status[cat.sku_to_idx[sku]]) if sku in cat.sku_to_idx else None  # noqa: E731
        for r in s.get("rules") or []:
            nm = r["name"]
            if r.get("end") and r["end"] < today:
                out.append(rec("rule_expired", "cleanup", 22, f"Rule “{nm}” ended {r['end']}",
                    "It does nothing now. Remove it to keep the list readable (the change log keeps it).",
                    [], actions=[draft("Remove from draft", {"op": "remove", "list": "rules", "id": r["id"]})], source="settings", key=r["id"]))
                continue
            skus = [p["sku"] for p in r.get("pin") or []] + list(r.get("hide") or []) + [b["value"] for b in r.get("boost") or [] if b["field"] == "sku"]
            missing = [x for x in skus if status(x) is None]
            disc = [x for x in skus if status(x) == "D" and x in [p["sku"] for p in r.get("pin") or []]]
            if missing:
                out.append(rec("rule_missing_sku", "data", 58, f"Rule “{nm}” points at {len(missing)} item(s) not in the catalog",
                    "Pins and boosts on items that no longer exist do nothing; the rule looks active and isn't.",
                    [f"#{', #'.join(missing[:5])}"], actions=[go("rules", "Open rules", rule=r["id"])], source="settings", key=r["id"]))
            if disc:
                out.append(rec("rule_disc_pin", "data", 52, f"Rule “{nm}” pins a discontinued item",
                    "Customers see “Discontinued - see replacements” in the top spot. Pin the replacement.",
                    [f"#{', #'.join(disc[:5])}"], actions=[go("rules", "Open rules", rule=r["id"])], source="settings", key=r["id"] + "d"))
            if r["enabled"] and r["match"]["type"] == "contains" and r.get("pin"):
                exact = copy.deepcopy(r)
                exact["match"]["type"] = "exact"
                out.append(rec("contains_pin", "rules", 56, f"Rule “{nm}” pins items on every search containing “{r['match']['q']}”",
                    "A pin on a containing rule lands on “1/2 …”, “3 way …” and pasted lists too, usually above the item those customers pick. "
                    "Exact phrases keep the promotion where it fits. Run the analysis to measure it on replayed searches.",
                    [], actions=[draft("Switch to exact in draft", {"op": "patch", "list": "rules", "id": r["id"], "value": {"match": exact["match"]}}),
                                 go("rules", "Open rules", rule=r["id"])], source="settings", key=r["id"]))
            started = not r.get("start") or r["start"] <= today
            if r["enabled"] and started and r["match"]["type"] != "any" and n_search >= 100 and not (rr.get(r["id"]) or {}).get("searches"):
                q1 = r["match"]["q"].split(" | ")[0]
                out.append(rec("rule_idle", "cleanup", 18, f"Rule “{nm}” didn't fire in {days} days",
                    "No search matched its words and audience. Check the phrases against what people type (Historical log), or remove it.",
                    [], actions=[look(q1), go("rules", "Open rules", rule=r["id"])], source="settings", key=r["id"] + "idle"))
        carried = {str(b).lower() for b in set(cat.brand.tolist())}
        for c in s.get("crosses") or []:
            if not c.get("enabled", True):
                continue
            if c["kind"] == "brand":
                if c["to_brand"].lower() not in carried:
                    out.append(rec("cross_to_missing", "data", 62, f"Cross {c['label']}: Webb's catalog has no {c['to_brand']}",
                        "Lines crossed to it search for a brand with no items and fall back to their other words.",
                        [], actions=[go("crosses", "Open Crosses")], source="settings", key=c["id"]))
                if c["from_brand"].lower() in carried:
                    out.append(rec("cross_from_carried", "data", 38, f"Cross {c['label']}: Webb carries {c['from_brand']}",
                        f"Lines naming {c['from_brand']} will never get {c['from_brand']} items. Intended for a discontinued line? Otherwise remove it.",
                        [], actions=[go("crosses", "Open Crosses")], source="settings", key=c["id"] + "c"))
            elif status(c["to_sku"]) in (None, "D"):
                out.append(rec("cross_sku", "data", 55, f"Item cross {c['label']}: the item is {'not in the catalog' if status(c['to_sku']) is None else 'discontinued'}",
                    "Update it to the replacement item.", [], actions=[go("crosses", "Open Crosses")], source="settings", key=c["id"]))
        bc = self._brand_counts()
        for b in s.get("brand_prefs") or []:
            zero = [x for x in b["brands"] if not bc.get((b["family"].lower(), x.lower()))]
            if zero:
                out.append(rec("pref_empty", "data", 40, f"Brand order {b['label']}: no {', '.join(zero)} items in {b['family']}",
                    "Those positions do nothing. Check the brand's spelling or the category.", [],
                    actions=[go("brandprefs", "Open Brand preferences")], source="settings", key=b["id"]))
        for w in s.get("rewrites") or []:
            if not w.get("enabled", True):
                continue
            try:
                n = self.e.results.results(w["to"], _anon(), llm="off", merch=False).get("total", 0)
            except Exception:
                n = None
            if n == 0:
                out.append(rec("rewrite_zero", "data", 60, f"Rewrite “{w['from']}” → “{w['to']}” finds nothing",
                    "Every search containing the word now returns no products. Fix the replacement words.", [],
                    actions=[go("synonyms", "Open Rewrites")], source="settings", key=w["id"]))
        return out

    # ------------------------------------------------------------------ catalog data checks (cached)
    def _catalog_checks(self) -> dict:
        if self._catalog is not None:
            return copy.deepcopy(self._catalog)
        cat = self.e.catalog
        n_all = len(cat.sku)
        step = max(1, n_all // 120_000)
        idx = range(0, n_all, step)
        sizes, bad_size, fmt = Counter(), Counter(), Counter()
        bad_ex: dict = defaultdict(list)
        tmis, tsz = Counter(), Counter()
        tmis_ex: dict = defaultdict(list)
        keys_by_cat: dict = defaultdict(Counter)
        n_by_cat = Counter()
        for i in idx:
            a = cat.attrs(i)
            c3 = str(cat.cat3[i])
            n_by_cat[c3] += 1
            keys_by_cat[c3].update(a.keys())
            z = a.get("size")
            if not z:
                continue
            z = str(z)
            sizes[z] += 1
            if not STD_SIZE.match(z):
                bad_size[c3] += 1
                f = "range" if " - " in z else "with a unit" if re.search(r"[a-z]", z) else "other"
                fmt[f] += 1
                if len(bad_ex[f]) < 3 and z not in bad_ex[f]:
                    bad_ex[f].append(z)
            found = TITLE_SIZE.findall(str(cat.title[i]))
            if found and STD_SIZE.match(z):
                tsz[c3] += 1
                if z not in found:
                    tmis[c3] += 1
                    if len(tmis_ex[c3]) < 2:
                        tmis_ex[c3].append(f"#{cat.sku[i]} “{str(cat.title[i])[:60]}” has size {z}")
        scale = n_all / max(len(idx), 1)
        rows, recs = [], []
        nb = sum(bad_size.values())
        rows.append({"check": "Nominal size in a standard form (1/2, 1-1/4, 2)", "status": "issue" if nb else "ok",
                     "affected": f"≈{round(nb * scale):,} items" if nb else "0",
                     "detail": "; ".join(f"{k}: {', '.join(bad_ex[k])}" for k in fmt) or "all sizes standard"})
        if nb:
            top = ", ".join(f"{c} ({round(n * scale):,})" for c, n in bad_size.most_common(4))
            recs.append(rec("size_format", "data", 46, f"Sizes stored in {len(fmt) + 1} formats (≈{round(nb * scale):,} items)",
                "The size filter lists “10 in.” apart from “10”, size grouping sorts a range by its first size, and a search for “10 inch …” "
                "misses the unit form. Normalize in the PIM: nominal size without the unit, ranges in their own attribute.",
                [f"{k}: {', '.join(bad_ex[k])}" for k in fmt] + [f"most in {top}"], source="catalog", key="sizefmt"))
        mis = [(tmis[c] / tsz[c], c) for c in tsz if tsz[c] >= 60 and tmis[c] / tsz[c] >= 0.05]
        rows.append({"check": "Title size agrees with the size attribute", "status": "issue" if mis else "ok",
                     "affected": f"{len(mis)} categories" if mis else "0",
                     "detail": ", ".join(f"{c} {r * 100:.0f}%" for r, c in sorted(mis, reverse=True)[:4])
                     or f"{sum(tsz.values()):,} sampled items give a size in the title; every category agrees with its attribute (< 5% differ)"})
        for r_, c in sorted(mis, reverse=True)[:3]:
            recs.append(rec("title_size", "data", 34 + r_ * 30, f"{c}: {r_ * 100:.0f}% of titles give a different size than the size attribute",
                "Filters and grouping use the attribute; customers read the title. One of them is wrong.",
                tmis_ex[c], source="catalog", key=c))
        gaps = []
        demand = self.h.df()["family"].value_counts().head(25).index if (self.h is not None and self.h.ok) else list(n_by_cat)[:25]
        for c in demand:
            n = n_by_cat.get(c, 0)
            if n < 50:
                continue
            for k, v in keys_by_cat[c].items():
                if 0.5 <= v / n < 0.9:
                    gaps.append((c, k, v / n))
        rows.append({"check": "Attribute coverage in the 25 most-searched categories (≥ 90% of items)", "status": "issue" if gaps else "ok",
                     "affected": f"{len(gaps)} attributes" if gaps else "0", "detail": ", ".join(f"{c}: {k} {r * 100:.0f}%" for c, k, r in gaps[:4])
                     or "every attribute a category uses is on 90% or more of its items"})
        for c, k, r_ in gaps[:4]:
            recs.append(rec("attr_gap", "data", 40, f"{c}: {k} missing on {100 - r_ * 100:.0f}% of items",
                "Items without it drop out of that filter and out of size grouping. A candidate for Claude enrichment with review.",
                [], source="catalog", key=f"{c}:{k}"))
        idn = getattr(cat, "_idn", {}) or {}
        dup = len(idn)
        rows.append({"check": "Part numbers and UPCs unique", "status": "issue" if dup else "ok", "affected": f"{dup:,} shared" if dup else "0",
                     "detail": "an exact search for a shared number shows several items" if dup else "each number belongs to one item"})
        if dup:
            exs = [f"{k}: " + ", ".join(f"#{cat.sku[j]} {cat.brand[j]}" for j in v[:3]) for k, v in list(idn.items())[:3]]
            recs.append(rec("dup_ids", "data", 36, f"{dup:,} part numbers or UPCs belong to more than one item",
                "An exact search for one of them shows several items as “Exact part #”. Usually a data-entry duplicate or a UPC reused "
                "across pack sizes; fix in the item master.", exs, source="catalog", key="dupids"))
        lp = [float(cat.list_price[i]) for i in idx]
        nz = sum(1 for x in lp if x <= 0)
        rows.append({"check": "List price present", "status": "issue" if nz else "ok", "affected": f"≈{round(nz * scale):,}" if nz else "0",
                     "detail": "items without a price" if nz else f"all {len(lp):,} sampled items have one"})
        st = Counter(str(cat.status[i]) for i in idx)
        rows.append({"check": "Discontinued and superseded items", "status": "info",
                     "affected": f"≈{round(st.get('D', 0) * scale):,} D · ≈{round(st.get('S', 0) * scale):,} S",
                     "detail": "searchable with a “see replacements” note; the analysis checks whether they reach page one"})
        acc = self.e.accounts.accounts
        noseg = int(acc["segment"].isna().sum()) if "segment" in acc else 0
        rows.append({"check": "Accounts with a discipline (trade)", "status": "issue" if noseg else "ok", "affected": f"{noseg:,}" if noseg else "0",
                     "detail": "without one, brand affinity uses the average of the table" if noseg else f"all {len(acc):,} accounts"})
        lines = getattr(self.e.features, "acct_lines", {}) or {}
        mn = self.e.merch.get()["affinity"]["min_lines"]
        below = sum(1 for a_ in acc.index if lines.get(a_, 0) < mn)
        rows.append({"check": f"Accounts with enough history for brand affinity (≥ {mn} order lines)", "status": "info",
                     "affected": f"{below:,} of {len(acc):,} below", "detail": "they get purchase weights but no brand affinity"})
        self._catalog = {"rows": rows, "recs": recs, "sampled": len(idx), "of": n_all}
        return copy.deepcopy(self._catalog)

    # ------------------------------------------------------------------ deep run: replay-tested recommendations
    def _deep_summary(self, s: dict) -> dict:
        with self.lock:
            st = dict(self.deep_state)
        d = self.deep or {}
        return {**st, "at": d.get("at"), "by": d.get("by"), "seconds": d.get("seconds"), "n": d.get("n"),
                "settings_version": d.get("settings_version"), "stale": bool(d) and d.get("settings_version") != s.get("version"),
                "baseline": d.get("baseline"), "tested": d.get("tested", []), "segments": d.get("segments", []),
                "pins_checked": d.get("pins_checked"), "rules_checked": d.get("rules_checked", []), "discontinued": d.get("discontinued")}

    def start_deep(self, who: str = "", n: int = 150) -> bool:
        if self.h is None or not self.h.ok:
            return False
        with self.lock:
            if self.deep_state["running"]:
                return False
            self.deep_state = {"running": True, "done": 0, "total": 1, "step": "preparing", "started": time.strftime("%H:%M:%S")}
        threading.Thread(target=self._deep_run, args=(who, n), daemon=True).start()
        return True

    def _tick(self, step: str | None = None, add: int = 1):
        with self.lock:
            self.deep_state["done"] += add
            if step:
                self.deep_state["step"] = step

    def _deep_run(self, who: str, n: int):
        t0 = time.time()
        try:
            s = copy.deepcopy(self.e.merch.get())
            sample = self.au.sample(n, "recent")
            rows = [(r.query, self._principal(r.account_id, r.user_id), str(r.clicked_sku), r.segment) for r in sample.itertuples()]
            variants = self._variants(s)
            summ = self.h.summary()
            have_exact = {q for x in s["rules"] if x["match"]["type"] == "exact" for q in x["match"]["q"].split(" | ")}
            heads = [t for t in summ["top_queries"] if t["n"] >= 15 and not PARTISH.match(t["qn"]) and " " in t["qn"]]
            rules = [r for r in s["rules"] if r["enabled"] and (r.get("pin") or r.get("boost") or r.get("only") or r.get("exclude"))]
            with self.lock:
                self.deep_state["total"] = len(rows) * (len(variants) + 2) + len(heads) + 12 * 2 * 12 + len(rules) * 90
            def run(settings, label):
                self._tick(label, 0)
                out = []
                for q, p, sku, _ in rows:
                    out.append(self._pos(q, p, sku, settings)[0])
                    self._tick()
                return out
            base = run(s, "baseline: live settings")
            bm = metrics(base)
            tested, recs = [], []
            by_variant = {}
            for path, value, label in variants:
                s2 = copy.deepcopy(s)
                if path[0] == "affinity_scale":
                    for sg in s2["affinity"]["segments"].values():
                        for k in ("within", "across"):
                            sg[k] = round(max(LIMITS["affinity"][0], min(LIMITS["affinity"][1], sg[k] * value)), 3)
                else:
                    s2[path[0]][path[1]] = value
                pos = run(s2, f"testing {label}")
                by_variant[(tuple(path), value)] = pos
                m = metrics(pos)
                better = sum(1 for a, b in zip(pos, base) if _better(a, b))
                worse = sum(1 for a, b in zip(pos, base) if _better(b, a))
                tested.append({"label": label, "path": list(path), "value": value, "metrics": m, "better": better, "worse": worse,
                               "d_mrr": round(m["mrr"] - bm["mrr"], 4), "d_hit1": round(m["hit1"] - bm["hit1"], 4)})
            recs += self._weight_recs(s, bm, tested, len(rows))
            segs, seg_recs = self._affinity_recs(s, rows, base, by_variant)
            recs += seg_recs
            pins_checked, pin_recs, disc = self._head_pins(s, heads, have_exact)
            recs += pin_recs
            rules_checked, rule_recs = self._rule_impact(s, rules)
            recs += rule_recs
            if disc["queries"] >= 3:
                recs += self._bury_discontinued(s, rows, base, bm, disc, run)
            for t in tested:
                t["verdict"] = "recommended" if any(r.get("check", {}).get("type") == "weight" and r["check"]["path"] == t["path"]
                                                    and r["check"]["value"] == t["value"] for r in recs) else \
                    "better" if t["d_mrr"] > 0 and t["better"] > t["worse"] else "worse" if t["d_mrr"] < 0 else "no change"
            if not any(r["kind"] in ("weight", "affinity") for r in recs):
                recs.append(rec("weights_hold", "ranking", 14, "The current weights hold up: no single change improved the replayed searches",
                    f"Each weight was tried up and down, one at a time, on {len(rows)} logged searches (table below). None raised MRR by 0.004 with more searches "
                    "helped than hurt. Re-run after a few weeks of real traffic.", [], source="audit replay", key=f"hold{s.get('version')}"))
            out = {"at": time.strftime("%Y-%m-%d %H:%M"), "by": who or "", "n": len(rows), "seconds": round(time.time() - t0, 1),
                   "settings_version": s.get("version", 0), "baseline": bm, "tested": tested, "segments": segs,
                   "pins_checked": pins_checked, "rules_checked": rules_checked, "discontinued": disc, "recs": recs}
            self.root.mkdir(parents=True, exist_ok=True)
            self.deep_path.write_text(json.dumps(out))
            with self.lock:
                self.deep = out
        except Exception as ex:
            with self.lock:
                self.deep_state["error"] = f"{type(ex).__name__}: {ex}"
        finally:
            with self.lock:
                self.deep_state["running"] = False
                self.deep_state["step"] = "done"

    @staticmethod
    def _variants(s: dict) -> list:
        w = s["weights"]
        out = []
        lo, hi = LIMITS["weights"]

        def add(k, vals):
            for v in vals:
                v = round(max(lo, min(hi, v)), 3)
                if v != w[k] and ([k], v) not in [(p[1:], x) for p, x, _ in out]:
                    out.append((["weights", k], v, f"{WLABEL[k]} {w[k]} → {v}"))
        add("purchased", [w["purchased"] * 0.5, w["purchased"] * 2 if w["purchased"] else BASE["purchased"]])
        add("purchased_times", [w["purchased_times"] * 2 if w["purchased_times"] else BASE["purchased_times"]])
        add("purchased_recent", [w["purchased_recent"] * 2 if w["purchased_recent"] else BASE["purchased_recent"]])
        for k in ("popularity", "stocked", "branch_stock"):
            add(k, [w[k] * 2 if w[k] else BASE[k]])
        cap = s["layout"]["brand_cap"]
        for v in ([0, max(1, cap - 2), cap + 2] if cap else [4]):
            if v != cap:
                out.append((["layout", "brand_cap"], v, f"Brand cap {cap} → {v}"))
        if s["affinity"]["enabled"]:
            for f in (0.5, 2.0):
                out.append((["affinity_scale", "all"], f, f"Brand affinity × {f:g} (every discipline)"))
        return out

    def _weight_recs(self, s: dict, bm: dict, tested: list, n: int) -> list[dict]:
        best: dict = {}
        for t in tested:
            if t["path"][0] == "affinity_scale":
                continue
            ok = t["d_mrr"] >= 0.004 and t["d_hit1"] >= 0 and t["better"] >= t["worse"] + 3
            k = tuple(t["path"])
            if ok and (k not in best or t["d_mrr"] > best[k]["d_mrr"]):
                best[k] = t
        out = []
        for k, t in best.items():
            sect, key = k
            cur = s[sect][key]
            m = t["metrics"]
            lab = WLABEL.get(key, "Brand cap")
            out.append(rec("weight", "ranking", 58 + min(t["d_mrr"] * 800, 30), f"{lab}: {cur} → {t['value']} helped the replayed searches",
                f"On {n} logged searches replayed as the accounts that searched, the item each customer clicked moved up for {t['better']} and down for {t['worse']}. "
                "Samples this size move by a few searches: preview it, then confirm with an A/B test before publishing.",
                [f"clicked item #1: {bm['hit1'] * 100:.1f}% → {m['hit1'] * 100:.1f}%", f"top 10: {bm['hit10'] * 100:.1f}% → {m['hit10'] * 100:.1f}%",
                 f"MRR: {bm['mrr']:.3f} → {m['mrr']:.3f}"],
                impact=f"MRR +{t['d_mrr']:.3f}", source="audit replay",
                actions=[draft("Set in draft", {"op": "set", "path": [sect, key], "value": t["value"]}), go("tests", "A/B test it")],
                key=f"{sect}.{key}={t['value']}", check={"type": "weight", "path": [sect, key], "value": t["value"]}))
        return out

    def _affinity_recs(self, s: dict, rows, base, by_variant) -> tuple[list, list]:
        segs, out = [], []
        half = by_variant.get((("affinity_scale", "all"), 0.5))
        dbl = by_variant.get((("affinity_scale", "all"), 2.0))
        if not half or not dbl:
            return segs, out
        groups = defaultdict(list)
        for k, (_, _, _, sg) in enumerate(rows):
            groups[sg or "unknown"].append(k)
        for sg, ks in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            b = metrics([base[k] for k in ks])
            h = metrics([half[k] for k in ks])
            d = metrics([dbl[k] for k in ks])
            row = {"segment": sg, "n": len(ks), "base": b, "half": h, "double": d}
            segs.append(row)
            if sg not in SEGMENTS or len(ks) < 8:
                continue
            for f, m, pos in ((0.5, h, half), (2.0, d, dbl)):
                better = sum(1 for k in ks if _better(pos[k], base[k]))
                worse = sum(1 for k in ks if _better(base[k], pos[k]))
                if m["mrr"] - b["mrr"] >= 0.01 and better >= worse + 2 and m["hit1"] >= b["hit1"]:
                    cur = s["affinity"]["segments"][sg]
                    new = {k: round(max(LIMITS["affinity"][0], min(LIMITS["affinity"][1], cur[k] * f)), 3) for k in ("within", "across")}
                    out.append(rec("affinity", "ranking", 52 + min((m["mrr"] - b["mrr"]) * 400, 25),
                        f"Brand affinity for {SEGMENTS[sg]}: {'halve' if f < 1 else 'double'} it ({cur['within']}/{cur['across']} → {new['within']}/{new['across']})",
                        f"On the {len(ks)} replayed searches by {SEGMENTS[sg].lower()} accounts, the clicked item moved up for {better} and down for {worse}. "
                        "Only this discipline's weights change.",
                        [f"clicked item #1: {b['hit1'] * 100:.0f}% → {m['hit1'] * 100:.0f}%", f"MRR: {b['mrr']:.3f} → {m['mrr']:.3f}"],
                        impact=f"MRR +{m['mrr'] - b['mrr']:.3f} for {SEGMENTS[sg]}", source="audit replay",
                        actions=[draft("Set in draft", *[{"op": "set", "path": ["affinity", "segments", sg, k], "value": v} for k, v in new.items()]),
                                 go("affinity", "Open Brand affinity")],
                        key=f"aff:{sg}:{f}", check={"type": "affinity", "segments": {sg: new}}))
                    break
        return segs, out

    def _head_pins(self, s: dict, heads: list, have_exact: set) -> tuple[dict, list, dict]:
        from webbsearch.retrieval.entitlements import ANONYMOUS
        cat = self.e.catalog
        out, checked = [], {"queries": 0, "candidates": 0, "recommended": 0}
        disc = {"queries": 0, "items": 0, "examples": []}
        d = self.h.df()
        months = max(1.0, (d["ts"].max() - d["ts"].min()).days / 30.4)
        cands = []
        for t in heads:
            pos, r = self._pos(t["q"], ANONYMOUS, t["top_sku"], s)
            self._tick("checking head searches")
            checked["queries"] += 1
            dn = [k + 1 for k, it in enumerate((r.get("items") or [])[:24]) if it.get("status") == "D"]
            if dn:
                disc["queries"] += 1
                disc["items"] += len(dn)
                disc["examples"].append((dn[0], f"“{t['q']}” ({t['n']} searches): discontinued at {', '.join('#' + str(x) for x in dn[:3])}"))
            if t["qn"] in have_exact or t["top_share"] < 0.4 or t["top_sku"] not in cat.sku_to_idx:
                continue
            if pos is None or pos > 3:
                cands.append((t, pos))
        disc["examples"] = [e for _, e in sorted(disc["examples"])[:6]]       # the highest-placed first
        cands.sort(key=lambda x: -x[0]["n"] * x[0]["top_share"])
        for t, gpos in cands[:12]:
            checked["candidates"] += 1
            logs = d[d["qn"] == t["qn"]].sort_values("ts", ascending=False).head(12)
            rule = validate_rule({"name": f"Lift #{t['top_sku']} for “{t['qn']}”", "match": {"type": "exact", "q": t["qn"]},
                                  "boost": [{"field": "sku", "value": t["top_sku"], "weight": 10}],
                                  "note": f"From Recommendations: {round(t['top_share'] * 100)}% of {t['n']} logged searches pick this item"})
            s2 = {**s, "rules": s["rules"] + [rule]}
            before, after = [], []
            for r in logs.itertuples():
                p = self._principal(r.account_id, r.user_id)
                before.append(self._pos(r.query, p, str(r.clicked_sku), s)[0])
                after.append(self._pos(r.query, p, str(r.clicked_sku), s2)[0])
                self._tick("testing lifts for head searches", 2)
            mb, ma = metrics(before), metrics(after)
            if ma["hit1"] <= mb["hit1"]:
                continue
            checked["recommended"] += 1
            per_month = t["n"] / months
            title = str(cat.title[cat.sku_to_idx[t["top_sku"]]])
            out.append(rec("pin", "rules", 50 + min(t["n"] / 4, 25) + (ma["hit1"] - mb["hit1"]) * 20,
                f"“{t['q']}”: lift the item {round(t['top_share'] * 100)}% of customers pick (now {'not on the page' if gpos is None else f'#{gpos}'} for guests)",
                f"{title[:90]} (#{t['top_sku']}). A rule for this exact search adds +10 to it: first on the page without a “Featured” badge, "
                "and personal history still moves an account's own items above it.",
                [f"{t['n']} logged searches (≈{per_month:.0f} a month), {round(t['top_share'] * 100)}% clicked this item",
                 f"replaying the latest {len(before)}: clicked item #1 {mb['hit1'] * 100:.0f}% → {ma['hit1'] * 100:.0f}%, MRR {mb['mrr']:.2f} → {ma['mrr']:.2f}"],
                impact=f"≈{per_month * (ma['hit1'] - mb['hit1']):.1f} more searches a month with the clicked item first",
                source="historical log + audit replay",
                actions=[draft("Add rule to draft", {"op": "append", "list": "rules", "value": rule}), look(t["q"])],
                key=f"{t['qn']}:{t['top_sku']}", check={"type": "query_rule", "q": t["qn"]}))
        return checked, out, disc

    def _rule_impact(self, s: dict, rules: list) -> tuple[list, list]:
        out, checked = [], []
        d = self.h.df()
        today = date.today()
        qn_counts = d["qn"].value_counts()
        for r in rules:
            if r["match"]["type"] == "any":
                continue
            loose = {**r, "audience": {"who": "all", "segments": [], "roles": []}, "start": None, "end": None, "enabled": True}
            qs = [q for q in qn_counts.index[:4000] if rule_matches(loose, q, today, None)][:30]
            if not qs:
                checked.append({"rule": r["name"], "searches": 0, "note": "no logged search matches its words"})
                continue
            logs = d[d["qn"].isin(qs)].sort_values("ts", ascending=False).groupby("qn").head(3).head(45)
            without = copy.deepcopy(s)
            for x in without["rules"]:
                if x["id"] == r["id"]:
                    x["enabled"] = False
            narrowed = None
            if r["match"]["type"] == "contains":
                narrowed = copy.deepcopy(s)
                for x in narrowed["rules"]:
                    if x["id"] == r["id"]:
                        x["match"]["type"] = "exact"
            live, off, nar, used = [], [], [], 0
            for row in logs.itertuples():
                p = self._principal(row.account_id, row.user_id)
                if not rule_matches(r, row.qn, today, p):
                    continue
                used += 1
                live.append(self._pos(row.query, p, str(row.clicked_sku), s)[0])
                off.append(self._pos(row.query, p, str(row.clicked_sku), without)[0])
                if narrowed is not None:
                    nar.append(self._pos(row.query, p, str(row.clicked_sku), narrowed)[0])
                self._tick(f"testing rule “{r['name'][:30]}”", 2)
            if not used:
                checked.append({"rule": r["name"], "searches": 0, "note": "its audience didn't search these words"})
                continue
            ml, mo = metrics(live), metrics(off)
            mn = metrics(nar) if nar else None
            better = sum(1 for a, b in zip(off, live) if _better(a, b))
            worse = sum(1 for a, b in zip(off, live) if _better(b, a))
            checked.append({"rule": r["name"], "searches": used, "queries": len(set(logs["qn"])), "live": ml, "without": mo, "narrowed": mn})
            if mo["mrr"] - ml["mrr"] >= 0.02 and better >= worse + 3:
                acts = [draft("Switch it off in draft", {"op": "patch", "list": "rules", "id": r["id"], "value": {"enabled": False}})]
                narrow_txt = ""
                if mn is not None:
                    acts.insert(0, draft("Narrow to exact in draft", {"op": "patch", "list": "rules", "id": r["id"],
                                                                      "value": {"match": {"type": "exact", "q": r["match"]["q"]}}}))
                    narrow_txt = f" Narrowed to exact phrases: clicked item #1 {mn['hit1'] * 100:.0f}%, MRR {mn['mrr']:.2f}."
                acts.append(go("rules", "Open rules", rule=r["id"]))
                out.append(rec("rule_cost", "rules", 68 + min((mo["mrr"] - ml["mrr"]) * 100, 25),
                    f"Rule “{r['name']}” pushes down what customers pick on {used} replayed searches",
                    ("If it's a paid or planned promotion, keep it and narrow it; otherwise switch it off." if r.get("pin") or r.get("badge") != "Featured" else
                     "Switch it off, or narrow its words.") + narrow_txt,
                    [f"with the rule: clicked item #1 {ml['hit1'] * 100:.0f}%, MRR {ml['mrr']:.2f}",
                     f"without it: {mo['hit1'] * 100:.0f}%, MRR {mo['mrr']:.2f} ({better} searches better, {worse} worse)",
                     f"{len(set(logs['qn']))} distinct searches, e.g. “{logs['query'].iloc[0]}”"],
                    impact=f"MRR +{mo['mrr'] - ml['mrr']:.2f} on its searches without it", source="audit replay",
                    actions=acts, key=r["id"], check={"type": "rule", "id": r["id"], "match_type": r["match"]["type"]}))
        return checked, out

    def _bury_discontinued(self, s: dict, rows, base, bm, disc, run) -> list[dict]:
        rule = validate_rule({"name": "Bury discontinued items", "match": {"type": "any"},
                              "boost": [{"field": "status", "value": "discontinued", "weight": -3}],
                              "note": "From Recommendations: discontinued items reached page one of head searches"})
        s2 = {**s, "rules": s["rules"] + [rule]}
        pos = run(s2, "testing: bury discontinued items")
        m = metrics(pos)
        worse = sum(1 for a, b in zip(pos, base) if _better(b, a))
        if m["mrr"] < bm["mrr"] - 0.002:
            return []
        return [rec("bury_disc", "rules", 48 + min(disc["queries"], 20), f"Discontinued items reach page one on {disc['queries']} head searches",
            "Customers see “Discontinued - see replacements” where a stocked item could be. A rule for every search buries them (−3) without hiding them.",
            disc["examples"][:3] + [f"on {len(pos)} replayed searches: MRR {bm['mrr']:.3f} → {m['mrr']:.3f}, {worse} searches worse"],
            source="audit replay", actions=[draft("Add rule to draft", {"op": "append", "list": "rules", "value": rule})],
            key="burydisc", check={"type": "status_bury"})]


def _anon():
    from webbsearch.retrieval.entitlements import ANONYMOUS
    return ANONYMOUS
