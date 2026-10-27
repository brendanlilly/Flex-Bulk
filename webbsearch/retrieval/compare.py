"""Compare two products (v2.02).

From the search box ("GE 3ton vs Rheem 3ton", "GE vs Rheem 3 ton condenser", "compare 3/4 sweat and press ball valves")
or from any two items (/api/compare?skus=a,b). Each side's search picks its best product, like with like; the table
lines up price, stock, every spec and the HVAC ratings, marks the rows that differ and which side is better where
there is a better (lower price, higher SEER2, quieter); the summary says what differs and when to choose each.

The summary is FlexIQ's when it's on, given the table and told to use only it; a FlexIQ summary with a number the
table doesn't hold is replaced by Webb's rule-written one (ChatGPT QA F03: claims beyond the evidence).
"""
from __future__ import annotations

import hashlib
import json
import re

from webbsearch.config import settings
from webbsearch.llm import guardrails as guard
from webbsearch.retrieval.analyzer import normalize

# attribute key -> (row key, label)
ATTR = {"ton": ("tonnage", "Tonnage"), "seer": ("seer2", "SEER2"), "ref": ("refrigerant", "Refrigerant"),
        "eer2": ("eer2", "EER2"), "hspf2": ("hspf2", "HSPF2"), "cabinet": ("cabinet", "Cabinet (W×D×H)"),
        "discharge": ("discharge", "Discharge"), "type": ("type", "Type"), "series": ("series", "Series"),
        "size": ("size", "Size"), "connection": ("connection", "Ends"), "end": ("connection", "Ends"),
        "material": ("material", "Material"), "port": ("port", "Port"), "handle": ("handle", "Handle"),
        "afue": ("afue", "AFUE"), "hp": ("hp", "HP"), "voltage": ("voltage", "Voltage"), "gpm": ("gpm", "GPM"),
        "warranty": ("warranty", "Warranty")}
HIGHER = {"seer2", "eer2", "hspf2", "afue", "cap5", "energy_star", "cold_climate", "warranty", "stock"}
LOWER = {"price", "sound"}
WHY = {"seer2": "efficiency matters most",
       "eer2": "efficiency matters most", "hspf2": "efficiency matters most",
       "afue": "efficiency matters most",
       "cap5": "it's a cold-climate install", "cold_climate": "it's a cold-climate install",
       "energy_star": "the job needs ENERGY STAR", "sound": "quiet matters (near a bedroom or a neighbor)",
       "stock": "you need it today", "warranty": "a longer warranty matters"}
SCHEMA = {"type": "object", "properties": {"text": {"type": "string"}, "choose_a_if": {"type": "string"},
                                           "choose_b_if": {"type": "string"}},
          "required": ["text", "choose_a_if", "choose_b_if"]}
_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
# numbers in words FlexIQ could invent past the digit check ("lasts twenty years", "half the noise")
_NUM_WORDS = re.compile(r"\b(?:eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|"
                        r"forty|fifty|sixty|seventy|eighty|ninety|hundreds?|thousands?|millions?|dozens?|half|halves|"
                        r"double|twice|triple|quarter|percent)\b", re.I)


class UnknownItem(KeyError):
    """An item number the catalog doesn't have (a 404), as opposed to a bug inside the compare (a 500)."""


def _cap(s: str) -> str:
    """Capitalize the first letter only: "PEX-A" and "CPVC" keep their capitals."""
    return s[:1].upper() + s[1:]


def _trim0(n: str) -> str:
    """2.50 -> 2.5, 17.0 -> 17; whole numbers keep their zeros (20 is not 2)."""
    return n.rstrip("0").rstrip(".") if "." in n else n


def _num(v) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = _NUM.search(str(v))
    return float(m.group(0).replace(",", "")) if m else None


def _money(v) -> str:
    return "—" if v is None else f"${v:,.2f}"


class Compare:
    def __init__(self, engine):
        self.e = engine

    # ------------------------------------------------------------ sides from the search box
    def sides(self, intent) -> list[str] | None:
        """The two searches to compare, from a compare intent; None when it isn't two of something."""
        if not intent or intent.kind != "compare" or len(intent.alternatives) != 2:
            return None
        a, b = (x.strip() for x in intent.alternatives)
        if not a or not b:
            return None
        if len(a.split()) >= 2 or len(b.split()) >= 2:
            # two products: "GE 3ton vs Rheem 3ton". A side that's only a brand borrows the other's product
            # ("GE vs Rheem 3 ton condenser")
            if len(a.split()) == 1:
                a = f"{a} {self._without_brand(b)}"
            elif len(b.split()) == 1:
                b = f"{b} {self._without_brand(a)}"
            return [a, b]
        prod = intent.product.strip()
        if not prod:
            return None
        words = {w.lower() for w in prod.split()}
        if a.lower() in words or b.lower() in words or a.lower() == b.lower():
            return None             # "Taco VS circulator", "type L and type M copper": not two kinds of one product
        return [f"{a} {prod}", f"{b} {prod}"]          # two kinds of one product: "sweat" / "press" 3/4 ball valves

    def _without_brand(self, s: str) -> str:
        brands = {str(b).lower() for b in self.e.results._brands()}
        words = s.split()
        for n in (3, 2, 1):
            if " ".join(words[:n]).lower() in brands:
                return " ".join(words[n:])
        return s

    def grid(self, sides: list[str], picks: list[dict]) -> tuple[str, list[str]] | None:
        """The results grid under a compare: the shared product, filtered to both brands when they differ."""
        rest = [self._without_brand(s) for s in sides]
        brands = [p["brand"] for p in picks]
        if brands[0] != brands[1] and normalize(rest[0]) == normalize(rest[1]) and rest[0].strip():
            return rest[0], brands
        return None

    def from_search(self, sides: list[str], p, branch=None, llm: str = "auto", **search) -> dict | None:
        """`llm` and `search` (settings, vocab, personalize) are the page's own, so llm=off and a draft preview reach
        both sides' searches and the summary."""
        picks, cands = [], []
        for k, q in enumerate(sides):
            res = self.e.results.results(q, p, branch, llm=llm, _nested=True, **search)
            items = [i for i in res.get("items") or [] if i.get("sku")]
            if not items:
                return None
            pick = items[0]
            if k == 1:      # like with like: the first side's kind of product (family, then type), when the second has one
                same = [i for i in items if i.get("family") == picks[0].get("family")] or items
                t0 = (picks[0].get("attrs") or {}).get("type")
                pick = next((i for i in same if t0 and (i.get("attrs") or {}).get("type") == t0), same[0])
            picks.append(pick)
            fam = picks[0]["family"] if k == 1 else pick["family"]
            cands.append([{"sku": i["sku"], "title": i["title"], "price": i.get("price")}
                          for i in items[:12] if i["sku"] != pick["sku"] and i.get("family") == fam][:5])
        if picks[0]["sku"] == picks[1]["sku"]:
            return None
        out = self.build([x["sku"] for x in picks], p, branch, queries=sides, llm=llm)
        for s, c in zip(out["sides"], cands):
            s["candidates"] = c
        return out

    # ------------------------------------------------------------ the table
    def _card(self, sku: str, p, branch=None) -> dict | None:
        return self.e.results.product(str(sku), p, branch)

    def build(self, skus: list[str], p, branch=None, queries: list[str] | None = None, llm: str = "auto") -> dict:
        cards = [self._card(s, p, branch) for s in skus[:2]]
        if any(c is None for c in cards):
            raise UnknownItem("unknown item")
        a, b = cards
        sides = []
        for k, c in enumerate(cards):
            here = next((x for x in c.get("availability") or [] if x.get("home")), None)
            sides.append({"query": (queries or [None, None])[k], "label": None, "candidates": [],
                          "pick": {"sku": c["sku"], "title": c["title"], "brand": c["brand"], "family": c["family"],
                                   "mfr_part": c["mfr_part"], "price": (c.get("price") or {}).get("price"),
                                   "attrs": c.get("attrs") or {}, "eta": c.get("eta"),
                                   "at_branch": here["qty"] if here else 0, "branch": here["name"] if here else None,
                                   "install_note": c.get("install_note")}})
        rows = self.rows(a, b)
        self._labels(sides, queries)
        summary = self.summary(sides, rows, llm=llm)
        return {"sides": sides, "rows": rows, "summary": summary}

    def rows(self, a: dict, b: dict) -> list[dict]:
        out: list[dict] = []

        def add(key, label, va, vb, na=None, nb=None):
            if va in (None, "", "—") and vb in (None, "", "—"):
                return
            va = "—" if va in (None, "") else va
            vb = "—" if vb in (None, "") else vb
            diff = str(va) != str(vb)
            better = None
            if diff and (key in HIGHER or key in LOWER):
                na = _num(va) if na is None else na
                nb = _num(vb) if nb is None else nb
                if na is not None and nb is not None and abs(na - nb) >= (3 if key == "sound" else 1e-9):
                    better = ("a" if na > nb else "b") if key in HIGHER else ("a" if na < nb else "b")
            out.append({"key": key, "label": label, "a": va, "b": vb, "diff": diff, "better": better})

        pa, pb = (a.get("price") or {}).get("price"), (b.get("price") or {}).get("price")
        add("brand", "Brand", a["brand"], b["brand"])
        add("mfr_part", "Mfr #", a["mfr_part"], b["mfr_part"])
        add("price", "Price", _money(pa), _money(pb), pa, pb)

        def stock(c):
            here = next((x for x in c.get("availability") or [] if x.get("home")), None)
            return (f"{here['qty']} at {here['name'].split(',')[0]}" if here and here["qty"] else c.get("eta") or "—",
                    float(here["qty"]) if here else 0.0)
        (sa, qa), (sb, qb) = stock(a), stock(b)
        add("stock", "Availability", sa, sb, qa, qb)
        seen = set()
        for k in list(a.get("attrs") or {}) + [k for k in (b.get("attrs") or {}) if k not in (a.get("attrs") or {})]:
            key, label = ATTR.get(k, (k, k.replace("_", " ").capitalize()))
            if key in seen:
                continue
            seen.add(key)
            add(key, label, (a.get("attrs") or {}).get(k), (b.get("attrs") or {}).get(k))
        ha, hb = (a.get("hvac") or {}).get("spec"), (b.get("hvac") or {}).get("spec")
        if ha and hb:
            add("sound", "Sound", f"{ha['sound_db']} dB", f"{hb['sound_db']} dB")
            hp = lambda h: f"{round(100 * h['cap5_ratio'])}%" if h.get("kind") == "HP" and h.get("cap5_ratio") else None  # noqa: E731
            add("cap5", "Heating capacity at 5°F", hp(ha), hp(hb))
            ca = {c["key"]: c["ok"] for c in (a.get("hvac") or {}).get("checks") or []}
            cb = {c["key"]: c["ok"] for c in (b.get("hvac") or {}).get("checks") or []}
            yn = {True: "Yes", False: "No", None: "—"}
            for key, label in (("energy_star", "ENERGY STAR"), ("cold_climate", "Cold-climate listed")):
                if key in ca or key in cb:
                    add(key, label, yn[ca.get(key)], yn[cb.get(key)], float(bool(ca.get(key))), float(bool(cb.get(key))))
            add("weight", "Weight", f"{ha['weight_lb']} lb", f"{hb['weight_lb']} lb")
            add("electrical", "Electrical", f"{ha['voltage']}, MCA {ha['mca']} A", f"{hb['voltage']}, MCA {hb['mca']} A")
        add("history", "You bought", (a.get("history") or {}).get("times") and f"{a['history']['times']}×",
            (b.get("history") or {}).get("times") and f"{b['history']['times']}×")
        return out

    def _labels(self, sides: list[dict], queries) -> None:
        """What to call each side in the summary: the brand when they differ, else the word that tells them apart.
        Words from the query are at most two per side (they go to FlexIQ and into the cached summary)."""
        pa, pb = sides[0]["pick"], sides[1]["pick"]
        brands = {str(b).lower() for b in self.e.results._brands()}
        if queries:
            wa, wb = set(normalize(queries[0])), set(normalize(queries[1]))
            da = [w for w in queries[0].split() if normalize(w) and set(normalize(w)) - wb]
            db = [w for w in queries[1].split() if normalize(w) and set(normalize(w)) - wa]
            if da and db and " ".join(da).lower() not in brands and " ".join(db).lower() not in brands \
                    and len(da) <= 2 and len(db) <= 2:
                sides[0]["label"], sides[1]["label"] = _cap(" ".join(da)), _cap(" ".join(db))
                return
        if pa["brand"] != pb["brand"]:
            sides[0]["label"], sides[1]["label"] = pa["brand"], pb["brand"]
            return
        if queries:
            wa, wb = set(normalize(queries[0])), set(normalize(queries[1]))
            da, db = [w for w in queries[0].split() if normalize(w) and set(normalize(w)) - wb], \
                     [w for w in queries[1].split() if normalize(w) and set(normalize(w)) - wa]
            if da and db and len(da) <= 2 and len(db) <= 2 and len(" ".join(da + db)) <= 40:
                sides[0]["label"], sides[1]["label"] = _cap(" ".join(da)), _cap(" ".join(db))
                return
        sides[0]["label"], sides[1]["label"] = f"{pa['brand']} {pa['mfr_part']}", f"{pb['brand']} {pb['mfr_part']}"

    # ------------------------------------------------------------ the summary
    def summary(self, sides: list[dict], rows: list[dict], llm: str = "auto") -> dict:
        rules = self.rules_summary(sides, rows)
        claude = getattr(self.e, "claude", None)
        if llm == "off" or claude is None or not getattr(claude, "enabled", False):
            return rules
        table = self.table(sides, rows)
        system = ("You are FlexIQ, F.W. Webb's search assistant, comparing two products for a contractor. Use only the "
                  "table you are given: every number you write must appear in it, and say nothing about features, "
                  "reliability or costs the table doesn't show. Never comment on price, cost or value. Call the products by their call_it names. Text: two or "
                  "three sentences on the differences that matter. choose_a_if / choose_b_if: a short clause each.")
        try:
            out = claude.structured(model=settings.model_fast, system=system, user=json.dumps(table), tool_name="compare",
                                    schema=SCHEMA, timeout=6, max_tokens=400,
                                    cache_key="cmp|" + hashlib.sha1(json.dumps(table, sort_keys=True).encode()).hexdigest())
        except Exception:
            out = None
        if not isinstance(out, dict) or not all(isinstance(out.get(k), str) and out[k].strip() for k in SCHEMA["required"]) \
                or not self._grounded(out, table) or guard.violations(
                    " ".join(out[k] for k in SCHEMA["required"]), sources=json.dumps(table)):
            return rules
        return {"text": out["text"].strip(), "choose_a_if": out["choose_a_if"].strip(),
                "choose_b_if": out["choose_b_if"].strip(), "engine": "flexiq"}

    @staticmethod
    def table(sides: list[dict], rows: list[dict]) -> dict:
        """What FlexIQ is given, and all it may cite. The prices, stock and "you bought" are the viewer's, so the
        summary is cached per table, not per pair of items."""
        return {"a": {"sku": sides[0]["pick"]["sku"], "title": sides[0]["pick"]["title"], "call_it": sides[0]["label"]},
                "b": {"sku": sides[1]["pick"]["sku"], "title": sides[1]["pick"]["title"], "call_it": sides[1]["label"]},
                "rows": [{k: x[k] for k in ("label", "a", "b", "better")} for x in rows if x.get("key") != "price"]}

    @staticmethod
    def _grounded(out: dict, table: dict) -> bool:
        """Every number FlexIQ writes is in the table (2.50 and 2.5 are the same; 20 and 2 are not), and it writes
        no number in words beyond ten."""
        have = {_trim0(n.replace(",", "")) for n in _NUM.findall(json.dumps(table, ensure_ascii=False))}
        said = " ".join(str(out.get(k) or "") for k in ("text", "choose_a_if", "choose_b_if"))
        if _NUM_WORDS.search(said):
            return False
        return all(_trim0(n.replace(",", "")) in have for n in _NUM.findall(said))

    def rules_summary(self, sides: list[dict], rows: list[dict]) -> dict:
        la, lb = sides[0]["label"], sides[1]["label"]
        name = {"a": la, "b": lb}
        for k, sd in zip("ab", sides):        # a kind ("Sweat") reads better with its brand: "Sweat (NIBCO)"
            if sd["label"] != sd["pick"]["brand"] and sd["pick"]["brand"] not in sd["label"]:
                name[k] = f"{sd['label']} ({sd['pick']['brand']})"
        other = {"a": "b", "b": "a"}
        said = []
        by = {x["key"]: x for x in rows}
        # no sentence about price (v2.2): the table's price row shows the account's prices, the summary doesn't comment
        eff = [by[k] for k in ("seer2", "eer2", "hspf2", "afue") if by.get(k) and by[k]["better"]]
        for w in ("a", "b"):
            mine = [x for x in eff if x["better"] == w]
            if mine:
                said.append(f"{name[w]} is more efficient: " + ", ".join(
                    f"{x['label']} {_num(x[w]):g} against {_num(x[other[w]]):g}" for x in mine) + ".")
        for key in ("cap5", "sound", "energy_star", "cold_climate"):
            x = by.get(key)
            if x and x["better"] and len(said) < 5:
                w = x["better"]
                word = {"sound": "is quieter", "energy_star": "is ENERGY STAR", "cold_climate": "is cold-climate listed",
                        "cap5": "keeps more heating capacity at 5°F"}.get(key, f"has the higher {x['label']}")
                said.append(f"{name[w]} {word}." if x[w] in ("Yes", "No") else f"{name[w]} {word} ({x[w]} against {x[other[w]]}).")
        s = by.get("stock")
        if s and s["diff"]:
            said.append(f"Availability: {name['a']} {s['a']}, {name['b']} {s['b']}.")
        same = [f"{x['label'].lower()} {x['a']}" for x in rows
                if not x["diff"] and x["key"] in ("tonnage", "refrigerant", "type", "size", "connection", "material", "port")
                and x["a"] != "—"][:3]
        if same:
            said.append("Both: " + ", ".join(same) + ".")
        if not said:
            said.append(f"{la} and {lb} list the same specs here.")

        def why(side):
            r_ = [WHY[x["key"]] for x in rows if x["better"] == side and x["key"] in WHY]
            if not r_:      # nothing better, but something different: "you want the Tee Handle"
                r_ = [f"you want {x[side]} ({x['label'].lower()})" for x in rows if x["diff"] and not x["better"]
                      and x["key"] in ("type", "connection", "port", "handle", "material", "refrigerant", "discharge",
                                       "size", "voltage") and x[side] != "—"][:2]
            return "; ".join(dict.fromkeys(r_)) or f"you prefer {name[side]}"
        return {"text": " ".join(said), "choose_a_if": why("a"), "choose_b_if": why("b"), "engine": "rules"}
