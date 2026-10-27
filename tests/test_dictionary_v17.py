"""v1.7: the search dictionary (webbsearch/merch/dictionary.py): matching, context, guards, lint, the store's
review workflow and history, files, and the results page using it."""
import json

import pytest

from webbsearch.merch.dictionary import Dictionary, Lint, Matcher, clean, key


def E(term, to="", kind="synonym", status="live", **kw):
    return clean({"kind": kind, "term": term, "to": to, "status": status, **kw})


# ------------------------------------------------------------------ matching
def test_phrase_match_is_whole_word_case_and_punctuation_insensitive():
    m = Matcher([E("bathroom faucet", "lavatory faucet"), E("san tee", "sanitary tee"), E("T&P valve", "t&p relief valve")])
    assert m.apply("Delta Bathroom Faucet chrome")[0] == "Delta lavatory faucet chrome"
    assert m.apply("3 in. san-tee PVC")[0] == "3 in. sanitary tee PVC"            # hyphen = space
    assert m.apply("3/4 T&P valve")[0] == "3/4 t&p relief valve"
    assert m.apply("bathroomfaucet")[1] == []                                      # whole words only
    q, hits = m.apply("bathroom faucets")                                          # plural
    assert q == "lavatory faucet" and hits[0]["from"] == "bathroom faucets"


def test_longest_phrase_wins_and_replacements_are_not_rematched():
    m = Matcher([E("cap", "capacitor", any=["mfd", "run"]), E("run cap", "run capacitor"), E("capacitor", "cap")])
    assert m.apply("45/5 run cap")[0] == "45/5 run capacitor"                     # "run cap", not "cap"
    assert m.apply("capacitor")[0] == "cap"                                         # one pass: no loop back


def test_context_any_and_none():
    m = Matcher([E("CO", "cleanout", any=["plug", "pipe", "fitting"], none=["detector", "alarm"])])
    assert m.apply("4 in. CO plug")[0] == "4 in. cleanout plug"
    assert m.apply("CO detector plug in")[1] == []
    assert m.apply("CO")[1] == []


def test_part_numbers_are_not_rewritten_unless_named_exactly():
    m = Matcher([E("r410a", "R-410A"), E("lf", "lead free")])
    assert m.apply("R410A 25 lb")[0] == "R-410A 25 lb"                            # named exactly: rewritten
    assert m.apply("LF25AUB-Z3")[1] == []                                            # a part number containing LF
    assert m.apply("25 lb R-410A")[1] == []


def test_only_live_rewrite_kinds_apply():
    m = Matcher([E("stat", kind="block"), E("SEER2", "seasonal energy efficiency ratio 2", kind="glossary"),
                 E("hose bib", "hose bibb", status="review"), E("B&G", "Bell & Gossett", kind="brand"),
                 E("facuet", "faucet", kind="spelling")])
    assert m.n == 2
    assert m.apply("B&G pump")[0] == "Bell & Gossett pump" and m.apply("facuet")[0] == "faucet"
    assert m.apply("stat 24v hose bib seer2")[1] == []


# ------------------------------------------------------------------ lint
DF = {"faucet": 45908, "lavatory": 22200, "bathroom": 0, "garbage": 588, "disposal": 588, "food": 0, "waste": 30,
      "watt": 10849, "cap": 900, "capacitor": 2000}


def lint():
    return Lint(df=lambda t: DF.get(t, 0), brands={"Bell & Gossett", "Watts"}, canon={"el": "elbow", "stat": "thermostat"})


def codes(e, others=()):
    return {x["code"]: x["level"] for x in lint().check(e, list(others))}


def test_lint_catches_what_the_review_found():
    assert codes(E("45", "45 degree elbow"))["bare_number"] == "error"
    assert "bare_number" not in codes(E("45", "45 degree elbow", any=["ell", "elbow"]))
    assert codes(E("CO", "cleanout"))["short"] == "error"
    assert codes(E("garbage disposal", "food waste disposer"))["backwards"] == "warn"
    assert "backwards" not in codes(E("bathroom faucet", "lavatory faucet"))
    assert codes(E("wye", "wye fitting"))["adds_words"] == "warn"
    assert codes(E("watts", "watt"))["brand_word"] == "warn"
    assert codes(E("NH coupling", "no-hub coupling"))["state"] == "warn"
    assert "state" not in codes(E("NH", "no-hub", any=["coupling", "band"]))
    assert codes(E("el", "elbow"))["builtin"] == "warn"
    assert codes(E("faucet", "tap", kind="spelling"))["real_word"] == "error"
    assert codes(E("B and G", "Bell and Gosset", kind="brand"))["unknown_brand"] == "warn"
    blk = E("cap", kind="block", note="capacitor, capillary tube or pipe cap")
    assert codes(E("cap", "capacitor"), [blk])["blocked"] == "error"
    assert "blocked" not in codes(E("run cap", "run capacitor"), [blk])
    other = E("condensate removal pump", "condensate pump")
    assert codes(E("condensate removal pump", "HVAC condensate pump"), [other])["conflict"] == "error"


# ------------------------------------------------------------------ store
def test_store_workflow_history_and_restore(tmp_path):
    d = Dictionary(tmp_path)
    d.lint = lint()
    e = d.save({"kind": "synonym", "term": "bathroom faucet", "to": "lavatory faucet", "status": "review"}, who="QA")
    assert d.apply("bathroom faucet")[1] == []                                      # review: no effect
    with pytest.raises(ValueError, match="Can't go live"):
        d.save({"kind": "synonym", "term": "90", "to": "90 degree elbow", "status": "live"}, who="QA")
    r = d.set_status([e["id"]], "live", who="Brendan", base_version=d.version)
    assert r["changed"] == 1 and d.apply("bathroom faucet")[0] == "lavatory faucet"
    bad = d.save({"kind": "synonym", "term": "90", "to": "90 degree elbow"}, who="QA")
    assert d.set_status([bad["id"]], "live", who="QA")["refused"][0]["term"] == "90"
    with pytest.raises(Exception):
        d.save({"kind": "synonym", "term": "x y", "to": "z"}, base_version=d.version - 1)   # stale editor
    d.delete(e["id"], who="QA", note="test")
    assert d.apply("bathroom faucet")[1] == []
    h = d.history(e["id"])
    assert [x["action"] for x in h] == ["delete", "status", "add"] and h[1]["who"] == "Brendan"
    back = d.restore(e["id"], h[0]["at"], who="QA")
    assert back["status"] == "review" and d.get(e["id"])
    d2 = Dictionary(tmp_path)                                                       # persisted
    assert d2.get(e["id"])["term"] == "bathroom faucet" and d2.version == d.version


def test_import_workbook_columns_and_csv_round_trip(tmp_path):
    d = Dictionary(tmp_path)
    d.lint = lint()
    csv_text = ("Search Term,Synonym,Department,Handling,Notes\n"
                "bath spout,tub spout,Plumbing,Upload candidate,Retain diverter\n"
                "cap,,HVAC/R,Do not expand,capacitor or pipe cap\n"
                "SEER,seasonal energy efficiency ratio,HVAC/R,Attribute mapping,\n")
    rows = Dictionary.rows_from_file("x.csv", csv_text.encode())
    assert [r["kind"] for r in rows] == ["synonym", "block", "glossary"]
    res = d.import_rows([{**r, "status": "live"} for r in rows], who="QA", source="workbook")
    assert res["imported"] == 3 and d.apply("bath spout")[0] == "tub spout"
    out = d.to_csv()
    again = Dictionary.rows_from_file("y.csv", out.encode())
    assert {r["term"] for r in again} == {"bath spout", "cap", "SEER"}
    res = d.import_rows([again[0]], who="QA", source="workbook", replace_source=True)
    assert res["removed"] == 2 and len(d.entries()) == 1


def test_key_forms():
    assert key("Plumber's Putty") == key("plumbers putty")
    assert key("1-1/4 street 90") == ("1", "1/4", "street", "90")


# ------------------------------------------------------------------ on the engine
def test_results_use_the_dictionary_and_as_typed_skips_it(engine):
    from webbsearch.retrieval.entitlements import ANONYMOUS
    entries = [E("bathroom faucet", "lavatory faucet")]
    r = engine.results.results("bathroom faucet", ANONYMOUS, llm="off", vocab=entries)
    assert r["merch"]["vocab"][0]["to"] == "lavatory faucet" and r["merch"]["rewritten_from"] == "bathroom faucet"
    assert sum("lavatory" in i["title"].lower() for i in r["items"][:10]) >= 8
    r0 = engine.results.results("bathroom faucet", ANONYMOUS, llm="off", vocab=False)
    assert r0["merch"]["vocab"] == [] and r0["merch"]["rewritten_from"] is None


# ------------------------------------------------------------------ the dashboard API
def _req(token=True, host="127.0.0.1"):
    from types import SimpleNamespace as NS
    from webbsearch.api import admin
    return NS(client=NS(host=host), headers={"x-admin-token": admin.TOKEN} if token else {})


@pytest.fixture()
def api(engine, monkeypatch, tmp_path):
    """The admin module wired to the test engine, with a scratch dictionary so tests don't disturb each other."""
    from webbsearch.api import admin, server
    monkeypatch.setattr(server, "_engine", engine)
    d = Dictionary(tmp_path)
    d.lint = engine.vocab.lint
    d.import_rows([{"kind": "synonym", "term": "bathroom faucet", "to": "lavatory faucet", "status": "live"},
                   {"kind": "block", "term": "cap", "status": "live", "note": "capacitor or pipe cap"}], who="QA")
    monkeypatch.setattr(engine, "vocab", d)
    return admin


def test_dictionary_api_requires_the_dashboard_token(api):
    from fastapi import HTTPException
    for r in (_req(token=False), _req(host="10.1.2.3")):
        with pytest.raises(HTTPException) as ex:
            api.dict_list(r)
        assert ex.value.status_code == 403


def test_dictionary_api_list_lint_save_status_history(api):
    from fastapi import HTTPException
    lst = api.dict_list(_req(), q="faucet")
    assert lst["total"] == 1 and lst["entries"][0]["to"] == "lavatory faucet" and lst["counts"]["live_rules"] == 1
    lint = api.dict_lint(_req(), api.DictSaveBody(entry={"kind": "synonym", "term": "cap", "to": "capacitor"}))
    assert any(c["code"] == "blocked" and c["level"] == "error" for c in lint["checks"])
    with pytest.raises(HTTPException) as ex:                          # an error can't go live
        api.dict_save(_req(), api.DictSaveBody(entry={"kind": "synonym", "term": "cap", "to": "capacitor", "status": "live"}, who="QA"))
    assert ex.value.status_code == 422
    v = lst["version"]
    s = api.dict_save(_req(), api.DictSaveBody(entry={"kind": "synonym", "term": "hose bib", "to": "hose bibb"}, who="QA", base_version=v))
    with pytest.raises(HTTPException) as ex:                          # a stale editor
        api.dict_save(_req(), api.DictSaveBody(entry={"kind": "synonym", "term": "x y", "to": "z"}, base_version=v))
    assert ex.value.status_code == 409
    r = api.dict_status(_req(), api.DictStatusBody(ids=[s["entry"]["id"]], status="live", who="Brendan"))
    assert r["changed"] == 1
    h = api.dict_history(_req(), id=s["entry"]["id"])["history"]
    assert [x["action"] for x in h] == ["status", "add"] and h[0]["who"] == "Brendan"
    api.dict_delete(_req(), api.DictDeleteBody(id=s["entry"]["id"], who="QA"))
    back = api.dict_restore(_req(), api.DictRestoreBody(id=s["entry"]["id"], at=h[0]["at"], who="QA"))
    assert back["entry"]["term"] == "hose bib"


def test_dictionary_api_try_measure_import_export(api):
    import base64
    t = api.dict_try(_req(), api.DictTryBody(q="bathroom faucet", entry=api._vocab().entries()[0] if api._vocab().entries()[0]["kind"] == "synonym"
                                              else api._vocab().entries()[1]))
    assert t["live"]["searched"] == "bathroom faucet" and t["draft"]["searched"] == "lavatory faucet"     # without vs with
    m = api.dict_measure(_req(), api.DictSaveBody(entry={"kind": "synonym", "term": "bathroom faucet", "to": "lavatory faucet"}))
    assert m["suggest"] == "live" and m["measure"]["target_on"] > m["measure"]["typed_on"]
    csv_text = "term,to,kind,status\nfacuet,faucet,spelling,live\n45,45 degree elbow,synonym,live\n"
    r = api.dict_import(_req(), api.DictImportBody(name="x.csv", data=base64.b64encode(csv_text.encode()).decode(), who="QA", keep_status=True))
    assert r["imported"] == 2 and r["held_for_approval"] == 2              # v2.4: live rows wait for someone else
    ids = [e["id"] for e in api._vocab().entries() if e["term"] in ("facuet", "45")]
    own = api.dict_status(_req(), api.DictStatusBody(ids=ids, status="live", who="QA"))
    assert own["changed"] == 0 and all("your own change" in x["why"] for x in own["refused"])
    ok = api.dict_status(_req(), api.DictStatusBody(ids=ids, status="live", who="Brendan"))
    assert ok["changed"] == 1 and ok["refused"][0]["term"] == "45"                   # the bare 45 can't go live
    r2 = api.dict_import(_req(), api.DictImportBody(name="y.csv", data=base64.b64encode(b"term,to\nfoo bar,baz\n").decode(), who="QA"))
    assert api._vocab().get(r2 and next(e["id"] for e in api._vocab().entries() if e["term"] == "foo bar"))["status"] == "review"
    out = api.dict_export(_req(), status="live").body.decode()
    assert "facuet" in out and "foo bar" not in out
    assert any(x["term"] == "ci" for x in api.dict_builtin(_req())["rules"])


# ------------------------------------------------------------------ pages always load the scripts they were written with
