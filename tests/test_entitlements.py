from webbsearch.llm.assistant import TOOLS
from webbsearch.retrieval.entitlements import ANONYMOUS, Principal


def test_acl_tokens_by_role():
    owner = Principal("A1", "U1", "owner")
    tech = Principal("A1", "U2", "tech")
    ap = Principal("A1", "U3", "ap")
    assert owner.acl("orders") == ["acct:A1"]
    assert tech.acl("orders") == ["user:U2"]            # technicians see only their own orders
    assert tech.acl("invoices") == []                   # and no invoices
    assert ap.acl("lists") == []                        # AP role has no lists
    assert set(owner.acl("lists")) == {"public", "acct:A1", "user:U1"}
    assert ANONYMOUS.acl("orders") == [] and ANONYMOUS.acl("content") == ["public"]
    assert not tech.sees_prices and owner.sees_prices


def test_assistant_tools_cannot_select_account():
    for t in TOOLS:
        props = t["input_schema"].get("properties", {})
        assert not {"account_id", "account", "user_id", "customer"} & set(props), t["name"]
