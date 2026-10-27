"""Search by photo: identify a part from a picture and find it, its replacements, or what could substitute for it.

Three readings of the photo, strongest first:
  1. Numbers on the part or its label (Claude vision reads them when a key is set; otherwise Apple Vision on a Mac or
     Tesseract). Each number is looked up like a typed part number: catalog exact, real Webb item, cross-reference
     lake. A number that resolves is an exact identification, and the results page shows the item or the Webb
     replacements with their evidence.
  2. Claude vision's identification: product type (checked against the catalog's types), brand, specs, condition and
     the words to search.
  3. Image likeness (CLIP): the photo against photos of real Webb items (nearest neighbours) and against the catalog's
     product types (zero-shot), which covers the synthetic catalog too.

The answer is a search the results page can run (the part number, or the type and specs), plus the evidence, the
photos of similar-looking Webb items, other readings to try, and the better photo to take when it isn't sure.
"""
from __future__ import annotations

import re
import threading
import time
import uuid
from collections import Counter

import numpy as np

from webbsearch.retrieval.ocr import open_image, thumb
from webbsearch.vision import clip as clipmod
from webbsearch.vision.identify import identify
from webbsearch.vision.index import PhotoIndex, images_dir

MAX_PHOTOS = 3
LOOKS_SAME = 0.90         # cosine: a photo this close to a product photo is very likely the same product line
SIMILAR = 0.72            # shown as "looks like" at all
TYPE_SURE = 0.45          # zero-shot probability that names the type on its own
LOOKALIKE_SURE = 0.78     # a look-alike this close, agreeing with the others, names the type
NUM_TRY = 8


def local_ocr_engine() -> str | None:
    from webbsearch.retrieval import ocr
    have = ocr.engines(None)
    return next((e for e in ("vision", "tesseract") if e in have), None)


def _numbers_from_text(text: str) -> list[str]:
    from webbsearch.retrieval.resolve import part_tokens
    out = []
    for ln in text.splitlines():
        out += part_tokens(ln)
    return out


def type_words(noun: str, cls: str | None) -> str:
    noun = (noun or (cls or "").replace("_", " ")).strip()
    return f"copper press {noun}" if cls == "press_fitting" and "press" not in noun.lower() else noun


GUESS_FLOOR = 0.25      # below this, a picture-only guess isn't searched: the page asks for a better photo (v2.01)


def pick_query(found, ai, tp) -> tuple[str, str, str, str]:
    """(query, outcome, confidence, headline): what the photo search runs. A photo FlexIQ says isn't a part, or a
    picture-only guess below GUESS_FLOOR, searches nothing (ChatGPT QA W024: an 8% match searched "Upgrade Kit")."""
    if found:
        return found["query"], "exact", ("high" if not found.get("close") else "medium"), found["title"]
    if ai and ai.get("is_part") and (ai.get("search_query") or ai.get("part_type")):
        query = ai["search_query"] or " ".join(x for x in (ai.get("brand"), ai["part_type"]) if x)
        headline = " ".join(x for x in (ai.get("brand"), ai["part_type"] or (tp or {}).get("text", "")) if x)
        return query, ("type" if ai["confidence"] >= 0.6 else "guess"), ai["confidence_word"], headline
    if ai and not ai.get("is_part"):
        return "", "none", "low", ""
    if tp and tp["how"] in ("lookalike", "likeness"):
        return tp["query"], "type", "medium", tp["text"]
    if tp and (tp.get("p") or 0) >= GUESS_FLOOR:
        return tp["query"], "guess", "low", tp["text"]
    return "", "none", "low", ""


def decide_type(labels_z: list, near: list) -> dict | None:
    """The part type from the picture alone. Look-alike product photos vote first (weighted by how close they are); a
    strict majority with one close enough names the type. Otherwise a confident zero-shot product type does; failing
    that, the look-alikes' plurality (when the closest photo agrees) or the zero-shot type is offered as a guess."""
    votes: Counter = Counter()
    names: dict[str, Counter] = {}
    best: dict[str, float] = {}
    for it, s in near[:8]:
        c = it.get("cls")
        if s < SIMILAR or not c or c == "other":
            continue
        w = s - 0.6
        votes[c] += w
        names.setdefault(c, Counter())[type_words(it.get("noun") or "", c)] += w
        best[c] = max(best.get(c, 0), s)
    plural = None
    if votes:
        c, w = votes.most_common(1)[0]
        share = w / sum(votes.values())
        words = names[c].most_common(1)[0][0]
        lab = next((lb for lb, _ in labels_z if lb.get("cls") == c), None)
        plural = {"text": words, "query": words.lower() if words else (lab or {}).get("query", ""), "cls": c,
                  "p": round(share, 2), "how": "lookalike", "sim": round(best[c], 3)}
        if share > 0.5 and best[c] >= LOOKALIKE_SURE:
            return plural
    z = labels_z[0] if labels_z else None
    if z and z[1] >= TYPE_SURE:
        lb, pr = z
        return {"text": lb["text"], "query": lb["query"], "cls": lb.get("cls"), "p": round(pr, 2), "how": "likeness"}
    # not sure: the look-alikes' plurality when the closest photo votes (right 25% of the time on the product photos,
    # zero-shot 6%), else zero-shot, which also covers types with no Webb photo
    if plural and near and near[0][0].get("cls") == plural["cls"] and near[0][1] >= SIMILAR:
        return {**plural, "how": "guess"}
    if z:
        lb, pr = z
        return {"text": lb["text"], "query": lb["query"], "cls": lb.get("cls"), "p": round(pr, 2), "how": "guess"}
    return None


class PhotoSearch:
    def __init__(self, engine):
        self.e = engine
        self._ix: PhotoIndex | None = None
        self._lock = threading.Lock()
        self._imgs: set | None = None

    # ------------------------------------------------------------------ resources
    def index(self) -> PhotoIndex:
        with self._lock:
            if self._ix is None:
                self._ix = PhotoIndex()
            return self._ix

    def has_image(self, item: str) -> bool:
        if self._imgs is None:
            d = images_dir()
            self._imgs = {f.stem for f in d.glob("*.jpg")} if d.exists() else set()
        return str(item) in self._imgs

    def status(self) -> dict:
        ix = self.index()
        ocr_engine = local_ocr_engine()
        from webbsearch.retrieval.ocr import ENGINE_LABEL
        ex = {}
        for it in ix.items:
            if it.get("cls") in ("capacitor", "gas_valve", "igniter", "motor") and it["cls"] not in ex:
                ex[it["cls"]] = it["webb_item"]
        return {"likeness": clipmod.available() and ix.ok, "photos": len(ix.items), "types": len(ix.labels), "examples": ex,
                "claude": bool(getattr(self.e.claude, "enabled", False)),
                "ocr": ENGINE_LABEL.get(ocr_engine) if ocr_engine else None}

    # ------------------------------------------------------------------ lookups
    def _resolve(self, tok: str, p, branch) -> dict | None:
        """A number as if typed: exact catalog item, real Webb item, or a part the cross-reference knows."""
        try:
            r = self.e.results.results(tok, p, branch=branch, llm="off")
        except Exception:
            return None
        items = r.get("items") or []
        if items and items[0].get("exact"):
            it = items[0]
            return {"query": tok, "kind": "catalog", "title": it["title"], "brand": it.get("brand"), "sku": it["sku"]}
        xr = r.get("xref")
        if xr and xr.get("part") and xr["part"].get("how") in ("exact", "close"):
            pt = xr["part"]
            noun = pt.get("noun") or (pt.get("cls") or "").replace("_", " ")
            return {"query": tok, "kind": "xref", "title": " ".join(x for x in (pt.get("brand"), pt.get("part"), noun) if x),
                    "brand": pt.get("brand"), "cls": pt.get("cls"), "replacements": len(xr.get("replacements") or []),
                    "close": bool(pt.get("close"))}
        w = r.get("webb_items")
        if w and w.get("match") == "exact" and w.get("items"):
            it = w["items"][0]
            return {"query": tok, "kind": "webb", "title": it.get("title"), "brand": it.get("manufacturer"),
                    "webb_item": it.get("webb_item")}
        return None

    def _similar_card(self, it: dict, sim: float, branch) -> dict:
        card = {"webb_item": it["webb_item"], "title": it.get("title"), "brand": it.get("brand"), "mpn": it.get("mpn"),
                "noun": it.get("noun"), "cls": it.get("cls"), "sim": round(sim, 3),
                "img": f"/api/img/webb/{it['webb_item']}"}
        xr = getattr(self.e, "xref", None)
        card["url"] = getattr(xr, "url_tpl", "https://www.fwwebb.com/product/{item}").format(item=it["webb_item"])
        if xr is not None and getattr(xr, "stock", None) is not None:
            try:
                card["stock"] = xr.stock.summary(it["webb_item"], it.get("cls") or "", branch)
            except Exception:
                pass
        return card

    # ------------------------------------------------------------------ search
    def search(self, raws: list[bytes], p, branch: str | None = None, note: str = "", llm: str = "auto") -> dict:
        t0 = time.time()
        tm: dict[str, float] = {}
        ims = []
        for raw in raws[:MAX_PHOTOS]:
            im = open_image(raw)
            if min(im.size) < 64:
                raise ValueError("That photo is too small to read. Use one at least 64 pixels on each side.")
            ims.append(im)
        if not ims:
            raise ValueError("No photo received.")
        note = re.sub(r"\s+", " ", note or "").strip()[:300]
        out = {"id": uuid.uuid4().hex[:12], "photos": len(ims), "thumbs": [thumb(im, 480) for im in ims], "note": note,
               "evidence": [], "alternatives": [], "similar": [], "numbers": [], "found": None}

        # 3. image likeness (fast; also feeds the other two)
        ix = self.index()
        model = clipmod.get() if ix.ok else None
        labels_z, near = [], []
        if model is not None:
            t = time.time()
            v = model.images(ims).mean(axis=0)
            v /= max(np.linalg.norm(v), 1e-9)
            labels_z = ix.zero_shot(v, 10)
            near = ix.neighbors(v, 16)
            tm["likeness_ms"] = round((time.time() - t) * 1000)

        # 2. Claude vision
        ai = None
        if llm != "off":
            t = time.time()
            ai = identify(ims, self.e.claude, ix.labels, note)
            tm["claude_ms"] = round((time.time() - t) * 1000)

        # 1. numbers: Claude's reading, or local OCR when Claude isn't there; plus anything in the note
        cands: list[tuple[str, str]] = []
        if ai:
            for n in ai["numbers"]:
                if n["kind"] in ("model", "part", "upc", "other"):
                    if ai["brand"] and not n["partial"]:
                        cands.append((f"{ai['brand']} {n['text']}", "claude"))
                    cands.append((n["text"], "claude"))
        ocr_lines = []
        eng = None if ai else local_ocr_engine()
        if eng:
            from webbsearch.retrieval.ocr import read_images
            t = time.time()
            try:
                ocr_lines = [ln["text"] for ln in read_images(ims, claude=None, engine=eng)["lines"]]
            except Exception:
                ocr_lines = []
            tm["ocr_ms"] = round((time.time() - t) * 1000)
            brand_ocr = self._brand_in(ocr_lines) if ocr_lines else None
            for x in _numbers_from_text("\n".join(ocr_lines)):
                tok = x.split()[-1]
                if brand_ocr:
                    cands.append((f"{brand_ocr} {tok}", "label"))
                cands.append((tok, "label"))
        cands += [(x, "note") for x in _numbers_from_text(note)]
        seen, found = set(), None
        t = time.time()
        for tok, how in cands:
            k = tok.lower()
            if k in seen:
                continue
            seen.add(k)
            if len(out["numbers"]) >= NUM_TRY:
                break
            r = self._resolve(tok, p, branch)
            out["numbers"].append({"text": tok, "how": how, "found": bool(r)})
            if r and not found:
                found = {**r, "how": how}
        tm["lookup_ms"] = round((time.time() - t) * 1000)
        out["ocr"] = {"engine": eng, "lines": ocr_lines[:12]} if eng else None

        # the part type: Claude's (checked) when it has one; otherwise what the picture says (look-alikes, then zero-shot)
        near_ok = [(it, s_) for it, s_ in near if s_ >= SIMILAR]
        pic = decide_type(labels_z, near)
        z_top = labels_z[0] if labels_z else None
        if ai and ai["label"]:
            tp = {"text": ai["label"]["text"], "query": ai["label"]["query"], "cls": ai["label"].get("cls"),
                  "p": ai["confidence"], "how": "claude"}
        else:
            tp = pic

        # what to search
        query, outcome, conf, headline = pick_query(found, ai, tp)
        brand_seen = None
        if not found and not ai and ocr_lines:
            brand_seen = self._brand_in(ocr_lines)
            if brand_seen and query and brand_seen.lower() not in query.lower():
                query = f"{brand_seen} {query}"
        out.update(outcome=outcome, confidence=conf, headline=headline, query=query, found=found, type=tp)

        # evidence, in the order a counter person would weigh it
        ev = out["evidence"]
        if found:
            src = {"claude": "FlexIQ read", "label": "The label reads", "note": "Your note has"}[found["how"]]
            what = {"catalog": "an exact catalog item", "webb": "an F.W. Webb item",
                    "xref": f"a {found.get('brand') or ''} part in the cross-reference"
                            + (f" with {found['replacements']} Webb replacement{'s' if found['replacements'] != 1 else ''}"
                               if found.get("replacements") else "")}[found["kind"]]
            ev.append({"kind": "number", "text": f"{src} “{found['query']}”: {what.strip()}"})
        for n in out["numbers"]:
            if not n["found"] and not (found and n["text"] == found["query"]):
                ev.append({"kind": "number_miss", "text": f"“{n['text']}” isn't a part number Webb or the cross-reference knows"})
                break
        if ai:
            bits = [ai["part_type"]] + ([f"brand {ai['brand']}"] if ai["brand"] else []) + \
                   [f"{a['name']} {a['value']}" for a in ai["attributes"][:4]] + \
                   ([ai["condition"]] if ai["condition"] not in ("unknown", "new") else [])
            ev.append({"kind": "claude", "text": "FlexIQ says: " + ", ".join(b for b in bits if b) + f" ({ai['confidence_word']} confidence)"})
        if found and not (pic and pic["how"] in ("lookalike", "likeness")):
            pass                                     # a weak picture guess adds nothing to a number that resolved
        elif pic and pic["how"] == "lookalike":
            ev.append({"kind": "likeness", "text": f"Looks like a {pic['text'].lower()}: the closest Webb product photos are {pic['text'].lower()}s"})
        elif z_top:
            ev.append({"kind": "likeness", "text": f"Looks like a {z_top[0]['text'].lower()} ({round(z_top[1] * 100)}% of the picture match across {len(ix.labels)} product types)"})
        if near_ok:
            it, s = near_ok[0]
            ev.append({"kind": "photo", "text": f"Closest product photo: Webb #{it['webb_item']} {it.get('title') or ''} (similarity {s:.2f}"
                                                 + ("; very close" if s >= LOOKS_SAME else "") + ")"})
        if brand_seen:
            ev.append({"kind": "brand", "text": f"The label shows the brand {brand_seen}"})

        # other readings to try, and look-alike Webb items
        chosen = (tp or {}).get("text", "").lower()
        out["alternatives"] = [] if found else [{"text": lb["text"], "query": lb["query"], "p": round(pr, 2)}
                                                for lb, pr in labels_z if lb["text"].lower() != chosen and pr >= 0.03][:5]
        # Webb shows one photo for a whole series (every Rectorseal oval run capacitor): one card per photo, with the rest named
        cards, by_photo = [], {}
        for it, s_ in near_ok:
            key = it.get("md5") or it["webb_item"]
            if key in by_photo:
                by_photo[key]["same_photo"].append(it.get("mpn") or it["webb_item"])
                continue
            if len(cards) >= 8:
                continue
            c = self._similar_card(it, s_, branch)
            c["same_photo"] = []
            by_photo[key] = c
            cards.append(c)
        out["similar"] = cards
        out["identification"] = ai
        if outcome != "exact":
            out["better_photo"] = (ai or {}).get("better_photo") or \
                "A clear photo of the nameplate or label (model and part numbers) usually identifies the exact part."
        out["engines"] = {"likeness": model is not None, "claude": ai is not None,
                          "claude_available": bool(getattr(self.e.claude, "enabled", False)), "ocr": eng}
        tm["total_ms"] = round((time.time() - t0) * 1000)
        out["timings"] = tm
        return out

    def _brand_in(self, lines: list[str]) -> str | None:
        """A catalog brand spelled on the label (Honeywell, White-Rodgers, Taco...)."""
        if not hasattr(self, "_brands"):
            bs = {str(b) for b in set(self.e.catalog.brand.tolist())}
            xr = getattr(self.e, "xref", None)
            if xr is not None:
                try:
                    bs |= {r[0] for r in xr._db().execute("select name from brand where n >= 20")}
                except Exception:
                    pass
            self._brands = sorted((b for b in bs if len(b) >= 3), key=len, reverse=True)
        text = " " + " ".join(lines).lower() + " "
        for b in self._brands:
            if re.search(r"(?<![a-z])" + re.escape(b.lower()) + r"(?![a-z])", text):
                return b
        return None
