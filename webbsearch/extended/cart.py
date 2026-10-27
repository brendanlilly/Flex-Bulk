"""The cart, planned by how it arrives (design §8): groups, delivery estimates, freight with free-freight status, return
terms, ship-to per group and the order routing it will make (ERP lines, vendor POs, quote requests).

Groups: pickup today · same-day from a nearby branch · transfer (region, CD or company-wide) · from the manufacturer
(one group per manufacturer, since each is its own PO and shipment) · special-order quote requests."""
from __future__ import annotations

import numpy as np

from webbsearch.extended.freight import ACCESSORIALS, JOBSITE_DEFAULT, ShipHawkSim, ffa_status
from webbsearch.extended.service import RETURNS, SHIP_NOTE, sees_prices
from webbsearch.extended.suppliers import WH_LAT, WH_LON
from webbsearch.knowledge.pdfs import _qty
from webbsearch.retrieval.availability import _miles

GROUP_ORDER = ("pickup", "same_day", "transfer", "manufacturer", "quote")
GROUP_LABEL = {"pickup": "Pickup today", "same_day": "Same-day from a nearby branch",
               "transfer": "Transfer to your branch", "manufacturer": "From the manufacturer",
               "quote": "Special order: quote request"}


def clean_ship_to(ship_to) -> dict:
    """Ship-tos from the page, checked: known group keys, a type, a short address, known accessorials only."""
    out = {}
    if not isinstance(ship_to, dict):
        return out
    for k, v in list(ship_to.items())[:50]:
        if not isinstance(v, dict) or not str(k).startswith("manufacturer:"):
            continue
        st = {"type": "jobsite" if v.get("type") == "jobsite" else "branch",
              "address": str(v.get("address") or "")[:200]}
        acc = v.get("accessorials")
        if isinstance(acc, list):
            st["accessorials"] = [a for a in dict.fromkeys(str(x) for x in acc[:10]) if a in ACCESSORIALS]
        out[str(k)[:40]] = st
    return out


class CartPlanner:
    def __init__(self, xt):
        self.xt = xt
        self.rater = ShipHawkSim()

    def _line_coded(self, i: int, sku: str, qty: int, p, branch) -> list[dict]:
        e = self.xt.e
        price = e.accounts.price(p, i)["price"]
        title, brand = str(e.catalog.title[i]), str(e.catalog.brand[i])
        t = self.xt.coded_tiers([i], branch, qty)
        at = int(t["at_branch"][0])
        base = {"sku": sku, "title": title, "brand": brand, "price": price, "ec": False}
        out = []
        if 0 < at < qty:            # part on the shelf; the rest by its own tier, the shelf's units not counted twice
            out.append(base | {"qty": at, "tier": "1a", "lo": 0, "hi": 0})
            t = self.xt.coded_tiers([i], branch, qty - at, held=at)
            qty -= at
        out.append(base | {"qty": qty, "tier": str(t["tier"][0]), "lo": int(t["lo"][0]), "hi": int(t["hi"][0]),
                           "at_branch": int(t["at_branch"][0]), "long_lead": bool(t["long_lead"][0])})
        return out

    def _line_ec(self, sku: str, qty: int, branch, p) -> list[dict]:
        xt = self.xt
        price, r, snap = xt.lookup(sku, p)
        if r is None:
            return []
        t = snap.tiering.extended(snap.store, np.array([r["row"]]), branch, qty, xt.cfg()["stale_hours"],
                                  snap.sup_of_store, snap.aged_h())
        sup = snap.sup.list[int(snap.sup_of_store[r["brand"]])]
        return [{"sku": r["sku"], "title": r["title"], "brand": r["brand_name"], "price": price, "ec": True,
                 "qty": qty, "tier": str(t["tier"][0]), "lo": int(t["lo"][0]), "hi": int(t["hi"][0]), "row": r,
                 "long_lead": bool(t["long_lead"][0]), "vendor": sup.code}]

    def plan(self, lines: list[dict], p, branch=None, ship_to: dict | None = None) -> dict:
        """`lines`: [{sku, qty}]. `ship_to`: {group key: {"type": "branch"|"jobsite", "address": str,
        "accessorials": [...]}}; manufacturer groups are keyed "manufacturer:<vendor code>"."""
        xt, e = self.xt, self.xt.e
        ship_to = clean_ship_to(ship_to)
        bi = e.availability.branch_index(branch)
        bname = str(e.availability.names[bi]).split(",")[0]
        planned, skipped = [], []
        for ln in lines[:200]:
            sku, qty = str(ln.get("sku", ""))[:60], _qty(ln.get("qty"))
            i = e.catalog.sku_to_idx.get(sku)
            if i is not None:
                planned += self._line_coded(int(i), sku, qty, p, branch)
            elif sku.upper().endswith("-EC") and xt.visible(p, xt.cfg()):
                got = self._line_ec(sku, qty, branch, p)
                planned += got
                if not got:
                    skipped.append(sku)
            else:
                skipped.append(sku)
        groups: dict[str, dict] = {}
        for ln in planned:
            tier = ln["tier"]
            if tier == "1a":
                key = "pickup" if ln.get("at_branch", ln["qty"]) >= ln["qty"] or ln.get("at_branch") is None else "same_day"
            elif tier in ("1b", "1c"):
                key = "transfer"
            else:
                code = ln.get("vendor") or xt.sup.list[xt.sup.id.get(ln["brand"], 0)].code
                key = f"manufacturer:{code}"
            g = groups.setdefault(key, {"key": key, "kind": key.split(":")[0], "lines": [], "vendor": None})
            g["lines"].append(ln)
        out_groups = []
        merch_total, freight_total, estimated, freight_unknown = 0.0, 0.0, False, False
        vendor_pos, erp = [], []
        for key in sorted(groups, key=lambda k: (GROUP_ORDER.index(k.split(":")[0]), k)):
            g = groups[key]
            kind = g["kind"]
            lines_out = []
            sub = 0.0
            for ln in g["lines"]:
                ext = None if ln["price"] is None else round(ln["price"] * ln["qty"], 2)
                if ext is None:
                    estimated = True
                else:
                    sub += ext
                lines_out.append({"sku": ln["sku"], "title": ln["title"], "qty": ln["qty"], "price": ln["price"],
                                  "extended": ext, "tier": ln["tier"], "ec": ln["ec"],
                                  "price_note": None if ln["price"] is not None else
                                  "Price confirmed at order" if sees_prices(p) else "Price hidden for your role",
                                  "availability": xt.card(ln["tier"], ln["lo"], ln["hi"], ln["brand"], bname,
                                                          ln.get("at_branch", 0), ln["qty"], ln.get("long_lead", False))})
                if ln["ec"]:
                    erp.append({"sku": ln["sku"], "qty": ln["qty"], "line_type": "NonStock", "ec_number": ln["sku"],
                                "vendor_po": True})
                elif ln["tier"].startswith("2"):
                    erp.append({"sku": ln["sku"], "qty": ln["qty"], "line_type": "Non-stock (coded)",
                                "vendor_po": True})
                else:
                    erp.append({"sku": ln["sku"], "qty": ln["qty"], "line_type": "Stock",
                                "vendor_po": False, "fulfil": GROUP_LABEL[kind]})
            merch_total += sub
            hi = max((ln["hi"] for ln in g["lines"]), default=0)
            unknown_days = any(ln["hi"] < 0 for ln in g["lines"])
            grp = {"key": key, "kind": kind, "label": GROUP_LABEL[kind], "lines": lines_out, "subtotal": round(sub, 2),
                   "arrives": "today" if kind == "pickup" else None if unknown_days else xt.tiering.arrives(hi),
                   "days": None if unknown_days else hi,
                   "returns": RETURNS["3"] if any(ln["ec"] for ln in g["lines"]) else
                   RETURNS["2"] if kind == "manufacturer" else RETURNS["1"],
                   "ship_to": {"type": "branch", "name": bname}, "freight": None}
            if kind == "manufacturer":
                code = key.split(":", 1)[1]
                sup = xt.sup.by_code[code]
                grp["label"] = f"From {sup.brand}"
                st = ship_to.get(key) or {}
                to_job = st.get("type") == "jobsite"
                acc = list(st.get("accessorials", JOBSITE_DEFAULT)) if to_job else []      # cleaned: clean_ship_to
                grp["ship_to"] = ({"type": "jobsite", "address": str(st.get("address", ""))[:200], "accessorials": acc}
                                  if to_job else {"type": "branch", "name": bname})
                fr = self._freight(g["lines"], bi, to_job, acc)
                ffa = ffa_status(sub, sup.ffa_min, sup.brand)
                if not sees_prices(p):          # a role without prices sees no amounts, freight terms only
                    ffa = {"free": False, "min": None, "short": None,
                           "text": f"{sup.brand} freight terms: your branch confirms them with the order"}
                coded_only = all(not ln["ec"] for ln in g["lines"])
                if coded_only and not to_job:
                    fr = {"status": "inbound", "amount": 0.0,
                          "text": "On Webb's inbound freight to your branch"}
                elif ffa["free"]:
                    fr = {"status": "free", "amount": 0.0, "text": ffa["text"], "rate": fr.get("rate")}
                elif fr["status"] == "unknown":
                    estimated = freight_unknown = True
                    fr["text"] = f"Freight: confirmed after you order. {SHIP_NOTE}."
                elif not sees_prices(p):
                    estimated = True
                    fr = {"status": "hidden", "amount": None, "text": "Freight: your branch confirms it with the order"}
                else:
                    freight_total += fr["amount"]
                    estimated = True                # a rate is an estimate until the invoice
                    fr["text"] = f"Estimated freight ${fr['amount']:,.2f} ({fr['rate']['service']})"
                grp["freight"] = fr | {"ffa": ffa}
                vendor_pos.append({"vendor": sup.brand, "vendor_code": code, "lines": len(g["lines"]),
                                   "subtotal": round(sub, 2), "ship_to": grp["ship_to"]["type"],
                                   "free_freight": ffa["free"]})
            out_groups.append(grp)
        needs_ack = any(ln["ec"] or ln["tier"][:1] in ("2", "3") for ln in planned)
        return {"branch": bname, "groups": out_groups, "skipped": skipped,
                "totals": {"merchandise": round(merch_total, 2), "freight": round(freight_total, 2),
                           "freight_unknown": freight_unknown,
                           "freight_note": "Freight on some lines is confirmed after you order" if freight_unknown else None,
                           "tax": None, "tax_note": "Tax is calculated at order (by ship-to)",
                           "total": round(merch_total + freight_total, 2), "estimated": estimated},
                "returns_ack_required": needs_ack,
                "returns_ack_text": ("Some items are ordered from the manufacturer for you. Coded non-stock items "
                                     "return within 30 days with a 25% restocking fee; Extended Catalog items are not "
                                     "returnable unless defective.") if needs_ack else None,
                "routing": {"erp_lines": erp, "vendor_pos": vendor_pos, "quotes": []}}

    def _freight(self, lines: list[dict], bi: int, to_job: bool, acc: list[str]) -> dict:
        items, origin = [], None
        for ln in lines:
            r = ln.get("row")
            if r is None or r["wt"] <= 0:
                return {"status": "unknown", "amount": None}
            origin = r["origin"] if origin is None else origin
            items.append({"sku": ln["sku"], "weight_lb": r["wt"] / 10.0, "quantity": ln["qty"],
                          "dims_in": [r["dl"] / 10.0, r["dw"] / 10.0, r["dh"] / 10.0],
                          "freight_class": r["fclass"] or 100, "hazmat": bool(r["flags"] & 1)})
        av = self.xt.e.availability
        miles = float(_miles(WH_LAT[origin], WH_LON[origin], av.lat[bi], av.lon[bi]))
        res = self.rater.rate({"origin": {"miles_to_destination": miles},
                               "destination": {"type": "jobsite" if to_job else "branch"},
                               "items": items, "accessorials": acc})
        if not res["rates"]:
            return {"status": "unknown", "amount": None}
        best = res["rates"][0]
        return {"status": "estimate", "amount": best["price"], "rate": best, "mode": res["mode"],
                "options": res["rates"][:3]}
