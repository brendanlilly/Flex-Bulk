"""F.W. Webb branch locations: config/fwwebb_branches.json (the 109 locations on fwwebb.com/locations).

`counter` marks a wholesale counter: the only locations offered for pickup and shown with stock, transfers and
availability. A Frank Webb Home showroom (`showroom`) at a counter changes nothing; a showroom-only location
(Needham, MA) keeps its branch page and nothing else.

The synthetic data (accounts' home branches, orders' pickup branches) uses ids 100-154; the master keeps those ids
on real locations (a made-up town's id went to the nearest real branch), so nothing else needs rebuilding. An id in
the data that the master doesn't have is kept as it was, so a stale data folder still loads.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd

MASTER = Path(__file__).resolve().parents[2] / "config" / "fwwebb_branches.json"
COLS = ["branch_id", "name", "city", "state", "region", "lat", "lon", "counter", "showroom"]


def master() -> list[dict]:
    return json.loads(MASTER.read_text(encoding="utf-8"))["branches"] if MASTER.exists() else []


def load_branches(canonical_dir: Path) -> pd.DataFrame:
    """Real branch list (+ any synthetic id the master lacks), with lat/lon, counter and showroom.
    Flex Bulk: a branch file loaded by flexbulk.loader (marked by branches.own) replaces the list."""
    if (Path(canonical_dir) / "branches.own").exists():
        own = pd.read_parquet(Path(canonical_dir) / "branches.parquet")
        own["branch_id"] = own["branch_id"].astype(str)
        return own[[c for c in COLS if c in own.columns]].reset_index(drop=True)
    m = master()
    synth = pd.read_parquet(Path(canonical_dir) / "branches.parquet") if (Path(canonical_dir) / "branches.parquet").exists() \
        else pd.DataFrame(columns=["branch_id", "name", "city", "state", "region"])
    synth["branch_id"] = synth["branch_id"].astype(str)
    if not m:
        return synth
    df = pd.DataFrame(m)
    df["branch_id"] = df["branch_id"].astype(str)
    df["counter"] = df["counter"].fillna(True).astype(bool) if "counter" in df else True
    extra = synth[~synth["branch_id"].isin(df["branch_id"])]
    if len(extra):
        df = pd.concat([df, extra.assign(lat=None, lon=None, counter=True, showroom=False)], ignore_index=True)
    return df[[c for c in COLS if c in df.columns]].reset_index(drop=True)


def apply_master(tables: dict) -> dict:
    """At canonical build (ingest.loaders): the branches table becomes the real list, the synthetic branch pages in
    content become one page per real location (linking to its fwwebb.com page), and event titles that name a
    synthetic town name its real replacement. Tables change in place; returns {old name: new name}."""
    m = master()
    if not m or "branches" not in tables:
        return {}
    old = dict(zip(tables["branches"]["branch_id"].astype(str), tables["branches"]["name"]))
    new = {b["branch_id"]: b["name"] for b in m}
    renamed = {old[i]: new[i] for i in old if i in new and old[i] != new[i]}
    keep = tables["branches"][~tables["branches"]["branch_id"].astype(str).isin(new)]      # ids the master lacks
    real = pd.DataFrame([{k: b[k] for k in ("branch_id", "name", "city", "state", "region")} for b in m])
    real["branch_id"] = real["branch_id"].astype(tables["branches"]["branch_id"].dtype)
    tables["branches"] = pd.concat([real, keep], ignore_index=True)
    c = tables.get("content")
    if c is not None and len(c):
        pub = c.loc[c["content_type"] == "branch", "published_at"]
        pub = pub.iloc[0] if len(pub) else c["published_at"].iloc[0]
        c = c[c["content_type"] != "branch"].copy()
        n0 = 1 + max(int(str(x).lstrip("C") or 0) for x in c["content_id"])
        rows = []
        for k, b in enumerate(m):
            if b.get("counter", True):
                title = f"F.W. Webb {b['name']} branch"
                body = (f"F.W. Webb branch in {b['city']}, {b['state']}. Wholesale counter, will call and delivery."
                        + (" Frank Webb Home showroom on site." if b.get("showroom") else "")
                        + " Address, phone and counter hours on the branch page.")
            else:
                title = f"Frank Webb Home {b['name']} showroom"
                body = (f"Frank Webb Home kitchen and bath showroom in {b['city']}, {b['state']}. A retail showroom, not a "
                        "wholesale counter: orders aren't picked up here. Address and hours on the branch page.")
            rows.append({"content_id": f"C{n0 + k:06d}", "content_type": "branch", "title": title, "body": body,
                         "url": b.get("url") or "https://www.fwwebb.com/locations", "related_skus": "[]", "audience": "all",
                         "published_at": pub})
        c = pd.concat([c, pd.DataFrame(rows)], ignore_index=True)
        ev = c["content_type"].isin(["event", "promo"])
        if renamed:          # one pass, so a swap (Needham <-> Dedham) doesn't undo itself
            pat = "|".join(re.escape(o) for o in sorted(renamed, key=len, reverse=True))
            c.loc[ev, "title"] = c.loc[ev, "title"].str.replace(pat, lambda m: renamed[m.group(0)], regex=True)
        tables["content"] = c
    return renamed
