"""Measure a search-dictionary entry on the catalog: the typed words and the target are each searched (no dictionary,
no merchandising, anonymous) and the share of the first 10 results carrying the target's words is compared.
Used by scripts/qc_dictionary.py (a whole file) and the dashboard's Measure button (one entry)."""
from __future__ import annotations

from webbsearch.merch.dictionary import APPLIES

GENERIC = {"degree", "type", "connection", "configuration", "power", "fuel", "service", "rating", "system", "unit",
           "device", "assembly", "kit", "valve", "designation", "material", "operation", "end", "control"}
RISKY = ("brand_word", "state", "builtin", "part_number", "adds_words", "duplicate")


class Measurer:
    def __init__(self, engine):
        from webbsearch.retrieval.analyzer import normalize
        from webbsearch.retrieval.entitlements import ANONYMOUS
        self.e, self.norm, self.p = engine, normalize, ANONYMOUS
        self.cache: dict = {}

    def search(self, q: str) -> dict:
        if q not in self.cache:
            r = self.e.results.results(q, self.p, llm="off", merch=False, vocab=False, personalize=False)
            self.cache[q] = {"total": r["total"], "fallback": (r.get("notice") or {}).get("kind") == "fallback",
                             "top": [(i["sku"], set(self.norm(f"{i['title']} {i.get('brand', '')}"))) for i in r["items"][:10]]}
            if len(self.cache) > 5000:
                self.cache.pop(next(iter(self.cache)))
        return self.cache[q]

    @staticmethod
    def _on(res, words):
        if not res["top"] or not words:
            return 0.0
        return sum(len(words & t) / len(words) for _, t in res["top"]) / len(res["top"])

    def measure(self, e: dict) -> dict | None:
        if e["kind"] not in APPLIES or not e.get("to"):
            return None
        ctx = f" {e['any'][0]}" if e.get("any") else ""
        words = {w for w in self.norm(e["to"]) if not w.isdigit() and len(w) > 1 and w not in GENERIC} or set(self.norm(e["to"]))
        rt, rto = self.search(e["term"] + ctx), self.search(e["to"] + ctx)
        st, sto = {s for s, _ in rt["top"]}, {s for s, _ in rto["top"]}
        return {"typed_total": rt["total"], "typed_fallback": rt["fallback"], "typed_on": round(self._on(rt, words), 2),
                "target_total": rto["total"], "target_fallback": rto["fallback"], "target_on": round(self._on(rto, words), 2),
                "overlap": round(len(st & sto) / 10, 1)}


def verdict(e: dict, checks: list[dict], m: dict | None) -> tuple[str, str]:
    """(suggested status, why) from the lint checks and the measurement."""
    codes = {c["code"] for c in checks}
    errs = [c["text"] for c in checks if c["level"] == "error"]
    if e["kind"] == "block":
        return "live", "Don't-rewrite list: no one-word rule for this term (longer phrases still can be)."
    if e["kind"] == "glossary":
        return "review", "Glossary: a definition, no effect on search. Check the wording before publishing it."
    if "same" in codes:
        return "off", "Already handled: the same words once hyphens, spaces and plurals are ignored."
    if errs:
        return "review", "Needs work: " + " ".join(errs)
    if not m:
        return "review", "Not measured."
    help_ = m["target_on"] - m["typed_on"]
    if m["overlap"] >= 0.6:
        return "off", "Already handled: the typed words find the same products."
    if help_ >= 0.3 and m["target_on"] >= 0.67:
        risky = [c["text"] for c in checks if c["code"] in RISKY]
        return ("review", "Helps, but: " + " ".join(risky)) if risky else \
            ("live", "Helps: the target finds the right products the typed words miss.")
    if help_ >= 0.3:
        return "review", "Some gain, but the target's own results are mixed in this catalog."
    if (m["target_on"] < 0.3 and m["typed_on"] < 0.3) or not m["target_total"]:
        return "review", "Neither the typed words nor the target find these products in this catalog; check against the real one."
    if help_ <= 0:
        return "off", ("Backwards: the catalog uses the typed words; the target does worse." if "backwards" in codes
                       else "No gain here: the typed words find these products at least as well.")
    return "review", "Small or unclear effect in this catalog."


def describe(m: dict | None) -> str:
    if not m:
        return ""
    return (f"Typed: {m['typed_total']:,} results, {round(m['typed_on'] * 100)}% on target"
            f"{' (fallback)' if m['typed_fallback'] else ''}; as the target: {m['target_total']:,}, "
            f"{round(m['target_on'] * 100)}% on target; {round(m['overlap'] * 10)} of 10 the same.")
