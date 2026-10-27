"""v1.4: merchandising settings, rules, analytics and the dashboard API guard.

Store / rules / analytics tests run anywhere; `engine` tests need the index and check that the settings act on
real result pages (pins, hides, rewrites, previously-purchased weight, brand cap)."""
import time
from types import SimpleNamespace as NS

import numpy as np
import pytest
from fastapi import HTTPException

from webbsearch.merch.analytics import Analytics
from webbsearch.merch.history import metrics
from webbsearch.merch.rules import (active_rules, adjust_scores, bulk_weights, diversify, place_pins, rewrite_query,
                                    rule_matches)
from webbsearch.merch.store import DEFAULTS, Conflict, MerchStore, validate_rewrite, validate_rule


# ------------------------------------------------------------------ store
def test_defaults_raise_previously_purchased(tmp_path):
    s = MerchStore(tmp_path).get()
    assert s["weights"]["purchased"] > 0 and s["weights"]["purchased_times"] > 0
    assert all(s["personalization"].values())
    assert s["layout"]["brand_cap"] == 4


def test_update_logs_and_reloads(tmp_path):
    m = MerchStore(tmp_path)
    s = m.update({"weights": {"purchased": 1.5}}, who="Pat", note="more weight")
    assert s["version"] == 1 and s["updated_by"] == "Pat"
    assert MerchStore(tmp_path).get()["weights"]["purchased"] == 1.5          # persisted
    ch = m.changes()
    assert ch[0]["version"] == 1 and "weights.purchased: 0.8 → 1.5" in ch[0]["changes"] and "snapshot" not in ch[0]


def test_no_change_no_version(tmp_path):
    m = MerchStore(tmp_path)
    assert m.update({"weights": {"purchased": DEFAULTS["weights"]["purchased"]}})["version"] == 0


@pytest.mark.parametrize("patch", [{"weights": {"purchased": 9}}, {"weights": {"nope": 1}}, {"bulk": {"on_shelf": -1}},
                                   {"layout": {"brand_cap": 40}}])
def test_limits(tmp_path, patch):
    with pytest.raises(ValueError):
        MerchStore(tmp_path).update(patch)


def test_conflict_when_someone_else_saved(tmp_path):
    m = MerchStore(tmp_path)
    m.update({"weights": {"popularity": 0.5}}, base_version=0)
    with pytest.raises(Conflict):
        m.update({"weights": {"popularity": 1.0}}, base_version=0)


def test_revert_and_reset(tmp_path):
    m = MerchStore(tmp_path)
    m.update({"weights": {"stocked": 1.0}})
    m.update({"weights": {"stocked": 2.0}})
    assert m.revert(1)["weights"]["stocked"] == 1.0 and m.get()["version"] == 3
    assert m.reset("weights")["weights"] == DEFAULTS["weights"]
    with pytest.raises(ValueError):
        m.revert(99)


def test_merged_is_a_preview(tmp_path):
    m = MerchStore(tmp_path)
    new, changed = m.merged({"personalization": {"reasons": False}})
    assert new["personalization"]["reasons"] is False and changed
    assert m.get()["personalization"]["reasons"] is True and m.get()["version"] == 0


def test_rule_validation():
    r = validate_rule({"match": {"type": "exact", "q": "  3/4  Ball Valve "}, "pin": [{"sku": "123", "pos": 40}],
                       "boost": [{"field": "brand", "value": "Viega", "weight": 3}], "banner": {"text": "Promo", "link": "/promo"}})
    assert r["match"]["q"] == "3/4 ball valve" and r["pin"][0]["pos"] == 24 and r["id"]
    for bad in [{"match": {"type": "exact", "q": ""}}, {"match": {"type": "regex", "q": "x"}},
                {"match": {"type": "any"}, "boost": [{"field": "color", "value": "red", "weight": 1}]},
                {"match": {"type": "any"}, "boost": [{"field": "brand", "value": "X", "weight": 50}]},
                {"match": {"type": "any"}, "banner": {"text": "hi", "link": "javascript:alert(1)"}},
                {"match": {"type": "any"}, "banner": {"text": "hi", "link": "//evil.example"}},
                {"match": {"type": "any"}, "start": "next week"}]:
        with pytest.raises(ValueError):
            validate_rule(bad)


def test_rewrite_validation():
    assert validate_rewrite({"from": " TSTAT ", "to": "thermostat"})["from"] == "tstat"
    with pytest.raises(ValueError):
        validate_rewrite({"from": "x", "to": ""})


# ------------------------------------------------------------------ rules
def test_rewrite_whole_words_one_way():
    s = {"rewrites": [{"from": "bv", "to": "ball valve", "enabled": True}, {"from": "tstat", "to": "thermostat", "enabled": False}]}
    assert rewrite_query("3/4 BV lead free", s)[0] == "3/4 ball valve lead free"
    assert rewrite_query("bvx fitting", s)[0] == "bvx fitting"              # not inside a word
    assert rewrite_query("tstat", s)[0] == "tstat"                          # disabled
    assert rewrite_query("ball valve", s) == ("ball valve", [])             # one way


def test_rule_matching_and_dates():
    today = __import__("datetime").date(2026, 10, 15)
    r = validate_rule({"match": {"type": "contains", "q": "ball valve"}, "start": "2026-10-01", "end": "2026-10-31"})
    assert rule_matches(r, "3/4 ball valve", today) and not rule_matches(r, "ball valves", today)
    assert not rule_matches(r, "ball valve", __import__("datetime").date(2026, 11, 1))
    ex = validate_rule({"match": {"type": "exact", "q": "angle stop"}})
    assert rule_matches(ex, "angle stop", today) and not rule_matches(ex, "angle stop 1/2", today)
    off = validate_rule({"match": {"type": "any"}, "enabled": False})
    assert active_rules({"rules": [r, ex, off]}, "Angle  Stop", today) == [ex]


def test_place_pins_and_hide():
    cat = NS(sku=np.array(["a", "b", "c", "d", "e"], dtype=object))
    idx = np.arange(5)
    perm, pinned = place_pins(idx, cat, [("e", 1), ("c", 3)], {"b"})
    assert [cat.sku[i] for i in idx[perm]] == ["e", "a", "c", "d"] and pinned == {4, 2}


def test_diversify_caps_a_brand():
    order = diversify(list("AAAAAABBC"), 2, window=5)
    top = [list("AAAAAABBC")[k] for k in order[:5]]
    assert top.count("A") == 2 and sorted(order) == list(range(9))
    assert diversify(list("AAAA"), 2, window=4) == [0, 1, 2, 3]            # nothing else: the window still fills
    assert diversify(list("AB"), 0) == [0, 1]


def test_adjust_scores_previously_purchased():
    F = {"acct_bought_n": 0, "acct_bought_recency": 1, "acct_brand_share": 2, "pop_365": 3, "stock_rank": 4}
    X = np.array([[0, 0, 0, 10, 3], [3, 0.9, 0.5, 1, 1]], dtype=np.float32)   # item 1 was bought 3x recently
    s0 = np.array([1.0, 0.0])
    cat = NS(brand=np.array(["A", "B"], dtype=object), family=np.array(["f", "f"], dtype=object),
             cat3=np.array(["F", "F"], dtype=object), sku=np.array(["1", "2"], dtype=object))
    st = {k: dict(v) if isinstance(v, dict) else v for k, v in DEFAULTS.items()}
    s = adjust_scores(s0, X, np.arange(2), cat, F, st, True, [])
    assert s[1] > s[0]                                                        # the bought item now ranks first
    assert (adjust_scores(s0, X, np.arange(2), cat, F, st, False, []) == s0).all()   # guests: no change
    st["personalization"]["purchase_boost"] = False
    assert (adjust_scores(s0, X, np.arange(2), cat, F, st, True, []) == s0).all()
    rule = validate_rule({"match": {"type": "any"}, "boost": [{"field": "brand", "value": "a", "weight": -5}]})
    assert adjust_scores(s0, X, np.arange(2), cat, F, st, True, [rule])[0] == -4.0


def test_bulk_personal_switch():
    st = {"bulk": dict(DEFAULTS["bulk"]), "personalization": {"bulk_personal": False}}
    b = bulk_weights(st)
    assert b["bought_before"] == 0 and b["preferred_brand"] == 0 and b["on_shelf"] == DEFAULTS["bulk"]["on_shelf"]


# ------------------------------------------------------------------ analytics
P = NS(account_id="200295", user_id="50680", role="owner", segment="hvac_contractor")
ANON = NS(account_id=None, user_id=None, role=None, segment=None)


def test_analytics_round_trip(tmp_path):
    a = Analytics(tmp_path / "a.sqlite")
    s1 = a.log_search(q="Copper Elbow", principal=P, total=120, latency_ms=80, personalized=True, top=["1", "2"])
    s2 = a.log_search(q="zzqx", principal=ANON, total=150, latency_ms=40, personalized=False, outcome="fallback")
    a.log_search(q="nothing", principal=ANON, total=0, latency_ms=30, personalized=False)
    a.log_event("click", search_id=s1, principal=P, sku="2", pos=2)
    a.log_event("add_to_cart", search_id=s1, principal=P, sku="2", pos=2, qty=3, value=30.0)
    a.log_event("bulk_line", principal=P, meta={"status": "none", "missing": "wax ring", "text": "2 wax rings"})
    a.log_event("ask", principal=P, meta={"q": "what fits a 48x24 pad"})
    assert a.same_search(s1, "copper   elbow") and not a.same_search(s1, "copper tee") and a.search_exists(s2)
    ins = a.insights(7)
    k = ins["kpis"]
    assert k["searches"] == 3 and k["zero_rate"] == round(2 / 3, 4) and k["ctr"] == round(1 / 3, 4) and k["avg_pos"] == 2
    assert {z["q"] for z in ins["zero_results"]} == {"zzqx", "nothing"}
    assert ins["bulk"]["not_in_catalog"][0]["what"] == "wax ring" and ins["ask"][0]["q"].startswith("what fits")
    assert ins["personalization"]["personalized"]["ctr"] == 1.0
    assert a.query_detail("copper elbow")["skus"][0] == {"sku": "2", "clicks": 1, "atc": 1}
    assert a.recent(5)[0]["q"] == "nothing"
    assert a.clear() == {"searches": 3, "events": 4, "feedback": 0} and a.counts()["searches"] == 0


def test_analytics_rejects_unknown_events(tmp_path):
    with pytest.raises(ValueError):
        Analytics(tmp_path / "a.sqlite").log_event("drop_table", principal=ANON)


def test_audit_metrics():
    m = metrics([1, 2, 5, 30, None])
    assert m["hit1"] == 0.2 and m["hit3"] == 0.4 and m["hit10"] == 0.6 and m["missing"] == 0.2
    assert m["positions"]["not found"] == 1 and m["mrr"] == round((1 + 0.5 + 0.2 + 1 / 30) / 5, 3)


# ------------------------------------------------------------------ dashboard guard
def _req(host="127.0.0.1", token=None):
    return NS(client=NS(host=host), headers={"x-admin-token": token} if token else {})


def test_admin_needs_loopback_and_token():
    from webbsearch.api import admin
    admin.require(_req(token=admin.TOKEN))                  # ok
    for r in (_req(token=None), _req(token="wrong"), _req(host="10.0.0.5", token=admin.TOKEN)):
        with pytest.raises(HTTPException) as ex:
            admin.require(r)
        assert ex.value.status_code == 403


def test_admin_page_not_served_remotely():
    from webbsearch.api import admin
    assert admin.admin_page(_req(host="10.0.0.5")).status_code == 403
    html = admin.admin_page(_req()).body.decode()
    assert admin.TOKEN in html and "__ADMIN_TOKEN__" not in html


# ------------------------------------------------------------------ on real result pages
@pytest.fixture(scope="module")
def live(engine):
    return engine.merch.get()


def _skus(r, n=24):
    return [it["sku"] for it in r["items"][:n]]


def test_pin_hide_banner_on_results(engine, live):
    from webbsearch.retrieval.entitlements import ANONYMOUS
    base = engine.results.results("angle stop", ANONYMOUS, llm="off", settings=live)
    s0, s5 = base["items"][0]["sku"], base["items"][5]["sku"]
    rule = validate_rule({"match": {"type": "exact", "q": "angle stop"}, "pin": [{"sku": s5, "pos": 1}], "hide": [s0],
                          "banner": {"text": "Stops in stock", "link": "/stops"}})
    r = engine.results.results("angle stop", ANONYMOUS, llm="off", settings={**live, "rules": [rule]})
    assert r["items"][0]["sku"] == s5 and r["items"][0]["pinned"]
    assert s0 not in [it["sku"] for it in r["items"]]
    assert r["merch"]["banner"]["text"] == "Stops in stock" and r["merch"]["rules"][0]["id"] == rule["id"]


def test_pin_an_item_the_search_did_not_find(engine, live):
    from webbsearch.retrieval.entitlements import ANONYMOUS
    other = engine.results.results("wax ring", ANONYMOUS, llm="off", settings=live)["items"][0]["sku"]
    rule = validate_rule({"match": {"type": "exact", "q": "angle stop"}, "pin": [{"sku": other, "pos": 2}]})
    r = engine.results.results("angle stop", ANONYMOUS, llm="off", settings={**live, "rules": [rule]})
    assert r["items"][1]["sku"] == other


def test_rewrite_on_results(engine, live):
    from webbsearch.retrieval.entitlements import ANONYMOUS
    s = {**live, "rewrites": [validate_rewrite({"from": "bv", "to": "ball valve"})]}
    r = engine.results.results("3/4 bv", ANONYMOUS, llm="off", settings=s)
    assert r["query"] == "3/4 ball valve" and r["merch"]["rewritten_from"] == "3/4 bv"
    assert "Ball Valve" in r["items"][0]["title"]


def test_brand_cap(engine, live):
    from webbsearch.retrieval.entitlements import ANONYMOUS
    from collections import Counter
    r = engine.results.results("3/4 ball valve", ANONYMOUS, llm="off", settings=live)
    assert max(Counter(it["brand"] for it in r["items"][:24]).values()) <= live["layout"]["brand_cap"]
    named = engine.results.results("nibco ball valve", ANONYMOUS, llm="off", settings=live)
    assert "diversified" not in named                        # a brand search isn't capped
