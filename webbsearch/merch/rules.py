"""How merchandising settings act on one search.

Order of operations on the results page:
  1. rewrite_query   "tstat" -> "thermostat" (whole words; the page says it was rewritten)
  2. active_rules    rules whose query matches (exact / contains / any; "a | b" = either), whose audience includes
                     this customer (everyone, guests, signed-in; disciplines; roles) and whose dates include today
  3. extra_pins      pinned items the search didn't retrieve are added to the candidates
  4. adjust_scores   ranking weights (previously purchased, brand affinity per discipline, branch stock, stocked,
                     popularity) and rule boosts / buries (by brand, category, item, stocking level, title words, spec,
                     lead-free), added to the ranker's score before sorting; "only show" and hide conditions filter
  5. diversify       at most `layout.brand_cap` items from one brand in the first 24 (not when the search names
                     a brand or a part number)
  6. place_pins      pinned items go to their positions; hidden items leave the page
Rule shape: see store.validate_rule.
"""
from __future__ import annotations

import re
from datetime import date
from functools import lru_cache

import numpy as np

from webbsearch.merch.store import norm_q


def rewrite_query(q: str, s: dict) -> tuple[str, list[dict]]:
    applied = []
    out = q
    for r in s.get("rewrites") or []:
        if not r.get("enabled", True):
            continue
        pat = _phrase_re_i(r["from"])
        if pat.search(out):
            out = pat.sub(r["to"], out)
            applied.append({"from": r["from"], "to": r["to"]})
    return re.sub(r"\s+", " ", out).strip(), applied


def audience_ok(r: dict, p) -> bool:
    """Everyone, guests only, or signed-in customers (optionally only some disciplines and roles)."""
    a = r.get("audience") or {}
    who = a.get("who", "all")
    signed = bool(p is not None and getattr(p, "account_id", None))
    if who == "guests":
        return not signed
    if who == "signed_in" and not signed:
        return False
    if a.get("segments") and (not signed or getattr(p, "segment", None) not in a["segments"]):
        return False
    if a.get("roles") and (not signed or getattr(p, "role", None) not in a["roles"]):
        return False
    return True


def rule_matches(r: dict, qn: str, today: date, p=None) -> bool:
    if not r.get("enabled", True):
        return False
    if r.get("start") and str(today) < r["start"]:
        return False
    if r.get("end") and str(today) > r["end"]:
        return False
    if not audience_ok(r, p):
        return False
    m = r["match"]
    if m["type"] == "any":
        return True
    phrases = [x.strip() for x in m["q"].split("|") if x.strip()]
    if m["type"] == "exact":
        return qn in phrases
    return any(_phrase_re(x).search(qn) for x in phrases)


@lru_cache(maxsize=4096)
def _phrase_re(x: str) -> re.Pattern:
    return re.compile(rf"(?<![\w-]){re.escape(x)}(?![\w-])")


@lru_cache(maxsize=4096)
def _phrase_re_i(x: str) -> re.Pattern:
    return re.compile(rf"(?<![\w-]){re.escape(x)}(?![\w-])", re.I)


def active_rules(s: dict, q: str, today: date, p=None) -> list[dict]:
    qn = norm_q(q)
    return [r for r in s.get("rules") or [] if rule_matches(r, qn, today, p)]


def pinned_skus(rules: list[dict]) -> list[tuple[str, int]]:
    seen, out = set(), []
    for r in rules:
        for p in r.get("pin") or []:
            if p["sku"] not in seen:
                seen.add(p["sku"])
                out.append((p["sku"], p["pos"]))
    return out


def pin_badges(rules: list[dict]) -> dict[str, str]:
    """sku -> the badge its rule gives it ("Featured", "Sponsored", "New"...)"""
    out = {}
    for r in rules:
        for p in r.get("pin") or []:
            out.setdefault(p["sku"], r.get("badge") or "Featured")
    return out


def hidden_skus(rules: list[dict]) -> set[str]:
    return {h for r in rules for h in r.get("hide") or []}


def cond_mask(c: dict, idx: np.ndarray, cat, attr_fn=None) -> np.ndarray:
    """Which candidates a condition picks: brand, category, item #, stocking level, title words, spec (key=value)
    or lead-free. `attr_fn(i)` returns an item's specs (the results page adds connection and material groups)."""
    f, v = c["field"], str(c["value"]).strip()
    vl = v.lower()
    if f == "brand":
        return np.array([str(x).lower() == vl for x in cat.brand[idx]], bool)
    if f == "family":
        return np.array([str(x).lower() == vl or str(y).lower() == vl for x, y in zip(cat.family[idx], cat.cat3[idx])], bool)
    if f == "sku":
        return np.array([str(x) == v for x in cat.sku[idx]], bool)
    if f == "stock":
        return np.array([str(x) == vl for x in cat.stock_tier[idx]], bool)
    if f == "title":
        words = [w for w in re.split(r"\s+", vl) if w]
        return np.array([all(w in str(t).lower() for w in words) for t in cat.title[idx]], bool)
    if f == "leadfree":
        return np.array(["lead-free" in str(t).lower() or "leadfree" in str(t).lower() for t in cat.title[idx]], bool)
    if f == "status":
        code = {"active": "A", "discontinued": "D", "superseded": "S"}.get(vl, "?")
        return np.array([str(x) == code for x in cat.status[idx]], bool)
    if f == "attr":
        k, _, val = v.partition("=")
        k, val = k.strip().lower(), val.strip().lower()
        get = attr_fn or (lambda i: cat.attrs(int(i)))
        return np.array([str((get(int(i)) or {}).get(k, "")).lower() == val for i in idx], bool)
    return np.zeros(len(idx), bool)


def any_mask(conds: list[dict], idx: np.ndarray, cat, attr_fn=None) -> np.ndarray:
    m = np.zeros(len(idx), bool)
    for c in conds:
        m |= cond_mask(c, idx, cat, attr_fn)
    return m


def affinity_weights(settings: dict, segment: str | None) -> tuple[float, float]:
    """(within, across) brand-affinity weights for a customer discipline."""
    af = settings.get("affinity") or {}
    if not af.get("enabled", True):
        return 0.0, 0.0
    segs = af.get("segments") or {}
    w = segs.get(segment or "") or {}
    if not w:            # a discipline not in the table: the average of the table
        vals = list(segs.values()) or [{"within": 0.0, "across": 0.0}]
        return (sum(x.get("within", 0) for x in vals) / len(vals), sum(x.get("across", 0) for x in vals) / len(vals))
    return float(w.get("within", 0)), float(w.get("across", 0))


def adjust_scores(s: np.ndarray, X: np.ndarray, idx: np.ndarray, cat, F: dict, settings: dict, personal: bool,
                  rules: list[dict], at_branch=None, acct: dict | None = None, attr_fn=None,
                  explain: dict | None = None, field_adj: dict | None = None) -> np.ndarray:
    """Ranking weights, brand affinity and rule boosts, added to the ranker's scores (same order as idx / X rows).
    `acct`: {"segment", "lines", "brand_share"} for the signed-in account (brand_share: brand -> share of all its
    lines). `explain`, if given, is filled with each adjustment's array (for the dashboard's "why")."""
    w = settings["weights"]
    pers = settings["personalization"]
    s = s.astype(np.float64).copy()
    if not len(s):                 # no candidates: nothing to adjust (and max() of an empty array raises)
        return s

    def add(name, arr):
        nonlocal s
        arr = np.asarray(arr, np.float64)
        if arr.any():
            s += arr
            if explain is not None:
                explain[name] = explain.get(name, 0) + arr
    if personal and pers.get("purchase_boost", True):
        n = X[:, F["acct_bought_n"]]
        bought = n > 0
        if bought.any():
            add("bought before", bought * (w["purchased"] + w["purchased_times"] * np.log1p(n) + w["purchased_recent"] * X[:, F["acct_bought_recency"]]))
    if personal and acct:
        within, across = affinity_weights(settings, acct.get("segment"))
        min_lines = (settings.get("affinity") or {}).get("min_lines", 0)
        if (within or across) and acct.get("lines", 0) >= min_lines:
            bs = acct.get("brand_share") or {}
            add("brand affinity", within * X[:, F["acct_brand_share"]] +
                across * np.array([bs.get(str(b), 0.0) for b in cat.brand[idx]], np.float64))
    if w.get("popularity"):
        pop = X[:, F["pop_365"]]
        add("popularity", w["popularity"] * pop / max(float(pop.max()), 1e-9))
    if w.get("stocked"):
        sr = X[:, F["stock_rank"]]
        add("stocked", w["stocked"] * sr / max(float(sr.max()), 1e-9))
    if w.get("branch_stock") and at_branch is not None:
        add("on the shelf", w["branch_stock"] * (np.asarray(at_branch) > 0))
    for name, arr in (field_adj or {}).items():      # v2.44 relevance by field and attribute (fieldrel.py)
        add(name, arr)
    for r in rules:
        for b in r.get("boost") or []:
            add(f"rule: {r.get('name', '')}", b["weight"] * cond_mask(b, idx, cat, attr_fn))
    return s


def place_pins(idx: np.ndarray, cat, pins: list[tuple[str, int]], hidden: set[str]) -> tuple[np.ndarray, set[int]]:
    """A permutation of range(len(idx)) with hidden items dropped and pinned items at their positions."""
    skus = [str(x) for x in cat.sku[idx]]
    pos_of = {sk: k for k, sk in enumerate(skus)}
    keep = [k for k, sk in enumerate(skus) if sk not in hidden]
    pinned = [(pos_of[sk], p) for sk, p in pins if sk in pos_of and sk not in hidden]
    pk = {k for k, _ in pinned}
    rest = [k for k in keep if k not in pk]
    for k, p in sorted(pinned, key=lambda t: t[1]):
        rest.insert(min(p - 1, len(rest)), k)
    return np.array(rest, dtype=np.int64), {int(idx[k]) for k in pk}


def personal_reason_ok(settings: dict) -> bool:
    return bool(settings["personalization"].get("reasons", True))


def bulk_weights(settings: dict) -> dict:
    b = dict(settings["bulk"])
    if not settings["personalization"].get("bulk_personal", True):
        b.update(bought_before=0.0, bought_times=0.0, invoiced=0.0, preferred_brand=0.0)
    if not settings["personalization"].get("learned_crosses", True):
        b.update(learned=0.0)
    return b



def diversify(brands: list, cap: int, window: int = 24, groups: list | None = None) -> list[int]:
    """An order of range(len(brands)) where no brand has more than `cap` of the first `window` places. Items
    held back keep their order and follow; if nothing else is left, the window fills with them anyway.
    `groups` (e.g. category): only items of a group already on page one can move up into it, so a cap on
    "ball valve" brings in other brands' ball valves, not the push fittings further down."""
    n = len(brands)
    if cap <= 0 or n <= cap:
        return list(range(n))
    allowed = set(groups[:window]) if groups is not None else None
    out, held, seen = [], [], {}
    for k in range(n):
        if len(out) >= window:
            break
        b = brands[k]
        if allowed is not None and k >= window and groups[k] not in allowed:
            continue
        if seen.get(b, 0) < cap:
            out.append(k)
            seen[b] = seen.get(b, 0) + 1
        else:
            held.append(k)
    taken = set(out) | set(held)
    rest = [k for k in range(n) if k not in taken]
    # not enough other brands: relax the cap evenly (the brand with the fewest places so far goes next), so a
    # tight cap never ends up with more of the top brand than a loose one
    while len(out) < window and held:
        j = min(range(len(held)), key=lambda i: (seen[brands[held[i]]], i))
        k = held.pop(j)
        out.append(k)
        seen[brands[k]] += 1
    return out + held + rest
