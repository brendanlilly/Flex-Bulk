"""Account-scoped structured data access (orders, invoices, lists, pricing).

Every public method takes a Principal and filters by its account (and, for
technicians, by the orders they placed). These methods back both the search
verticals' result cards and the Claude assistant's tools, so there is exactly
one enforcement path.
In production they read from the order/AR services or Snowflake with a
row-access policy keyed on account_id; locally they read canonical parquet.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from webbsearch.retrieval.entitlements import Principal


class AccessDenied(Exception):
    pass


class AccountData:
    def __init__(self, canonical: Path, catalog, today: date):
        rd = lambda n: pd.read_parquet(canonical / f"{n}.parquet")
        self.today = today
        self.cat = catalog
        self.accounts = rd("accounts").set_index("account_id", drop=False)
        self.users = rd("users").set_index("user_id", drop=False)
        names = Path(__file__).resolve().parents[2] / "config" / "demo_names.json"   # demo display-name overrides
        if names.exists():
            import json
            for uid, cols in json.loads(names.read_text(encoding="utf-8")).items():
                if uid in self.users.index and isinstance(cols, dict):
                    for col, val in cols.items():
                        if col in self.users.columns:
                            self.users.loc[uid, col] = val
        from webbsearch.retrieval.branches import load_branches
        self.branches = load_branches(canonical).set_index("branch_id", drop=False)     # real F.W. Webb locations
        o = rd("orders")
        o["order_date"] = pd.to_datetime(o["order_date"])
        self.orders = o.set_index("order_id", drop=False)
        self.orders_by_acct = {a: g["order_id"].to_numpy() for a, g in o.groupby("account_id")}
        self.lines = rd("order_lines")
        self._line_pos = self.lines.groupby("order_id").indices   # order_id -> row positions
        self._orders_by_sku = self.lines.groupby("sku")["order_id"].unique().to_dict()
        inv = rd("invoices")
        for c in ("invoice_date", "due_date"):
            inv[c] = pd.to_datetime(inv[c])
        self.invoices = inv.set_index("invoice_id", drop=False)
        self.inv_by_acct = {a: g["invoice_id"].to_numpy() for a, g in inv.groupby("account_id")}
        self.lists = rd("lists").set_index("list_id", drop=False)
        self.list_items = rd("list_items")
        self.list_counts = self.list_items.groupby("list_id").size().to_dict()
        # last paid price per (account, sku) -> "your price" and "last bought" badges
        lo = self.lines.merge(o[["order_id", "account_id", "user_id", "order_date"]], on="order_id")
        lo = lo.sort_values("order_date")
        last = lo.groupby(["account_id", "sku"]).agg(last_price=("unit_price", "last"),
                                                      last_date=("order_date", "last"),
                                                      times=("order_id", "nunique"),
                                                      qty=("qty", "sum"))
        self.last_bought = {k: v for k, v in zip(last.index, last.to_dict("records"))}
        self._lo = lo
        self._user_hist: dict[str, dict] = {}

    # ---------------------------------------------------------------- guards
    @staticmethod
    def _need(p: Principal, vertical: str):
        if not p.is_authenticated or not p.can(vertical):
            raise AccessDenied(f"role '{p.role}' cannot access {vertical}")

    def _order_ids(self, p: Principal) -> np.ndarray:
        ids = self.orders_by_acct.get(p.account_id, np.array([], dtype=object))
        if p.role == "tech":
            sub = self.orders.loc[ids]
            ids = sub.index[sub["user_id"] == p.user_id].to_numpy()
        return ids

    # ---------------------------------------------------------------- principal
    def principal(self, account_id: str, user_id: str | None = None) -> Principal:
        a = self.accounts.loc[account_id]
        users = self.users[self.users["account_id"] == account_id]
        if user_id is None:
            user_id = users.iloc[0]["user_id"]
        u = self.users.loc[user_id]
        if u["account_id"] != account_id:
            raise AccessDenied("user does not belong to account")
        return Principal(account_id, user_id, u["role"], a["segment"], a["home_branch"], a["price_class"], a["name"])

    # ---------------------------------------------------------------- pricing
    def price(self, p: Principal, i: int) -> dict:
        if not p.is_authenticated:
            return {"price": float(self.cat.list_price[i]), "basis": "list"}
        if not p.sees_prices:
            return {"price": None, "basis": "hidden"}
        lb = self.last_bought.get((p.account_id, self.cat.sku[i]))
        if lb:
            return {"price": round(float(lb["last_price"]), 2), "basis": "last paid"}
        pc = int(str(p.price_class or "PC5")[2:] or 5)
        mult = (1.0 - 0.035 * pc ** 0.5 - 0.02) * 0.67
        return {"price": round(float(self.cat.list_price[i]) * mult, 2), "basis": f"price class {p.price_class}"}

    def user_history(self, user_id: str) -> dict:
        """Per-user purchase history (technicians see only orders they placed)."""
        h = self._user_hist.get(user_id)
        if h is None:
            sub = self._lo[self._lo["user_id"] == user_id]
            g = sub.groupby("sku").agg(last_price=("unit_price", "last"), last_date=("order_date", "last"),
                                       times=("order_id", "nunique"), qty=("qty", "sum"))
            h = {k: v for k, v in zip(g.index, g.to_dict("records"))}
            if len(self._user_hist) > 5000:
                self._user_hist.clear()
            self._user_hist[user_id] = h
        return h

    def history_badge(self, p: Principal, i: int) -> dict | None:
        if not p.is_authenticated:
            return None
        if p.role == "tech":
            lb = self.user_history(p.user_id).get(self.cat.sku[i])
        else:
            lb = self.last_bought.get((p.account_id, self.cat.sku[i]))
        if not lb:
            return None
        return {"times": int(lb["times"]), "last_date": str(lb["last_date"].date()), "qty": float(lb["qty"])}

    # ---------------------------------------------------------------- orders
    def find_orders(self, p: Principal, start: date | None = None, end: date | None = None, status: str | None = None,
                    text: str | None = None, sku: str | None = None, po: str | None = None, limit: int = 25) -> list[dict]:
        self._need(p, "orders")
        o = self.orders.loc[self._order_ids(p)]
        if start:
            o = o[o["order_date"] >= pd.Timestamp(start)]
        if end:
            o = o[o["order_date"] <= pd.Timestamp(end)]
        if status:
            o = o[o["status"] == status.upper()]
        if po:
            o = o[o["po_number"].str.upper() == po.upper()]
        if text:
            t = text.lower()
            o = o[o["job_name"].str.lower().str.contains(t, regex=False) | o["ship_to"].str.lower().str.contains(t, regex=False)
                  | o["po_number"].str.lower().str.contains(t, regex=False)]
        if sku:
            has = set(self._orders_by_sku.get(sku, ()))
            o = o[o.index.isin(has)]
        o = o.sort_values("order_date", ascending=False).head(limit)
        return [self.order_card(r, p) for r in o.to_dict("records")]

    def order_card(self, r: dict, p: Principal | None = None) -> dict:
        n = len(self._line_pos.get(r["order_id"], ()))
        total = round(float(r["total"]), 2) if (p is None or p.sees_prices) else None
        return {"order_id": r["order_id"], "date": str(pd.Timestamp(r["order_date"]).date()), "po": r["po_number"],
                "job": r["job_name"], "status": r["status"], "total": total, "lines": n,
                "branch": self.branches.loc[r["branch_id"], "name"] if r["branch_id"] in self.branches.index else "",
                "channel": r["channel"]}

    def order_detail(self, p: Principal, order_id: str) -> dict:
        self._need(p, "orders")
        if order_id not in set(self._order_ids(p)):
            raise AccessDenied("order not found for this account")  # same message as not-found: no oracle
        r = self.orders.loc[order_id].to_dict()
        card = self.order_card(r, p)
        pos = self._line_pos.get(order_id)
        lines = self.lines.iloc[pos] if pos is not None else None
        items = []
        if lines is not None:
            for l in lines.to_dict("records"):
                i = self.cat.sku_to_idx.get(l["sku"])
                items.append({"sku": l["sku"], "title": self.cat.title[i] if i is not None else l["sku"],
                              "mfr_part": self.cat.mfr_part[i] if i is not None else "", "qty": l["qty"],
                              "unit_price": l["unit_price"] if p.sees_prices else None})
        card["items"] = items
        inv = self.invoices[self.invoices["order_id"] == order_id]
        card["invoices"] = inv["invoice_id"].tolist() if p.can("invoices") else []
        return card

    def purchase_history(self, p: Principal, text: str | None = None, start: date | None = None,
                         end: date | None = None, limit: int = 25) -> list[dict]:
        """What did we buy? Aggregated by SKU over the window, optional text match on title/job."""
        self._need(p, "orders")
        ids = set(self._order_ids(p))
        lo = self._lo[self._lo["order_id"].isin(ids)]
        if start:
            lo = lo[lo["order_date"] >= pd.Timestamp(start)]
        if end:
            lo = lo[lo["order_date"] <= pd.Timestamp(end)]
        if text:
            t = text.lower()
            jobs = self.orders.loc[list(ids)]
            job_ids = set(jobs.index[jobs["job_name"].str.lower().str.contains(t, regex=False)])
            tmask = lo["sku"].map(lambda s: t in self.cat.title[self.cat.sku_to_idx[s]].lower()
                                  if s in self.cat.sku_to_idx else False)
            lo = lo[lo["order_id"].isin(job_ids) | tmask]
        g = lo.groupby("sku").agg(qty=("qty", "sum"), orders=("order_id", "nunique"),
                                  last=("order_date", "max")).sort_values(["orders", "qty"], ascending=False).head(limit)
        out = []
        for sku, r in g.iterrows():
            i = self.cat.sku_to_idx.get(sku)
            out.append({"sku": sku, "title": self.cat.title[i] if i is not None else sku, "orders": int(r["orders"]),
                        "qty": float(r["qty"]), "last_date": str(r["last"].date())})
        return out

    # ---------------------------------------------------------------- invoices
    def invoice_stats(self, p: Principal, status: str | None = None, start: date | None = None,
                      end: date | None = None, min_amount: float | None = None, text: str | None = None) -> dict:
        """Count and sums over ALL matching invoices (answers must not be computed from a truncated page)."""
        rows = self.find_invoices(p, status, start, end, min_amount, text, limit=10 ** 9)
        return {"count": len(rows), "total_amount": round(sum(r["amount"] for r in rows), 2),
                "total_balance": round(sum(r["balance"] for r in rows), 2)}

    def find_invoices(self, p: Principal, status: str | None = None, start: date | None = None,
                      end: date | None = None, min_amount: float | None = None, text: str | None = None,
                      limit: int = 25) -> list[dict]:
        self._need(p, "invoices")
        inv = self.invoices.loc[self.inv_by_acct.get(p.account_id, [])]
        if status:
            inv = inv[inv["status"] == status.upper()]
        if start:
            inv = inv[inv["invoice_date"] >= pd.Timestamp(start)]
        if end:
            inv = inv[inv["invoice_date"] <= pd.Timestamp(end)]
        if min_amount is not None:
            inv = inv[inv["amount"] >= min_amount]
        if text:
            oo = self.orders.loc[inv["order_id"]]
            t = text.lower()
            m = (oo["job_name"].str.lower().str.contains(t, regex=False) |
                 oo["po_number"].str.lower().str.contains(t, regex=False)).to_numpy()
            inv = inv[m]
        inv = inv.sort_values("invoice_date", ascending=False).head(limit)
        return [self.invoice_card(r) for r in inv.to_dict("records")]

    def invoice_card(self, r: dict) -> dict:
        o = self.orders.loc[r["order_id"]] if r["order_id"] in self.orders.index else None
        days_past = (pd.Timestamp(self.today) - r["due_date"]).days
        return {"invoice_id": r["invoice_id"], "order_id": r["order_id"], "date": str(r["invoice_date"].date()),
                "due": str(r["due_date"].date()), "amount": round(float(r["amount"]), 2),
                "balance": round(float(r["balance"]), 2), "status": r["status"],
                "days_past_due": int(days_past) if r["status"] == "PAST_DUE" else 0,
                "po": o["po_number"] if o is not None else "", "job": o["job_name"] if o is not None else ""}

    def account_summary(self, p: Principal) -> dict:
        if not p.is_authenticated:
            raise AccessDenied("sign in required")
        a = self.accounts.loc[p.account_id]
        out = {"account_id": p.account_id, "name": a["name"], "segment": a["segment"], "terms": a["terms"],
               "home_branch": self.branches.loc[a["home_branch"], "name"], "role": p.role}
        if p.can("invoices"):
            inv = self.invoices.loc[self.inv_by_acct.get(p.account_id, [])]
            out["open_balance"] = round(float(inv.loc[inv["status"] != "PAID", "balance"].sum()), 2)
            pdv = inv[inv["status"] == "PAST_DUE"]
            out["past_due_balance"] = round(float(pdv["balance"].sum()), 2)
            out["past_due_count"] = int(len(pdv))
        if p.can("orders"):
            o = self.orders.loc[self._order_ids(p)]
            out["open_orders"] = int((o["status"].isin(["OPEN", "SHIPPED", "BACKORDERED"])).sum())
            out["orders_last_90d"] = int((o["order_date"] >= pd.Timestamp(self.today) - pd.Timedelta(days=90)).sum())
        return out

    # ---------------------------------------------------------------- lists
    def visible_lists(self, p: Principal) -> pd.DataFrame:
        L = self.lists
        m = L["list_type"] == "WEBB_KIT"
        if p.is_authenticated and p.can("lists"):
            m |= (L["account_id"] == p.account_id) & (L["shared"] | (L["owner_user_id"] == p.user_id))
        return L[m]

    def list_detail(self, p: Principal, list_id: str) -> dict:
        L = self.visible_lists(p)
        if list_id not in L.index:
            raise AccessDenied("list not found")
        r = L.loc[list_id]
        items = self.list_items[self.list_items["list_id"] == list_id].sort_values("sort")
        out = []
        for it in items.to_dict("records"):
            i = self.cat.sku_to_idx.get(it["sku"])
            if i is None:
                continue
            out.append({"sku": it["sku"], "title": self.cat.title[i], "qty": it["qty"],
                        "price": self.price(p, i)["price"]})
        return {"list_id": list_id, "name": r["name"], "type": r["list_type"], "shared": bool(r["shared"]),
                "updated": str(r["updated_at"]), "items": out}

    def list_card(self, list_id: str) -> dict:
        r = self.lists.loc[list_id]
        n = int(self.list_counts.get(list_id, 0))
        return {"list_id": list_id, "name": r["name"], "list_type": r["list_type"], "items": n,
                "updated": str(r["updated_at"]), "shared": bool(r["shared"])}
