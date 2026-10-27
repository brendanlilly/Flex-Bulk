"""Merchandising settings: one JSON document, versioned, every change logged with who, when and a snapshot.

Weights are additive adjustments to the ranker's score. For scale: on a typical query the learned ranker's
top 20 results span 1-2 score units, so a weight of 1.0 moves an item several positions.
"""
from __future__ import annotations

import copy
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path

# customer disciplines (the account's trade segment on the customer master)
SEGMENTS = {
    "plumbing_contractor": "Plumbing", "hvac_contractor": "HVAC / heating", "mechanical_contractor": "Mechanical",
    "facilities_mro": "Facilities / MRO", "industrial": "Industrial", "builder_remodeler": "Builder / remodeler",
    "water_works": "Water works",
}
ROLES = ("owner", "buyer", "ap", "tech")
# v2.42: "reqs" (what the search asked for, as checked filters) and "series" (a brand's product lines); the category's own
# specs move up, after Type, with number ranges for capacity, horsepower, BTU and the like
FACET_IDS = ("reqs", "avail", "acct", "family", "type", "spec", "size", "connection", "material", "brand", "series", "price")
RESULTS_MODES = ("minimal", "normal", "advanced")
MINIMAL_IDS = ("instock", "nearby", "purchased", "lists", "family", "brand", "leadfree")
MINIMAL_LABELS = {"instock": "In stock at the pickup branch", "nearby": "Available nearby", "purchased": "Previously purchased",
                  "lists": "On my lists", "family": "Category", "brand": "Brand", "leadfree": "Lead-free"}
FACET_LABELS = {"reqs": "From your search", "series": "Line (brand series)", "avail": "Availability", "acct": "Your account", "family": "Category", "type": "Type", "size": "Nominal size",
                "connection": "Connection", "material": "Material", "brand": "Brand", "price": "Price", "spec": "Category specs"}

DEFAULTS: dict = {
    "version": 0,
    "updated_at": None,
    "updated_by": None,
    # additive, in ranker-score units
    "weights": {
        "purchased": 0.8,          # the account bought this item before (any number of times)
        "purchased_times": 0.35,   # x ln(1 + times bought)
        "purchased_recent": 0.5,   # x recency (1.0 = bought last week, fades over about a year)
        "branch_stock": 0.0,       # in stock at the pickup branch
        "stocked": 0.0,            # stocked at branches (not CD-only or special order)
        "popularity": 0.0,         # x relative popularity (orders, last 365 days)
    },
    # brand affinity: the brands an account buys, not only the items. Added to the ranker's score:
    #   within x (share of this category the account buys in this brand)
    # + across x (share of all the account's lines in this brand, any category)
    # per customer discipline; accounts with fewer than min_lines lines get none.
    "affinity": {
        "enabled": True,
        "min_lines": 10,
        "segments": {k: {"within": 1.0, "across": 1.5} for k in SEGMENTS},   # tuned with the relevance audit
    },
    "personalization": {
        "personal_model": True,    # signed-in searches use the personal ranking model
        "history_lane": True,      # the account's past purchases are pulled into the candidates
        "purchase_boost": True,    # the "previously purchased" weights above apply
        "reasons": True,           # "why this" chips on results (bought before, your brand...)
        "buy_again": True,         # Buy again on the start page
        "bulk_personal": True,     # Bulk order recommendations use the account's history and brands
        "learned_crosses": True,   # Bulk order learns from what people choose for a line (see bulkprefs.py)
    },
    "layout": {                    # page composition
        "brand_cap": 4,            # at most this many items from one brand in the first 24 (0 = off), unless the
                                   # search names a brand or a part number
        "group_variants": 0,       # 1: results group sizes of one product into one card by default (people can switch)
        "simple_home": 0,          # 1: the start page opens as the simple page (logo, search box, a line of links) by default;
                                   # people switch between simple and full on the start page (kept in their browser)
        "results_mode": "normal",  # results page mode for people who haven't chosen one: minimal, normal or advanced
        "mode_switch": 1,          # 1: customers see the Minimal / Normal / Advanced switch; 0: everyone gets the default
    },
    "minimal_rail": {              # the filter rail in Minimal mode: base filters only, in this order, at most `max`
        "order": ["instock", "purchased", "lists", "family", "brand"], "max": 5,
    },
    "facets": {                    # the filter rail on the results page
        "order": list(FACET_IDS), "hidden": [], "collapsed": [],
    },
    "bulk": {                      # Bulk order recommendation bonuses (added to a 0-10 rank-position score)
        "bought_before": 8.0, "bought_times": 2.0, "invoiced": 1.5, "preferred_brand": 5.0, "on_shelf": 2.0,
        "webb_brand": 3.0,         # x Webb's brand preference for the category (#1 of 3 = 1.0, #2 = 0.67, #3 = 0.33)
        "learned": 4.0,            # x how often people chose this item for the same line (company-wide and account)
        "learned_min": 2.0,        # choices needed before a learned cross counts
    },
    # Bulk order: crosses and substitutes merchandising sets (company-wide, or for one branch)
    #   brand: lines naming from_brand (one Webb doesn't carry) are matched as to_brand ("Kohler" -> "American Standard")
    #   item:  a part number or line text -> one catalog item
    "crosses": [],
    # Webb's brand preference per category, for lines that name no brand (company-wide, or a branch's own order)
    "brand_prefs": [],
    "learned_off": [],             # learned crosses switched off: "line key::sku" or "brand::asked::chosen"
    # v2.44 relevance by field and attribute (see retrieval/fieldrel.py):
    #   fields: x the search index's built-in weight per field (1.0 = built-in); also (weight - 1) x the share of the
    #           search's words in that field, added on the final order
    #   attrs:  attribute key -> points added when a search word matches that spec on the item
    "relevance": {"fields": {f: 1.0 for f in ("title", "brand", "cat", "attrs", "syn", "desc", "ids")}, "attrs": {}},
    "rules": [],                   # see rules.py for the shape
    "rewrites": [],                # [{"id", "from", "to", "enabled"}]
    "experiments": [],             # A/B tests: [{"id", "name", "patch", "split", "status", "started", "stopped", ...}]
}
from webbsearch.extended import ranking as _avail  # noqa: E402  (the Extended Catalog's availability settings)

DEFAULTS["availability"] = copy.deepcopy(_avail.DEFAULTS)
LIMITS = {"weights": (-5.0, 5.0), "bulk": (0.0, 20.0), "layout": (0, 24), "affinity": (-5.0, 5.0),
          "fields": (0.0, 3.0), "attrs": (-3.0, 3.0)}
REL_FIELD_NAMES = {"title": "Title", "brand": "Brand", "cat": "Category", "attrs": "Specs", "syn": "Synonyms",
                   "desc": "Description", "ids": "Part numbers"}
LAYOUT_SWITCHES = ("group_variants", "simple_home", "mode_switch")     # layout settings that are on/off, not numbers
LAYOUT_ENUMS = {"results_mode": RESULTS_MODES}                         # layout settings that are one of a few words
SECTIONS = ("weights", "personalization", "bulk", "layout")
MATCH_TYPES = ("exact", "contains", "any")
FIELDS = ("brand", "family", "sku", "stock", "title", "attr", "leadfree", "status")
FIELD_HELP = {"brand": "brand", "family": "category", "sku": "item #", "stock": "stocking level (stocked, regional, CD, special)",
              "title": "words in the title", "attr": "spec, as key=value (material=Brass, connection=Press, port=Full Port)",
              "leadfree": "lead-free items", "status": "item status (active, discontinued, superseded)"}
STOCK_VALUES = ("stocked", "regional", "dc", "special")
STATUS_VALUES = {"active": "A", "discontinued": "D", "superseded": "S"}


class Conflict(Exception):
    """Someone else saved since this editor loaded the settings."""


def norm_q(q: str) -> str:
    return re.sub(r"\s+", " ", (q or "").lower().strip())


class MerchStore:
    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.path = self.root / "settings.json"
        self.log_path = self.root / "changes.jsonl"
        self.lock = threading.Lock()
        self._s = self._load()

    # ------------------------------------------------------------------ read
    def _load(self) -> dict:
        if self.path.exists():
            try:
                return _with_defaults(json.loads(self.path.read_text()))
            except (json.JSONDecodeError, OSError):
                pass
        return copy.deepcopy(DEFAULTS)

    def get(self) -> dict:
        """The live settings. Treat as read-only; change them with update()."""
        return self._s

    def defaults(self) -> dict:
        return copy.deepcopy(DEFAULTS)

    # ------------------------------------------------------------------ write
    def update(self, patch: dict, who: str = "", note: str = "", base_version: int | None = None) -> dict:
        with self.lock:
            if base_version is not None and int(base_version) != int(self._s.get("version", 0)):
                raise Conflict(f"settings changed since you loaded them (now version {self._s.get('version', 0)}); reload and reapply")
            new, changed = self.merged(patch)
            if not changed:
                return self._s
            return self._commit(new, who, note, changed)

    def merged(self, patch: dict) -> tuple[dict, list[str]]:
        """The settings with `patch` applied and validated, not saved (for previews), and what changed."""
        new = copy.deepcopy(self._s)
        changed = []
        for sect in SECTIONS:
            for k, v in (patch.get(sect) or {}).items():
                if k not in new[sect]:
                    raise ValueError(f"unknown {sect} setting: {k}")
                if sect == "layout" and k in LAYOUT_ENUMS:
                    v = str(v).strip().lower()
                    if v not in LAYOUT_ENUMS[k]:
                        raise ValueError(f"{sect}.{k} must be one of {', '.join(LAYOUT_ENUMS[k])}")
                elif sect == "personalization" or (sect == "layout" and k in LAYOUT_SWITCHES):
                    v = bool(v) if sect == "personalization" else int(bool(v))
                elif sect == "layout":
                    lo, hi = LIMITS[sect]
                    v = int(round(float(v)))
                    if not lo <= v <= hi:
                        raise ValueError(f"{sect}.{k} must be between {lo} and {hi}")
                else:
                    lo, hi = LIMITS[sect]
                    v = round(float(v), 3)
                    if not lo <= v <= hi:
                        raise ValueError(f"{sect}.{k} must be between {lo} and {hi}")
                if new[sect][k] != v:
                    changed.append(f"{sect}.{k}: {new[sect][k]} → {v}")
                    new[sect][k] = v
        if "affinity" in patch:
            af = patch["affinity"] or {}
            old = copy.deepcopy(new["affinity"])
            if "enabled" in af:
                new["affinity"]["enabled"] = bool(af["enabled"])
            if "min_lines" in af:
                v = int(float(af["min_lines"]))
                if not 0 <= v <= 1000:
                    raise ValueError("affinity.min_lines must be between 0 and 1000")
                new["affinity"]["min_lines"] = v
            lo, hi = LIMITS["affinity"]
            for seg, w in (af.get("segments") or {}).items():
                if seg not in SEGMENTS:
                    raise ValueError(f"unknown customer discipline: {seg}")
                for k, v in (w or {}).items():
                    if k not in ("within", "across"):
                        raise ValueError(f"unknown affinity weight: {k}")
                    v = round(float(v), 3)
                    if not lo <= v <= hi:
                        raise ValueError(f"affinity weights must be between {lo} and {hi}")
                    new["affinity"]["segments"][seg][k] = v
            for k in ("enabled", "min_lines"):
                if new["affinity"][k] != old[k]:
                    changed.append(f"affinity.{k}: {old[k]} → {new['affinity'][k]}")
            for seg in SEGMENTS:
                for k in ("within", "across"):
                    if new["affinity"]["segments"][seg][k] != old["segments"][seg][k]:
                        changed.append(f"brand affinity {SEGMENTS[seg]} {k}: {old['segments'][seg][k]} → {new['affinity']['segments'][seg][k]}")
        if "relevance" in patch:
            rp = patch["relevance"] or {}
            old = copy.deepcopy(new["relevance"])
            lo, hi = LIMITS["fields"]
            for f, v in (rp.get("fields") or {}).items():
                if f not in REL_FIELD_NAMES:
                    raise ValueError(f"unknown field: {f}")
                v = round(float(v), 2)
                if not lo <= v <= hi:
                    raise ValueError(f"field weights must be between {lo:g} and {hi:g}")
                new["relevance"]["fields"][f] = v
            if "attrs" in rp:
                lo, hi = LIMITS["attrs"]
                at = {}
                for k, v in (rp.get("attrs") or {}).items():
                    k = str(k).strip().lower()
                    if not re.fullmatch(r"[a-z][a-z0-9_]{0,23}", k):
                        raise ValueError(f"not an attribute name: {k!r}")
                    v = round(float(v), 2)
                    if not lo <= v <= hi:
                        raise ValueError(f"attribute weights must be between {lo:g} and {hi:g}")
                    if v:
                        at[k] = v
                if len(at) > 80:
                    raise ValueError("at most 80 attribute weights")
                new["relevance"]["attrs"] = dict(sorted(at.items()))
            for f in REL_FIELD_NAMES:
                if new["relevance"]["fields"][f] != old["fields"].get(f, 1.0):
                    changed.append(f"field weight {REL_FIELD_NAMES[f]}: ×{old['fields'].get(f, 1.0):g} → ×{new['relevance']['fields'][f]:g}")
            for k in sorted(set(old["attrs"]) | set(new["relevance"]["attrs"])):
                a, b = old["attrs"].get(k, 0), new["relevance"]["attrs"].get(k, 0)
                if a != b:
                    changed.append(f"spec weight {k}: {a:+g} → {b:+g}")
        if "facets" in patch:
            f = patch["facets"] or {}
            order = [x for x in (f.get("order") or new["facets"]["order"]) if x in FACET_IDS]
            order += [x for x in FACET_IDS if x not in order]
            nf = {"order": order, "hidden": sorted({x for x in (f.get("hidden", new["facets"]["hidden"]) or []) if x in FACET_IDS}),
                  "collapsed": sorted({x for x in (f.get("collapsed", new["facets"]["collapsed"]) or []) if x in FACET_IDS})}
            if nf != new["facets"]:
                changed.append("filter rail: " + ", ".join(p_ for p_ in [
                    "order" if nf["order"] != new["facets"]["order"] else "",
                    f"hidden {', '.join(FACET_LABELS[x] for x in nf['hidden']) or 'none'}" if nf["hidden"] != new["facets"]["hidden"] else "",
                    f"collapsed {', '.join(FACET_LABELS[x] for x in nf['collapsed']) or 'none'}" if nf["collapsed"] != new["facets"]["collapsed"] else ""] if p_))
                new["facets"] = nf
        if "minimal_rail" in patch:
            mr = patch["minimal_rail"] or {}
            order = list(mr.get("order", new["minimal_rail"]["order"]) or [])
            if len(order) < 2 or len(set(order)) != len(order) or any(x not in MINIMAL_IDS for x in order):
                raise ValueError(f"Minimal rail: two or more different filters from {', '.join(MINIMAL_IDS)}")
            mx = int(float(mr.get("max", new["minimal_rail"]["max"])))
            if not 2 <= mx <= 5:
                raise ValueError("Minimal rail shows at most 2 to 5 filters")
            nm = {"order": order, "max": mx}
            if nm != new["minimal_rail"]:
                changed.append("Minimal rail: " + ", ".join(MINIMAL_LABELS[x] for x in order) + f" (max {mx})")
                new["minimal_rail"] = nm
        if "availability" in patch:
            try:
                av = _avail.merged(new.get("availability"), patch["availability"])
            except _avail.SettingsError as ex_:
                raise ValueError(f"Availability ranking: {ex_}")
            if av != new.get("availability"):
                changed.append("availability ranking: " + ", ".join(
                    k for k in av if av[k] != (new.get("availability") or {}).get(k)))
                new["availability"] = av
        if "experiments" in patch:
            ex = [validate_experiment(x, self) for x in patch["experiments"]]
            if sum(1 for x in ex if x["status"] == "running") > 1:
                raise ValueError("one A/B test at a time")
            if ex != new["experiments"]:
                changed.append(_diff_named("A/B test", new["experiments"], ex))
                new["experiments"] = ex
        for key, fn, kind, label in (("crosses", validate_cross, "cross", lambda r: r["label"]),
                                     ("brand_prefs", validate_brand_pref, "brand preference", lambda r: r["label"])):
            if key in patch:
                v = [fn(r) for r in patch[key]]
                if v != new[key]:
                    changed.append(_diff_named(kind, new[key], v, key=label))
                    new[key] = v
        if "learned_off" in patch:
            lo = sorted({str(x)[:200] for x in patch["learned_off"] or [] if str(x).strip()})[:2000]
            if lo != new["learned_off"]:
                changed.append(f"learned crosses switched off: {len(new['learned_off'])} → {len(lo)}")
                new["learned_off"] = lo
        if "rules" in patch:
            rules = [validate_rule(r) for r in patch["rules"]]
            if rules != new["rules"]:
                changed.append(_diff_named("rule", new["rules"], rules))
                new["rules"] = rules
        if "rewrites" in patch:
            rw = [validate_rewrite(r) for r in patch["rewrites"]]
            if rw != new["rewrites"]:
                changed.append(_diff_named("rewrite", new["rewrites"], rw, key=lambda r: f"{r['from']} → {r['to']}"))
                new["rewrites"] = rw
        return new, changed

    def reset(self, section: str, who: str = "") -> dict:
        if section not in SECTIONS + ("affinity", "facets", "minimal_rail", "availability", "relevance"):
            raise ValueError("only weights, affinity, personalization, bulk, layout, facets, minimal_rail, "
                             "availability or relevance can be reset")
        if section == "availability":
            with self.lock:
                new = copy.deepcopy(self._s)
                new["availability"] = copy.deepcopy(DEFAULTS["availability"])
                if new["availability"] == self._s.get("availability"):
                    return self._s
                return self._commit(new, who, "reset availability to defaults", ["availability ranking: defaults"])
        return self.update({section: DEFAULTS[section]}, who, note=f"reset {section} to defaults")

    def revert(self, version: int, who: str = "") -> dict:
        snap = next((c["snapshot"] for c in self._read_log() if c["version"] == version), None)
        if snap is None and version == 0:
            snap = copy.deepcopy(DEFAULTS)           # version 0 is the defaults, which were never logged
        if snap is None:
            raise ValueError(f"no version {version}")
        with self.lock:
            new = _with_defaults(snap)               # a version saved before a setting existed gets its default
            return self._commit(new, who, f"reverted to version {version}", [f"reverted to version {version}"])

    def _commit(self, new: dict, who: str, note: str, changed: list) -> dict:
        new["version"] = int(self._s.get("version", 0)) + 1
        new["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        new["updated_by"] = (who or "unknown")[:60]
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(new, indent=1))
        os.replace(tmp, self.path)
        with open(self.log_path, "a") as f:
            f.write(json.dumps({"version": new["version"], "at": new["updated_at"], "who": new["updated_by"],
                                "note": note[:200], "changes": [c for c in changed if c], "snapshot": new}) + "\n")
        self._s = new
        return new

    def _read_log(self) -> list[dict]:
        if not self.log_path.exists():
            return []
        out = []
        for ln in self.log_path.read_text().splitlines():
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
        return out

    def changes(self, limit: int = 100) -> list[dict]:
        return [{k: v for k, v in c.items() if k != "snapshot"} for c in reversed(self._read_log())][:limit]


def _with_defaults(saved: dict) -> dict:
    """Saved settings over the defaults: settings added since they were saved get their default; unknown keys
    in a section are dropped."""
    s = copy.deepcopy(DEFAULTS)
    for k, v in saved.items():
        if isinstance(v, dict) and isinstance(s.get(k), dict):
            s[k].update({kk: copy.deepcopy(vv) for kk, vv in v.items() if kk in s[k]})
        elif k in s:
            s[k] = copy.deepcopy(v)
    # the filter rail's order (v2.42): an order saved as the old default becomes the new default; an order someone
    # arranged keeps its arrangement, with the new filters added where they belong
    fo = s.get("facets", {}).get("order") or []
    if list(fo) == list(_OLD_FACET_ORDER):
        s["facets"]["order"] = list(FACET_IDS)
    else:
        fo = [x for x in fo if x in FACET_IDS]
        if "reqs" not in fo:
            fo.insert(0, "reqs")
        if "series" not in fo:
            fo.insert(fo.index("brand") + 1 if "brand" in fo else len(fo), "series")
        s["facets"]["order"] = fo + [x for x in FACET_IDS if x not in fo]
    return s


_OLD_FACET_ORDER = ("avail", "acct", "family", "type", "size", "connection", "material", "brand", "price", "spec")


def _diff_named(kind: str, old: list, new: list, key=lambda r: r.get("name") or r.get("id")) -> str:
    o = {r["id"]: r for r in old}
    n = {r["id"]: r for r in new}
    parts = [f"added {kind} “{key(n[i])}”" for i in n if i not in o]
    parts += [f"removed {kind} “{key(o[i])}”" for i in o if i not in n]
    parts += [f"changed {kind} “{key(n[i])}”" for i in n if i in o and n[i] != o[i]]
    return "; ".join(parts)


def _skus(v) -> list[str]:
    return [str(x).strip() for x in (v or []) if str(x).strip()][:50]


def _cond(c: dict, what: str) -> dict:
    f, v = c.get("field"), str(c.get("value", "")).strip()
    if f not in FIELDS:
        raise ValueError(f"{what} needs a field: {', '.join(FIELDS)}")
    if f == "leadfree":
        v = "yes"
    if not v:
        raise ValueError(f"{what} needs a value")
    if f == "stock":
        v = v.lower()
        v = {"cd": "dc"}.get(v, v)                 # Webb says CD (central distribution); the stored code stays "dc"
        if v not in STOCK_VALUES:
            raise ValueError("stocking level is one of stocked, regional, CD, special")
    if f == "status":
        v = v.lower()
        if v not in STATUS_VALUES:
            raise ValueError(f"item status is one of {', '.join(STATUS_VALUES)}")
    if f == "attr" and "=" not in v:
        raise ValueError("a spec condition looks like key=value, e.g. material=Brass")
    return {"field": f, "value": v[:120]}


def validate_rule(r: dict) -> dict:
    """A merchandising rule. When: query (exact / contains / any; several phrases separated by | make a campaign),
    audience (everyone, guests, signed-in; optionally disciplines and roles), dates. Does: pins, hides (items or
    conditions), boosts and buries (by brand, category, item, stocking level, title words, spec, lead-free),
    "only show" filters, a banner, a redirect and the badge pinned items carry."""
    m = r.get("match") or {}
    mt = m.get("type", "exact")
    if mt not in MATCH_TYPES:
        raise ValueError(f"match type must be one of {MATCH_TYPES}")
    phrases = [norm_q(x) for x in re.split(r"[|\n]", str(m.get("q", ""))) if norm_q(x)][:25]
    q = " | ".join(phrases)
    if mt != "any" and not q:
        raise ValueError("a rule needs the query it applies to")
    aud = r.get("audience") or {}
    who = aud.get("who", "all")
    if who not in ("all", "guests", "signed_in"):
        raise ValueError("audience is all, guests or signed_in")
    segs = [x for x in (aud.get("segments") or []) if x in SEGMENTS]
    roles = [x for x in (aud.get("roles") or []) if x in ROLES]
    if who == "guests" and (segs or roles):
        raise ValueError("guests have no discipline or role; pick signed-in customers to target those")
    pins = []
    for p in (r.get("pin") or [])[:12]:
        sku = str(p.get("sku", "")).strip()
        if sku:
            pins.append({"sku": sku, "pos": max(1, min(int(p.get("pos") or len(pins) + 1), 24))})
    boosts = []
    for b in (r.get("boost") or [])[:20]:
        c = _cond(b, "a boost or bury")
        w = float(b.get("weight", 0))
        if not -10 <= w <= 10:
            raise ValueError("boost weights run from -10 (bury) to 10")
        boosts.append({**c, "weight": round(w, 2)})
    only = [_cond(c, "an only-show filter") for c in (r.get("only") or [])[:10]]
    exclude = [_cond(c, "a hide condition") for c in (r.get("exclude") or [])[:10]]
    banner = r.get("banner") or {}
    link = str(banner.get("link", "")).strip()
    if link and not re.match(r"(?:https?://|/(?!/))", link):
        raise ValueError("a banner link must start with https://, http:// or /")
    red = r.get("redirect") or {}
    rurl = str(red.get("url", "")).strip()
    if rurl and not re.match(r"(?:https?://|/(?!/)|#)", rurl):
        raise ValueError("a redirect must start with https://, http://, / or #")
    for d in ("start", "end"):
        if r.get(d) and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(r[d])):
            raise ValueError(f"{d} date must look like 2026-10-01")
    return {"id": str(r.get("id") or uuid.uuid4().hex[:8]), "name": str(r.get("name") or q or "rule")[:80],
            "enabled": bool(r.get("enabled", True)), "match": {"type": mt, "q": q},
            "audience": {"who": who, "segments": segs, "roles": roles},
            "start": r.get("start") or None, "end": r.get("end") or None,
            "pin": pins, "hide": _skus(r.get("hide")), "exclude": exclude, "only": only, "boost": boosts,
            "badge": str(r.get("badge") or "Featured").strip()[:24] or "Featured",
            "banner": {"text": str(banner.get("text", ""))[:200], "link": link[:300]}
            if banner.get("text") else None,
            "redirect": {"url": rurl[:300], "auto": bool(red.get("auto"))} if rurl else None,
            "note": str(r.get("note", ""))[:300]}


def _scope(v) -> str:
    v = str(v or "company").strip()
    if v != "company" and not re.fullmatch(r"\d{2,5}", v):
        raise ValueError("scope is company or a branch id")
    return v


def validate_cross(c: dict) -> dict:
    kind = c.get("kind", "brand")
    if kind not in ("brand", "item"):
        raise ValueError("a cross is brand-to-brand or to an item")
    fb, fp, ft = str(c.get("from_brand", "")).strip(), str(c.get("from_part", "")).strip(), norm_q(c.get("from_text", ""))
    tb, ts = str(c.get("to_brand", "")).strip(), str(c.get("to_sku", "")).strip()
    if kind == "brand":
        if not fb or not tb:
            raise ValueError("a brand cross needs the brand people ask for and the brand to match instead")
        if fb.lower() == tb.lower():
            raise ValueError("a brand can't cross to itself")
        label = f"{fb} → {tb}"
    else:
        if not (fp or ft) or not ts:
            raise ValueError("an item cross needs a part number or line text, and the item to use")
        label = f"{(fb + ' ' if fb else '')}{fp or ft} → #{ts}"
    scope = _scope(c.get("scope"))
    return {"id": str(c.get("id") or uuid.uuid4().hex[:8]), "kind": kind, "from_brand": fb[:60], "from_part": fp[:60],
            "from_text": ft[:120], "to_brand": tb[:60] if kind == "brand" else "", "to_sku": ts[:30] if kind == "item" else "",
            "scope": scope, "enabled": bool(c.get("enabled", True)), "note": str(c.get("note", ""))[:200],
            "label": (label + ("" if scope == "company" else f" (branch {scope})"))[:120]}


def validate_brand_pref(b: dict) -> dict:
    fam = str(b.get("family", "")).strip()
    brands = [str(x).strip() for x in (b.get("brands") or []) if str(x).strip()][:6]
    if not fam or not brands:
        raise ValueError("a brand preference needs a category and at least one brand")
    if len({x.lower() for x in brands}) != len(brands):
        raise ValueError("list each brand once")
    scope = _scope(b.get("scope"))
    return {"id": str(b.get("id") or uuid.uuid4().hex[:8]), "family": fam[:80], "brands": brands, "scope": scope,
            "enabled": bool(b.get("enabled", True)), "note": str(b.get("note", ""))[:200],
            "label": (f"{fam}: {' > '.join(brands)}" + ("" if scope == "company" else f" (branch {scope})"))[:160]}


def validate_experiment(x: dict, store: "MerchStore") -> dict:
    """An A/B test: `patch` (settings changes, validated like a draft) applied to `split` percent of browser
    sessions; the rest are the control. Status running / stopped."""
    patch = x.get("patch") or {}
    if any(k in patch for k in ("experiments",)):
        raise ValueError("a test can't contain tests")
    _, ch = store.merged(patch)                            # validates the variant
    if not ch and x.get("status", "running") == "running" and not x.get("id"):
        raise ValueError("the test's variant changes nothing: edit the draft first")
    split = int(float(x.get("split", 50)))
    if not 5 <= split <= 50:
        raise ValueError("send 5 to 50% of sessions to the variant")
    st = x.get("status", "running")
    if st not in ("running", "stopped"):
        raise ValueError("status is running or stopped")
    return {"id": str(x.get("id") or uuid.uuid4().hex[:8]), "name": str(x.get("name") or "A/B test")[:80],
            "patch": patch, "changes": [str(c)[:200] for c in (x.get("changes") or ch)][:20], "split": split, "status": st,
            "started": x.get("started") or time.strftime("%Y-%m-%d %H:%M:%S"),
            "stopped": (x.get("stopped") or time.strftime("%Y-%m-%d %H:%M:%S")) if st == "stopped" else None,
            "by": str(x.get("by") or "")[:60], "note": str(x.get("note") or "")[:300]}


def validate_rewrite(r: dict) -> dict:
    f, t = norm_q(r.get("from", "")), (r.get("to") or "").strip()
    if not f or not t:
        raise ValueError("a rewrite needs both the words people type and what to search instead")
    if len(f) > 60 or len(t) > 120:
        raise ValueError("keep rewrites short")
    return {"id": str(r.get("id") or uuid.uuid4().hex[:8]), "from": f, "to": t, "enabled": bool(r.get("enabled", True))}
