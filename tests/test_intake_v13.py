"""v1.3 order intake: forwarded emails (.eml with attachments), Word material lists, photos and scans of
handwritten notes (OCR engines mocked), trade shorthand, and the line-fit rules that pick the product a line
names. Every fixture here is synthetic; the real customer samples stay out of the repo."""
import base64
import io
from email.message import EmailMessage

import pytest

from webbsearch.retrieval import linefit, ocr
from webbsearch.retrieval.intake import free_text_lines, ingest, is_item_line, meta_from_text, ocr_fold
from webbsearch.retrieval.resolve import parse_line
from webbsearch.retrieval.shorthand import expand


# ------------------------------------------------------------------ shorthand
@pytest.mark.parametrize("raw,has", [
    ('2"NH 90', ['2" no-hub 1/4 bend']), ('3" Class 300GateValves', ["300 Gate Valves"]), ("2\" HD2000", ["heavy duty coupling"]),
    ("3M F/S IPS Tee", ["3000#", "forged steel", "threaded"]), ("Wibco LD 2003 L/O BFV", ["NIBCO", "lever", "butterfly valve"]),
    ("pvc glue", ["pvc cement"]), ('6 - 11/4 3M tee', ["1-1/4 3000#"]), ('3/4"X21/2" heel tee', ['X2-1/2"']), ("45/5 cap", ["45/5 cap"]), ("3/8\" treadrods", ["threaded rod"]), ("300#CS flanges", ["300# carbon steel"]),
])
def test_shorthand(raw, has):
    e = expand(raw)
    assert all(h in e for h in has), e


@pytest.mark.parametrize("read,searched", [
    ("14 3m F/s IPS Tee", "1¼ 3m F/s IPS Tee"), ('1y" 300Lb RIF IlS Flanges', '1¼" 300# RIF IPS Flanges'),
    ('14" 3m F/S IPS +5 ELL', '1¼" 3m F/S IPS 45 ELL'), ("34  PxFTG St ELLs", "¾  PxFTG St ELLs"), ('/4" 3m F/S IPS ELLS', '1¼" 3m F/S IPS ELLS'),
    ("14x6 Std WOL", "14x6 Std WOL"), ('12" 150LB R/F Blind FL', '12" 150# R/F Blind FL'), ("6\" Std L/R Well ELL", '6" Std L/R weld ELL'),
])
def test_handwriting_repairs_for_search(read, searched):
    from webbsearch.retrieval.shorthand import ocr_repair
    assert ocr_repair(read) == searched


def test_job_number_line_is_a_header():
    assert is_item_line("Acme Labs  #512-044", parse_line("Acme Labs  #512-044")) == (False, "customer or job number")
    assert not is_item_line("American Standard  #512-044", parse_line("American Standard  #512-044"), vocab=lambda w: True)[0]
    assert meta_from_text(["Acme Labs  #512-044", "5/T 20 Main St"])["customer"] == "Acme Labs"


def test_ocr_fold_maps_cyrillic_lookalikes():
    assert ocr_fold("3/4 СХС 90") == "3/4 CXC 90"


# ------------------------------------------------------------------ line spec
@pytest.mark.parametrize("text,sizes", [
    ('2" x 1½" x 1¼" copper tee', ["2", "1-1/2", "1-1/4"]), ('4"x2 no-hub tee', ["4", "2"]), ("1-1/2' black elbows", ["1-1/2"]),
    ("1\"X10' Copper K Pipe", ["1"]), ("3 no-hub wye", ["3"]), ("½\" street 45", ["1/2"]),
])
def test_sizes(text, sizes):
    assert linefit.sizes_in(text) == sizes


def test_spec_reads_the_product_the_line_names():
    s = linefit.spec(expand('4"x3" NH wye'))
    assert (s.head, s.material, s.size, s.reducing) == ("wye", "cast iron", "4", True)
    s = linefit.spec('3" Grooved Schedule 40 galvanized', "FT")          # 400 FT of it: pipe, though it never says so
    assert (s.head, s.conn, s.material) == ("pipe", "grooved", "galvanized")
    s = linefit.spec(expand("2-1/2\" sweat St elbows"))
    assert (s.head, s.street, s.angle, s.material) == ("elbow", True, "90", "copper")
    assert linefit.spec("3/4 copper 45").angle == "45"
    assert linefit.spec(expand('3" Class 300#CS RF Screw Flanges')).flange_kind == "threaded"


def _it(title, brand, size=None):
    return {"title": title, "brand": brand, "attrs": {"size": size} if size else {}}


def test_fit_gates():
    wye = linefit.spec(expand('3" NH wye'))
    assert not linefit.fit(wye, _it("AB&I 3 in. Cast Iron No-Hub Coupling", "AB&I", "3"))["noun"]
    assert linefit.fit(wye, _it("Charlotte Pipe 3 in. Cast Iron Wye", "Charlotte Pipe", "3"))["good"]
    assert not linefit.fit(wye, _it("Imperial 3 in. Wye, 26 GA", "Imperial", "3"))["noun"]          # duct, not DWV
    cap = linefit.spec(expand('3" NH cap'))
    assert not linefit.fit(cap, _it("Titan Pro 3 MFD Run Capacitor", "Titan Pro"))["noun"]
    assert not linefit.fit(cap, _it("Elkhart 3 in. Cast Bronze Cap, Sweat CxC", "Elkhart", "3"))["noun"]   # wrong material
    pipe = linefit.spec('3" SCH 80 black pipe')
    assert not linefit.fit(pipe, _it("Unistrut 3 in. Pipe Strap, Epoxy", "Unistrut", "3"))["noun"]
    tee = linefit.spec('2" x 1" copper tees')
    assert linefit.fit(tee, _it("Mueller 2 in. Wrot Copper Reducing Tee, Sweat CxC", "Mueller", "2"))["good"]
    assert not linefit.fit(tee, _it("Viega 1 in. Copper Tee, Press PxP", "Viega", "1"))["size"]
    bv = linefit.spec('2" sweat ball valve')
    assert linefit.fit(bv, _it("Legend Valve 2 in. Brass Full Port Ball Valve, Sweat", "Legend Valve", "2"))["good"]


def test_focus_query():
    assert linefit.focus_query(linefit.spec(expand('2"NH 90'))) == "2 in. cast iron 1/4 bend"
    assert linefit.focus_query(linefit.spec("1/2 copper elbows")) == "1/2 in. copper sweat CxC 90 elbow"


# ------------------------------------------------------------------ free text
def test_email_body_classification():
    body = """Hi Pat,
Please add
10 - 3/4" copper 90s
(4) 1/2 ball valves
2" NH wye
Use P.O. DP1234, delivery at 7am.
Thanks,
--
Jane Doe
Acme Plumbing
212-555-0100 Main
10 Main Street, Springfield, MA 01101"""
    lines = free_text_lines(body, {"id": 0, "label": "Email"})
    items = [(x["qty"], x["text"]) for x in lines if x["item"]]
    assert items == [(10, '3/4" copper 90s'), (4, "1/2 ball valves"), (1, '2" NH wye')]
    why = {x["raw"]: x["why"] for x in lines if not x["item"]}
    assert "contact" in why["212-555-0100 Main"] and why["Hi Pat,"].startswith("greeting")
    assert meta_from_text(body.split("\n")) == {"po": "DP1234", "fulfil": "delivery at 7am"}


def test_quoted_reply_marks_duplicates():
    t = "5 - 1\" black tees\nOn Tue someone wrote:\n5 - 1\" black tees"
    lines = free_text_lines(t, {"id": 0, "label": "x"})
    assert lines[0]["item"] and not lines[2]["item"] and lines[2]["why"].startswith("same as line 1")


@pytest.mark.parametrize("line", ["555-0100 Main", "Cell: 555-0199", "212-555-0100 Main", "Fax 866-794-7332"])
def test_phone_lines_are_contact_details(line):
    assert not is_item_line(line, parse_line(line))[0]


def test_item_number_with_count_is_an_item():
    assert is_item_line("1358103 25", parse_line("1358103 25"))[0]
    assert not is_item_line("02115", parse_line("02115"))[0]


def test_item_needs_a_product():
    assert not is_item_line("Please call me at 3", parse_line("Please call me at 3"))[0]
    assert is_item_line("6 1/2 cxc 90", parse_line("6 1/2 cxc 90"))[0]


# ------------------------------------------------------------------ email and Word
def _docx(paras, rows):
    """A Word file with paragraphs and one table, built with zipfile (no python-docx needed)."""
    import zipfile
    from xml.sax.saxutils import escape as _x
    W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    p_ = "".join(f"<w:p><w:r><w:t>{_x(p)}</w:t></w:r></w:p>" for p in paras)
    t_ = "<w:tbl>" + "".join("<w:tr>" + "".join(f"<w:tc><w:p><w:r><w:t>{_x(c)}</w:t></w:r></w:p></w:tc>" for c in r) + "</w:tr>"
                             for r in rows) + "</w:tbl>"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="xml" ContentType="application/xml"/><Default Extension="rels" '
                   'ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Override PartName="/word/document.xml" '
                   'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        z.writestr("_rels/.rels", '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="r1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
                   'Target="word/document.xml"/></Relationships>')
        z.writestr("word/document.xml", f'<?xml version="1.0"?><w:document {W}><w:body>{p_}{t_}</w:body></w:document>')
    return buf.getvalue()


def test_docx_table_and_header():
    raw = _docx(["Project: Riverside Apts - 4th floor", "Date: 04/21/2026"],
                [["Qty", "Size", "Description"], ["70'ft", '2-1/2"', "Copper K Pipe"], ["12", '2-1/2"', "CXC 90 Elbows"]])
    doc = ingest("takeoff.docx", raw)
    assert doc["kind"] == "docx" and doc["meta"]["project"].startswith("Riverside")
    got = [(x["qty"], x["uom"], x["text"]) for x in doc["sources"][0]["lines"]]
    assert got == [(70, "FT", '2-1/2" Copper K Pipe'), (12, "", '2-1/2" CXC 90 Elbows')]


def _eml(body, attachments=(), html=None, inline_png=None):
    m = EmailMessage()
    m["Subject"] = "FW: 25 Main St material"
    m["From"] = "Pat Rep <pat@fwwebb.com>"
    m["To"] = "counter@fwwebb.com"
    m.set_content(body)
    if html:
        m.add_alternative(html, subtype="html")
    if inline_png:
        m.get_payload()[-1].add_related(inline_png, "image", "png", cid="<logo1>", filename="logo.png")
    for name, data, mt in attachments:
        if mt == "message/rfc822":          # how mail clients attach a forwarded message
            from email import message_from_bytes, policy
            m.add_attachment(message_from_bytes(data, policy=policy.default), filename=name)
            continue
        main, sub = mt.split("/")
        m.add_attachment(data, maintype=main, subtype=sub, filename=name)
    return bytes(m)


def test_forwarded_email_with_attachment():
    body = ("FYI\n\nFrom: Jane Doe <jane@acmeplumbing.com>\nSent: Tuesday\nSubject: 25 Main St material\n\n"
            "Please place order, P.O. AC-7781 for delivery.\n6 - 1/2\" copper tee\n3 - 3/4\" ball valve\nThanks\nJane")
    csv = b"Qty,Description\n2,Honeywell V8168A1069 gas valve\n1,Taco 007-F5 circulator\n"
    raw = _eml(body, [("takeoff.csv", csv, "text/csv")])
    doc = ingest("order.eml", raw)
    assert doc["kind"] == "eml" and len(doc["sources"]) == 2
    assert doc["meta"]["customer"] == "Jane Doe (acmeplumbing.com)" and doc["meta"]["po"] == "AC-7781"
    assert doc["meta"]["job"] == "25 Main St material" and doc["email"]["attachments"] == ["takeoff.csv"]
    body_items = [x["text"] for x in doc["sources"][0]["lines"] if x["item"]]
    assert body_items == ['1/2" copper tee', '3/4" ball valve']
    att = doc["sources"][1]
    assert att["label"] == "takeoff.csv" and [x["qty"] for x in att["lines"]] == [2, 1]


def test_html_email_and_inline_logo_and_nested_message():
    html = "<p>Hi</p><table><tr><td>4</td><td>3\" no-hub coupling</td></tr><tr><td>2</td><td>3\" NH wye</td></tr></table><img src='cid:logo1'>"
    png = io.BytesIO()
    PIL = pytest.importorskip("PIL.Image")
    PIL.new("RGB", (8, 8)).save(png, "PNG")
    inner = _eml("2 - 1\" black tee\n")
    raw = _eml("see below", [("original.eml", inner, "message/rfc822")], html=html, inline_png=png.getvalue())
    doc = ingest("chain.eml", raw)
    assert any("inline image" in n for n in doc["notes"])
    labels = [s["label"] for s in doc["sources"]]
    assert any(lbl.startswith("original.eml") for lbl in labels)
    all_items = [x["text"] for s in doc["sources"] for x in s["lines"] if x.get("item")]
    assert '1" black tee' in all_items


# ------------------------------------------------------------------ handwriting (engines mocked)
def _note_png():
    Image = pytest.importorskip("PIL.Image")
    im = Image.new("RGB", (1000, 600), "white")
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue(), im


def _fake_vision(im):
    # Apple Vision reads the quantity and the item as separate observations on one baseline
    return [{"text": "10", "conf": 0.9, "bbox": [0.05, 0.10, 0.10, 0.16]},
            {"text": "3/4 СХС 90", "conf": 0.6, "bbox": [0.20, 0.11, 0.55, 0.17]},
            {"text": "Tag: Unit 4B", "conf": 0.8, "bbox": [0.05, 0.30, 0.40, 0.36]},
            {"text": "2 - 3 NH wye", "conf": 0.5, "bbox": [0.05, 0.50, 0.40, 0.56]}]


def test_handwritten_photo_with_vision(monkeypatch):
    raw, _ = _note_png()
    monkeypatch.setattr(ocr, "_has_vision", lambda: True)
    monkeypatch.setattr(ocr, "_has_tesseract", lambda: False)
    monkeypatch.setattr(ocr, "_vision", _fake_vision)
    doc = ingest("note.png", raw)
    s = doc["sources"][0]
    assert doc["kind"] == "image" and s["ocr"]["engine"] == "vision" and s["ocr"]["thumbs"]
    items = [x for x in s["lines"] if x["item"]]
    assert [(x["qty"], x["text"]) for x in items] == [(10, "3/4 CXC 90"), (2, "3 NH wye")]
    assert all(x["from_ocr"] and x["crop"].startswith("data:image/jpeg") for x in items)
    assert items[0]["conf"] == 0.6 and doc["meta"]["tag"] == "Unit 4B"


def test_scanned_pdf_pages_go_to_ocr(monkeypatch):
    _, im = _note_png()
    buf = io.BytesIO()
    im.save(buf, "PDF")
    monkeypatch.setattr(ocr, "_has_vision", lambda: True)
    monkeypatch.setattr(ocr, "_has_tesseract", lambda: False)
    monkeypatch.setattr(ocr, "_vision", _fake_vision)
    doc = ingest("scan.pdf", buf.getvalue())
    assert doc["kind"] == "scan" and doc["sources"][0]["ocr"]["pages"] == 1
    assert sum(x["item"] for x in doc["sources"][0]["lines"]) == 2


class _ToolUse:
    type = "tool_use"

    def __init__(self, lines):
        self.input = {"lines": lines}


class _Resp:
    def __init__(self, lines):
        self.content = [_ToolUse(lines)]


class FakeClaude:
    enabled = True

    def messages(self, **kw):
        assert kw["tool_choice"]["name"] == "note_lines"
        return _Resp([{"text": "6 - 1/2 copper 90", "confidence": "high", "top": 0.1, "bottom": 0.16},
                      {"text": "4 - 3/4 ball valve", "confidence": "low"}])


def test_handwritten_photo_with_claude(monkeypatch):
    raw, _ = _note_png()
    monkeypatch.setattr(ocr, "_has_vision", lambda: False)
    doc = ingest("note.jpg", raw, claude=FakeClaude())
    s = doc["sources"][0]
    assert s["ocr"]["engine"] == "claude"
    assert [(x["qty"], x["text"], x["conf"]) for x in s["lines"]] == [(6, "1/2 copper 90", 0.95), (4, "3/4 ball valve", 0.4)]
    assert s["lines"][0]["crop"].startswith("data:image/jpeg") and s["lines"][1]["crop"] is None      # where Claude placed it


def test_no_engine_says_so(monkeypatch):
    raw, _ = _note_png()
    monkeypatch.setattr(ocr, "_has_vision", lambda: False)
    monkeypatch.setattr(ocr, "_has_tesseract", lambda: False)
    doc = ingest("note.png", raw)
    assert doc["sources"][0]["lines"] == [] and "No handwriting or OCR engine" in doc["notes"][0]


def test_printed_scan_with_tesseract(monkeypatch):
    if not ocr._has_tesseract():
        pytest.skip("tesseract not installed")
    from PIL import Image, ImageDraw, ImageFont
    im = Image.new("RGB", (1400, 400), "white")
    d = ImageDraw.Draw(im)
    try:
        f = ImageFont.truetype("DejaVuSans.ttf", 44)
    except OSError:
        pytest.skip("no TrueType font for the printed fixture")
    d.text((40, 60), "12  3/4 copper tee", fill="black", font=f)
    d.text((40, 200), "4  1/2 ball valve", fill="black", font=f)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    monkeypatch.setattr(ocr, "_has_vision", lambda: False)
    doc = ingest("printed.png", buf.getvalue())
    got = [(x["qty"], x["text"]) for x in doc["sources"][0]["lines"] if x["item"]]
    assert got == [(12, "3/4 copper tee"), (4, "1/2 ball valve")]


def test_upload_limit_is_15mb():
    from webbsearch.retrieval.bom import read_upload
    read_upload("ok.csv", base64.b64encode(b"x" * 1000).decode())
    with pytest.raises(ValueError, match="15 MB"):
        read_upload("big.csv", base64.b64encode(b"x" * 15_100_000).decode())


# ------------------------------------------------------------------ matching on the catalog
@pytest.fixture(scope="module")
def bom(engine):
    from webbsearch.retrieval.bom import Bom
    return Bom(engine)


def _top(bom, text, qty=1, uom=""):
    from webbsearch.retrieval.entitlements import ANONYMOUS
    return bom.match({"text": text, "qty": qty, "uom": uom}, ANONYMOUS, None)


@pytest.mark.parametrize("text,want", [
    ('3" NH wye', ["Cast Iron", "Wye"]), ('2" black elbows', ["Black", "90 Elbow"]), ('1/2" copper elbows', ["90 Elbow", "Sweat CxC"]),
    ('2" x 1" Copper Tees', ["2 in.", "Reducing Tee"]), ('2" HD2000', ["Heavy Duty Coupling"]), ('3" Class 300GateValves', ["Gate Valve"]),
    ('2"NH 90', ["Cast Iron 1/4 Bend"]), ("pvc glue", ["PVC Cement"]), ('1/2" copper split ring', ["Split Ring Hanger", "Copper"]),
])
def test_line_gets_the_named_product(bom, text, want):
    r = _top(bom, text)
    t = r["options"][0]["title"]
    assert all(w in t for w in want), t


def test_missing_product_is_said_not_guessed(bom):
    r = _top(bom, '3" SCH 80 Black Pipe', 40, "FT")
    assert r["status"] == "none" and not r["options"] and r["closest"]
    assert r["note"] == 'No 3" black pipe in this catalog.'


def test_ocr_lines_always_need_a_person(bom):
    r = bom.match({"text": "3/4 cxc 90", "qty": 10, "from_ocr": True}, __import__("webbsearch.retrieval.entitlements",
                                                                                   fromlist=["ANONYMOUS"]).ANONYMOUS, None)
    assert r["options"] and not r["confirmed"] and r["note"].startswith("Read from the note")
