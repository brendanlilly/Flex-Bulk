"""v2.0: findings from the bulk-order demo files (estimating exports, counter emails)."""
from webbsearch.retrieval.resolve import parse_line, parse_table


def test_qty_before_an_inch_size_is_the_quantity():
    # "25 2" brz 90 ell": 25 of the 2-inch elbow, not one of a "25 2 inch" something
    ln = parse_line('25 2" brz 90 ell, CxMIP')
    assert (ln["qty"], ln["qty_how"]) == (25, "leading")
    assert ln["text"].startswith('2" brz')
    ln = parse_line('20 3" Stainless Steel std port ball vlv PEX Expansion Lever Handle')
    assert ln["qty"] == 20 and ln["text"].startswith('3"')
    assert parse_line('10 1-1/2" copper tee')["qty"] == 10
    # a hyphenated size needs no inch mark to be a size
    ln = parse_line("5 2-1/2 LF brs std port ball vlv Push-to-Connect Tee Handle")
    assert ln["qty"] == 5 and ln["text"].startswith("2-1/2 LF")
    from webbsearch.retrieval.intake import is_item_line
    assert is_item_line("5 2-1/2 LF brs std port ball vlv Push-to-Connect Tee Handle", ln)[0]


def test_mixed_number_size_is_not_split_into_a_quantity():
    # "1 1/2" copper tee" is a 1-1/2 inch tee: the count stays unread
    ln = parse_line('1 1/2" copper tee')
    assert ln["qty_how"] == "default" and ln["text"].startswith("1 1/2")


CSV = '''Item ID,Qty,Unit,Size,Material,Description,Spec,System
EST-1001,25,EA,3,Bronze,"Cast Bronze Female Adapter, Sweat CxMIP",22 11 16,Domestic water
EST-1002,100,EA,1,,"Copper Cap, Press FTGxP",22 11 16,Domestic water
EST-1003,5,EA,3/4,PEX,"PEX Expansion (F1960) Stub Out Elbow, Poly Alloy",22 11 16,Domestic water
EST-1004,12,EA,2,Copper,"Wrot Copper 90 Elbow, Sweat CxC",22 11 16,Domestic water
'''


def test_description_column_wins_over_material():
    t = parse_table(CSV)
    assert t["header"] is not None
    assert t["header"][t["roles"]["desc"]] == "Description"
    assert len(t["lines"]) == 4                       # a row with no material isn't dropped
    a, b, c, d = t["lines"]
    assert a["qty"] == 25 and "Female Adapter" in a["text"] and a["text"].startswith("3 ")
    assert b["qty"] == 100 and "Copper Cap" in b["text"]
    assert "Bronze" in a["text"]                      # material already in the description: not repeated
    assert a["text"].count("Bronze") == 1
    assert "PEX" in c["text"] and c["text"].count("PEX") == 1
    assert "Copper" in d["text"]


def test_material_column_adds_to_a_description_without_it():
    t = parse_table("Qty,Size,Material,Description\n10,3/4,Copper,90 Elbow Sweat\n")
    assert t["lines"][0]["text"] == "3/4 Copper 90 Elbow Sweat"


def test_material_alone_still_reads_as_the_description():
    # BOMs with a "Material" column and no Description column keep working
    t = parse_table("Qty,Material\n4,3/4 copper 90\n")
    assert t["lines"][0]["text"] == "3/4 copper 90" and t["lines"][0]["qty"] == 4


def test_pipe_fraction_cap_is_a_fitting_not_a_capacitor():
    from webbsearch.retrieval.linefit import spec
    for t in ("1/4 Copper Cap, Press PxMIP", "1/2 sharkbite caps", "3/4 cu cap", "1-1/4 pvc cap", "2 1/2 caps black"):
        assert spec(t).head == "cap", t
    for t in ("45/5 cap", "35+5 cap 440v", "40/5 MFD dual run cap", "run capacitor 7.5"):
        assert spec(t).head == "capacitor", t


def test_spec_notes_are_not_searched():
    # "or approved equal", "LF required", "deliver to level 2": instructions, not product words
    t = parse_table("Qty,Size,Description,Notes\n10,2-1/2,Double Check Valve Assembly Z-Pattern,LF required\n"
                    "4,1-1/4,Double Check Valve Assembly,or approved equal\n2,3/4,Ball Valves,deliver to level 2\n")
    assert [ln["text"] for ln in t["lines"]] == ["2-1/2 Double Check Valve Assembly Z-Pattern",
                                                  "1-1/4 Double Check Valve Assembly", "3/4 Ball Valves"]


def test_a_model_in_the_notes_is_still_read():
    t = parse_table('Qty,Description,Notes\n6,3/4" Ball Valves,Milwaukee BA-100S\n')
    assert "BA-100S" in t["lines"][0]["text"]


def test_price_sheet_line_is_an_item():
    # another supplier's price sheet: brand, number, description, unit and price; no quantity
    from webbsearch.retrieval.intake import is_item_line
    for t in ["Boss 31500 Silicone sealant clear 10 oz EA 3.26", "Moen F9900 RO system 4-stage 3.2 gal 100 psi EA 170.63",
              'Shurtape 232622 Foil tape 2.5" x 60 yd UL EA 11.06']:
        assert is_item_line(t, parse_line(t))[0], t
    for t in ["Freight allowed on orders over $750. Lead-free brass where applicable.", "Page 1 of 1",
              "Subtotal EA 1,204.50", "Net price sheet for Pine Hill Plumbing & Heating Effective 10/01/2026"]:
        assert not is_item_line(t, parse_line(t))[0], t


def test_pipe_strap_is_not_pipe():
    from webbsearch.retrieval.linefit import spec
    for t in ("4 Pipe Strap, Electro-Galvanized", "2 pipe hanger", "1/2 pipe clamp"):
        assert spec(t).head != "pipe", t
    assert spec("4 galvanized pipe").head == "pipe"
    assert spec("3/4 type L copper tube").head == "pipe"


def test_spec_section_number_is_not_part_of_the_product():
    # an RFQ line: line number, MasterFormat section, description, quantity, unit
    ln = parse_line("25 22 05 23 1/4 in. Stainless Steel Full Port Ball Valve, Sweat 10 EA")
    assert ln["qty"] == 10 and ln["text"].startswith("1/4 in. Stainless")
    ln = parse_line("22 11 16 3/4 in. Wrot Copper Tee, Sweat 100 EA")
    assert ln["qty"] == 100 and ln["text"].startswith("3/4 in.")
    # ordinary counts and sizes are untouched
    assert parse_line("22 3/4 copper 90s")["qty"] == 22
    assert parse_line("12 22 ga duct strap")["text"] == "12 22 ga duct strap"


def test_unit_and_price_alone_is_not_an_item():
    # the tail of a price-sheet line whose description wrapped in the PDF
    from webbsearch.retrieval.intake import is_item_line
    for t in ("EA 342.58", "ea $1,204.50", "FT 2.10"):
        assert not is_item_line(t, parse_line(t), vocab=lambda w: True)[0], t


def _heading_vision(im):
    return [{"text": "42 Maple St - boiler swap", "conf": 0.8, "bbox": [0.05, 0.05, 0.60, 0.10]},
            {"text": "1 - Taco 110-251", "conf": 0.8, "bbox": [0.05, 0.20, 0.40, 0.25]},
            {"text": "10 - copper elbow 90 3/4 wrot", "conf": 0.8, "bbox": [0.05, 0.30, 0.50, 0.35]}]


def test_a_handwritten_notes_heading_is_not_an_order_line(monkeypatch):
    import io
    from PIL import Image
    from webbsearch.retrieval import ocr
    from webbsearch.retrieval.intake import ingest
    buf = io.BytesIO()
    Image.new("RGB", (1000, 600), "white").save(buf, "PNG")
    monkeypatch.setattr(ocr, "_has_vision", lambda: True)
    monkeypatch.setattr(ocr, "_has_tesseract", lambda: False)
    monkeypatch.setattr(ocr, "_vision", _heading_vision)
    lines = ingest("note.png", buf.getvalue(), vocab=lambda w: True)["sources"][0]["lines"]
    assert not lines[0]["item"] and lines[0]["why"] == "heading"
    assert [x["text"] for x in lines if x["item"]] == ["Taco 110-251", "copper elbow 90 3/4 wrot"]


def test_typed_paste_first_line_is_still_an_item():
    from webbsearch.retrieval.intake import free_text_lines
    # the heading rule is for photos and scans; a typed paste reads as it always has
    lines = free_text_lines("42 Maple St - boiler swap\n2 - 3/4 cxc 90", {"id": 0, "label": "Pasted"}, vocab=lambda w: True)
    assert lines[0]["item"]
