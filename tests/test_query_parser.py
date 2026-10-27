from datetime import date

from webbsearch.retrieval.query_parser import QueryParser, parse_dates

TODAY = date(2026, 9, 25)
P = QueryParser(["Bradford White", "NIBCO", "Rheem"], TODAY)


def test_entities():
    pq = P.parse("status of order 7012345")
    assert pq.order_no == "7012345" and pq.intent in ("orders", "question")
    pq = P.parse("invoice #30001234")
    assert pq.invoice_no == "30001234" and pq.intent == "invoices"
    assert P.parse("PO MAIN-123").po == "MAIN-123"


def test_brand_and_style():
    pq = P.parse("bradford white 50 gal gas")
    assert pq.brands == ["Bradford White"] and pq.intent == "products"
    assert P.parse("NI81478-02LF").style == "part"
    assert P.parse("what do I need to replace a 40 gallon gas water heater").style == "natural"


def test_dates():
    assert parse_dates("orders last month", TODAY) == (date(2026, 8, 1), date(2026, 8, 31))
    assert parse_dates("invoices in march", TODAY) == (date(2026, 3, 1), date(2026, 3, 31))
    assert parse_dates("what did we buy last spring", TODAY) == (date(2026, 3, 1), date(2026, 5, 31))
    assert parse_dates("last 30 days", TODAY)[0] == date(2026, 8, 26)
    assert parse_dates("may i have a valve", TODAY) is None


def test_invoice_intent():
    pq = P.parse("past due invoices")
    assert pq.status == "PAST_DUE" and pq.intent == "invoices"
