"""Build all serving indexes from canonical tables.

    python -m webbsearch.index.build
"""
from __future__ import annotations

import sys
import time

import numpy as np

import pandas as pd

from webbsearch.config import settings
from webbsearch.index.catalog import Catalog
from webbsearch.index.embed import LSAEmbedder
from webbsearch.index.local_backend import LocalBackend


def vertical_docs(c: dict[str, pd.DataFrame], catalog: Catalog) -> dict[str, pd.DataFrame]:
    title_of = dict(zip(catalog.sku, catalog.title))
    mpn_of = dict(zip(catalog.sku, catalog.mfr_part))
    from webbsearch.config import settings
    from webbsearch.retrieval.branches import load_branches
    bt = load_branches(settings.canonical_dir)                 # real F.W. Webb location names
    branch = dict(zip(bt["branch_id"], bt["name"]))
    lines = c["order_lines"].sort_values(["order_id", "line_no"])
    lines_by_order = lines.groupby("order_id")["sku"].apply(list).to_dict()
    o = c["orders"]
    od = pd.DataFrame({
        "doc_id": o["order_id"],
        "title": "Order " + o["order_id"] + " PO " + o["po_number"] + " " + o["job_name"].fillna(""),
        "body": [f"{s} {st} {ch} {branch.get(b, '')} " + " ".join(title_of.get(x, "") for x in lines_by_order.get(oid, [])[:12])
                 for oid, s, st, ch, b in zip(o["order_id"], o["ship_to"].fillna(""), o["status"], o["channel"].fillna(""),
                                               o["branch_id"])],
        "ids": [f"{oid} {po} " + " ".join(lines_by_order.get(oid, [])[:30]) + " " +
                " ".join(mpn_of.get(x, "") for x in lines_by_order.get(oid, [])[:30])
                for oid, po in zip(o["order_id"], o["po_number"])],
        "visible_to": [[f"acct:{a}", f"user:{u}"] for a, u in zip(o["account_id"], o["user_id"])],
        "kind": o["status"],
    })
    inv = c["invoices"].merge(o[["order_id", "po_number", "job_name"]], on="order_id", how="left")
    idoc = pd.DataFrame({
        "doc_id": inv["invoice_id"],
        "title": "Invoice " + inv["invoice_id"] + " order " + inv["order_id"] + " PO " + inv["po_number"].fillna("") +
                 " " + inv["job_name"].fillna(""),
        "body": inv["status"].str.replace("_", " ") + " " + inv["terms"].fillna(""),
        "ids": inv["invoice_id"] + " " + inv["order_id"] + " " + inv["po_number"].fillna(""),
        "visible_to": [[f"acct:{a}"] for a in inv["account_id"]],
        "kind": inv["status"],
    })
    li = c["list_items"].sort_values(["list_id", "sort"]).groupby("list_id")["sku"].apply(list).to_dict()
    L = c["lists"]

    def vis(r):
        if r.list_type == "WEBB_KIT":
            return ["public"]
        return [f"acct:{r.account_id}"] if r.shared else [f"user:{r.owner_user_id}"]

    ldoc = pd.DataFrame({
        "doc_id": L["list_id"], "title": L["name"],
        "body": [" ".join(title_of.get(x, "") for x in li.get(i, [])[:25]) for i in L["list_id"]],
        "ids": [" ".join(li.get(i, [])[:60]) for i in L["list_id"]],
        "visible_to": [vis(r) for r in L.itertuples()],
        "kind": L["list_type"],
    })
    ct = c["content"]
    cdoc = pd.DataFrame({"doc_id": ct["content_id"], "title": ct["title"], "body": ct["body"], "ids": "",
                         "visible_to": [["public"]] * len(ct), "kind": ct["content_type"]})
    return {"orders": od, "invoices": idoc, "lists": ldoc, "content": cdoc}


def embedding_mode(argv: list[str], have_model: bool, have_vectors: bool) -> str:
    """load: reuse saved vectors. reencode (--reencode): keep the fitted embedding model, encode the titles again
    (after an analyzer change; the trained ranker stays valid). fit (--reembed, or nothing saved): fit a new model."""
    if "--reembed" in argv or not have_model:
        return "fit"
    if "--reencode" in argv:
        return "reencode"
    return "load" if have_vectors else "fit"


def main():
    t0 = time.time()
    log = lambda m: print(f"[{time.time() - t0:6.0f}s] {m}", flush=True)
    cdir = settings.canonical_dir
    names = ["products", "orders", "order_lines", "invoices", "lists", "list_items", "content", "branches"]
    c = {n: pd.read_parquet(cdir / f"{n}.parquet") for n in names}
    catalog = Catalog(c["products"], keep_text=True)
    log(f"catalog loaded: {catalog.n:,}")
    texts = [f"{t} {b} {c3}" for t, b, c3 in zip(catalog.title, catalog.brand, catalog.cat3)]
    settings.models_dir.mkdir(parents=True, exist_ok=True)
    vec_path = settings.models_dir / "product_vectors.npy"
    mode = embedding_mode(sys.argv, (settings.models_dir / "embedder.pkl").exists(), vec_path.exists())
    if mode == "load":
        vecs = np.load(vec_path)
    elif mode == "reencode":
        emb = LSAEmbedder.load(settings.models_dir / "embedder.pkl")
        vecs = emb.encode(texts)
        np.save(vec_path, vecs)
    else:
        emb = LSAEmbedder().fit(texts)
        emb.save(settings.models_dir / "embedder.pkl")
        vecs = emb.encode(texts)
        np.save(vec_path, vecs)
    log(f"embeddings: {vecs.shape}")
    verts = vertical_docs(c, catalog)
    log("vertical docs assembled")
    LocalBackend.build(settings.index_dir, catalog, vecs, verts, log=log)
    log("done")


if __name__ == "__main__":
    main()
