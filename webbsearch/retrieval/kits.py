"""Job kits: every part for a standard job, keyed to an equipment configuration.

Source: config/kits/job_kits.json, generated from the product-expert review
workbook by scripts/import_kits.py (80 kits, 220 kit x brand combinations).
Kit lines carry real fwwebb.com item numbers and guest list prices; the rest
of the prototype's catalog is synthetic, so kit lines are shown and carted as
their own lines rather than resolved to synthetic SKUs.

Hybrid kit model (from the workbook): role P = primary equipment, one per
brand (choosing a brand swaps it); S = shared accessory; B = brand-only
accessory. Totals are per brand at list.

Search is a small field-weighted scorer over 80 documents; at this size a
dictionary beats an index. In production kits are one more OpenSearch index
(SPEC.md section 5) fed from the kit tables in Snowflake.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from pathlib import Path

from webbsearch.config import ROOT
from webbsearch.retrieval.analyzer import normalize

KITS_FILE = ROOT / "config" / "kits" / "job_kits.json"

# words that signal "I want the whole job" but don't identify which job
INTENT = {"kit", "kits", "install", "installation", "installing", "replace", "replacement", "replacing", "job",
          "everything", "all", "parts", "need", "needed", "materials", "bundle", "package", "complete", "hookup",
          "add", "on", "new"}
STOP = {"a", "an", "the", "for", "to", "of", "with", "and", "i", "do", "what", "my", "me", "in", "w", "is", "it",
        "system", "standard"}

# kit family -> synthetic catalog family (drives illustrations and "kits for this product")
FAMILY_MAP = [
    (r"tankless", "wh_tankless"), (r"indirect", "wh_indirect"), (r"water heater", "wh_tank"),
    (r"boiler install|boiler\b", "boiler"), (r"zone add-on", "baseboard"), (r"circulator", "circulator"),
    (r"zone valve", "mixing_valve"), (r"expansion tank|pressure tank", "expansion_tank"),
    (r"feed valve|backflow", "backflow"), (r"air separator|oil tank", "boiler_parts"),
    (r"furnace install", "furnace"), (r"blower motor|inducer motor", "motor"),
    (r"central ac|condensing unit|heat pump install|air handler|evaporator coil|rooftop", "ac_condenser"),
    (r"ductwork|humidifier|erv|exhaust fan|dryer vent", "duct"), (r"line set", "refrigerant"),
    (r"mini-split", "minisplit"), (r"toilet|bidet", "toilet"), (r"faucet|shower valve|tub spout", "faucet"),
    (r"drain", "drainage"), (r"vanity|sink", "sink"), (r"disposal|hot water dispenser", "disposal"),
    (r"dishwasher|ice maker|washing machine", "stop_valve"), (r"gas range", "gas"),
    (r"reverse osmosis|water filter|softener|well pump", "well"), (r"sump|sewage|condensate pump", "pumps"),
    (r"pressure reducing", "prv"), (r"mixing valve", "mixing_valve"), (r"shutoff", "ball_valve"),
]
FIELD_W = {"name": 3.0, "variant": 3.0, "category": 1.2, "meta": 1.0, "primary": 1.0, "lines": 0.35}


def catalog_family(kit_family: str) -> str | None:
    low = kit_family.lower()
    for pat, fam in FAMILY_MAP:
        if re.search(pat, low):
            return fam
    return None


def _toks(text: str) -> set[str]:
    t = set(normalize(text or ""))
    # "2–3 ton" -> also 2.5 so "2.5 ton" hits the 2-3 ton kit
    for a, b in re.findall(r"(\d+(?:\.\d+)?)\s*[–-]\s*(\d+(?:\.\d+)?)\s*ton", text or ""):
        x = float(a)
        while x <= float(b) + 1e-9:
            t.add(f"{x:g}")
            x += 0.5
    return t


class KitStore:
    def __init__(self, path: Path = KITS_FILE):
        self.ok = path.exists()
        self.kits: list[dict] = []
        self.note = ""
        if not self.ok:
            return
        d = json.loads(path.read_text(encoding="utf-8"))
        self.note = d.get("note", "")
        self.source = d.get("source", "")
        self.kits = d["kits"]
        self.by_id = {k["kit_id"]: k for k in self.kits}
        self.fields = []
        df = Counter()
        for k in self.kits:
            combos = k["combos"].values()
            prim = " ".join(l["desc"] + " " + (l.get("manufacturer") or "") for c in combos for l in c["lines"]
                            if l["role"] == "P")
            rest = " ".join(l["desc"] for c in combos for l in c["lines"] if l["role"] != "P")
            f = {"name": _toks(k["family"]), "variant": _toks(k["variant"]), "category": _toks(k["category"]),
                 "meta": _toks(" ".join(filter(None, [k["job_type"], k["trade"], k["fuel"] or "", " ".join(k["brands"])]))),
                 "primary": _toks(prim), "lines": _toks(rest)}
            self.fields.append(f)
            for t in set().union(*f.values()):
                df[t] += 1
        n = len(self.kits)
        self.idf = {t: math.log(1 + n / c) for t, c in df.items()}
        self.cat_family = {k["kit_id"]: catalog_family(k["family"]) for k in self.kits}

    # ------------------------------------------------------------------ cards
    def card(self, k: dict, brand: str | None = None) -> dict:
        combos = k["combos"]
        totals = {b: c["total"] for b, c in combos.items()}
        b = brand if brand in combos else (k["brands"][0] if k["brands"] else next(iter(combos)))
        c = combos[b]
        prim = next((l for l in c["lines"] if l["role"] == "P"), None)
        return {"kit_id": k["kit_id"], "name": k["family"], "variant": k["variant"], "variant_label": k["variant_label"],
                "category": k["category"], "job_type": k["job_type"], "trade": k["trade"], "fuel": k["fuel"],
                "brands": k["brands"], "lines": len(c["lines"]), "min_total": min(totals.values()),
                "max_total": max(totals.values()), "brand": b, "primary": prim["desc"] if prim else None,
                "family": self.cat_family.get(k["kit_id"]),
                "verify_lines": c["verify_lines"]}

    def detail(self, kit_id: str, brand: str | None = None) -> dict | None:
        k = self.by_id.get(kit_id)
        if not k:
            return None
        out = self.card(k, brand)
        c = k["combos"][out["brand"]]
        out["items"] = c["lines"]
        out["total"] = c["total"]
        out["brand_totals"] = {b: k["combos"][b]["total"] for b in k["brands"] if b in k["combos"]}
        out["optional"] = k["optional"]
        out["siblings"] = [{"kit_id": s["kit_id"], "variant": s["variant"]} for s in self.kits
                           if s["family"] == k["family"]]
        out["note"] = self.note
        return out

    # ----------------------------------------------------------------- search
    def search(self, q: str, k: int = 3, min_score: float = 0.75) -> list[dict]:
        """Kits whose name/variant explain the query. Returns cards with a score in [0, 1]."""
        if not self.ok:
            return []
        qt = [t for t in dict.fromkeys(normalize(q)) if t not in STOP]
        intent = any(t in INTENT for t in qt)
        content = [t for t in qt if t not in INTENT]
        if not content:
            return []
        wsum = sum(self.idf.get(t, 2.0) for t in content)
        scored = []
        for kit, f in zip(self.kits, self.fields):
            s, name_hit, fam_hit = 0.0, 0.0, 0.0
            for t in content:
                w = max((FIELD_W[fn] for fn, toks in f.items() if t in toks), default=0.0)
                s += self.idf.get(t, 2.0) * min(w, 3.0) / 3.0
                if t in f["name"] or t in f["variant"] or t in f["category"]:
                    name_hit += self.idf.get(t, 2.0)
                if t in f["name"]:
                    fam_hit += self.idf.get(t, 2.0)
            score = s / wsum
            name_cov = name_hit / wsum
            # the kit's own name must carry the query; accessory text alone never surfaces a kit
            if name_cov < (0.34 if intent else 0.5):
                continue
            score = 0.7 * score + 0.3 * name_cov + 0.05 * fam_hit / wsum + (0.1 if intent else 0.0)
            if score >= min_score:
                scored.append((score, kit))
        scored.sort(key=lambda x: -x[0])
        # one variant per family first; other variants only fill leftover slots
        first, rest, seen = [], [], set()
        for s, kit in scored:
            (rest if kit["family"] in seen else first).append((s, kit))
            seen.add(kit["family"])
        return [self.card(kit) | {"score": round(s, 3)} for s, kit in (first + rest)[:k]]

    def browse(self, category: str | None = None) -> list[dict]:
        return [self.card(k) for k in self.kits if not category or k["category"] == category]

    def categories(self) -> list[dict]:
        c = Counter(k["category"] for k in self.kits)
        return [{"category": a, "count": n} for a, n in c.items()]

    def for_product(self, family: str, title: str) -> list[dict]:
        """Kits whose primary equipment is this kind of product (PDP cross-sell)."""
        if not self.ok or not family:
            return []
        cands = [k for k in self.kits if self.cat_family.get(k["kit_id"]) == family]
        if not cands:
            return []
        tt = _toks(title)
        best = sorted(cands, key=lambda k: -len(_toks(k["variant"] + " " + k["family"]) & tt))
        return [self.card(k) for k in best[:2]]
