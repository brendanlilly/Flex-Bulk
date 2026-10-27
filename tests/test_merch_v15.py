"""v1.5: expanded merchandising (rule audiences, campaigns, conditions, only/exclude, badges, redirects, facets,
A/B tests), brand affinity per customer discipline, size grouping defaults, and Bulk order crosses, brand order
and learned crosses.

Store / rules / bulkprefs / analytics tests run anywhere; `engine` tests need the index."""
import json
import time
from types import SimpleNamespace as NS

import numpy as np
import pytest

from webbsearch.merch.analytics import Analytics, _ztest
from webbsearch.merch.bulkprefs import (Learned, brand_cross, brand_rank, item_cross, line_key, other_brand,
                                        replace_brand, scoped)
from webbsearch.merch.rules import (adjust_scores, affinity_weights, audience_ok, bulk_weights, cond_mask, diversify,
                                    pin_badges, rule_matches)
from webbsearch.merch.store import (DEFAULTS, SEGMENTS, MerchStore, validate_brand_pref, validate_cross,
                                    validate_rule)

TODAY = __import__("datetime").date(2026, 10, 1)


# ------------------------------------------------------------------ store: new sections
def test_affinity_patch_and_labels(tmp_path):
    m = MerchStore(tmp_path)
    s, ch = m.merged({"affinity": {"segments": {"plumbing_contractor": {"within": 2.0}}, "min_lines": 25}})
    assert s["affinity"]["segments"]["plumbing_contractor"] == {"within": 2.0, "across": 1.5}
    assert "brand affinity Plumbing within: 1.0 → 2.0" in ch and "affinity.min_lines: 10 → 25" in ch
    for bad in ({"segments": {"nope": {"within": 1}}}, {"segments": {"industrial": {"within": 9}}},
                {"segments": {"industrial": {"sideways": 1}}}, {"min_lines": -1}):
        with pytest.raises(ValueError):
            m.merged({"affinity": bad})
    assert m.reset("affinity")["affinity"] == DEFAULTS["affinity"]


def test_facets_normalized(tmp_path):
    s, ch = MerchStore(tmp_path).merged({"facets": {"order": ["brand", "bogus", "price"], "hidden": ["price", "x"],
                                                    "collapsed": ["spec"]}})
    f = s["facets"]
    assert f["order"][:2] == ["brand", "price"] and len(f["order"]) == len(DEFAULTS["facets"]["order"])
    assert f["hidden"] == ["price"] and f["collapsed"] == ["spec"] and any("filter rail" in c for c in ch)


def test_group_variants_is_a_switch(tmp_path):
    s, ch = MerchStore(tmp_path).merged({"layout": {"group_variants": True}})
    assert s["layout"]["group_variants"] == 1 and ch


def test_cross_validation():
    c = validate_cross({"kind": "brand", "from_brand": "Kohler", "to_brand": "American Standard"})
    assert c["label"] == "Kohler → American Standard" and c["scope"] == "company" and c["enabled"]
    b = validate_cross({"kind": "brand", "from_brand": "Kohler", "to_brand": "Gerber", "scope": "105"})
    assert b["label"].endswith("(branch 105)")
    i = validate_cross({"kind": "item", "from_text": "  Kohler  Highline Toilet ", "to_sku": "2593859"})
    assert i["from_text"] == "kohler highline toilet" and i["label"] == "kohler highline toilet → #2593859"
    for bad in ({"kind": "brand", "from_brand": "Kohler"}, {"kind": "brand", "from_brand": "Moen", "to_brand": "moen"},
                {"kind": "item", "to_sku": "1"}, {"kind": "brand", "from_brand": "A", "to_brand": "B", "scope": "Boston"},
                {"kind": "other"}):
        with pytest.raises(ValueError):
            validate_cross(bad)


def test_brand_pref_validation():
    b = validate_brand_pref({"family": "Ball Valves", "brands": ["NIBCO", "Apollo"]})
    assert b["label"] == "Ball Valves: NIBCO > Apollo"
    for bad in ({"family": "Ball Valves", "brands": []}, {"family": "", "brands": ["NIBCO"]},
                {"family": "Ball Valves", "brands": ["NIBCO", "nibco"]}):
        with pytest.raises(ValueError):
            validate_brand_pref(bad)
    assert len(validate_brand_pref({"family": "X", "brands": list("ABCDEFGH")})["brands"]) == 6


def test_experiments(tmp_path):
    m = MerchStore(tmp_path)
    s = m.update({"experiments": [{"name": "cap 2", "patch": {"layout": {"brand_cap": 2}}, "split": 20}]})
    x = s["experiments"][0]
    assert x["status"] == "running" and x["changes"] == ["layout.brand_cap: 4 → 2"] and x["split"] == 20
    assert s["layout"]["brand_cap"] == 4                                   # the test doesn't change live settings
    with pytest.raises(ValueError):                                        # one running at a time
        m.merged({"experiments": s["experiments"] + [{"name": "b", "patch": {"weights": {"popularity": 1}}}]})
    for bad in ({"patch": {"layout": {"brand_cap": 2}}, "split": 80}, {"patch": {"experiments": []}},
                {"patch": {"weights": {"purchased": 99}}}, {"patch": {}}):
        with pytest.raises(ValueError):
            m.merged({"experiments": [bad]})
    stopped = m.update({"experiments": [{**x, "status": "stopped"}]})["experiments"][0]
    assert stopped["stopped"] and stopped["started"] == x["started"]


def test_learned_off_deduped(tmp_path):
    s, ch = MerchStore(tmp_path).merged({"learned_off": ["a::1", "a::1", " ", "brand::kohler::gerber"]})
    assert s["learned_off"] == ["a::1", "brand::kohler::gerber"] and ch


def test_rule_validation_v15():
    r = validate_rule({"match": {"type": "contains", "q": "ProPress | press fitting\nviega press"},
                       "audience": {"who": "signed_in", "segments": ["plumbing_contractor", "nope"], "roles": ["buyer", "x"]},
                       "boost": [{"field": "attr", "value": "material=Brass", "weight": 2}, {"field": "leadfree", "value": ""}],
                       "only": [{"field": "stock", "value": "stocked"}], "exclude": [{"field": "title", "value": "stainless"}],
                       "badge": "Sponsored", "redirect": {"url": "#tab=kits&q=press", "auto": True}})
    assert r["match"]["q"] == "propress | press fitting | viega press"
    assert r["audience"] == {"who": "signed_in", "segments": ["plumbing_contractor"], "roles": ["buyer"]}
    assert r["boost"][1] == {"field": "leadfree", "value": "yes", "weight": 0.0}
    assert r["badge"] == "Sponsored" and r["redirect"] == {"url": "#tab=kits&q=press", "auto": True}
    for bad in ({"match": {"q": "x"}, "audience": {"who": "guests", "segments": ["industrial"]}},
                {"match": {"q": "x"}, "boost": [{"field": "attr", "value": "brass"}]},
                {"match": {"q": "x"}, "only": [{"field": "stock", "value": "lots"}]},
                {"match": {"q": "x"}, "redirect": {"url": "javascript:alert(1)"}},
                {"match": {"q": "x"}, "redirect": {"url": "//evil.example"}},
                {"match": {"q": "x"}, "exclude": [{"field": "color", "value": "red"}]}):
        with pytest.raises(ValueError):
            validate_rule(bad)


def test_stock_level_cd_is_webbs_word_for_the_distribution_center():
    r = validate_rule({"match": {"q": "x"}, "boost": [{"field": "stock", "value": "CD", "weight": -1}],
                       "only": [{"field": "stock", "value": "Stocked"}]})
    assert r["boost"][0]["value"] == "dc" and r["only"][0]["value"] == "stocked"      # the stored code is unchanged
    with pytest.raises(ValueError, match="CD"):
        validate_rule({"match": {"q": "x"}, "only": [{"field": "stock", "value": "DC-only"}]})


# ------------------------------------------------------------------ rules
P = lambda seg=None, role=None, acct="1": NS(account_id=acct, segment=seg, role=role)   # noqa: E731
GUEST = NS(account_id=None, segment=None, role=None)


def test_campaign_phrases_and_audience():
    r = validate_rule({"match": {"type": "contains", "q": "propress | press fitting"},
                       "audience": {"who": "all", "segments": ["plumbing_contractor"]}})
    assert rule_matches(r, "3/4 propress elbow", TODAY, P("plumbing_contractor"))
    assert rule_matches(r, "copper press fitting", TODAY, P("plumbing_contractor"))
    assert not rule_matches(r, "copper elbow", TODAY, P("plumbing_contractor"))
    assert not rule_matches(r, "propress", TODAY, P("hvac_contractor"))          # other discipline
    assert not rule_matches(r, "propress", TODAY, GUEST)                         # a discipline means signed-in
    g = {"audience": {"who": "guests"}}
    assert audience_ok(g, GUEST) and not audience_ok(g, P())
    assert audience_ok({"audience": {"who": "signed_in", "roles": ["buyer"]}}, P(role="buyer"))
    assert not audience_ok({"audience": {"who": "signed_in", "roles": ["buyer"]}}, P(role="tech"))
    assert audience_ok({}, GUEST)                                                # v1.4 rules: everyone


def test_pin_badges():
    rules = [{"pin": [{"sku": "1", "pos": 1}], "badge": "Sponsored"}, {"pin": [{"sku": "1", "pos": 2}, {"sku": "2", "pos": 3}]}]
    assert pin_badges(rules) == {"1": "Sponsored", "2": "Featured"}


CAT = NS(brand=np.array(["NIBCO", "Apollo", "Viega", "NIBCO"]), family=np.array(["valves", "valves", "fittings", "valves"]),
         cat3=np.array(["Ball Valves", "Ball Valves", "Copper Press Fittings", "Ball Valves"]),
         sku=np.array(["10", "11", "12", "13"]), stock_tier=np.array(["stocked", "dc", "stocked", "special"]),
         title=np.array(["NIBCO 3/4 Lead-Free Brass Ball Valve", "Apollo 1/2 Ball Valve", "Viega ProPress Elbow",
                         "NIBCO 1 Stainless Ball Valve"]))
IDX = np.arange(4)


@pytest.mark.parametrize("cond,want", [
    ({"field": "brand", "value": "nibco"}, [1, 0, 0, 1]),
    ({"field": "family", "value": "Ball Valves"}, [1, 1, 0, 1]),
    ({"field": "family", "value": "fittings"}, [0, 0, 1, 0]),
    ({"field": "sku", "value": "11"}, [0, 1, 0, 0]),
    ({"field": "stock", "value": "stocked"}, [1, 0, 1, 0]),
    ({"field": "title", "value": "ball valve"}, [1, 1, 0, 1]),
    ({"field": "leadfree", "value": "yes"}, [1, 0, 0, 0]),
    ({"field": "attr", "value": "material=Brass"}, [1, 0, 0, 0]),
])
def test_cond_mask(cond, want):
    attrs = [{"material": "Brass"}, {"material": "Bronze"}, {"material": "Copper"}, {"material": "Stainless"}]
    assert cond_mask(cond, IDX, CAT, lambda i: attrs[i]).astype(int).tolist() == want


def test_affinity_weights():
    s = {"affinity": {"enabled": True, "segments": {"a": {"within": 1.0, "across": 2.0}, "b": {"within": 3.0, "across": 0.0}}}}
    assert affinity_weights(s, "a") == (1.0, 2.0)
    assert affinity_weights(s, "unknown") == (2.0, 1.0)                      # the table's average
    assert affinity_weights({**s, "affinity": {**s["affinity"], "enabled": False}}, "a") == (0.0, 0.0)


def test_adjust_scores_brand_affinity():
    F = {"acct_bought_n": 0, "acct_bought_recency": 1, "acct_brand_share": 2, "pop_365": 3, "stock_rank": 4}
    X = np.zeros((4, 5))
    X[:, 2] = [0.7, 0.2, 0.0, 0.7]                       # the account's share of the category in each item's brand
    s0 = np.zeros(4)
    st = json.loads(json.dumps(DEFAULTS))
    acct = {"segment": "plumbing_contractor", "lines": 50, "brand_share": {"NIBCO": 0.2, "Viega": 0.5}}
    ex = {}
    out = adjust_scores(s0, X, IDX, CAT, F, st, True, [], acct=acct, explain=ex)
    assert np.allclose(out, [0.7 + 1.5 * 0.2, 0.2, 1.5 * 0.5, 0.7 + 1.5 * 0.2]) and "brand affinity" in ex
    assert not adjust_scores(s0, X, IDX, CAT, F, st, True, [], acct={**acct, "lines": 3}).any()     # below min_lines
    assert not adjust_scores(s0, X, IDX, CAT, F, st, False, [], acct=acct).any()                  # not personalized


def test_diversify_relaxes_evenly():
    rng = np.random.default_rng(7)
    for _ in range(50):
        brands = list(rng.choice(list("AAAAAABBC"), size=40))
        top = lambda cap: max(__import__("collections").Counter(brands[k] for k in diversify(brands, cap)[:24]).values())  # noqa: E731
        assert top(2) <= top(4) <= top(8)
    order = diversify(list("AAAAAAABC"), 2, window=6)
    assert sorted(order) == list(range(9)) and [list("AAAAAAABC")[k] for k in order[:6]].count("A") == 4


def test_bulk_weights_switches():
    s = json.loads(json.dumps(DEFAULTS))
    assert bulk_weights(s)["learned"] == 4.0
    s["personalization"]["learned_crosses"] = False
    assert bulk_weights(s)["learned"] == 0.0


# ------------------------------------------------------------------ bulkprefs
def test_line_key_and_brands():
    assert line_key("10 3/4 Ball Valve,  (FIP)") == "3/4 ball valve fip"
    assert line_key("2 - kohler toilet") == line_key("kohler toilet")
    assert other_brand("2 kohler k-3999 toilet") == "Kohler" and other_brand("3/4 ball valve") == ""
    assert other_brand("AO Smith 50 gal") == "AO Smith"
    assert replace_brand("KOHLER toilet, kohler seat", "Kohler", "Gerber") == "Gerber toilet, Gerber seat"


SET = {"crosses": [validate_cross({"kind": "brand", "from_brand": "Kohler", "to_brand": "American Standard"}),
                   validate_cross({"kind": "brand", "from_brand": "Kohler", "to_brand": "Gerber", "scope": "105"}),
                   validate_cross({"kind": "item", "from_part": "K-3999", "to_sku": "2593859"}),
                   validate_cross({"kind": "item", "from_text": "kohler highline toilet", "to_sku": "1", "enabled": False})],
       "brand_prefs": [validate_brand_pref({"family": "Ball Valves", "brands": ["NIBCO", "Apollo", "Watts"]}),
                       validate_brand_pref({"family": "Ball Valves", "brands": ["Apollo"], "scope": "105"})]}


def test_scoped_branch_first():
    assert [c["to_brand"] for c in scoped([c for c in SET["crosses"] if c["kind"] == "brand"], "105")] == ["Gerber", "American Standard"]
    assert brand_cross(SET, "105", "2 kohler toilet")["to_brand"] == "Gerber"
    assert brand_cross(SET, "100", "2 kohler toilet")["to_brand"] == "American Standard"
    assert brand_cross(SET, None, "toilet", brand_col="KOHLER")["to_brand"] == "American Standard"
    assert brand_cross(SET, None, "kohlerized toilet") is None                 # whole words only


def test_item_cross():
    assert item_cross(SET, None, "kohler k-3999 toilet", {"k3999", "toilet"})["to_sku"] == "2593859"
    assert item_cross(SET, None, "kohler highline toilet", set()) is None       # switched off


def test_brand_rank_branch_list_decides():
    assert brand_rank(SET, None, "valves", "Ball Valves", "Apollo") == (1, 3)
    assert brand_rank(SET, "105", "valves", "Ball Valves", "Apollo") == (0, 1)
    assert brand_rank(SET, "105", "valves", "Ball Valves", "NIBCO") is None     # the branch's own list has no NIBCO
    assert brand_rank(SET, None, "fittings", "Copper Fittings", "NIBCO") is None


def _choice(a, sess, key, sku, brand="Apollo", fam="valves", req="", acct=None):
    a.log_event("bulk_choice", session=sess, principal=NS(account_id=acct, role="buyer") if acct else None, sku=sku,
                meta={"key": key, "brand": brand, "family": fam, "req_brand": req, "rank": 1, "how": "choose"})


def test_learned_counts_bonus_and_off(tmp_path):
    a = Analytics(tmp_path / "a.sqlite")
    lr = Learned(a)
    bw = bulk_weights(json.loads(json.dumps(DEFAULTS)))
    _choice(a, "s1", "3/4 ball valve", "11")
    _choice(a, "s1", "3/4 ball valve", "11")                    # same session, same line: one vote
    lr.refresh()
    assert lr.counts("3/4 ball valve", "11", None) == (1, 0)
    assert lr.bonus(bw, set(), "3/4 ball valve", "11", "Apollo", "valves", "", None) == (0.0, None)   # below learned_min
    _choice(a, "s2", "3/4 ball valve", "11", acct="200")
    lr.refresh()
    b, why = lr.bonus(bw, set(), "3/4 ball valve", "11", "Apollo", "valves", "", "200")
    assert b > 0 and why == "Chosen 2× for this line (1 by your team)"
    assert lr.bonus(bw, {"3/4 ball valve::11"}, "3/4 ball valve", "11", "Apollo", "valves", "", "200")[0] == 0.0
    s1, s2 = lr.bonus(bw, set(), "3/4 ball valve", "11", "Apollo", "valves", "", None)[0], b
    assert s2 > s1                                             # the account's own choice counts extra
    # a changed mind in the same session: the last pick counts
    _choice(a, "s3", "1/2 ball valve", "10"); _choice(a, "s3", "1/2 ball valve", "11")
    lr.refresh()
    assert lr.counts("1/2 ball valve", "10", None)[0] == 0 and lr.counts("1/2 ball valve", "11", None)[0] == 1


def test_learned_brand_swaps(tmp_path):
    a = Analytics(tmp_path / "a.sqlite")
    for i in range(3):
        _choice(a, f"s{i}", f"kohler toilet {i}", "20", brand="Gerber", fam="toilets", req="Kohler")
    _choice(a, "s9", "kohler toilet x", "21", brand="American Standard", fam="toilets", req="Kohler")
    lr = Learned(a)
    lr.refresh()
    bw = bulk_weights(json.loads(json.dumps(DEFAULTS)))
    b, why = lr.bonus(bw, set(), "kohler toilet new", "99", "Gerber", "toilets", "Kohler", None)
    assert b == pytest.approx(4.0 * 0.75) and why == "Kohler lines are usually matched to Gerber (3×)"
    assert lr.bonus(bw, {"brand::kohler::gerber"}, "kohler toilet new", "99", "Gerber", "toilets", "Kohler", None)[0] == 0
    cat = NS(sku_to_idx={"20": 0}, title=np.array(["Gerber Toilet"]), brand=np.array(["Gerber"]), family=np.array(["toilets"]),
             cat3=np.array(["Toilets & Bowls"]))
    t = lr.table(cat)
    assert t["brands"][0] == {"from": "kohler", "family": "toilets", "family_label": "Toilets & Bowls", "to": "Gerber", "n": 3, "share": 0.75}
    assert t["items"][0]["title"] == "Gerber Toilet"


# ------------------------------------------------------------------ analytics
def test_ztest():
    t = _ztest(300, 1000, 340, 1000)
    assert t["p"] == pytest.approx(0.0552, abs=1e-3) and t["lift"] == pytest.approx(0.1333, abs=1e-3)
    assert _ztest(0, 0, 0, 0)["p"] is None or _ztest(0, 0, 0, 0)["p"] == 1.0


def test_experiment_and_rule_reports(tmp_path):
    a = Analytics(tmp_path / "a.sqlite")
    exp = {"id": "e1", "started": time.strftime("%Y-%m-%d %H:%M:%S")}
    for i in range(40):
        arm = "A" if i % 2 else "B"
        sid = a.log_search(q="ball valve", principal=GUEST_P, total=10, latency_ms=20, personalized=False,
                           session=f"s{i}", variant=f"e1:{arm}", rules=[{"id": "r1", "name": "x"}] if i < 10 else [])
        if arm == "B" or i % 4 == 1:
            a.log_event("click", search_id=sid, session=f"s{i}", sku="1", pos=1)
    rep = a.experiment_report(exp)
    assert rep["arms"]["A"]["searches"] == 20 and rep["arms"]["B"]["searches"] == 20
    assert rep["arms"]["B"]["ctr"] == 1.0 and rep["arms"]["A"]["ctr"] == 0.5 and rep["ctr"]["lift"] == 1.0
    rr = a.rule_report(30)
    assert rr["rules"]["r1"]["searches"] == 10


GUEST_P = NS(account_id=None, user_id=None, role=None, segment=None)


def test_bulk_report(tmp_path):
    a = Analytics(tmp_path / "a.sqlite")
    a.log_event("bulk_upload", qty=3, meta={"kind": "xlsx", "lines": 3, "set_aside": 1})
    for st, extra in (("rec", {"req_brand": "Kohler", "uncarried": True, "brand_cross": "Kohler → Gerber"}),
                      ("none", {"missing": "3\" black pipe", "text": "3 black pipe"}), ("exact", {})):
        a.log_event("bulk_line", meta={"status": st, "source": "xlsx", **extra})
    _choice(a, "s", "k", "1"); a.log_event("bulk_choice", sku="2", meta={"key": "k2", "rank": 0, "how": "confirm"})
    b = a.bulk_report(30)
    assert b["uploads"] == 1 and b["upload_lines"] == 3 and b["lines"] == 3 and b["matched_rate"] == pytest.approx(0.667, abs=1e-3)
    assert b["brands_asked"] == [{"brand": "Kohler", "crossed_to": "Gerber", "n": 1}]
    assert b["top_pick_rate"] == 0.5 and b["override_rate"] == 0.5 and b["not_in_catalog"][0]["what"] == '3" black pipe'


# ------------------------------------------------------------------ A/B assignment (server helper)
def test_experiment_arm_is_sticky_and_split(tmp_path, monkeypatch):
    from webbsearch.api import admin
    m = MerchStore(tmp_path)
    m.update({"experiments": [{"name": "cap", "patch": {"layout": {"brand_cap": 2}}, "split": 30}]})
    monkeypatch.setattr(admin, "engine", lambda: NS(merch=m))
    admin._EXP.clear()
    arms = [admin.experiment_arm(f"sess{i}") for i in range(2000)]
    b = [s for s, v in arms if v.endswith(":B")]
    assert 0.26 < len(b) / 2000 < 0.34 and all(s["layout"]["brand_cap"] == 2 for s in b)
    assert all(s is None for s, v in arms if v.endswith(":A"))
    assert admin.experiment_arm("sess7") == admin.experiment_arm("sess7")
    assert admin.experiment_arm("") == (None, "")
    m.update({"experiments": [{**m.get()["experiments"][0], "status": "stopped"}]})
    assert admin.experiment_arm("sess7") == (None, "")


# ------------------------------------------------------------------ on real result pages and Bulk order lines
@pytest.fixture(scope="module")
def live(engine):
    return json.loads(json.dumps(engine.merch.get()))


def test_audience_only_exclude_badge_redirect(engine, live):
    from webbsearch.retrieval.entitlements import ANONYMOUS
    rule = validate_rule({"match": {"type": "contains", "q": "ball valve"}, "only": [{"field": "brand", "value": "Apollo"}],
                          "exclude": [{"field": "title", "value": "stainless"}], "badge": "Sponsored",
                          "redirect": {"url": "/promo", "auto": False}})
    base = engine.results.results("3/4 ball valve", ANONYMOUS, llm="off", settings=live)
    # a pin the rule's own filters would drop (a pin stays on the page regardless): neither Apollo nor stainless
    pin = next(it["sku"] for it in base["items"][5:] if it["brand"] != "Apollo" and "stainless" not in it["title"].lower())
    rule["pin"] = [{"sku": pin, "pos": 1}]
    r = engine.results.results("3/4 ball valve", ANONYMOUS, llm="off", settings={**live, "rules": [rule]})
    assert r["items"][0]["sku"] == pin and r["items"][0]["badge"] == "Sponsored"
    assert all(it["brand"] == "Apollo" for it in r["items"][1:]) and not any("stainless" in it["title"].lower() for it in r["items"])
    assert r["merch"]["redirect"] == {"url": "/promo", "auto": False}
    guests_only = {**rule, "audience": {"who": "signed_in", "segments": [], "roles": []}}
    g = engine.results.results("3/4 ball valve", ANONYMOUS, llm="off", settings={**live, "rules": [guests_only]})
    assert [i["sku"] for i in g["items"][:10]] == [i["sku"] for i in base["items"][:10]]      # rule is for signed-in only


def test_layout_in_results(engine, live):
    from webbsearch.retrieval.entitlements import ANONYMOUS
    s = {**live, "layout": {**live["layout"], "group_variants": 1}, "facets": {**live["facets"], "hidden": ["price"]}}
    r = engine.results.results("copper elbow", ANONYMOUS, llm="off", settings=s)
    assert r["layout"]["group_variants"] is True and r["layout"]["facets"]["hidden"] == ["price"]


@pytest.fixture(scope="module")
def bom(engine):
    from webbsearch.retrieval.bom import Bom
    return Bom(engine)


def _line(t, q=1):
    return {"n": 1, "raw": t, "text": t, "qty": q}


def test_brand_cross_and_branch_scope(engine, bom, live):
    from webbsearch.retrieval.entitlements import ANONYMOUS
    s = {**live, **{k: SET[k] for k in ("crosses", "brand_prefs")}}
    m = bom.match(_line("kohler toilet", 2), ANONYMOUS, None, settings=s)
    assert m["brand_cross"]["to"] == "American Standard" and m["options"][0]["brand"] == "American Standard"
    assert m["note"].startswith("Webb crosses Kohler to American Standard") and m["req_brand"] == "Kohler"
    m105 = bom.match(_line("kohler toilet", 2), ANONYMOUS, "105", settings=s)
    assert m105["options"][0]["brand"] == "Gerber"
    plain = bom.match(_line("kohler toilet", 2), ANONYMOUS, None, settings=live)
    assert "brand_cross" not in plain and plain["uncarried"] and plain["req_brand"] == "Kohler"


def test_learned_choice_moves_a_pick_up(engine, bom, live, tmp_path):
    from webbsearch.retrieval.entitlements import ANONYMOUS
    before = bom.match(_line("3/4 sweat ball valve full port", 4), ANONYMOUS, None, settings=live)
    pick = before["options"][3]
    a = Analytics(tmp_path / "a.sqlite")
    for i in range(6):
        _choice(a, f"s{i}", before["key"], pick["sku"], brand=pick["brand"], fam=pick["family"])
    old = getattr(engine, "learned", None)
    engine.learned = Learned(a)
    try:
        engine.learned.refresh()
        after = bom.match(_line("3/4 sweat ball valve full port", 4), ANONYMOUS, None, settings=live)
        pos = [o["sku"] for o in after["options"]].index(pick["sku"])
        assert pos < 3 and any(w["k"] == "learned" for w in after["options"][pos]["why"])
        off = {**live, "learned_off": [f"{before['key']}::{pick['sku']}"]}
        back = bom.match(_line("3/4 sweat ball valve full port", 4), ANONYMOUS, None, settings=off)
        assert [o["sku"] for o in back["options"]].index(pick["sku"]) == 3
    finally:
        engine.learned = old
