"""Ask Webb on availability (v2.3): "can I pick up a 3/4 ball valve today?", "what's the lead time on a Honeywell
RTH9585WF?", "I need 2 in. copper cut to length". The answer states the tier (on the shelf, in the region, at Webb,
from the manufacturer, the Extended Catalog, a special order), says lead times are the manufacturer's estimate, and
offers the stocked item first."""
from __future__ import annotations

import re

from webbsearch.extended import special

AVAIL_RE = re.compile(
    r"\b(in stock|on hand|on the shelf|pick ?(it )?up|lead ?times?|how (soon|long|fast)|when (can|could|will) (i|we) "
    r"(get|have|pick)|ship(s|ping)? (time|in|from)|arriv\w*|deliver\w* (time|by)|available (today|now|at|near)|"
    r"(have|got) any|do you (have|carry|stock)|can i get|special order|custom order|cut to (length|size)|"
    r"fabricat\w*|made to order)\b", re.I)
ACCOUNT_RE = re.compile(r"\b(my|our) (order|po|invoice|delivery|shipment)\b|\border (#|no\.?|number)|\bpo\s?#?\d", re.I)
LEAD_RE = re.compile(r"\blead ?times?|how (soon|long)|when (can|could|will)|arriv|deliver", re.I)


FILLER = re.compile(r"\b(can|could|would|will|do|does|is|are|there|what'?s|what|how|where|when|the|a|an|on|for|any|"
                    r"you|i|we|me|us|today|now|tomorrow|this week|near me|nearby|at|my|our|branch|need|needs|want|get|"
                    r"have|got|it|carry|stock|please|some|of|to|job ?site|site|delivered)\b", re.I)


def product_phrase(q: str) -> str:
    """The product in an availability question: "what's the lead time on a Honeywell RTH9585WF thermostat?" ->
    "Honeywell RTH9585WF thermostat"."""
    t = AVAIL_RE.sub(" ", q or "")
    t = FILLER.sub(" ", t)
    t = re.sub(r"[?!,]", " ", t)
    return re.sub(r"\s+", " ", t).strip(" .") or (q or "")


def handles(q: str) -> bool:
    return bool(AVAIL_RE.search(q or "")) and not ACCOUNT_RE.search(q or "")


def _line(it: dict) -> str:
    t = it.get("tier") or it.get("availability") or {}
    return f"{it['title']} (#{it['sku']}): {t.get('text', '')}{(' · ' + t['detail']) if t.get('detail') else ''}"


def top_all(items, off, exact):
    return [] if off and not exact else items


def answer(e, q: str, p, branch=None) -> dict | None:
    if not handles(q):
        return None
    bi = e.availability.branch_index(branch or (p.home_branch if p.is_authenticated else None))
    bid, bname = str(e.availability.ids[bi]), str(e.availability.names[bi]).split(",")[0]
    phrase = product_phrase(q)
    r = e.results.results(phrase, p, branch=bid, llm="off")
    items = r.get("items") or []
    fam = (r.get("dominant") or {}).get("family") or (items[0]["family"] if items else None)
    lines, ec_cards, follow = [], [], []
    # a special order: the quote row, not a grid item
    quote = special.row(q, fam, bname) if special.family_for(q, fam) else None
    if quote:
        lines.append(f"That's a special order ({quote['label'].lower()}). {quote['detail']} "
                     f"It asks: {', '.join(a.lower() for a in quote['asks'][:4])}.")
        follow.append({"label": "Request a quote", "q": q, "search": True})
    exact = [it for it in items if it.get("exact")]
    # the page found related products, not the one named ("Honeywell RTH9585WF": no such model coded): none of those
    off = (r.get("notice") or {}).get("kind") in ("no_exact_model", "brand_miss", "not_carried", "real_only",
                                                   "phrase_miss")
    top = [] if off and not exact else (exact or items)[:3]
    here = [it for it in top_all(items, off, exact) if (it.get("tier") or {}).get("tier") == "1a" and (it.get("avail") or {}).get("at_branch", 0) > 0]
    wi = r.get("webb_items") or {}
    real = wi.get("items")[0] if wi.get("match") == "exact" and wi.get("items") else None
    if real and not quote:          # a real F.W. Webb item (job kits, fwwebb.com): that's the answer
        lines.append(f"F.W. Webb carries it: {real['manufacturer']} {real['mpn']} {real['title'].lower()}, Webb item "
                     f"#{real['webb_item']}" + (", marked in stock in Webb's item data." if real.get("in_stock")
                                                else ". Your branch confirms stock."))
        follow.append({"label": "Open it in search", "q": q, "search": True})
    if top and not quote:
        if LEAD_RE.search(q) or not here:
            lines.append(f"For pickup at {bname}:")
        else:
            lines.append(f"Yes: {len(here)} matching items are on the shelf at {bname} today. The top ones:"
                         if len(here) > 1 else f"Yes: on the shelf at {bname} today.")
        for it in (here[:3] if here and not LEAD_RE.search(q) else top):
            lines.append(_line(it))
    xt = getattr(e, "extended", None)
    ec = None
    if xt is not None and p.is_authenticated and not quote:
        try:
            ec = xt.section(phrase, p, bid, 1, limit=3)
        except Exception:  # noqa: BLE001
            ec = None
    if ec and ec.get("status") == "ok" and ec.get("items") and (not top or ec.get("exact") or not here):
        e0 = ec["items"][0]
        intro = ("Not stocked or coded at Webb, but the Extended Catalog has it" if not (top or real) or ec.get("exact")
                 else "Also in the Extended Catalog, from the manufacturer")
        lines.append(f"{intro}: {e0['title']} ({e0['sku']}): {e0['availability']['text']} · "
                     f"{e0['availability']['detail']}. {e0['returns']}.")
        ec_cards.append({"type": "product", **{k: e0[k] for k in ("sku", "title", "brand", "family", "attrs", "price", "uom")},
                         "tier": e0["availability"], "ec": True})
    if not quote and (any(((it.get("tier") or {}).get("tier") or "1")[0] in "23" for it in top)
                      or (ec and ec.get("items"))):
        lines.append("Lead times are the manufacturer's estimates; your branch confirms the date with the order.")
    if not lines:
        return None
    cards = [{"type": "product", **e.results._mini(int(e.catalog.sku_to_idx[it["sku"]]), p), "tier": it.get("tier")}
             for it in top if it["sku"] in e.catalog.sku_to_idx] + ec_cards
    if top:
        follow.append({"label": "See them all", "q": phrase, "search": True})
    return {"answer": "\n".join(lines), "cards": cards[:5], "kind": "availability", "engine": "rules",
            "followups": follow, "ms": 0}
