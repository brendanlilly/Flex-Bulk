"""Who may see what.

The principal comes from the authenticated session (never from the query, the
UI, or the LLM). Every account-scoped retrieval call takes ACL tokens computed
here; the Claude assistant's tools receive a Principal bound server-side and
have no account_id parameter to tamper with.

Role matrix (mirror of typical B2B portal permissions; configure per Webb policy):

    vertical   owner  buyer  ap   tech
    products    Y      Y     Y    Y      (tech: prices hidden unless enabled)
    prices      Y      Y     Y    N
    orders      all    all   all  own orders only
    invoices    Y      Y     Y    N
    lists       shared + own private + Webb kits
    content     Y      Y     Y    Y
"""
from __future__ import annotations

from dataclasses import dataclass

ROLE_VERTICALS = {
    "owner": {"products", "orders", "invoices", "lists", "content"},
    "buyer": {"products", "orders", "invoices", "lists", "content"},
    "ap": {"products", "orders", "invoices", "content"},
    "tech": {"products", "orders", "lists", "content"},
    "anonymous": {"products", "content", "lists"},
}


@dataclass(frozen=True)
class Principal:
    account_id: str | None
    user_id: str | None
    role: str = "anonymous"
    segment: str | None = None
    home_branch: str | None = None
    price_class: str | None = None
    account_name: str | None = None

    @property
    def is_authenticated(self) -> bool:
        return bool(self.account_id)

    def can(self, vertical: str) -> bool:
        return vertical in ROLE_VERTICALS.get(self.role, set())

    @property
    def sees_prices(self) -> bool:
        return self.role in ("owner", "buyer", "ap")

    def acl(self, vertical: str) -> list[str]:
        """ACL tokens this principal may match in a vertical. Empty = no access."""
        if not self.can(vertical):
            return []
        if vertical == "content":
            return ["public"]
        if vertical == "lists":
            toks = ["public"]
            if self.account_id:
                toks.append(f"acct:{self.account_id}")
            if self.user_id:
                toks.append(f"user:{self.user_id}")
            return toks
        if not self.account_id:
            return []
        if vertical == "orders" and self.role == "tech":
            return [f"user:{self.user_id}"]
        return [f"acct:{self.account_id}"]


ANONYMOUS = Principal(None, None, "anonymous")
