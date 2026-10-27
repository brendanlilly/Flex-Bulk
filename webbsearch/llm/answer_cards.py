"""FlexIQ answers and their product cards (v2.01).

The cards under an answer used to be every product FlexIQ's searches returned, so the prose could recommend the
lead-free valve while the Add buttons offered the original (ChatGPT QA review, A008). Now FlexIQ ends its answer with
a CARDS line naming the items to show; the cards are those items, in that order. Without the line, the items the
answer cites (#1234567) are the cards. With neither, the search cards stay but are labelled as related results.

Also here: the message hygiene the Messages API needs. An empty text block or an empty turn is a 400
("text content blocks must be non-empty"), which is how two Ask turns failed on the demo.
"""
from __future__ import annotations

import re

CARDS_RULE = ("End your answer with a last line `CARDS: #1234567, #2345678` naming only the Webb items the user should "
              "act on, best first (never an item you ruled out), or `CARDS: none`.")
RELATED = "Related search results, not checked against the answer"
_CARDS = re.compile(r"\n?[ \t]*CARDS:[ \t]*([^\n]*)\s*$", re.I)
_CITE = re.compile(r"#(\d{6,8})\b")


def clean_history(history) -> list[dict]:
    """Plain-text turns with content, alternating user/assistant, starting with the user."""
    out: list[dict] = []
    for h in history or []:
        role, content = h.get("role"), h.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str) or not content.strip():
            continue
        if out and out[-1]["role"] == role:
            out[-1] = {"role": role, "content": out[-1]["content"] + "\n\n" + content.strip()}
        else:
            out.append({"role": role, "content": content.strip()})
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out


def with_question(history, question: str) -> list[dict]:
    """The history plus the new question, still alternating (a trailing user turn absorbs the question)."""
    msgs = clean_history(history)
    if msgs and msgs[-1]["role"] == "user":
        msgs[-1] = {"role": "user", "content": msgs[-1]["content"] + "\n\n" + question}
    else:
        msgs.append({"role": "user", "content": question})
    return msgs


def blocks(content) -> list[dict]:
    """An assistant response as message blocks, without empty text."""
    out = []
    for b in content:
        if b.type == "tool_use":
            out.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
        elif b.type == "text":
            if (getattr(b, "text", "") or "").strip():
                out.append({"type": "text", "text": b.text})
        elif hasattr(b, "model_dump"):
            out.append(b.model_dump(exclude_none=True))
    return out


def settle(text: str, cards: list[dict], product_card) -> tuple[str, list[dict], str | None]:
    """(answer without the CARDS line, cards, label or None). product_card(sku) -> a card dict or None."""
    m = _CARDS.search(text or "")
    named = None
    if m:
        text = text[:m.start()].rstrip()
        named = [] if m.group(1).strip().lower().startswith("none") else _CITE.findall(m.group(1)) or \
            re.findall(r"\b(\d{6,8})\b", m.group(1))
    if named is None:
        named = list(dict.fromkeys(_CITE.findall(text or "")))
    others = [c for c in cards if c.get("type") != "product"]
    prods = [c for c in cards if c.get("type") == "product"]
    if named:
        by_sku = {str(c.get("sku")): c for c in prods}
        picked = []
        for sku in dict.fromkeys(named):
            c = by_sku.get(sku) or product_card(sku)
            if c:
                picked.append(c)
        if picked:
            return text, picked + others, None
    return text, prods + others, (RELATED if prods else None)
