"""The Knowledge service: a question in, a cited answer out.

  1. Classify: emergency, tier 3, a competitor, off topic, an error code, a serial number, or a question.
  2. Scope: the product line the question is about (from the item, a model number or a series in the words).
  3. Retrieve: BM25 over the library's pages, the line's documents and Webb's guides first.
  4. Answer: FlexIQ writes statements that each cite a page; code checks every citation, every number and every
     name before it's shown. A failed check shows the documents' own sentences instead (rules), never an unchecked
     answer. Tier 3 and emergencies never reach FlexIQ.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
import time
from collections import Counter
from functools import cached_property

from webbsearch.config import settings
from webbsearch.knowledge import codes as codes_mod
from webbsearch.knowledge import diagram as diag
from webbsearch.knowledge import scope as scope_mod
from webbsearch.knowledge import serials as serials_mod
from webbsearch.knowledge.library import CLS_NAME, Library, warranty_years
from webbsearch.knowledge.safety import BLOCKS, FOOTER, SECTION_TIER, Tier, classify
from webbsearch.llm import guardrails as guard
from webbsearch.retrieval.analyzer import normalize
from webbsearch.retrieval.entitlements import ANONYMOUS

STOP = {"what", "is", "the", "a", "an", "how", "do", "i", "it", "this", "of", "to", "for", "my", "does", "can", "should",
        "on", "in", "and", "or", "with", "be", "are", "you", "me", "we", "there", "that", "when", "where", "why", "which",
        "need", "much", "many", "often", "will", "would", "could", "your", "if", "at", "from", "as", "its", "heater",
        "water", "unit", "model", "get", "use", "have", "has", "they", "them", "these", "those", "any", "about"}
NUM = re.compile(r"(?<![\w.])\d+(?:[.,/]\d+)*(?![\w/])")
SCHEMA = {"type": "object", "properties": {
    "statements": {"type": "array", "items": {"type": "object", "properties": {
        "text": {"type": "string"}, "cites": {"type": "array", "items": {"type": "integer"}}}, "required": ["text", "cites"]}},
    "not_in_docs": {"type": "string"}, "follow_ups": {"type": "array", "items": {"type": "string"}}},
    "required": ["statements", "not_in_docs", "follow_ups"]}
SYSTEM = ("You are FlexIQ, F.W. Webb's product assistant, answering a contractor's question from the numbered document "
          "passages you are given and nothing else. Write 1 to 4 short statements; each must cite the passage numbers "
          "it comes from. Every number, size, rating and step you write must appear in a cited passage. If the passages "
          "don't answer part of the question, say what is missing in not_in_docs instead of guessing. For gas, venting, "
          "refrigerant, line-voltage or pressure work, restate only what the passages say; never add steps, workarounds "
          "or shortcuts. Talk only about F.W. Webb and Frank Webb products and services: never name or mention another "
          "distributor or retailer. follow_ups: up to 3 short follow-up questions the same documents can answer.")
# v2.4 (review G12): a question asking for one value that differs by model (a connection size, a clearance, a rating,
# an electrical spec, a setting). Without a model, the answer is a question back, never one model's number
MODEL_SPECIFIC = re.compile(
    r"\b(?:what|which|how\s+(?:big|tall|wide|deep|heavy|much|many))\b.{0,60}?\b(?:size|sized|diameter|inlet|outlet|"
    r"connection|connector|clearance|clearances|dimensions?|rating|input|btu\w*|amps?|amperage|breaker|fuse|wire|wiring|"
    r"gauge|voltage|volts?|setting|settings|pressure|temperature|gpm|flow|vent\w*\s+(?:size|length|diameter)|anode|"
    r"element|weight|height|width|depth|capacity|recovery|rough[\s-]?in)\b", re.I)


def needs_model(q: str) -> bool:
    return bool(MODEL_SPECIFIC.search(q or ""))


FOLLOW = {"gas_tank": ["What's the vent connector size?", "Clearance to combustibles?", "Show the parts diagram",
                       "Warranty for this model"],
          "elec_tank": ["What breaker does it need?", "How do I flush the tank?", "Show the parts diagram",
                        "Warranty for this model"],
          "tankless": ["Maximum vent length?", "How often should it be descaled?", "Show the parts diagram",
                       "Warranty for this model"]}


def _stem(t: str) -> str:
    if len(t) > 4 and t.isalpha():
        for suf in ("ing", "ed", "es", "s", "e"):
            if t.endswith(suf) and len(t) - len(suf) >= 4:
                return t[: -len(suf)]
    return t


STOP |= set(normalize(" ".join(sorted(STOP))))          # the analyzer's forms too ("does" -> "doe")


def _toks(text: str) -> list[str]:
    out = []
    for t in normalize(text):
        if t in STOP:
            continue
        st = _stem(t)
        if st not in STOP:
            out.append(st)
    return out


def _nums(text: str) -> set[str]:
    return {n.replace(",", "") for n in NUM.findall(text or "")}


# numbers the way a checker has to read them: glued to units ("50A", "6AWG"), ".5", "¾", spelled out ("fifty")
_FRAC = {"½": "1/2", "¼": "1/4", "¾": "3/4", "⅓": "1/3", "⅔": "2/3", "⅛": "1/8", "⅜": "3/8", "⅝": "5/8", "⅞": "7/8"}
_UNIT = {"a": "a", "amp": "a", "amps": "a", "ampere": "a", "amperes": "a", "v": "v", "vac": "v", "volt": "v", "volts": "v",
         "awg": "awg", "ga": "awg", "gauge": "awg", "w": "w", "watt": "w", "watts": "w", "kw": "kw", "gal": "gal",
         "gallon": "gal", "gallons": "gal", "in": "in", "inch": "in", "inches": "in", "ft": "ft", "feet": "ft", "foot": "ft",
         "psi": "psi", "btu": "btu", "btuh": "btu", "mbh": "mbh", "gpm": "gpm", "gph": "gph", "f": "f", "°f": "f",
         "degrees": "f", "deg": "f", "years": "yr", "year": "yr", "yr": "yr", "months": "mo", "month": "mo", "mm": "mm",
         "lbs": "lb", "lb": "lb", "hz": "hz", "phase": "ph", "%": "%", "minutes": "min", "min": "min", "seconds": "s"}
_NUMU = re.compile(r"(?<![\w.])(\d+(?:[.,/]\d+)*|\.\d+)\s?(°\s?f\b|%|[a-z]+\b)?", re.I)
_SPELLED = re.compile(r"\b(?:two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|"
                      r"seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|"
                      r"thousand|half|quarter|dozen)\b", re.I)


def _numunits(text: str) -> tuple[set[str], set[tuple[str, str]]]:
    t = text or ""
    for k, v in _FRAC.items():
        t = t.replace(k, " " + v)
    nums, pairs = set(), set()
    for m in _NUMU.finditer(t):
        n = m.group(1).replace(",", "")
        if n.startswith("."):
            n = "0" + n
        nums.add(n)
        u = _UNIT.get((m.group(2) or "").lower().replace(" ", ""))
        if u:
            pairs.add((n, u))
    return nums, pairs


_FOLLOWUP = re.compile(r"\b(?:that|this|it|those|them|these|do\s+so|same|instead|then|which\s+(?:two\s+)?wires?|"
                       r"how\s+(?:exactly|else)|what\s+if|and\s+if|ok|okay|so)\b", re.I)


def _history(history) -> list[str]:
    """Client-held turns, reduced to the last few questions as plain text."""
    out = []
    for h in (history or [])[-6:]:
        if not isinstance(h, dict):
            continue
        q = h.get("q") if isinstance(h.get("q"), str) else (h.get("content") if h.get("role") == "user" else None)
        if isinstance(q, str) and q.strip():
            out.append(q.strip()[:300])
    return out[-4:]


_ABBR = re.compile(r"\b(in|ft|No|no|e\.g|i\.e|approx|min|max|Sch|vs)\.\s")


def _sentences(text: str) -> list[str]:
    """Split into sentences without breaking at "1/2 in. NPT" or "e.g. "."""
    t = _ABBR.sub(lambda m: m.group(1) + ".\x00", text or "")
    return [s.replace("\x00", " ").strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", t) if s.strip()]


FLEXIQ_PER_HOUR = 60  # FlexIQ answers per account per hour; past it, the documents' own sentences (v2.2)
WEAK = 3.0          # a best passage scoring under this is not worth FlexIQ's time: the rules answer (or "not in the docs")


class Knowledge:
    def __init__(self, engine):
        self.e = engine
        self.codes = codes_mod
        self.serials = serials_mod
        self._spent: dict[str, list[float]] = {}      # account -> FlexIQ call times in the last hour

    def _budget_ok(self, p) -> bool:
        key = str(getattr(p, "account_id", None) or "anonymous")
        now = time.time()
        recent = [t for t in self._spent.get(key, []) if now - t < 3600]
        if len(recent) >= FLEXIQ_PER_HOUR:
            self._spent[key] = recent
            return False
        self._spent[key] = recent + [now]
        return True

    def _branch_label(self, p, branch) -> str | None:
        try:
            b = self.e.accounts.branches
            for bid in (branch, getattr(p, "home_branch", None)):
                if bid and bid in b.index:
                    return f"the {b.loc[bid, 'name']} branch"
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------ library and index (built on first use)
    @cached_property
    def lib(self) -> Library:
        return Library(self.e.catalog)

    @cached_property
    def _index(self):
        docs, tf, df, pairs = [], [], Counter(), []
        for d, pg in self.lib.passages():
            toks = _toks(f"{pg['section']} {pg['text']} {d.title}")
            c = Counter(toks)
            docs.append((d, pg)); tf.append(c); df.update(c.keys()); pairs.append(set(zip(toks, toks[1:])))
        n = len(docs)
        idf = {t: math.log(1 + (n - k + 0.5) / (k + 0.5)) for t, k in df.items()}
        avg = sum(sum(c.values()) for c in tf) / max(n, 1)
        return docs, tf, idf, avg, pairs

    @staticmethod
    def cls_hint(q: str) -> str | None:
        """The kind of heater the words name when no model does: "50 gallon gas" is a tank, "tankless" isn't."""
        if re.search(r"\btankless\b|\bon[\s-]demand\b", q or "", re.I):
            return "tankless"
        if re.search(r"\belectric\b", q or "", re.I):
            return "elec_tank"
        if re.search(r"\b(?:gas|propane|lp)\b", q or "", re.I) and re.search(r"\b(?:tank|\d+\s*(?:gal\w*|g)\b)", q or "", re.I):
            return "gas_tank"
        return None

    def search(self, q: str, line_id: str | None = None, k: int = 6) -> list[tuple[float, object, dict]]:
        docs, tf, idf, avg, pairs = self._index
        qs = _toks(q)
        qt, qpairs = set(qs), set(zip(qs, qs[1:]))
        hint = None if line_id else self.cls_hint(q)
        lines = self.lib.lines
        out = []
        for (d, pg), c, pr in zip(docs, tf, pairs):
            if line_id and d.line_id not in (line_id, None):
                continue
            ln = sum(c.values())
            # a number in the question ("50 gallon") counts half: the noun beside it says what is asked
            s = sum(idf.get(t, 0) * (0.5 if t.isdigit() else 1) * c[t] * 2.2 / (c[t] + 1.2 * (0.25 + 0.75 * ln / avg))
                    for t in qt if c.get(t))
            if s > 0:
                s += 2.5 * len(qpairs & pr)        # "gas inlet" as a phrase beats "gas" and "inlet" apart
                if hint and d.line_id in lines:
                    s *= 1.25 if lines[d.line_id].cls == hint else 0.6
                out.append((s * (1.15 if d.line_id else 1.0), d, pg))
        out.sort(key=lambda x: -x[0])
        return out[:k]

    # ------------------------------------------------------------------ scope
    def detect_line(self, q: str):
        """A model number or series in the words -> its product line; several lines -> None and the choices."""
        lib = self.lib
        for tok in re.findall(r"[A-Za-z0-9][A-Za-z0-9-]{3,}", q or ""):
            ln = lib.line_for_model(tok)
            if ln:
                return ln, []
        cands = [l for l in lib.lines.values() if re.search(rf"\b{re.escape(l.series)}\b", q or "", re.I)]
        if len(cands) > 1:
            fuel = "elec_tank" if re.search(r"\belectric\b", q, re.I) else "tankless" if re.search(r"\btankless\b", q, re.I) \
                else "gas_tank" if re.search(r"\b(?:gas|propane)\b", q, re.I) else None
            cands = [l for l in cands if l.cls == fuel] or cands
        if len(cands) == 1:
            return cands[0], []
        return None, cands

    def _scope(self, sku=None, line_id=None, q: str = ""):
        lib = self.lib
        if sku:
            ln = lib.line_for_sku(sku)
            if ln:
                return ln, str(sku), []
        if line_id and line_id in lib.lines:
            return lib.lines[line_id], None, []
        ln, cands = self.detect_line(q)
        return ln, None, cands

    def _named_product(self, q: str) -> dict | None:
        """A part number, model number or Webb item number in the question that names exactly one catalog product."""
        from webbsearch.retrieval.analyzer import compact_id, looks_like_part_number
        cat = self.e.catalog
        for tok in re.findall(r"[A-Za-z0-9][A-Za-z0-9-]{3,}", q or ""):
            if not (looks_like_part_number(tok) or (tok.isdigit() and len(tok) >= 6)):
                continue
            if self.lib.line_for_model(tok):
                return None                          # a model the library knows: detect_line scopes it
            hits = cat.id_lookup(compact_id(tok))
            if len(hits) == 1:
                i = int(hits[0])
                return {"sku": str(cat.sku[i]), "title": str(cat.title[i]), "mfr_part": str(cat.mfr_part[i])}
        return None

    def tier(self, q: str, line=None, floor: int = 0):
        return classify(q, line, floor)

    # ------------------------------------------------------------------ the question
    def ask(self, question: str, p=None, sku=None, line_id: str | None = None, history: list | None = None,
            llm: str = "auto", branch: str | None = None) -> dict:
        t0 = time.time()
        q = (question or "").strip()[:1000]
        p = p or ANONYMOUS
        ln, sku_, cands = self._scope(sku, line_id, q)
        named = None if (ln or sku) else self._named_product(q)
        if named is not None:      # v2.4 (review G03): a part or model number the catalog knows scopes the answer to it
            ln2 = self.lib.line_for_sku(named["sku"])
            if ln2:
                ln, sku_ = ln2, named["sku"]
        base = {"question": q, "statements": [], "sources": [], "cards": [], "follow_ups": [], "not_in_docs": "",
                "footer": FOOTER, "notice": self.lib.notice, "engine": "rules", "flags": [],
                "scope": self._scope_out(ln, sku_, cands), "expert": {"raised": False}}
        gi = guard.inspect(q, self._branch_label(p, branch), price_lookup_ok=False)
        base["flags"], base["notes"] = list(gi.flags), list(gi.notes)
        tier = self.tier(q, ln)
        past = _history(history)
        if tier.level < 3 and past and _FOLLOWUP.search(q):
            for prev in past[-3:]:               # "ok, which two wires do I connect to do that?" after a declined question
                pt = self.tier(f"{prev} {q}", ln)
                if pt.level >= 3 and self.tier(prev, ln).level >= 3:
                    tier = Tier(pt.level, pt.reasons + ["follows a question FlexIQ declined"])
                    break
        safe_past = [x for x in past if self.tier(x, ln).level < 3]
        if tier.level == 9:
            return self._done(base, t0, kind="emergency", tier=tier.as_dict(), emergency=BLOCKS[9][1],
                              expert={"raised": True, "reason": "Emergency: get to safety first, then call your branch."})
        if tier.level == 3:
            return self._done(base, t0, kind="declined", tier=tier.as_dict(), message=BLOCKS[3][1],
                              expert={"raised": True, "reason": "A Webb expert can help find the cause and the right part."})
        if scope_mod.competitor(q):
            return self._done(base, t0, kind="webb_only", tier=tier.as_dict(), message=scope_mod.WEBB_ONLY,
                              flags=["competitor"], expert={"raised": True, "reason": "Pricing and availability: your Webb branch."})
        if gi.kind:                              # pricing, a complaint, a claim, injection, abuse, where to buy (v2.2)
            kind = "out_of_scope" if gi.kind == "injection" else gi.kind
            reason = {"pricing": "Pricing and quotes: your Webb branch.", "complaint": "Your branch manager will follow up.",
                      "legal": "Your branch manager will follow up.", "where_to_buy": "Stock and pickup: your Webb branch."
                      }.get(gi.kind, "")
            return self._done(base, t0, kind=kind, tier=tier.as_dict(), message=gi.reply,
                              expert={"raised": bool(reason), "reason": reason})
        if named is not None and ln is None:
            # the product is known and the library has nothing on it: say so, and never cite another product's documents
            return self._done(base, t0, kind="no_docs", tier=tier.as_dict(), flags=["no_docs"],
                              message=(f"The library has no documents for {named['title']} ({named['mfr_part']}) yet. "
                                       "A Webb expert can send the manufacturer's manual."),
                              scope={"line": None, "sku": named["sku"], "choices": []},
                              expert={"raised": True, "reason": "No documents for this product: a Webb expert can help."})
        parsed = codes_mod.parse(q)
        if parsed and codes_mod.ASKS.search(q):
            code = self.error_code(q, sku=sku_, line_id=ln.id if ln else None, p=p)
            return self._done(base, t0, kind="error_code", code=code, tier=code.get("tier") or tier.as_dict(),
                              sources=code.get("sources", []), expert={"raised": not code.get("found") or
                                                                       (code.get("tier") or {}).get("level", 0) >= 2})
        if re.search(r"\bserial\b", q, re.I) or serials_mod.nameplate(q).get("serial"):
            s = self.decode(text=q, sku=sku_)
            if s.get("serial"):
                return self._done(base, t0, kind="serial", serial=s, tier=tier.as_dict(), sources=s.get("sources", []))
        hits = self.search(q, ln.id if ln else None)
        if scope_mod.not_a_question(q) or (not ln and not scope_mod.on_topic(q)):
            return self._done(base, t0, kind="out_of_scope", tier=tier.as_dict(), message=guard.off_topic_reply(q),
                              flags=["out_of_scope"])
        # v2.4 (review G12): a model-specific value with no model to scope it: ask which model first. The documents
        # that matched cover more than one product line, so any number would be one line's number stated as everyone's
        lines_hit = list(dict.fromkeys(d.line_id for _, d, _ in hits if d.line_id))
        if not ln and needs_model(q) and len(lines_hit) > 1:
            lib = self.lib
            opts = [lib.lines[i] for i in lines_hit if i in lib.lines][:6] or cands[:6]
            from webbsearch.retrieval.analyzer import looks_like_part_number
            typed = [t for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9-]{4,}", q) if looks_like_part_number(t)]
            lead = (f"The library has no documents for {typed[0]}. " if typed else "")
            return self._done(base, t0, kind="needs_model", tier=tier.as_dict(), flags=["needs_model"],
                              message=(lead + "That depends on the model, and the documents cover several. Type the "
                                       "model number from the rating plate (or a serial number), or pick the product line:"),
                              scope={"line": None, "choices": [{"line": c.id, "name": c.name} for c in opts]},
                              follow_ups=[f"{q.rstrip('?')} on a {c.name}?" for c in opts[:3]],
                              expert={"raised": False, "reason": ""})
        numbered = [{"n": k + 1, "score": s, "doc": d, "page": pg} for k, (s, d, pg) in enumerate(hits)]
        rules = self._extract(q, numbered, self._prefer(sku_))
        out, engine, flags = rules, "rules", []
        if llm != "off" and getattr(self.e.claude, "enabled", False) and numbered and numbered[0]["score"] >= WEAK \
                and not self._budget_ok(p):
            flags.append("flexiq_budget")
        elif llm != "off" and getattr(self.e.claude, "enabled", False) and numbered and numbered[0]["score"] >= WEAK:
            fx = self._flexiq(guard.redact(q), numbered, tier.level, [guard.redact(x) for x in safe_past])
            if fx is not None and self._verified(fx, numbered, tier.level):
                out, engine = fx, "flexiq"
            elif fx is not None:
                flags.append("flexiq_rejected")
        if numbered and numbered[0]["score"] < 6:
            flags.append("low_match")
        if not ln and len({h["doc"].line_id for h in numbered[:3] if h["doc"].line_id}) > 1:
            flags.append("unscoped")
        cited = sorted({n for s in out["statements"] for n in s["cites"]})
        by_n = {h["n"]: h for h in numbered}
        page_tier = max([SECTION_TIER.get(by_n[n]["page"]["section"], 0) for n in cited if n in by_n] or [0])
        if page_tier > tier.level:                # the block follows what the answer quotes, not only the words
            tier = Tier(page_tier, tier.reasons + [f"quotes the “{by_n[cited[0]]['page']['section']}” section"
                                                   if len(cited) == 1 else "quotes a gas, venting or electrical section"])
        sources = [self._src(by_n[n]) for n in cited if n in by_n]
        not_in = out.get("not_in_docs") or ("" if out["statements"] else
                                            "The library doesn't cover this. A Webb expert can help.")
        cards = self._cards([by_n[n] for n in cited if n in by_n], q, ln, p)
        return self._done(base, t0, kind="answer", statements=out["statements"], sources=sources, engine=engine,
                          tier=tier.as_dict(), not_in_docs=not_in, cards=cards, flags=flags,
                          follow_ups=(out.get("follow_ups") or [])[:3] or (FOLLOW.get(ln.cls, []) if ln else []),
                          expert={"raised": tier.level >= 2 or bool(not_in),
                                  "reason": "Licensed work: a Webb expert can confirm the details." if tier.level >= 2
                                  else "Not in the documents: a Webb expert can help." if not_in else ""})

    def _done(self, base: dict, t0: float, **kw) -> dict:
        out = {**base, **kw}
        out["flags"] = list(dict.fromkeys((base.get("flags") or []) + (kw.get("flags") or [])))
        out["ms"] = round((time.time() - t0) * 1000)
        return out

    def _prefer(self, sku) -> list[str]:
        if not sku:
            return []
        a = self.lib.sku_spec(sku)
        out = []
        if a.get("gal") and a.get("profile"):
            out.append(f"{a['gal']} gal {a['profile']}")
        if a.get("warranty"):
            out.append(f"{warranty_years(a['warranty'])[0]}-year")
        if a.get("btu"):
            out.append(f"{a['btu']} BTU")
        return out

    def _scope_out(self, ln, sku, cands) -> dict:
        if ln:
            return {"line": ln.id, "name": ln.name, "sku": sku, "cls": ln.cls}
        return {"line": None, "choices": [{"line": c.id, "name": c.name} for c in cands[:8]]}

    def _src(self, h: dict) -> dict:
        d, pg = h["doc"], h["page"]
        return {"n": h["n"], "doc_id": d.id, "title": d.title, "kind": d.kind, "page": pg["n"], "section": pg["section"],
                "rev": d.rev, "synthetic": d.synthetic, "owner": d.owner or None, "text": pg["text"][:600]}

    def _extract(self, q: str, numbered: list[dict], prefer: list[str] | None = None) -> dict:
        """Rules: the documents' own sentences that best match the question, each citing its page. `prefer`: strings
        that pick out the asked-about model's row ("50 gal Tall", "10-year")."""
        qseq = _toks(q)
        qt, pairs = set(qseq), {(a, b) for a, b in zip(qseq, qseq[1:])}
        often = bool(re.search(r"\bhow\s+often|\bwhen\s+should|\binterval\b", q, re.I))
        want_num = bool(re.search(r"\b(?:what\s+size|how\s+(?:big|many|much|often|far|long|tall)|rating|input|pressure|"
                                  r"clearance|length|dimension|breaker|wire|gpm|btu|capacity|recovery|temperature)\b", q, re.I))
        need = 2 if len(qt) >= 3 else 1
        scored = []
        for h in numbered[:4]:
            for k, s in enumerate(_sentences(h["page"]["text"])):
                st = set(_toks(s))
                hit = len(qt & st)
                if hit < need or (len(s.split()) < 9 and "Series" in s and not _nums(s.replace(h["doc"].title, ""))
                                  and ":" not in s):
                    continue                      # a heading-like sentence ("Rheem X Series Gas Tank Water Heaters.")
                sc = hit + (0.6 if want_num and _nums(s) else 0) - 0.02 * k + 0.3 * (h is numbered[0])
                sseq = _toks(s)
                sc += 1.0 * len(pairs & set(zip(sseq, sseq[1:])))      # "gas inlet" together beats "inlet" alone
                if often and re.search(r"\b(?:once|every|annual\w*|year|month\w*|times)\b", s, re.I):
                    sc += 1.2
                if prefer and any(x.lower() in s.lower() for x in prefer):
                    sc += 1.5
                scored.append((sc, h["n"], k, s))
        scored.sort(key=lambda x: -x[0])
        best = scored[0][0] if scored else 0
        picked, seen = [], set()
        for sc, n, k, s in scored:
            if sc < 0.7 * best:
                break
            key = re.sub(r"\W+", "", s.lower())[:80]
            if key in seen:
                continue
            seen.add(key)
            picked.append((n, k, s))
            if len(picked) == 3:
                break
        picked.sort(key=lambda x: (x[0], x[1]))
        return {"statements": [{"text": s, "cites": [n]} for n, _, s in picked], "not_in_docs": "", "follow_ups": []}

    def _flexiq(self, q: str, numbered: list[dict], level: int, history) -> dict | None:
        passages = [{"n": h["n"], "document": h["doc"].title, "section": h["page"]["section"], "page": h["page"]["n"],
                     "text": h["page"]["text"]} for h in numbered]
        user = json.dumps({"question": q, "safety_tier": level, "passages": passages,
                           "earlier_questions": [x for x in (history or []) if isinstance(x, str)][-3:]})
        key = "kc|" + hashlib.sha1(user.encode()).hexdigest()
        try:
            out = self.e.claude.structured(model=settings.model_smart, system=SYSTEM, user=user, tool_name="answer",
                                           schema=SCHEMA, timeout=12, max_tokens=700, cache_key=key)
        except Exception:
            return None
        if not isinstance(out, dict) or not isinstance(out.get("statements"), list):
            return None
        sts = [{"text": str(s.get("text") or "").strip(),
                "cites": [c for c in s.get("cites") or [] if type(c) is int] if isinstance(s.get("cites"), list) else []}
               for s in out["statements"] if isinstance(s, dict)]
        return {"statements": [s for s in sts if s["text"]], "not_in_docs": str(out.get("not_in_docs") or "").strip(),
                "follow_ups": [str(f) for f in out.get("follow_ups") or []][:3]}

    @staticmethod
    def _verified(out: dict, numbered: list[dict], level: int = 0) -> bool:
        """Every statement cites passages that exist and says nothing they don't: each number (and its unit) is in a
        cited passage, a spelled-out number is in one too, no statement tells how to defeat a safety device, and no
        competitor is named. not_in_docs and follow-ups are held to the passages' numbers as well."""
        by_n = {h["n"]: h for h in numbered}
        if not out["statements"] and not out.get("not_in_docs"):
            return False
        everything = " ".join([s["text"] for s in out["statements"]] + [out.get("not_in_docs") or ""] + out.get("follow_ups", []))
        if scope_mod.competitor(everything):
            return False

        def text_of(ns):
            return " ".join(by_n[c]["page"]["text"] + " " + by_n[c]["page"]["section"] for c in ns)

        def holds(claim: str, source: str) -> bool:
            cn, cp = _numunits(claim)
            sn, sp = _numunits(source)
            if not cn <= sn or not cp <= sp:
                return False
            sw = {w.lower() for w in _SPELLED.findall(source)}
            return all(w.lower() in sw for w in _SPELLED.findall(claim))

        for s in out["statements"]:
            cites = s.get("cites")
            if not isinstance(cites, list) or not cites or any(type(c) is not int or c not in by_n for c in cites):
                return False
            if not holds(s["text"], text_of(cites)):
                return False
            if guard.violations(s["text"], sources=text_of(cites) + " " + " ".join(
                    h["doc"].title for c, h in by_n.items() if c in cites)):
                return False                        # price talk, Webb, elsewhere, profanity, an unsourced claim (v2.2)
            t = classify(s["text"])
            if t.level == 9 or (t.level == 3 and not re.search(r"\b(?:never|do\s+not|don['’]t|must\s+not|not\s+be)\b",
                                                                s["text"], re.I)):
                return False
        all_text = text_of(list(by_n))
        for extra in [out.get("not_in_docs") or ""] + list(out.get("follow_ups") or []):
            if not holds(str(extra), all_text) or guard.violations(str(extra), sources=all_text):
                return False
        return True

    def _cards(self, used: list[dict], q: str, ln, p) -> list[dict]:
        cat, seen, out = self.e.catalog, set(), []
        skus = []
        for h in used:
            skus += re.findall(r"#(\d{6,8})\b", h["page"]["text"])
        if ln:
            generic = {"gas", "upper", "lower", "drain", "main", "heat", "fan", "flow", "flame", "control", "burner",
                       "pilot", "thermal", "heating"}
            ql = q.lower()
            for r in self.lib.parts_rows(ln.id):
                w = r["part"].lower().replace(" 4500w", "")
                first = w.split()[0]
                key = w if first in generic else first
                if re.search(rf"(?<!\w){re.escape(key)}", ql):
                    skus.append(r["sku"])
        for s in skus:
            i = cat.sku_to_idx.get(s)
            if i is None or s in seen:
                continue
            seen.add(s)
            try:
                out.append({"type": "product", **self.e.results._mini(i, p)})
            except Exception:
                continue
            if len(out) == 3:
                break
        return out

    # ------------------------------------------------------------------ error codes
    def error_code(self, q: str, sku=None, line_id: str | None = None, p=None) -> dict:
        p = p or ANONYMOUS
        parsed = codes_mod.parse(q)
        ln, sku_, cands = self._scope(sku, line_id, q)
        out = {"found": False, "ask_model": False, "choices": [], "message": "", "footer": FOOTER, "notice": self.lib.notice}
        if not parsed:
            out["message"] = "There's no error code in that question. Try “E05” or “4 flashes”."
            return out
        out["label"] = codes_mod.label(parsed)
        if not ln:
            kind = parsed[0]
            want = {"flash": ("gas_tank",), "E": ("tankless",)}.get(kind, ("gas_tank", "tankless"))
            brand = next((b for b in sorted({l.brand for l in self.lib.lines.values()}, key=len, reverse=True)
                          if re.search(rf"\b{re.escape(b)}\b", q, re.I)), None)
            pool = cands or [l for l in self.lib.lines.values() if l.cls in want and (not brand or l.brand == brand)]
            out.update(ask_model=True, message="Which heater? The same code means different things on different models.",
                       choices=[{"line": l.id, "name": l.name} for l in pool[:12]])
            return out
        out["line"], out["line_name"] = ln.id, ln.name
        iom = self.lib.doc_of(ln.id, "iom")
        if ln.cls == "elec_tank":
            pg = next((x for x in iom.pages if x["section"] == "Troubleshooting"), iom.pages[-1])
            out.update(message=f"The {ln.name} have no fault codes. See Troubleshooting in the installation manual, "
                       f"page {pg['n']}.", troubleshooting=pg["text"], page=pg["n"], doc_id=iom.id,
                       sources=[{"n": 1, "doc_id": iom.id, "title": iom.title, "page": pg["n"], "section": pg["section"],
                                 "rev": iom.rev, "synthetic": True}])
            return out
        row = codes_mod.lookup(self.lib.codes.get(ln.id, []), parsed)
        if not row:
            listed = ", ".join(r["label"] for r in self.lib.codes.get(ln.id, []))
            out["message"] = f"{out['label']} isn't listed for the {ln.name}. Codes listed: {listed}."
            return out
        cat = self.e.catalog
        parts = []
        for pr in row["parts"]:
            i = cat.sku_to_idx.get(pr["sku"])
            card = self.e.results._mini(i, p) if i is not None else {}
            parts.append({**pr, **{k: card.get(k) for k in ("title", "price", "avail", "uom", "stock_label") if k in card}})
        tier = self.tier(q, ln, floor=row["tier"]).as_dict()
        out.update(found=True, label=row["label"], meaning=row["meaning"], checks=row["checks"], parts=parts,
                   page=row["page"], doc_id=row["doc_id"], tier=tier,
                   sources=[{"n": 1, "doc_id": row["doc_id"], "title": iom.title, "page": row["page"],
                             "section": "Status light codes" if ln.cls == "gas_tank" else "Error codes", "rev": iom.rev,
                             "synthetic": True}])
        return out

    # ------------------------------------------------------------------ serial numbers
    def decode(self, serial: str | None = None, model: str | None = None, text: str | None = None, sku=None,
               today: dt.date | None = None) -> dict:
        today = today or dt.date.today()
        if text:
            np_ = serials_mod.nameplate(text)
            serial, model = serial or np_["serial"], model or np_["model"]
        ln = self.lib.line_for_model(model) if model else None
        if not ln and sku:
            ln = self.lib.line_for_sku(sku)
        out = {"serial": serial, "model": model, "decoded": False, "line": ln.id if ln else None,
               "line_name": ln.name if ln else None, "footer": FOOTER, "notice": self.lib.notice}
        d = serials_mod.decode(serial or "")
        if not d:
            out["message"] = (f"Can't decode “{serial}”: it doesn't match a serial format in the library. Check the "
                              "rating plate, or send it to a Webb expert." if serial else
                              "Can't decode: no serial number found. Type it as it appears on the rating plate.")
            return out
        made = dt.date(d["year"], d["month"], 1)
        out.update(decoded=True, brand=d["brand"], made=f"{d['year']:04d}-{d['month']:02d}",
                   made_label=made.strftime("%B %Y"), age=serials_mod.age_label(made, today))
        if ln and ln.brand != d["brand"]:
            out["note"] = f"The serial looks like a {d['brand']} serial, but the model is a {ln.brand} model."
        if ln:
            idx = next((k for k, m in enumerate(ln.models) if model and m.upper() == model.upper()), 0)
            spec = self.lib.sku_spec(ln.skus[idx])
            start = serials_mod.add_months(made, 3)
            if ln.cls == "tankless":
                comps = [("Heat exchanger (residential)", 12), ("Parts", 5), ("Labor", 1)]
            else:
                t, pt, lab = warranty_years(spec.get("warranty"))
                comps = [("Tank", t), ("Parts", pt)] + ([("Labor", lab)] if lab else [])
            rows = [{"name": n, "years": y, "until": f"{start.year + y:04d}-{start.month:02d}",
                     "active": dt.date(start.year + y, start.month, 1) > today} for n, y in comps]
            main = rows[0]
            wd = self.lib.doc_of(ln.id, "warranty")
            iom = self.lib.doc_of(ln.id, "iom")
            spg = next(x for x in iom.pages if x["section"] == "Serial number")
            out["warranty"] = {"status": "likely in warranty" if main["active"] else "likely out of warranty",
                               "basis": "manufacture date", "components": rows,
                               "note": "Measured from 3 months after the manufacture date. The install date and "
                                       "registration change it; the branch confirms before a claim."}
            out["sources"] = [{"n": 1, "doc_id": wd.id, "title": wd.title, "page": 2, "section": "Conditions",
                               "rev": wd.rev, "synthetic": True},
                              {"n": 2, "doc_id": iom.id, "title": iom.title, "page": spg["n"], "section": spg["section"],
                               "rev": iom.rev, "synthetic": True}]
            out["documents"] = [{"doc_id": x.id, "title": x.title, "kind": x.kind} for x in self.lib.docs_for_line(ln.id)]
        return out

    # ------------------------------------------------------------------ diagrams, documents, submittals
    def diagram(self, line_id: str, hot: int | None = None) -> dict | None:
        ln = self.lib.lines.get(line_id)
        if not ln:
            return None
        rows = self.lib.parts_rows(ln.id)
        d = self.lib.doc_of(ln.id, "parts")
        return {"line": ln.id, "name": ln.name, "cls": ln.cls, "platform": ln.platform, "doc_id": d.id if d else None,
                "page": 1, "svg": diag.svg(ln.cls, rows, hot), "callouts": diag.callouts(ln.cls, rows),
                "notice": self.lib.notice}

    def doc_pdf(self, doc_id: str) -> bytes | None:
        from webbsearch.knowledge.pdfs import render_doc
        d = self.lib.docs.get(doc_id)
        return render_doc(d, self.lib) if d else None

    def submittal(self, items: list[dict], project: dict) -> tuple[bytes, dict]:
        from webbsearch.knowledge.pdfs import submittal
        return submittal(items, project, self.lib, self.e.catalog)

    def documents_for(self, sku) -> list[dict]:
        return [{"doc_id": d.id, "title": d.title, "kind": d.kind, "rev": d.rev, "pages": len(d.pages),
                 "synthetic": d.synthetic} for d in self.lib.docs_for_sku(sku)]

    def coverage(self) -> dict:
        lib, cat = self.lib, self.e.catalog
        fams = {"wh_tank": 0, "wh_tankless": 0, "wh_indirect": 0}
        covered = {k: 0 for k in fams}
        for i in range(cat.n):
            f = cat.family[i]
            if f in fams:
                fams[f] += 1
                covered[f] += str(cat.sku[i]) in lib.sku_line
        return {"lines": len(lib.lines), "documents": len(lib.docs), "pages": sum(len(d.pages) for d in lib.docs.values()),
                "codes": sum(len(v) for v in lib.codes.values()),
                "families": [{"family": k, "items": fams[k], "with_documents": covered[k]} for k in fams],
                "classes": {c: CLS_NAME[c] for c in CLS_NAME}, "synthetic": True}
