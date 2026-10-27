"""Product advisor: the "Ask" path for hard product searches.

A query like "I'm looking for a 3 ton condenser that fits on a 48 inch footing"
fails in a search box for three reasons: the constraint (a pad size) isn't a
catalog attribute, the obvious reading hides an assumption (48 x 48, or 48 x 24?),
and the right answer depends on rules the user didn't mention (state rules on
installing R-410A; the outdoor unit has to be an AHRI match with the
indoor coil).

The advisor does three things the results page can't:
  1. extracts constraints (tonnage, pad/clearance dimensions, refrigerant,
     AC vs heat pump, cold climate, efficiency, brand, stock) with rules;
  2. filters the equipment on derived specs (retrieval/hvac.py) and says what
     each constraint removed, including the assumption it made;
  3. returns follow-up refinements as one-tap chips, the relevant job kit, and
     AHRI matches for the top pick.

With an API key Claude runs the same tools in a tool-use loop and writes the
answer (ClaudeAdvisor); without one, RuleAdvisor templates it. Either way every
number in the answer comes from a tool result.
"""
from __future__ import annotations

import json
import re
import time
from collections import Counter

import numpy as np

from webbsearch.config import settings
from webbsearch.retrieval.entitlements import Principal
from webbsearch.llm import guardrails as guard
from webbsearch.llm.answer_cards import CARDS_RULE, blocks, settle, with_question
from webbsearch.llm.assistant import _product_card
from webbsearch.retrieval.hvac import R410A_NOTE, climate_zone

OUTDOOR_RE = re.compile(r"\b(condens(?:er|ing unit|ing)|heat ?pump|a/?c unit|air condition(?:er|ing)|outdoor unit|"
                        r"central air|split system|cu\b)", re.I)
PRODUCT_Q_RE = re.compile(r"\b(i'?m looking for|looking for|i need|need a|need an|find me|what .* (fits|works|goes)|"
                          r"which .* (fits|should)|recommend|fits?|compatible|replacement for|replace my|will .* fit|"
                          r"that (fits|works|matches)|what do i need)\b", re.I)
IN_NUM = r"(\d{2,3}(?:\.\d)?)\s*(?:\"|”|''|in\b|inch(?:es)?\b)?"


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def parse_constraints(q: str, brands: list[str]) -> dict:
    """Rule-based constraint extraction. Everything optional."""
    low = q.lower().replace("×", "x")
    c: dict = {"raw": q}
    m = re.search(r"(\d+(?:\.\d+)?)\s*-?\s*tons?\b", low)
    if m:
        c["ton"] = float(m.group(1))
    else:
        m = re.search(r"(\d{2,3})[,]?(\d{3})?\s*(?:k\b|btu)", low)
        if m:
            btu = float(m.group(1) + (m.group(2) or "")) * (1000 if not m.group(2) and "k" in m.group(0) else 1)
            if 12000 <= btu <= 60000:
                c["ton"] = round(btu / 12000 * 2) / 2
    # pad / footing / slab: "48x24 pad", "48 inch footing", "pad is 40 by 40"
    pad_words = r"(?:pad|footing|slab|base|stand|platform|concrete|bracket)"
    m = re.search(IN_NUM + r"\s*(?:x|by)\s*" + IN_NUM + r"[^.?!]{0,25}?" + pad_words, low) or \
        re.search(pad_words + r"[^.?!\d]{0,25}?" + IN_NUM + r"\s*(?:x|by)\s*" + IN_NUM, low)
    if m:
        c["pad"] = [float(m.group(1)), float(m.group(2))]
    else:
        m = re.search(IN_NUM + r"[^.?!\d]{0,12}?" + pad_words, low) or re.search(pad_words + r"[^.?!\d]{0,20}?" + IN_NUM, low)
        if m:
            c["pad"] = [float(m.group(1)), None]   # one number: ambiguous, answered both ways
    m = re.search(r"(?:under|max(?:imum)?|less than|no (?:taller|higher) than|shorter than)\s*" + IN_NUM +
                  r"\s*(?:tall|high|height)", low) or re.search(r"(?:height|tall)[^.?!\d]{0,15}?(?:under|max|<)\s*" + IN_NUM, low)
    if m:
        c["max_height"] = float(m.group(1))
    m = re.search(r"(?:under|max(?:imum)?|less than|no wider than)\s*" + IN_NUM + r"\s*(?:wide|width)", low)
    if m:
        c["max_width"] = float(m.group(1))
    # "AC condenser only, no heat pump": a heat pump the buyer rules out is an air conditioner
    no_hp = re.compile(r"\b(?:no|not\s+(?:an?\s+)?|without|except|but\s+not)\s*(?:a\s+)?heat ?pumps?\b")
    if no_hp.search(low):
        low = no_hp.sub(" ", low)
        c["kind"] = "AC"
    elif re.search(r"\bheat ?pump|\bhp\b|dual fuel", low):
        c["kind"] = "HP"
    elif re.search(r"\b(a/?c|air condition\w*|cooling only|condens(?:er|ing unit))\b", low) and "heat pump" not in low:
        c["kind_hint"] = "AC"          # "condenser" is how contractors say both; don't filter on it
    if re.search(r"cold[- ]climate|ccashp|hyper[- ]?heat|low ambient", low):
        c["cold_climate"] = True
        c["kind"] = "HP"
    m = re.search(r"r-?\s?(410a|454b|32|22)\b", low)
    if m:
        c["refrigerant"] = "R-" + m.group(1).upper()
    m = re.search(r"(\d{2}(?:\.\d)?)\s*seer2?", low)
    if m:
        c["min_seer2"] = float(m.group(1))
    if re.search(r"energy star", low):
        c["energy_star"] = True
    if re.search(r"side[- ]discharge|horizontal discharge|compact", low):
        c["discharge"] = "side"
    if re.search(r"quiet|low noise|sound", low):
        c["quiet"] = True
    if re.search(r"in stock|today|pick ?up|on hand|same day", low):
        c["in_stock"] = True
    if re.search(r"repair|\bexisting\b|replace (?:just|only) the|condenser only|keep the coil|swap out", low):
        c["repair"] = True
    for b in brands:
        if re.search(r"\b" + re.escape(b.lower()) + r"\b", low):
            c.setdefault("brands", []).append(b)
    return c


def render(c: dict) -> str:
    """Constraints -> a canonical question that parse_constraints reads back the same way (used for chips)."""
    parts = []
    if c.get("ton"):
        parts.append(f"{c['ton']:g} ton")
    parts.append("cold climate heat pump" if c.get("cold_climate") else
                 ("heat pump" if c.get("kind") == "HP" else ("AC condenser" if c.get("kind") == "AC" else "condenser")))
    if c.get("refrigerant"):
        parts.append(c["refrigerant"])
    if c.get("brands"):
        parts.append(" ".join(c["brands"]))
    if c.get("pad"):
        pw, pd_ = c["pad"][0], c["pad"][1]
        parts.append(f"for a {pw:g}x{pd_:g} pad" if pd_ else f"for a {pw:g} inch pad")
    if c.get("max_height"):
        parts.append(f"under {c['max_height']:g} inches tall")
    if c.get("max_width"):
        parts.append(f"under {c['max_width']:g} inches wide")
    if c.get("min_seer2"):
        parts.append(f"{c['min_seer2']:g} SEER2")
    if c.get("energy_star"):
        parts.append("energy star")
    if c.get("discharge") == "side":
        parts.append("side discharge")
    if c.get("quiet"):
        parts.append("quiet")
    if c.get("repair"):
        parts.append("to repair an existing system")
    if c.get("in_stock"):
        parts.append("in stock")
    return " ".join(parts)


class AdvisorTools:
    """Deterministic tools shared by the rule and Claude advisors."""

    def __init__(self, engine, p: Principal, branch: str | None):
        self.e = engine
        self.p = p
        self.branch = branch or (p.home_branch if p.is_authenticated else None)
        self.cards: list[dict] = []
        self.kits: list[dict] = []
        self.matchups: list[dict] = []
        self.pool: list[int] = []

    # ------------------------------------------------------------ equipment
    def find_outdoor_units(self, ton=None, pad=None, kind=None, refrigerant=None, cold_climate=None, min_seer2=None,
                           energy_star=None, discharge=None, max_height=None, max_width=None, brands=None,
                           in_stock=None, repair=None, quiet=None, kind_hint=None, limit=6, **_):
        e, cat, hv = self.e, self.e.catalog, self.e.hvac
        idx = np.flatnonzero(np.isin(cat.family, list(hv.OUTDOOR)))
        steps = []
        pool = [int(i) for i in idx]
        steps.append(("outdoor units in catalog", len(pool)))

        near = {}

        def keep(label, fn, key=None):
            nonlocal pool
            before = pool
            pool = [i for i in pool if fn(i)]
            steps.append((label, len(pool), len(before) - len(pool)))
            if not pool and before and not near:   # the constraint that emptied the list: report the closest miss
                near["label"] = label
                if key == "height":
                    near["value"] = min(hv.spec(i)["height_in"] for i in before)
                elif key == "width":
                    near["value"] = min(hv.spec(i)["width_in"] for i in before)
                elif key == "pad":
                    near["value"] = min(hv.spec(i)["width_in"] * hv.spec(i)["depth_in"] for i in before)
                    j = min(before, key=lambda i: hv.spec(i)["width_in"] * hv.spec(i)["depth_in"])
                    near["footprint"] = [hv.spec(j)["width_in"], hv.spec(j)["depth_in"]]
                near["key"] = key

        if ton:
            keep(f"{ton:g} ton", lambda i: hv.spec(i)["ton"] == float(ton))
        if kind in ("AC", "HP"):
            keep("heat pump" if kind == "HP" else "AC only", lambda i: hv.spec(i)["kind"] == kind)
        if brands:
            keep(" / ".join(brands), lambda i: cat.brand[i] in brands)
        new_install = not repair
        if refrigerant:
            keep(refrigerant, lambda i: hv.spec(i)["refrigerant"] == refrigerant)
        if new_install:
            keep("meets the DOE minimum", lambda i: hv.install_note(i) is None)
        if cold_climate:
            keep("cold-climate listed", lambda i: next(c["ok"] for c in hv.checks(hv.spec(i)) if c["key"] == "cold_climate"))
        if min_seer2:
            keep(f"SEER2 ≥ {min_seer2:g}", lambda i: hv.spec(i)["seer2"] >= float(min_seer2))
        if energy_star:
            keep("ENERGY STAR", lambda i: next(c["ok"] for c in hv.checks(hv.spec(i)) if c["key"] == "energy_star"))
        if discharge:
            keep(f"{discharge}-discharge", lambda i: hv.spec(i)["cabinet"].startswith(discharge))
        if max_height:
            keep(f"≤ {max_height:g} in tall", lambda i: hv.spec(i)["height_in"] <= float(max_height), "height")
        if max_width:
            keep(f"≤ {max_width:g} in wide", lambda i: hv.spec(i)["width_in"] <= float(max_width), "width")
        pad_report = None
        if pad:
            pw, pd_ = float(pad[0]), (float(pad[1]) if len(pad) > 1 and pad[1] else None)
            if pd_ is None:
                sq = [i for i in pool if hv.fits(hv.spec(i), pw, pw)[0]]
                half = [i for i in pool if hv.fits(hv.spec(i), pw, pw / 2)[0]]
                pad_report = {"assumed": [pw, pw], "alt": [pw, pw / 2], "fit_square": len(sq), "fit_alt": len(half),
                              "of": len(pool)}
                keep(f"fits a {pw:g}×{pw:g} in pad (assumed square)", lambda i: hv.fits(hv.spec(i), pw, pw)[0], "pad")
            else:
                pad_report = {"assumed": [pw, pd_], "of": len(pool)}
                keep(f"fits a {pw:g}×{pd_:g} in pad", lambda i: hv.fits(hv.spec(i), pw, pd_)[0], "pad")
        # availability + ranking
        av = e.availability.summary(np.array(pool, np.int64), self.branch) if pool else None
        stock = {i: (int(av["at_branch"][k]), int(av["nearby_qty"][k]), av["eta"][k]) for k, i in enumerate(pool)} if pool else {}
        if in_stock:
            keep("in stock at or near your branch", lambda i: stock[i][0] + stock[i][1] > 0)
        bought = Counter()
        if self.p.is_authenticated:
            for j, (n, _, _) in e.features.acct_prod.get(self.p.account_id, {}).items():
                if cat.family[j] in hv.OUTDOOR:
                    bought[cat.brand[j]] += n

        def score(i):
            s = hv.spec(i)
            es = next(c["ok"] for c in hv.checks(s) if c["key"] == "energy_star")
            # R-410A after current refrigerants for a new install (shown, not filtered: state rules differ, v2.01)
            late = new_install and not refrigerant and s["refrigerant"] == "R-410A"
            return (s["kind"] != (kind_hint or s["kind"]), late, -(stock[i][0] > 0), -(stock[i][0] + stock[i][1] > 0),
                    -bought[cat.brand[i]], -es,
                    s["sound_db"] if quiet else 0, -s["seer2"], float(cat.list_price[i]))
        pool.sort(key=score)
        self.pool = list(pool)      # the full ranked match set: the results grid shows exactly these
        # collapse identical configurations (same brand/type/SEER2/refrigerant/cabinet)
        seen, picks = set(), []
        for i in pool:
            s = hv.spec(i)
            key = (cat.brand[i], cat.attrs(i).get("type"), s["seer2"], s["refrigerant"], s["width_in"], s["depth_in"])
            if key in seen:
                continue
            seen.add(key)
            picks.append(i)
            if len(picks) >= limit:
                break
        rows = []
        for i in picks:
            s = hv.spec(i)
            fit = hv.fits(s, *(pad_report["assumed"] if pad_report else (999, 999)))
            pr = e.accounts.price(self.p, i)
            rows.append({"sku": str(cat.sku[i]), "title": cat.title[i], "brand": cat.brand[i], "mfr_part": cat.mfr_part[i],
                         "price": pr["price"], "cabinet": f'{s["width_in"]:g}×{s["depth_in"]:g}×{s["height_in"]:g} in',
                         "discharge": s["cabinet"], "seer2": s["seer2"], "eer2": s["eer2"], "hspf2": s["hspf2"],
                         "refrigerant": s["refrigerant"], "sound_db": s["sound_db"],
                         "pad_spare_in": fit[1] if pad_report else None,
                         "at_branch": stock[i][0], "nearby": stock[i][1], "eta": stock[i][2],
                         "brand_bought_before": bought[cat.brand[i]] > 0})
            m = self.e.results._mini(i, self.p)
            m["avail"] = {"at_branch": stock[i][0], "nearby_qty": stock[i][1], "eta": stock[i][2]}
            m["attrs"] = {**m["attrs"], **hv.attrs_extra(i)}
            m["fit"] = fit[1] if pad_report else None
            self.cards.append({"type": "product", **m})
        # dimension range of what survived, for the answer text
        dims = None
        if pool:
            ws = [hv.spec(i)["width_in"] for i in pool]
            ds = [hv.spec(i)["depth_in"] for i in pool]
            dims = {"max_footprint": [max(ws), max(ds)], "min_footprint": [min(ws), min(ds)],
                    "side": sum(hv.spec(i)["cabinet"] == "side-discharge" for i in pool)}
        kinds = Counter(hv.spec(i)["kind"] for i in pool)
        return {"count": len(pool), "steps": [list(x) for x in steps], "pad": pad_report, "units": rows, "dims": dims,
                "pad_margin_in": 2.0, "near_miss": near or None, "by_kind": dict(kinds)}

    def ahri_matchups(self, sku: str, limit: int = 4):
        i = self.e.catalog.sku_to_idx.get(str(sku))
        if i is None or not self.e.hvac.is_outdoor(i):
            return {"error": "not_found", "detail": "not an outdoor unit"}
        ms = self.e.hvac.matchups(i, limit=limit)
        if self.p.is_authenticated and not self.p.sees_prices:
            for m in ms:
                m["indoor"]["list_price"] = None
        self.matchups = ms
        s = self.e.hvac.spec(i)
        return {"sku": sku, "matchups": ms, "checks": self.e.hvac.checks(s), "clearance": s["clearance"]}

    def find_kits(self, query: str):
        ks = self.e.kits.search(query, k=3, min_score=0.6)
        self.kits = ks
        return {"kits": [{k: x[k] for k in ("kit_id", "name", "variant", "brands", "lines", "min_total")} for x in ks]}

    def search_products(self, query: str, limit: int = 6):
        pq = self.e.parser.parse(query)
        r = self.e.service.search_products(pq, self.p, size=min(limit, 10))
        self.cards += [{"type": "product", **c} for c in r["items"][:limit]]
        return {"results": [{"sku": c["sku"], "title": c["title"], "price": c["price"]["price"]} for c in r["items"][:limit]]}


FILLER = re.compile(r"\b(i'?m|i am|looking for|i need|need|find me|can you|please|that|which|will|fits?|on|a|an|the|"
                    r"for|my|to|with|what|do|is|it|would|should|recommend|some|something)\b", re.I)


class RuleAdvisor:
    def __init__(self, engine):
        self.e = engine

    def handles(self, q: str) -> bool:
        return bool(OUTDOOR_RE.search(q) and (PRODUCT_Q_RE.search(q) or re.search(r"pad|footing|slab|ton\b|fit", q, re.I))) \
            or bool(PRODUCT_Q_RE.search(q) and not re.search(r"\b(order|invoice|balance|owe|past due|po\b|bought|buy last)", q, re.I))

    def answer(self, q: str, p: Principal, branch: str | None = None, history=None) -> dict:
        t0 = time.time()
        tools = AdvisorTools(self.e, p, branch)
        c = parse_constraints(q, sorted(set(self.e.catalog.brand[np.isin(self.e.catalog.family, list(self.e.hvac.OUTDOOR))])))
        if OUTDOOR_RE.search(q):
            ans, follow = self._outdoor(q, c, tools)
        else:
            ans, follow = self._generic(q, tools)
        return {"answer": ans, "cards": tools.cards[:6], "kits": tools.kits, "matchups": tools.matchups[:4],
                "followups": follow, "constraints": {k: v for k, v in c.items() if k != "raw"}, "engine": "rules",
                "kind": "advisor", "ms": round((time.time() - t0) * 1000)}

    def _outdoor(self, q, c, tools: AdvisorTools):
        r = tools.find_outdoor_units(**c)
        ton = c.get("ton")
        what = f"{ton:g}-ton " if ton else ""
        kind = {"HP": "heat pumps", "AC": "AC condensers"}.get(c.get("kind"), "outdoor units")
        lines, f = [], []
        bname = self.e.availability.branch_info(tools.branch)["name"].split(",")[0]
        if not r["count"]:
            nm = r["near_miss"] or {}
            lines.append(f"No {what}{kind} meet all of that. The list went to zero at “{nm.get('label', r['steps'][-1][0])}”.")
            lines.append("Counts after each constraint: " + " → ".join(f"{x[0]} {x[1]}" for x in r["steps"][1:]) + ".")
            c2 = dict(c)
            if nm.get("key") == "height":
                lines.append(f"Shortest unit that meets everything else is {nm['value']:g} in tall.")
                c2["max_height"] = nm["value"]; f.append({"label": f"Up to {nm['value']:g} in tall", "q": render(c2)})
            elif nm.get("key") == "width":
                lines.append(f"Narrowest unit that meets everything else is {nm['value']:g} in wide.")
                c2["max_width"] = nm["value"]; f.append({"label": f"Up to {nm['value']:g} in wide", "q": render(c2)})
            elif nm.get("key") == "pad":
                fw, fd = nm["footprint"]
                lines.append(f"Smallest cabinet that meets everything else is {fw:g}×{fd:g} in, which needs a "
                             f"{fw + 4:g}×{fd + 4:g} in pad with 2 in all round.")
                c2["pad"] = [fw + 4, fd + 4]; f.append({"label": f"{fw + 4:g}×{fd + 4:g} pad", "q": render(c2)})
            for k in ("refrigerant", "cold_climate", "in_stock", "brands", "min_seer2", "energy_star", "discharge"):
                if c.get(k):
                    c3 = {x: v for x, v in c.items() if x != k}
                    f.append({"label": f"Without {k.replace('_', ' ')}", "q": render(c3)})
            return "\n".join(lines), f[:4]
        pad = r["pad"]
        head = f"{r['count']} {what}{kind}"
        if c.get("kind") is None and r["by_kind"]:
            head += f" ({r['by_kind'].get('AC', 0)} AC, {r['by_kind'].get('HP', 0)} heat pump)"
        if pad and pad.get("alt"):
            pw = pad["assumed"][0]
            head += (f" fit a {pw:g}×{pw:g} in pad. I read “{pw:g} inch” as a square pad; if it's {pw:g}×{pad['alt'][1]:g} "
                     f"(common for side-discharge units), {pad['fit_alt']} of the {pad['of']} fit.")
        elif pad:
            head += f" fit a {pad['assumed'][0]:g}×{pad['assumed'][1]:g} in pad."
        else:
            head += " match."
        lines.append(head)
        excl = [x for x in r["steps"][1:] if len(x) > 2 and x[2] and not x[0].startswith("fits") and "ton" not in x[0]]
        if excl:
            lines.append("Also filtered: " + "; ".join(f"{x[0]} (−{x[2]})" for x in excl) + ".")
        if not c.get("repair") and (c.get("refrigerant") == "R-410A" or any(
                u.get("refrigerant") == "R-410A" for u in (r.get("units") or []))):
            lines.append(R410A_NOTE[0].upper() + R410A_NOTE[1:] + ".")
        if pad and r["dims"]:
            mx = r["dims"]["max_footprint"]
            lines.append(f"Pad rule: cabinet plus {r['pad_margin_in']:g} in on every side, either orientation. Largest "
                         f"cabinet that qualifies is {mx[0]:g}×{mx[1]:g} in.")
        m = {}
        if r["units"]:
            u = r["units"][0]
            st = f"{u['at_branch']} at {bname}" if u["at_branch"] else (f"{u['nearby']} nearby" if u["nearby"] else u["eta"])
            price = f", ${u['price']:,.2f}" if u["price"] is not None else ""
            lines.append(f"Top pick: {u['title']} (#{u['sku']}), {u['cabinet']} {u['discharge']}, SEER2 {u['seer2']:g} / "
                         f"EER2 {u['eer2']:g}{', HSPF2 ' + format(u['hspf2'], 'g') if u['hspf2'] else ''}{price}; {st}"
                         f"{'; a brand you buy' if u['brand_bought_before'] else ''}.")
            m = tools.ahri_matchups(u["sku"], limit=3)
            if m.get("matchups"):
                best = m["matchups"][0]
                lines.append(f"It's rated with a matched indoor unit: best AHRI match {best['indoor']['model']} "
                             f"({best['indoor']['type'].lower()}), AHRI #{best['ahri_ref']}, SEER2 {best['seer2']:g}"
                             f"{', ENERGY STAR' if best['energy_star'] else ''}. Keeping the existing coil? Check its model "
                             "against the AHRI list first.")
            lines.append(f"Site check: {m.get('clearance', '')}.")
        # the matching job kit(s): only kits whose tonnage range covers this job
        def ton_ok(k):
            m = re.search(r"(\d+(?:\.\d+)?)\s*[–-]\s*(\d+(?:\.\d+)?)\s*ton", k["variant"])
            return not (ton and m) or float(m.group(1)) <= ton <= float(m.group(2))
        ks = []
        if c.get("repair"):
            ks += self.e.kits.search("condensing unit replacement", k=2, min_score=0.6)
        if c.get("kind") != "HP":
            ks += self.e.kits.search(f"central ac install {ton or 3:g} ton", k=2, min_score=0.6)
        if c.get("kind") != "AC":
            ks += self.e.kits.search("air-source heat pump install", k=1, min_score=0.6)
        seen = set()
        tools.kits = [k for k in ks if ton_ok(k) and "Rooftop" not in k["name"] and "Water Heater" not in k["name"]
                      and not (k["kit_id"] in seen or seen.add(k["kit_id"]))][:2]
        if pad and pad.get("alt"):
            f.append({"label": f"Pad is {pad['assumed'][0]:g}×{pad['alt'][1]:g}", "q": render({**c, "pad": pad["alt"]})})
        if c.get("kind") != "HP":
            f.append({"label": "Heat pumps only", "q": render({**c, "kind": "HP"})})
        if not c.get("cold_climate"):
            f.append({"label": "Cold-climate", "q": render({**c, "kind": "HP", "cold_climate": True})})
        if not c.get("in_stock"):
            f.append({"label": f"In stock near {bname}", "q": render({**c, "in_stock": True})})
        if not c.get("repair"):
            f.append({"label": "Repair, keep existing coil", "q": render({**c, "repair": True})})
        return "\n".join(lines), f[:4]

    def _generic(self, q, tools: AdvisorTools):
        # the results page's reading: quantity, branch, stock and "cheapest" words off ("I need 12 half inch ... at
        # Bedford today" searches "half inch ...", not a 12-1/2 in. size)
        from webbsearch.retrieval.phrase import split
        q_prod = split(q)[0] or q
        core = re.sub(r"\s+", " ", FILLER.sub(" ", q_prod)).strip(" ?.!,")
        tools.find_kits(q)
        if tools.kits:
            best = tools.kits[0]["score"]
            tools.kits = [k for k in tools.kits if k["score"] >= best - 0.1][:2]
        r = tools.search_products(core or q, limit=6)
        lines = []
        if tools.kits:
            k = tools.kits[0]
            lines.append(f"There's a job kit for this: {k['name']} ({k['variant']}), {k['lines']} lines"
                         f"{', from $' + format(k['min_total'], ',.0f') if k.get('min_total') else ''}. It lists every part "
                         f"for the job with the equipment brand as a choice.")
        if r["results"]:
            lines.append(f"Searched the catalog for “{core}”: top match {r['results'][0]['title']} (#{r['results'][0]['sku']}).")
        else:
            lines.append(f"Nothing in the catalog matched “{core}”.")
        return "\n".join(lines), [{"label": "Open as a search", "q": core, "search": True}]


ADVISOR_TOOLS = [
    {"name": "find_outdoor_units",
     "description": "Filter AC condensers and heat pumps on derived specs. Returns how many survive each constraint, "
                    "pad-fit (cabinet + 2 in on every side, either orientation; a single pad number is tested as square "
                    "and as half-depth), and the top units with stock at the user's branch.",
     "input_schema": {"type": "object", "properties": {
         "ton": {"type": "number"}, "pad": {"type": "array", "items": {"type": ["number", "null"]}, "maxItems": 2},
         "kind": {"type": "string", "enum": ["AC", "HP"]}, "refrigerant": {"type": "string", "enum": ["R-410A", "R-454B", "R-32"]},
         "cold_climate": {"type": "boolean"}, "min_seer2": {"type": "number"}, "energy_star": {"type": "boolean"},
         "discharge": {"type": "string", "enum": ["side", "top"]}, "max_height": {"type": "number"},
         "max_width": {"type": "number"}, "brands": {"type": "array", "items": {"type": "string"}},
         "in_stock": {"type": "boolean"}, "repair": {"type": "boolean", "description": "true = repair of an existing system"},
         "quiet": {"type": "boolean"}, "limit": {"type": "integer", "default": 6}}}},
    {"name": "ahri_matchups", "description": "AHRI-certified indoor matches (coils, air handlers, furnace+coil) for one "
                                             "outdoor unit SKU, with compliance checks and clearances.",
     "input_schema": {"type": "object", "properties": {"sku": {"type": "string"}, "limit": {"type": "integer", "default": 4}},
                      "required": ["sku"]}},
    {"name": "find_kits", "description": "Job kits (every part for an install) matching a job description.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "search_products", "description": "Keyword search of the catalog for anything that isn't an outdoor HVAC unit.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
                      "required": ["query"]}},
]


class ClaudeAdvisor(RuleAdvisor):
    def answer(self, q: str, p: Principal, branch: str | None = None, history=None, max_rounds: int = 5) -> dict:
        t0 = time.time()
        tools = AdvisorTools(self.e, p, branch)
        c = parse_constraints(q, sorted(set(self.e.catalog.brand[np.isin(self.e.catalog.family, list(self.e.hvac.OUTDOOR))])))
        zone = climate_zone(self.e.availability.branch_info(tools.branch).get("lat"))
        system = (
            f"You are FlexIQ, the F.W. Webb counter expert on fwwebb.com. Today is {self.e.today}. Pickup branch climate zone {zone}. "
            "If asked what you are or who made you, say you are FlexIQ, F.W. Webb's search assistant, and nothing more. "
            "Talk only about F.W. Webb and Frank Webb products and services; never name or mention another distributor or retailer, favorably or not. "
            "Help a contractor find the right product. Call tools; every number you state must come from a tool result. "
            "State the assumption you made in one clause (e.g. a single pad dimension read as square) and what changes "
            "if it's wrong. Mention rules the user didn't ask about only when they change the answer (R-410A systems "
            "made before 2025 can still be installed new under EPA's May 26, 2026 rule but state rules differ, New York "
            "among them; outdoor units need an AHRI match with the indoor coil). Cite SKUs as #123456. "
            "Four sentences max, then stop; the UI shows product cards and follow-up chips. " + CARDS_RULE + " Suggested constraints "
            f"parsed by rules (verify against the question): {json.dumps({k: v for k, v in c.items() if k != 'raw'})}")
        msgs = with_question(history, q)
        seen = [q, system] + [str(h.get("content")) for h in history or [] if isinstance(h, dict)
                                    and h.get("role") == "user"]       # the user's words; never the client's copy of an answer
        deadline = t0 + settings.assistant_timeout_s
        for _ in range(max_rounds):
            remaining = deadline - time.time()
            if remaining < 1.0:
                break
            resp = self.e.claude.messages(model=settings.model_smart, max_tokens=700,
                                          system=[{"type": "text", "text": system}], tools=ADVISOR_TOOLS,
                                          messages=msgs, timeout=remaining)
            msgs.append({"role": "assistant", "content": blocks(resp.content)})
            uses = [b for b in resp.content if b.type == "tool_use"]
            if not uses:
                ruled = RuleAdvisor.answer(self, q, p, branch)  # chips come from the parsed constraints
                text, cards, label = settle("".join(b.text for b in resp.content if b.type == "text"),
                                            tools.cards[:6] or ruled["cards"],
                                            lambda sku: _product_card(self.e, p, sku))
                bad = guard.ungrounded(text, " ".join(seen), item_ok=lambda s: s in self.e.catalog.sku_to_idx)
                if bad:            # a number, item or model no tool returned: the rules answer instead (v2.2)
                    return {**ruled, "flags": ["flexiq_ungrounded"], "ungrounded": bad[:5]}
                return {"answer": text, "cards": cards[:6], **({"cards_label": label} if label else {}),
                        "kits": tools.kits or ruled["kits"],
                        "matchups": tools.matchups[:4] or ruled["matchups"], "followups": ruled["followups"],
                        "constraints": ruled["constraints"], "engine": "claude", "kind": "advisor",
                        "ms": round((time.time() - t0) * 1000)}
            results = []
            for u in uses:
                fn = getattr(tools, u.name, None)
                try:
                    out = fn(**(u.input or {})) if fn else {"error": "unknown_tool"}
                except (TypeError, ValueError) as ex:
                    out = {"error": "bad_arguments", "detail": str(ex)}
                seen.append(json.dumps(out, default=str))
                results.append({"type": "tool_result", "tool_use_id": u.id, "content": json.dumps(out, default=str)[:12000]})
            msgs.append({"role": "user", "content": results})
        return RuleAdvisor.answer(self, q, p, branch)




def get_advisor(engine):
    return ClaudeAdvisor(engine) if engine.claude.enabled else RuleAdvisor(engine)
