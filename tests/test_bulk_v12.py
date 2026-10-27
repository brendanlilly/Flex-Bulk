"""v1.2 Bulk order: BOM parsing (CSV / Excel / PDF), transfer plans, recommendation rules, and the
personalized, stock-aware matcher. Parser and plan tests run anywhere; `engine` tests need the index."""
import base64
import io
import math

import numpy as np
import pytest

from webbsearch.index.fixture_rules import implausible
from webbsearch.retrieval.availability import Availability, fulfil
from webbsearch.retrieval.bom import read_upload
from webbsearch.retrieval.entitlements import ANONYMOUS
from webbsearch.retrieval.resolve import _hdr, detect_table, parse_table

BOM = """Needham Library boiler replacement · BOM rev 2
Line,Qty,U/M,Manufacturer,Mfr Part Number,Description
1,2,EA,Honeywell,V8168A1069,Gas valve 3/4 in
2,40,EA,,,3/4 cxc 90 elbow
3,200,FT,,,1/2 pex b tubing
,,,,,
4,1,EA,Taco,007-F5,Circulator"""


# ------------------------------------------------------------------ parsing
@pytest.mark.parametrize("cell,role", [("Qty", "qty"), ("Quantity Required", "qty"), ("Mfr Part Number", "part"),
                                       ("Model No.", "part"), ("Catalog #", "part"), ("Item Description", "desc"),
                                       ("Manufacturer Name", "brand"), ("U/M", "uom"), ("Line", "line"), ("Item", "line"),
                                       ("Notes", "notes"), ("Remarks", "notes"), ("Size", "size")])
def test_header_roles(cell, role):
    assert _hdr(cell) == role


def test_header_found_below_a_title_line():
    t = parse_table(BOM)
    assert t["header_row"] == 1 and t["roles"] == {"line": 0, "qty": 1, "uom": 2, "brand": 3, "part": 4, "desc": 5}
    assert [(x["text"], x["qty"], x["uom"]) for x in t["lines"]] == [
        ("Honeywell V8168A1069", 2, "EA"), ("3/4 cxc 90 elbow", 40, "EA"), ("1/2 pex b tubing", 200, "FT"), ("Taco 007-F5", 1, "EA")]
    assert t["lines"][0]["desc"] == "Gas valve 3/4 in"


def test_mapping_override():
    t = parse_table(BOM, roles={"qty": 1, "desc": 5}, header_row=1)
    assert [x["text"] for x in t["lines"]][:2] == ["Gas valve 3/4 in", "3/4 cxc 90 elbow"]


def test_no_header_reads_like_a_paste():
    t = parse_table("10 EA HONEYWELL V8168A1069 GAS VALVE\n3/4 cxc 90 elbow  25")
    assert t["header"] is None and [(x["text"], x["qty"]) for x in t["lines"]] == [
        ("HONEYWELL V8168A1069 GAS VALVE", 10), ("3/4 cxc 90 elbow", 25)]


def test_a_data_row_is_not_a_header():
    assert detect_table(["V8168A1069,4,Honeywell gas valve", "271W,2,igniter"]) is None


def test_truncation_flag():
    from webbsearch.retrieval.resolve import MAX_LINES
    t = parse_table("\n".join(f"2360175, {i + 1}" for i in range(MAX_LINES + 20)))
    assert t["total"] == MAX_LINES == 2000 and t["truncated"]


def test_excel_upload():
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "BOM"
    ws.append(["Riverside Apts · mechanical takeoff"])
    ws.append([])
    ws.append(["Item", "Model No.", "Description", "Quantity Required"])
    ws.append([1, "V8168A1069", "Gas valve", 3])
    ws.append([2, None, "3/4 ball valve sweat", 12.0])
    wb.create_sheet("Notes").append(["ignore me"])
    buf = io.BytesIO()
    wb.save(buf)
    src = read_upload("takeoff.xlsx", base64.b64encode(buf.getvalue()).decode())
    assert src["kind"] == "xlsx" and src["sheet"] == "BOM" and src["sheets"] == ["BOM", "Notes"]
    t = parse_table(src["text"])
    assert [(x["text"], x["qty"]) for x in t["lines"]] == [("V8168A1069", 3), ("3/4 ball valve sweat", 12)]
    assert read_upload("takeoff.xlsx", base64.b64encode(buf.getvalue()).decode(), sheet="Notes")["text"] == "ignore me"


def test_pdf_upload():
    pytest.importorskip("pypdf")
    canvas = pytest.importorskip("reportlab.pdfgen.canvas")
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    for k, line in enumerate(["PO 44871 Needham boiler room", "001  10  EA  HONEYWELL V8168A1069 GAS VALVE",
                              "002  6   EA  WR 50M56U-843 UNIVERSAL IGNITION KIT"]):
        c.drawString(72, 720 - 16 * k, line)
    c.save()
    src = read_upload("po.pdf", base64.b64encode(buf.getvalue()).decode())
    t = parse_table(src["text"])
    assert src["kind"] == "pdf" and [x["qty"] for x in t["lines"]][-2:] == [10, 6]


def test_upload_size_limit():
    with pytest.raises(ValueError):
        read_upload("big.csv", base64.b64encode(b"x" * 15_100_000).decode())


# ------------------------------------------------------------------ transfer plans
class Av:          # four branches on a line: 0 (home), 1 at 10 mi, 2 at 40 mi, 3 at 200 mi
    ids = np.array(["b0", "b1", "b2", "b3"])
    names = np.array(["Brockton, MA", "Boston, MA", "Worcester, MA", "Albany, NY"])
    dist = np.array([[0, 10, 40, 200], [10, 0, 30, 190], [40, 30, 0, 160], [200, 190, 160, 0]], float)
    counter = np.ones(4, bool)
    counters = np.arange(4)
    counters_by_distance = Availability.counters_by_distance

    def branch_index(self, b):
        return {"b0": 0, "b1": 1, "b2": 2, "b3": 3}.get(b, 0)


@pytest.mark.parametrize("need,qty,status,plan", [
    (5, [8, 0, 0, 0], "pickup", []),
    (10, [4, 3, 9, 0], "partial", [("Boston", 3), ("Worcester", 3)]),
    (6, [0, 2, 9, 0], "transfer", [("Boston", 2), ("Worcester", 4)]),
    (6, [0, 0, 0, 9], "transfer", [("Albany", 6)]),
    (20, [2, 3, 0, 0], "dc", [("Boston", 3)]),
])
def test_fulfil(need, qty, status, plan):
    f = fulfil(need, np.array(qty), Av(), "b0")
    assert f["status"] == status and [(p["branch"], p["qty"]) for p in f["plan"]] == plan
    if status == "transfer" and plan[0][0] == "Albany":
        assert f["eta"] == "Transfer in 3-4 days"
    if status == "partial":
        assert f["text"].startswith("4 on hand at Brockton; 6 by transfer") and f["eta"] == "Transfer tomorrow"


def test_fulfil_special_order():
    assert fulfil(3, np.zeros(4, int), Av(), "b0", "special")["eta"] == "Special order 5-10 days"


@pytest.mark.parametrize("title,rule", [("Titan Pro 45/5 MFD 370V Oval Start Capacitor", "dual_cap_start"),
                                        ("Genteq 45/5 MFD 440V Round Run Capacitor", None),
                                        ("RectorSeal Wax Ring, Gallon", "wax_ring_units")])
def test_implausible(title, rule):
    assert implausible(title) == rule


# ------------------------------------------------------------------ engine
def _owner(engine, name="Kennebec Heating & Cooling"):
    a = engine.accounts
    acct = a.accounts[a.accounts["name"] == name]
    if not len(acct):
        pytest.skip(f"no {name} account in this build")
    u = a.users[(a.users["account_id"] == acct.index[0]) & (a.users["role"] == "owner")].iloc[0]
    return a.principal(acct.index[0], u["user_id"])


@pytest.fixture(scope="module")
def bom(engine):
    from webbsearch.retrieval.bom import Bom
    return Bom(engine)


def test_feet_become_coils(bom):
    r = bom.match({"text": "1/2 pex b tubing", "qty": 200, "uom": "FT"}, ANONYMOUS, None)
    for o in r["options"]:
        if "conv" in o:
            L = float(o["conv"].split("×")[1].split("ft")[0])
            assert o["units"] == math.ceil(200 / L) and o["fulfil"]["need"] == o["units"]


def test_personalized_reasons(bom, engine):
    p = _owner(engine)
    r = bom.match({"text": "expansion tank 4.4 gal", "qty": 2}, p, None)
    top = r["options"][0]
    assert any(w["k"] == "hist" for w in top["why"]) and any(w["k"] == "inv" for w in top["why"])
    ctx = bom.context(p, None)
    assert ctx["signed_in"] and ctx["skus_bought"] > 0 and ctx["invoiced_skus"] > 0


def test_substitutes_are_on_the_shelf_and_same_spec(bom, engine):
    p = _owner(engine)
    r = bom.match({"text": "3/4 cxc 90 elbow", "qty": 40}, p, None)
    top = r["options"][0]
    if top["fulfil"]["status"] == "pickup":
        pytest.skip("top pick is in stock for this build")
    assert r["substitutes"]
    cat = engine.catalog
    a0 = {k: v for k, v in cat.attrs(cat.sku_to_idx[top["sku"]]).items() if k != "series"}
    for s in r["substitutes"]:
        assert s["fulfil"]["status"] == "pickup" and s["sku"] != top["sku"]
        a = cat.attrs(cat.sku_to_idx[s["sku"]])
        assert all(a.get(k) == v for k, v in a0.items())


def test_recommendations_skip_implausible_records(bom):
    r = bom.match({"text": "45/5 cap", "qty": 1}, ANONYMOUS, None)
    assert r["options"] and not any(implausible(o["title"]) for o in r["options"])


def test_description_match_needs_confirmation(bom):
    r = bom.match({"text": "Honeywell T6 Pro thermostat", "qty": 1}, ANONYMOUS, None)
    if r["status"] == "webb":
        assert r["confirmed"] is ("description" not in (r["via"] or ""))
