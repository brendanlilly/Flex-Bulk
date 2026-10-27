"""v1.9: a search with no candidates, under settings with a Stocked or Popularity weight, returns an empty page instead
of a 500 (max() of an empty array). Found with the jump-drive copy, which carries the dashboard settings of Sep 29
(Stocked 3.3)."""
import numpy as np

from webbsearch.merch.rules import adjust_scores
from webbsearch.merch.store import DEFAULTS


def test_no_candidates_with_stock_and_popularity_weights():
    settings = {**DEFAULTS, "weights": {**DEFAULTS["weights"], "stocked": 3.3, "popularity": 1.0}}
    out = adjust_scores(np.zeros(0), np.zeros((0, 40)), np.zeros(0, np.int64), None, {"stock_rank": 0, "pop_365": 1},
                        settings, personal=False, rules=[])
    assert out.shape == (0,)


def test_one_candidate_still_gets_the_stock_weight():
    settings = {**DEFAULTS, "weights": {**{k: 0.0 for k in DEFAULTS["weights"]}, "stocked": 2.0}}
    X = np.zeros((1, 2)); X[0, 0] = 5.0
    out = adjust_scores(np.array([1.0]), X, np.array([0]), None, {"stock_rank": 0, "pop_365": 1}, settings,
                        personal=False, rules=[])
    assert out.tolist() == [3.0]
