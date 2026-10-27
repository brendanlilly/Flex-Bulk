"""v2.4: trade words in 12 languages. Spanish (expanded), Portuguese, Polish, Russian and Chinese at full depth;
Albanian, Haitian Creole, Vietnamese, Arabic, French, Italian and Ukrainian at the core. A search in one of them is read
into the catalog's English; an English search is never touched."""
from __future__ import annotations

import re

import pytest

from webbsearch.retrieval import lexicon
from webbsearch.retrieval.entitlements import ANONYMOUS


def tr(q):
    return lexicon.translate(q)


# ------------------------------------------------------------------ each language reads into the catalog's words
CASES = [
    # Spanish (expanded)
    ("es", "válvula de bola 3/4", ["ball valve", "3/4"]),
    ("es", "codo de cobre 1/2", ["elbow", "copper", "1/2"]),
    ("es", "calentador de agua de 50 galones", ["water heater", "50", "gallon"]),
    ("es", "cinta de teflón", ["PTFE tape"]),
    ("es", "llave de paso de media pulgada", ["shutoff valve", "1/2 inch"]),
    ("es", "bomba de sumidero", ["sump pump"]),
    ("es", "coladera de piso", ["floor drain"]),
    # Portuguese (Brazilian usage first)
    ("pt", "registro de esfera 3/4", ["ball valve", "3/4"]),
    ("pt", "joelho de cobre 1/2", ["elbow", "copper", "1/2"]),
    ("pt", "luva soldável 3/4", ["coupling", "pvc", "3/4"]),        # soldável: PVC glue-joint in Brazil
    ("pt", "luva para solda 3/4", ["coupling", "sweat", "3/4"]),
    ("pt", "registro 1/4 de volta", ["valve"]),                      # "volta" (turn) isn't volt
    ("pt", "fita veda rosca", ["PTFE tape"]),
    ("pt", "registro de gaveta 1 polegada", ["gate valve", "1 inch"]),
    ("pt", "válvula de retenção", ["check valve"]),
    ("pt", "aquecedor de passagem a gás", ["tankless water heater"]),
    ("pt", "tê de cobre meia polegada", ["tee", "copper", "1/2 inch"]),
    ("pt", "sifão para pia de cozinha", ["p-trap", "kitchen sink"]),
    # Polish (inflected)
    ("pl", "zawór kulowy 3/4", ["ball valve", "3/4"]),
    ("pl", "zaworu kulowego 1/2 cala", ["ball valve", "1/2 inch"]),
    ("pl", "kolano miedziane 1/2", ["elbow", "copper"]),
    ("pl", "taśma teflonowa", ["PTFE tape"]),
    ("pl", "pompa obiegowa", ["circulator"]),
    ("pl", "trójnik mosiężny", ["tee", "brass"]),
    ("pl", "zawor zwrotny DN20", ["check valve", "3/4 inch"]),
    ("pl", "naczynie wzbiorcze", ["expansion tank"]),
    # Russian (Cyrillic and Latin)
    ("ru", "шаровой кран 3/4", ["ball valve", "3/4"]),
    ("ru", "кран шаровой латунный 1/2", ["ball valve", "brass", "1/2"]),
    ("ru", "уголок медный под пайку", ["elbow", "copper", "sweat"]),
    ("ru", "фум лента", ["PTFE tape"]),
    ("ru", "газовый ключ", ["pipe wrench"]),
    ("ru", "обратный клапан 1 дюйм", ["check valve", "1 inch"]),
    ("ru", "sharovoy kran 3/4", ["ball valve", "3/4"]),
    ("ru", "расширительный бак", ["expansion tank"]),
    ("ru", "циркуляционный насос", ["circulator"]),
    # Chinese (simplified, traditional, Cantonese; trade sizes)
    ("zh", "6分球阀", ["3/4 inch", "ball valve"]),
    ("zh", "4分铜弯头", ["1/2 inch", "copper", "elbow"]),
    ("zh", "生料带", ["PTFE tape"]),
    ("zh", "燃气热水器", ["gas water heater"]),
    ("zh", "寸半闸阀", ["1-1/2 inch", "gate valve"]),
    ("zh", "DN20 止回阀", ["3/4 inch", "check valve"]),
    ("zh", "球閥", ["ball valve"]),
    ("zh", "冷氣雪種", ["air conditioner", "refrigerant"]),
    ("zh", "膨胀罐", ["expansion tank"]),
    # the core seven
    ("sq", "valvol sferike 1/2", ["ball valve", "1/2"]),
    ("sq", "bërryl bakri", ["elbow", "copper"]),
    ("ht", "vann boul 3/4", ["ball valve", "3/4"]),
    ("ht", "chofè dlo", ["water heater"]),
    ("vi", "van bi 3/4", ["ball valve", "3/4"]),
    ("vi", "bình nóng lạnh", ["water heater"]),
    ("vi", "co phi 21", ["elbow", "1/2 inch"]),
    ("vi", "băng tan", ["PTFE tape"]),
    ("ar", "محبس كوره نص انش", ["ball valve", "1/2 inch"]),
    ("ar", "سخان مياه", ["water heater"]),
    ("ar", "كوع نحاس", ["elbow", "copper"]),
    ("fr", "robinet à bille 3/4", ["ball valve", "3/4"]),
    ("fr", "coude en cuivre", ["elbow", "copper"]),
    ("fr", "thermopompe", ["heat pump"]),
    ("fr", "champlure de cuisine", ["kitchen faucet"]),
    ("it", "valvola a sfera 3/4", ["ball valve", "3/4"]),
    ("it", "gomito in rame", ["elbow", "copper"]),
    ("it", "caldaia a gas", ["boiler"]),
    ("uk", "кульовий кран 3/4", ["ball valve", "3/4"]),
    ("uk", "трійник мідний", ["tee", "copper"]),
    ("uk", "водонагрівач", ["water heater"]),
]


@pytest.mark.parametrize("lang,q,want", CASES, ids=[f"{c[0]}:{c[1]}" for c in CASES])
def test_each_language_reads_into_the_catalogs_words(lang, q, want):
    out, got = tr(q)
    assert got == lang, (q, got, out)
    for w in want:
        assert w.lower() in out.lower(), (q, out)


def test_twelve_languages():
    assert set(lexicon.LANGS) == {"es", "pt", "pl", "ru", "zh", "sq", "ht", "vi", "ar", "fr", "it", "uk"}


def test_the_top_five_cover_the_full_list_and_the_rest_the_core():
    full, core = lexicon.concepts(), lexicon.core_concepts()
    assert len(full) >= 140 and len(core) >= 75 and set(core) <= set(full)
    for lang in ("es", "pt", "pl", "ru", "zh"):
        cov = lexicon.coverage(lang)
        assert cov["full"] >= 0.9, (lang, cov)
    for lang in ("sq", "ht", "vi", "ar", "fr", "it", "uk"):
        cov = lexicon.coverage(lang)
        assert cov["core"] >= 0.8, (lang, cov)


def test_every_target_is_a_concept():
    for lang in lexicon.LANGS:
        for en in lexicon.entries(lang):
            assert en in lexicon.concepts() or en in lexicon.EXTRA_TARGETS, (lang, en)


# ------------------------------------------------------------------ English is never touched
ENGLISH = ["water heater", "3/4 ball valve", "tub spout", "taco 007", "moen posi temp cartridge", "boiler drain",
           "3/4 te", "tee", "cap", "nut", "pipe dope", "flange", "bronze", "radiator", "compressor", "motor",
           "thermostat", "filter", "drain", "split", "coil", "trap", "union", "plug", "co2 detector", "pex a",
           "press 90", "fitting", "zone valve", "tub drain", "van tool", "gang box", "1/2 copper 90",
           "sharkbite coupling", "pressure tank", "gas valve", "flapper", "pipe", "kit", "ren", "con ed", "bom",
           "dong", "ong", "gang", "tube", "male adapter", "union ell", "manifold", "termination", "sillcock",
           "vent pipe", "condenser fan motor", "taco circulator", "boiler", "zurn", "Jomar 1/2 push fit ball valve",
           "what press do I need for 3/4 female adapter", "push fit coupling", "elkay sink",
           "cast iron no hub", "pvc cement", "water", "copper", "trap primer", "base", "lav faucet", "camfil filter",
           "radiant manifold", "propane", "solder", "torch", "flux", "teflon tape", "le 2634207", "honeywell v8168a1069",
           "16x25x1 filter", "50 gallon gas water heater", "3 ton condenser", "Zoëller sump pump", "pressure balance valve"]


@pytest.mark.parametrize("q", ENGLISH)
def test_english_searches_are_never_touched(q):
    out, lang = tr(q)
    assert lang is None and out == q, (q, lang, out)


def test_every_logged_search_in_english_stays_english(engine):
    import pandas as pd
    from webbsearch.config import settings
    log = pd.read_parquet(settings.canonical_dir / "search_log.parquet")
    col = next(c for c in ("query", "q", "text") if c in log.columns)
    qs = list(dict.fromkeys(str(q) for q in log[col].dropna()))
    hits = [(q, tr(q)) for q in qs if tr(q)[1]]
    # the synthetic log carries a few deliberate Spanish searches; nothing else may be read as another language
    bad = [(q, r) for q, r in hits if r[1] != "es"]
    assert not bad, bad[:20]
    assert len(hits) <= max(5, len(qs) // 500), hits[:20]


# ------------------------------------------------------------------ through the search page
def r(engine, q):
    return engine.results.results(q, ANONYMOUS, llm="off")


def titles(res, n=5):
    return [i["title"] for i in (res.get("items") or [])[:n]]


SEARCH = [
    ("registro de esfera 3/4", r"3/4 in\..*Ball Valve"),
    ("joelho de cobre 1/2", r"1/2 in\..*Copper.*Elbow"),
    ("zawór kulowy 3/4", r"3/4 in\..*Ball Valve"),
    ("kolano miedziane 1/2", r"1/2 in\..*Copper.*Elbow"),
    ("шаровой кран 3/4", r"3/4 in\..*Ball Valve"),
    ("медный тройник 1/2", r"1/2 in\..*Copper.*Tee"),
    ("6分球阀", r"3/4 in\..*Ball Valve"),
    ("燃气热水器", r"Gas.*Water Heater"),
    ("válvula de bola 3/4", r"3/4 in\..*Ball Valve"),
    ("robinet à bille 3/4", r"3/4 in\..*Ball Valve"),
    ("van bi 3/4", r"3/4 in\..*Ball Valve"),
    ("кульовий кран 1/2", r"1/2 in\..*Ball Valve"),
]


@pytest.mark.parametrize("q,want", SEARCH)
def test_a_search_in_another_language_finds_the_product(engine, q, want):
    res = r(engine, q)
    assert (res.get("notice") or {}).get("kind") != "not_carried"
    assert any(re.search(want, t) for t in titles(res, 3)), (q, titles(res))
    seen = (res.get("merch") or {}).get("vocab") or []
    assert any(f.get("why") == lexicon.LANGS[lexicon.translate(q)[1]]["name"] for f in seen), seen


# ------------------------------------------------------------------ the dashboard's Languages view
def test_dashboard_lists_the_languages_their_terms_and_tries_a_search(engine, monkeypatch):
    from fastapi.testclient import TestClient
    from webbsearch.api import admin, server
    monkeypatch.setattr(server, "_engine", engine)
    c = TestClient(server.app)
    h = {"x-admin-token": admin.TOKEN}
    assert c.get("/api/admin/lexicon").status_code in (401, 403)
    r = c.get("/api/admin/lexicon", headers=h).json()
    langs = {x["code"]: x for x in r["languages"]}
    assert set(langs) == set(lexicon.LANGS) and r["concepts"] >= 140
    assert langs["pt"]["name"] == "Portuguese" and langs["pt"]["terms"] > 200 and langs["pt"]["full"] >= 0.9
    assert langs["pt"]["depth"] == "full" and langs["ht"]["depth"] == "core"
    t = c.get("/api/admin/lexicon", headers=h, params={"q": "registro de esfera 3/4"}).json()["try"]
    assert t["lang"] == "pt" and t["name"] == "Portuguese" and "ball valve" in t["to"]
    assert c.get("/api/admin/lexicon", headers=h, params={"q": "3/4 ball valve"}).json()["try"]["lang"] is None
    d = c.get("/api/admin/lexicon", headers=h, params={"lang": "pl"}).json()["entries"]
    assert any(x["english"] == "ball valve" and any("kulow" in t for t in x["terms"]) for x in d)


@pytest.mark.parametrize("q,size", [("1寸半球阀", "1-1/2 inch"), ("1寸2球阀", "1-1/4 inch"), ("一寸半球阀", "1-1/2 inch"),
                                    ("一寸二球阀", "1-1/4 inch"), ("2寸半闸阀", "2-1/2 inch"), ("两寸半闸阀", "2-1/2 inch"),
                                    ("四分球阀", "1/2 inch"), ("六分弯头", "3/4 inch"), ("三分角阀", "3/8 inch"),
                                    ("两分球阀", "1/4 inch"), ("寸半闸阀", "1-1/2 inch"), ("一寸球阀", "1 inch")])
def test_chinese_trade_sizes_with_halves_and_chinese_numerals(q, size):
    out, lang = tr(q)
    assert lang == "zh" and size in out, (q, out)
    assert not re.search(r"\binch\s+(?:半|二|2)", out), out


def test_a_number_ending_in_6_before_fen_is_not_six_fen():
    out, _ = tr("16分钟 球阀")
    assert "3/4 inch" not in out, out


def test_a_cantonese_switch_is_not_a_valve():
    out, _ = tr("壓力掣")
    assert "pressure switch" in out and "valve" not in out, out
    out, _ = tr("电掣")
    assert "valve" not in out, out


@pytest.mark.parametrize("q,bad", [
    ("van bi cỡ 27", "elbow"),                  # cỡ = size, not co (elbow): tone marks are kept for Vietnamese
    ("máy bơm công suất 1hp", "drain"),          # công suất = power, not cống (drain)
    ("tủ điện", "capacitor"),                    # tủ = cabinet, not tụ (capacitor)
    ("عايز لي سخان", "hose"),                    # لي = "for me"
    ("تخفيض سخانات", "reducer"),                 # تخفيض = discount
    ("вентиляція", "valve"),                     # Ukrainian ventilation, not вентиль
    ("tiyo a fann", "fan"),                      # Haitian fann = cracked
])
def test_false_friends_found_in_review_stay_out(q, bad):
    out, _ = tr(q)
    assert not re.search(rf"\b{bad}\b", out), (q, out)


@pytest.mark.parametrize("q,good", [("van điện từ", "solenoid valve"), ("co phi 21", "elbow"), ("ống phi 75", "2-1/2 inch"),
                                    ("dàn nóng", "condenser")])
def test_vietnamese_with_and_without_tone_marks(q, good):
    out, lang = tr(q)
    assert lang == "vi" and good in out, (q, out)
