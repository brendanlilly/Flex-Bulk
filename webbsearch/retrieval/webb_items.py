"""Real F.W. Webb items: the exact-match bridge for a synthetic catalog.

The prototype's 750k-product catalog is synthetic, so a contractor who types a
real model (Taco 007-F5), a real Webb item number (24866) or a Webb house brand
(PurePro) gets nothing exact from it. Two small sources do carry real items:

  * job-kit lines (config/kits/job_kits.json): ~500 real fwwebb.com item
    numbers with manufacturer part numbers and guest list prices
  * config/webb_seed_items.csv: items verified on fwwebb.com during QA
    (item #, manufacturer, part #, description, product URL)

`exact()` matches a query to these by Webb item #, manufacturer part # or a
model code in the description ("Mighty-Mate M53"), optionally checked against
a brand the user typed. `related()` matches descriptive queries where every
meaningful word appears in the item ("PurePro 3/4 ball valve", "1/2 boiler
drain"). Results are labeled as real Webb items and carted as their own lines.
In production these are just catalog rows and this module goes away.
"""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path

from webbsearch.config import ROOT
from webbsearch.retrieval.analyzer import compact_id, normalize

KITS_FILE = ROOT / "config" / "kits" / "job_kits.json"
SEED_FILE = ROOT / "config" / "webb_seed_items.csv"
# model-like codes in free text: letters and digits mixed (007-F5, NPE-240A2, M53, 0015e3)
CODE_RE = re.compile(r"\b(?=[\w-]*\d)(?=[\w-]*[a-z])[a-z0-9][\w-]{1,20}\b", re.I)
GENERIC = {"valve", "copper", "air", "pipe", "plastic", "pvc", "pressure", "dwv", "cover", "adapter", "drop",
           "water", "gas", "supply", "heating", "cooling", "products", "domestic", "general", "universal", "pro", "electrical", "fan", "flared", "hvac",
           "line", "malleable", "nipple", "oil", "red", "refrigerant", "refrigeration", "toilet", "plenum", "thermo"}
# spec codes that look like model numbers but aren't (R-410A, SEER2, 24V, 48x24, F1960)
GENERIC_CODE = re.compile(r"^(R\d{2,3}[AB]?|SEER2?|EER2?|HSPF2?|AFUE\d*|UL\d+|NSF\d+|F\d{4}|PEX[ABC]?|"
                           r"\d+(V|VAC|VDC|VA|KVA|RPM|GR|GRAIN|A|AH|MAH|AMP|AMPS|W|KW|KWH|WH|HZ|HP|GPM|GPH|GPF|PSI|GAL|MFD|UF|BTU|BTUH|MBH|K|TON|TONS|FT|LB|LBS|OZ|IN|MM|CM|"
                           r"AWG|CFM|DB|SEER2?|FT2)|\d+X\d+(X\d+)?)$")
WORDY = re.compile(r"^\d+[-\s]?[a-z]{2,}$", re.I)   # "4-inch", "18-plate", "3-piece": a number and a word, not a model


def is_code(raw: str) -> bool:
    k = compact_id(raw)
    return len(k) >= 3 and any(ch.isdigit() for ch in k) and not GENERIC_CODE.match(k) and not WORDY.match(raw.strip())


UNIT_WORDS = {"gal", "ft", "volt", "amp", "psi", "btu", "mbh", "ton", "hp", "mfd", "gpm", "gph", "lb", "oz", "awg"}


class WebbItems:
    def __init__(self, kits_path: Path = KITS_FILE, seed_path: Path = SEED_FILE, kit_names: dict | None = None):
        self.items: dict[str, dict] = {}
        if kits_path.exists():
            for k in json.loads(kits_path.read_text())["kits"]:
                lines = [ln for c in k["combos"].values() for ln in c["lines"]] + list(k.get("optional", []))
                for ln in lines:
                    wi = str(ln.get("webb_item") or "").strip()
                    if not wi.isdigit():
                        continue
                    it = self.items.setdefault(wi, {
                        "webb_item": wi, "desc": ln["desc"], "manufacturer": ln.get("manufacturer") or "",
                        "mpn": ln.get("mpn") or "", "list_price": ln.get("list_price"),
                        "in_stock": ln.get("in_stock"), "kits": [], "url": None, "source": "job kit"})
                    if k["kit_id"] not in [x["kit_id"] for x in it["kits"]]:
                        it["kits"].append({"kit_id": k["kit_id"], "name": k["family"], "variant": k["variant"]})
        if seed_path.exists():
            with seed_path.open(newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    wi = r["webb_item"].strip()
                    it = self.items.get(wi)
                    if it:   # same item seen in a kit: keep the kit record, add the product page
                        it["url"] = r["url"]
                        it["manufacturer"] = it["manufacturer"] or r["manufacturer"]
                        it["desc_web"] = r["desc"]
                    else:
                        self.items[wi] = {"webb_item": wi, "desc": r["desc"], "manufacturer": r["manufacturer"],
                                          "mpn": r["mpn"], "list_price": None, "in_stock": None, "kits": [],
                                          "url": r["url"], "source": "fwwebb.com"}
        self.by_code: dict[str, set[str]] = {}
        self.tokens: dict[str, frozenset] = {}
        self.brand_tokens: set[str] = set()
        for wi, it in self.items.items():
            text = " ".join(x for x in (it["desc"], it.get("desc_web", ""), it["manufacturer"], it["mpn"]) if x)
            self.tokens[wi] = frozenset(normalize(text))
            codes = {compact_id(c) for c in [it["mpn"]] + CODE_RE.findall(it["desc"]) if is_code(c)}
            for c in codes:
                self.by_code.setdefault(c, set()).add(wi)
            first = normalize(re.split(r"[-/ &]", it["manufacturer"].strip())[0]) if it["manufacturer"].strip() else ()
            if first and len(first[0]) >= 3 and first[0] not in GENERIC:
                self.brand_tokens.add(first[0])

    def __len__(self):
        return len(self.items)

    def _brands_in(self, q_tokens) -> set[str]:
        return {t for t in q_tokens if t in self.brand_tokens}

    def exact(self, raw: str) -> list[tuple[dict, str]]:
        """Items the query names outright: Webb item #, manufacturer part # or model code (+ brand if typed)."""
        chunks = [c.strip(",;:()[]") for c in raw.split()]
        cands = chunks + [a + b for a, b in zip(chunks, chunks[1:]) if not WORDY.match(a + b)]
        q_tokens = set(normalize(raw))
        brands = self._brands_in(q_tokens)
        hits: dict[str, str] = {}
        for c in cands:
            k = compact_id(c)
            if not k or GENERIC_CODE.match(k) or WORDY.match(c):
                continue
            if k.isdigit() and k in self.items and len(k) >= 3 and len(chunks) <= 2:
                hits.setdefault(k, "Webb item #")
            for wi in self.by_code.get(k, ()):
                if k.isdigit() and len(k) < 5:
                    continue      # bare short numbers ("30") are sizes, not models
                hits.setdefault(wi, "manufacturer part #" if compact_id(self.items[wi]["mpn"]) == k else "model")
        out = []
        for wi, how in hits.items():
            it = self.items[wi]
            if brands and not (brands & self.tokens[wi]):
                continue          # "Honeywell M53" is not the Zoeller M53
            out.append((it, how))
        if not out and brands:    # brand + model words: "Amtrol Extrol 30"
            words = {t for t in q_tokens if t not in UNIT_WORDS}
            if len(words) >= 3:
                for wi, toks in self.tokens.items():
                    if words <= toks and brands & toks:
                        out.append((self.items[wi], "brand + description"))
        return out[:6]

    def related(self, raw: str, limit: int = 4) -> list[dict]:
        """Items that contain every meaningful word of a descriptive query."""
        words = {t for t in normalize(raw) if t not in UNIT_WORDS and t}
        if len(words) < 2:
            return []
        hits = [self.items[wi] for wi, toks in self.tokens.items() if words <= toks]
        hits.sort(key=lambda it: (it["list_price"] is None, -len(it["kits"])))
        return hits[:limit]

    @staticmethod
    def card(it: dict, how: str | None, sees_prices: bool = True) -> dict:
        return {"webb_item": it["webb_item"], "sku": "webb-" + it["webb_item"], "title": it.get("desc_web") or it["desc"],
                "kit_desc": it["desc"], "manufacturer": it["manufacturer"], "mpn": it["mpn"],
                "list_price": it["list_price"] if sees_prices else None, "in_stock": it["in_stock"],
                "kits": it["kits"][:3], "url": it["url"], "source": it["source"], "match": how}
