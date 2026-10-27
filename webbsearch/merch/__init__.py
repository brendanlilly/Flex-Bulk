"""Merchandising, ranking weights, personalization switches and search analytics for Webb employees.

store.py      settings (weights, personalization, bulk order weights, rules, rewrites) with a change log
rules.py      how the settings act on a search: rewrites, score adjustments, pins, hides, banners
analytics.py  search / click / cart / bulk order / Ask event log (SQLite) and the insights read from it
history.py    insights from the historical search log (SEARCH_LOG) and the relevance audit that replays it
"""
