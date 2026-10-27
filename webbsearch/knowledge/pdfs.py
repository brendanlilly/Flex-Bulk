"""Library documents and submittal packages as PDF (reportlab, no browser needed on the server).

Every page of a synthetic document carries the synthetic notice in a band at the top. A submittal package is one PDF:
a cover, an index of lines, then each item's spec sheet (the library's, or the catalog's data with the missing
manufacturer document flagged), with a bookmark per section."""
from __future__ import annotations

import datetime as dt
import io
import textwrap

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen.canvas import Canvas

from webbsearch.knowledge import diagram as diag

INK, MUTE, RED, BAND = HexColor("#26282c"), HexColor("#6f7278"), HexColor("#e0353b"), HexColor("#fff4e5")
PW, PH = letter
M = 54


class _Doc:
    def __init__(self, title: str):
        self.buf = io.BytesIO()
        self.c = Canvas(self.buf, pagesize=letter, pageCompression=1)
        self.c.setTitle(title)
        self.c.setAuthor("F.W. Webb · FlexSearch")
        self.y = PH - M
        self.page = 0

    def new_page(self, header: str, notice: str | None = None):
        if self.page:
            self.c.showPage()
        self.page += 1
        c = self.c
        if notice:
            c.setFillColor(BAND); c.rect(0, PH - 30, PW, 30, stroke=0, fill=1)
            c.setFillColor(HexColor("#8a5a00")); c.setFont("Helvetica-Bold", 7.5)
            for k, ln in enumerate(textwrap.wrap(notice, 150)[:2]):
                c.drawString(M, PH - 13 - 9 * k, ln)
        c.setFillColor(INK); c.setFont("Helvetica-Bold", 11)
        self.y = PH - (52 if notice else 40)
        for ln in textwrap.wrap(header, 85)[:2]:
            c.drawString(M, self.y, ln); self.y -= 14
        c.setStrokeColor(RED); c.setLineWidth(1.5); c.line(M, self.y + 4, PW - M, self.y + 4)
        self.y -= 14
        c.setFont("Helvetica", 7); c.setFillColor(MUTE)
        c.drawRightString(PW - M, 28, f"{self.page}")
        c.drawString(M, 28, "F.W. Webb · FlexSearch Knowledge Center")

    def heading(self, text: str):
        self._room(40)
        self.c.setFont("Helvetica-Bold", 10); self.c.setFillColor(INK)
        self.c.drawString(M, self.y, text); self.y -= 14

    def para(self, text: str, size: float = 9, width: int = 105, gap: float = 6):
        self.c.setFont("Helvetica", size); self.c.setFillColor(INK)
        for ln in textwrap.wrap(text, width):
            self._room(14)
            self.c.drawString(M, self.y, ln); self.y -= size + 3.5
        self.y -= gap

    def _room(self, need: float):
        if self.y - need < 50:
            hdr = getattr(self, "_hdr", ("", None))
            self.new_page(*hdr)

    def bookmark(self, key: str, title: str, level: int = 0):
        self.c.bookmarkPage(key)
        self.c.addOutlineEntry(title, key, level=level)

    def bytes(self) -> bytes:
        self.c.showPage(); self.c.save()
        return self.buf.getvalue()


def _draw_diagram(d: _Doc, cls: str, rows: list[dict], top: float):
    c, s = d.c, 0.82
    ox, oy = (PW - diag.W * s) / 2, top

    def P(x, y):
        return ox + x * s, oy - y * s
    c.setStrokeColor(HexColor("#6b6e74")); c.setLineWidth(1.4)
    for sh in diag.SHAPES[cls]:
        k = sh[0]
        if k == "rect":
            x, y = P(sh[1], sh[2] + sh[4]); c.roundRect(x, y, sh[3] * s, sh[4] * s, 3, stroke=1, fill=0)
        elif k == "ellipse":
            x1, y1 = P(sh[1] - sh[3], sh[2] + sh[4]); x2, y2 = P(sh[1] + sh[3], sh[2] - sh[4]); c.ellipse(x1, y1, x2, y2)
        elif k == "circle":
            x, y = P(sh[1], sh[2]); c.circle(x, y, sh[3] * s)
        else:
            c.setDash(4, 3) if k == "dash" else c.setDash()
            c.line(*P(sh[1], sh[2]), *P(sh[3], sh[4])); c.setDash()
    have = {r["n"] for r in rows}
    for n, ((ax, ay), (lx, ly)) in diag.CALLOUTS[cls].items():
        if n not in have:
            continue
        c.setStrokeColor(INK); c.setLineWidth(0.8); c.line(*P(ax, ay), *P(lx, ly))
        c.setFillColor(INK); x, y = P(lx, ly); c.circle(x, y, 9, stroke=0, fill=1)
        c.setFillColor(HexColor("#ffffff")); c.setFont("Helvetica-Bold", 8.5); c.drawCentredString(x, y - 3, str(n))
    d.y = oy - diag.H * s - 16


def render_doc(doc, lib) -> bytes:
    d = _Doc(doc.title)
    d._hdr = (f"{doc.title} · rev. {doc.rev}", lib.notice if doc.synthetic else None)
    d.new_page(*d._hdr)
    ln = lib.lines.get(doc.line_id) if doc.line_id else None
    for pg in doc.pages:
        if pg["n"] > 1:
            d.new_page(*d._hdr)
        d.bookmark(f"p{pg['n']}", f"{pg['n']}. {pg['section']}")
        d.heading(pg["section"])
        if doc.kind == "parts" and pg["section"] == "Exploded view" and ln:
            d.para(pg["text"])
            _draw_diagram(d, ln.cls, lib.parts_rows(ln.id), d.y)
        elif doc.kind == "parts" and pg["section"] == "Parts list" and ln:
            c = d.c
            c.setFont("Helvetica-Bold", 8); c.setFillColor(MUTE)
            for x, h in ((M, "#"), (M + 24, "Part"), (M + 200, "Manufacturer part"), (M + 320, "Webb item")):
                c.drawString(x, d.y, h)
            d.y -= 13
            c.setFont("Helvetica", 9); c.setFillColor(INK)
            for r in lib.parts_rows(ln.id):
                c.drawString(M, d.y, str(r["n"])); c.drawString(M + 24, d.y, r["part"][:34])
                c.drawString(M + 200, d.y, r["mfr_part"][:20]); c.drawString(M + 320, d.y, f"#{r['sku']}")
                d.y -= 13
        else:
            d.para(pg["text"])
    return d.bytes()


def _qty(x) -> int:
    """A quantity from a client: a whole number from 1 to 99,999; anything else is 1."""
    try:
        n = int(float(x))
    except (TypeError, ValueError, OverflowError):
        return 1
    return min(max(n, 1), 99_999)


def submittal(items: list[dict], project: dict, lib, catalog) -> tuple[bytes, dict]:
    proj = {k: str(project.get(k) or "").strip()[:120] for k in ("project", "address", "contractor", "engineer",
                                                                 "spec_section", "revision", "prepared_by")}
    title = f"Submittal: {proj['project'] or 'Project'}"
    d = _Doc(title)
    d._hdr = (title, lib.notice)
    d.new_page(title, lib.notice)
    d.bookmark("cover", "Cover")
    d.heading("Submittal package")
    for k, lab in (("project", "Project"), ("address", "Address"), ("contractor", "Contractor"), ("engineer", "Engineer"),
                   ("spec_section", "Spec section"), ("revision", "Revision"), ("prepared_by", "Prepared by")):
        if proj[k]:
            d.para(f"{lab}: {proj[k]}", gap=1)
    d.para(f"Date: {dt.date.today():%B %d, %Y}", gap=1)
    d.para(f"Supplied by F.W. Webb. {len(items)} line{'s' if len(items) != 1 else ''}.", gap=10)
    lines, missing, potable = [], [], []
    for k, it in enumerate(items, 1):
        sku = str(it.get("sku"))
        i = catalog.sku_to_idx.get(sku)
        if i is None:
            continue
        ln = lib.line_for_sku(sku)
        spec = lib.doc_of(ln.id, "spec") if ln else None
        if not spec:
            missing.append(sku)
        title_i = str(catalog.title[i])
        if any(w in title_i.lower() for w in ("ball valve", "water heater", "copper", "pex", "faucet")) and \
                "lead" not in title_i.lower() and "Water Heater" not in title_i:
            potable.append(sku)
        lines.append({"line": k, "qty": _qty(it.get("qty")), "sku": sku, "mfr_part": str(catalog.mfr_part[i]),
                      "brand": str(catalog.brand[i]), "title": title_i, "doc": spec, "attrs": catalog.attrs(i)})
    d.new_page(title, lib.notice)
    d.bookmark("index", "Index")
    d.heading("Index")
    c = d.c
    c.setFont("Helvetica-Bold", 8); c.setFillColor(MUTE)
    for x, h in ((M, "Line"), (M + 30, "Qty"), (M + 60, "Webb item"), (M + 125, "Mfr part"), (M + 210, "Description")):
        c.drawString(x, d.y, h)
    d.y -= 13
    c.setFont("Helvetica", 8.5); c.setFillColor(INK)
    for r in lines:
        d._room(14)
        c.drawString(M, d.y, str(r["line"])); c.drawString(M + 30, d.y, str(r["qty"]))
        c.drawString(M + 60, d.y, r["sku"]); c.drawString(M + 125, d.y, r["mfr_part"][:16])
        c.drawString(M + 210, d.y, r["title"][:62] + ("" if r["doc"] else "  (no mfr document)"))
        d.y -= 13
    if missing:
        d.y -= 8
        d.para(f"Missing manufacturer documents: {len(missing)} item{'s' if len(missing) != 1 else ''}. "
               "Webb has requested them from the vendors; the catalog data sheet is included meanwhile.", size=8.5)
    for r in lines:
        d.new_page(f"Item {r['line']}: {r['brand']} {r['mfr_part']}", lib.notice)
        d.bookmark(f"item{r['line']}", f"Item {r['line']}: {r['brand']} {r['mfr_part']} ({r['qty']})")
        d.para(f"{r['title']} · Webb item {r['sku']} · quantity {r['qty']}", gap=8)
        if r["doc"]:
            for pg in r["doc"].pages:
                d.heading(f"{r['doc'].title} · {pg['section']} (p. {pg['n']}, rev. {r['doc'].rev})")
                d.para(pg["text"], size=8.5)
        else:
            d.heading("Catalog data sheet (manufacturer document not in the library)")
            for k, v in r["attrs"].items():
                if v not in (None, ""):
                    d.para(f"{k.replace('_', ' ').capitalize()}: {v}", gap=1)
    report = {"lines": len(lines), "missing": missing, "pages": d.page,
              "checks": [{"sku": s, "check": "confirm lead-free certification for potable water"} for s in potable]}
    return d.bytes(), report
