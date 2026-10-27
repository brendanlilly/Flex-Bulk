"""Your item, stock and price files -> the tables the matcher reads (data/canonical/).

    python -m flexbulk.loader            # read, check, write
    python -m flexbulk.loader --check    # read and check only

Column names come from config/bulk_sources.yaml. The loader is loud on purpose: matching problems are mostly data
problems (duplicate item numbers, items with no price, stock rows for items that aren't on the list), so it says what
it found before anything is indexed, and saves the same report to data/canonical/load_report.json for the dashboard.

There are no customers, orders or invoices in this app; their tables are written empty (the shared search code
expects them), so matching ranks by the item list, stock, merchandising settings and what staff have chosen before.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import time
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import yaml

from webbsearch.config import ROOT, settings

CONFIG = Path(os.environ.get("FLEXBULK_SOURCES", ROOT / "config" / "bulk_sources.yaml"))
PRODUCT_COLS = ["sku", "brand", "mfr_part", "upc", "title", "description", "cat1", "cat2", "cat3", "family", "attrs",
                "list_price", "uom", "stock_tier", "status"]

# the customer-side tables the shared search code reads, written empty (dates typed so feature SQL still runs)
S, F, I, B, D, T = pa.string(), pa.float64(), pa.int64(), pa.bool_(), pa.date32(), pa.timestamp("us")
EMPTY = {
    "accounts": [("account_id", S), ("name", S), ("segment", S), ("tier", S), ("price_class", S), ("home_branch", S),
                 ("state", S), ("terms", S), ("credit_flag", B), ("open_date", D)],
    "users": [("user_id", S), ("account_id", S), ("role", S), ("first_name", S), ("email", S)],
    "orders": [("order_id", S), ("account_id", S), ("user_id", S), ("po_number", S), ("job_name", S), ("ship_to", S),
               ("branch_id", S), ("order_date", D), ("status", S), ("channel", S), ("total", F)],
    "order_lines": [("order_id", S), ("line_no", I), ("sku", S), ("qty", F), ("unit_price", F), ("ext_price", F)],
    "invoices": [("invoice_id", S), ("order_id", S), ("account_id", S), ("invoice_date", D), ("due_date", D),
                 ("amount", F), ("balance", F), ("status", S), ("terms", S)],
    "lists": [("list_id", S), ("account_id", S), ("owner_user_id", S), ("name", S), ("shared", B), ("list_type", S),
              ("updated_at", D)],
    "list_items": [("list_id", S), ("sku", S), ("qty", F), ("sort", I)],
    "content": [("content_id", S), ("content_type", S), ("title", S), ("body", S), ("url", S), ("related_skus", S),
                ("audience", S), ("published_at", D)],
    "events": [("ts", T), ("account_id", S), ("user_id", S), ("event_type", S), ("sku", S)],
    "search_log": [("ts", T), ("account_id", S), ("user_id", S), ("query", S), ("clicked_sku", S)],
}


def incoming_dir() -> Path:
    return Path(os.environ.get("FLEXBULK_INCOMING") or settings.data_dir / "incoming")


def config() -> dict:
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


# ------------------------------------------------------------------ reading files
def find(pattern: str, folder: Path | None = None) -> Path | None:
    """The newest file matching the pattern (Excel's ~$ lock files and hidden files skipped)."""
    folder = folder or incoming_dir()
    files = [Path(f) for f in glob.glob(str(folder / pattern))
             if not Path(f).name.startswith(("~$", ".")) and Path(f).is_file()]
    return max(files, key=lambda f: f.stat().st_mtime) if files else None


def sniff(path: Path) -> tuple[str, str]:
    """(delimiter, encoding) of a text export, from its first 256 KB: the delimiter the header line uses most
    (| tab , ;), and UTF-8 unless the bytes say otherwise (then Windows-1252, which ERP exports usually are)."""
    import gzip
    opener = gzip.open if path.name.lower().endswith(".gz") else open
    with opener(path, "rb") as fh:
        head = fh.read(262_144)
    try:
        head.decode("utf-8")
        enc = "utf-8-sig"
    except UnicodeDecodeError as e:
        enc = "utf-8-sig" if e.start >= len(head) - 4 else "cp1252"        # a character cut at the sample's end
    first = head.split(b"\n", 1)[0].decode(enc if enc != "utf-8-sig" else "utf-8", "replace")
    return max(["|", "\t", ",", ";"], key=first.count), enc


def read_any(path: Path, sheet: str | None = None, chunksize: int | None = None):
    """CSV / TSV / TXT / pipe-delimited (also gzipped), Excel or Parquet, every column as text. With `chunksize`,
    an iterator of frames (text files; Excel and Parquet come back as one frame in a list)."""
    name = path.name.lower()
    if name.endswith(".parquet"):
        df = pd.read_parquet(path).astype(str).replace({"None": "", "nan": "", "NaT": ""})
        return [df] if chunksize else df
    if name.endswith((".xlsx", ".xlsm", ".xls")):
        df = pd.read_excel(path, sheet_name=sheet or 0, dtype=str, keep_default_na=False)
        return [df] if chunksize else df
    sep, enc = sniff(path)
    # pipe- and tab-delimited exports don't quote: a 1/2" in a value is an inch mark, not the start of a quoted field
    return pd.read_csv(path, sep=sep, dtype=str, keep_default_na=False, encoding=enc, encoding_errors="replace",
                       quoting=3 if sep in "|\t" else 0, chunksize=chunksize, low_memory=False)


def _key(s: str) -> str:
    return re.sub(r"[^a-z0-9#]", "", str(s).lower())


def pick(df: pd.DataFrame, src) -> str | None:
    """The file's column for a source name or list of names (case, spaces and punctuation ignored)."""
    names = [src] if isinstance(src, str) else list(src or [])
    have = {_key(c): c for c in df.columns}
    for n in names:
        if n in df.columns:
            return n
        if _key(n) in have:
            return have[_key(n)]
    return None


def to_float(s: pd.Series) -> pd.Series:
    t = s.astype(str).str.replace(r"[$,\s]", "", regex=True).str.replace(r"^\((.*)\)$", r"-\1", regex=True)
    return pd.to_numeric(t, errors="coerce")


def columns(df: pd.DataFrame, spec: dict, table: str, report: dict) -> pd.DataFrame:
    """The mapped columns under their canonical names (missing optional ones left out)."""
    out, found = {}, {}
    for name, c in spec.items():
        col = pick(df, c.get("src"))
        if col is None:
            if not c.get("optional"):
                srcs = c.get("src")
                raise ValueError(f"{table}: no column for {name} (looked for {srcs if isinstance(srcs, str) else ', '.join(srcs)}). "
                                 f"The file has: {', '.join(map(str, df.columns[:30]))}. Edit config/bulk_sources.yaml.")
            continue
        found[name] = col
        v = df[col]
        t = c.get("t", "str")
        if t == "float":
            out[name] = to_float(v)
        elif t == "int":
            out[name] = to_float(v).round().astype("Int64")
        elif t == "json":
            out[name] = v.astype(str)
        elif t == "label":          # "P - Plumbing" -> "Plumbing"
            out[name] = v.astype(str).str.strip().str.replace(r"^[A-Z0-9]{1,3}\s+-\s+", "", regex=True)
        else:
            out[name] = v.astype(str).str.strip().replace({"nan": "", "None": ""})
    report.setdefault("columns", {})[table] = found
    return pd.DataFrame(out, index=df.index)


# ------------------------------------------------------------------ attributes from descriptions
def derive_attrs(title: str) -> str:
    """Size, material and joint as the description states them ("NIBCO 3/4 in. Copper Sweat Ball Valve" ->
    size 3/4, copper, sweat): what line-fit checks a match against when the file has no attributes."""
    from webbsearch.retrieval import linefit
    try:
        s = linefit.spec(title or "")
    except Exception:  # noqa: BLE001
        return "{}"
    a = {}
    if s.size:
        a["size"] = s.size
    if s.material:
        a["material"] = s.material
    if s.conn:
        a["connection"] = s.conn
    return json.dumps(a)


BLANK_BRANDS = {"miscellaneous", "misc", "unknown", "n/a", "na", "none", "various", "generic", "-"}
SIZE_RX = re.compile(r"(\d+(?:-\d+/\d+)?|\d+/\d+|\d*\.\d+)\s*(?:in\.?|inch(?:es)?|\")?", re.I)


def nice(v: str) -> str:
    """Attribute values as words: "SweatxRotating Flange" -> "Sweat x Rotating Flange", "1-1/2 IN" -> "1-1/2 in."."""
    v = re.sub(r"(?<=[a-z])x(?=[A-Z])", " x ", str(v).strip())
    return re.sub(r"(?<=\d) IN\b\.?", " in.", v)


def compose(raw: pd.DataFrame, it: pd.DataFrame, comp: dict, mapped: set) -> pd.DataFrame:
    """A product-attribute export with no description column: the description (title) from the columns named in
    `compose.title` ({Col} or {Col|Other} for the first non-empty, {brand} for the manufacturer), then
    `compose.title_after` comma-joined; every other column (`attributes: rest`) as a searchable attribute; the
    nominal size line-fit checks a match against from `compose.size`."""
    cols = list(raw.columns)
    attr_cols = [c for c in cols if c not in mapped] if comp.get("attributes", "rest") == "rest" else \
        [c for c in (pick(raw, x) for x in comp.get("attributes") or []) if c]
    vals = raw[attr_cols].to_numpy(dtype=object)
    rows, cs = np.nonzero(vals != "")
    attrs = [dict() for _ in range(len(raw))]
    for r, c in zip(rows.tolist(), cs.tolist()):
        attrs[r][attr_cols[c]] = nice(vals[r, c])[:200]
    # joints as the counter writes them: "Copper x Copper" is also CxC, "Female x Female" FxF (searchable attribute)
    for c in [c for c in (pick(raw, x) for x in comp.get("joint") or []) if c]:
        jv = raw[c].to_numpy(dtype=object)
        for r in range(len(raw)):
            parts = [x for x in re.split(r"\s+x\s+", nice(jv[r])) if x] if jv[r] else []
            if len(parts) >= 2 and all(x[:1].isalpha() for x in parts):
                attrs[r]["joint"] = "x".join(x[0].upper() for x in parts)
    size_cols = [c for c in (pick(raw, x) for x in comp.get("size") or []) if c]
    if size_cols:
        sv = raw[size_cols].to_numpy(dtype=object)
        for r in range(len(raw)):
            for v in sv[r]:
                m = SIZE_RX.fullmatch(v.strip()) if v else None
                if m:
                    attrs[r]["size"] = m.group(1)
                    break
    brand = it["brand"].to_numpy(dtype=object) if "brand" in it else np.array([""] * len(raw), dtype=object)

    def field(tok: str):
        if tok == "brand":
            return brand
        alts = [pick(raw, x.strip()) for x in tok.split("|")]
        out = np.array([""] * len(raw), dtype=object)
        for c in [a for a in alts if a][::-1]:
            v = raw[c].to_numpy(dtype=object)
            out = np.where(v != "", v, out)
        return out
    head = [field(re.sub(r"^\{|\}$", "", t)) for t in comp.get("title") or []]
    after = [field(t) for t in comp.get("title_after") or []]
    titles, descs = [], []
    for r in range(len(raw)):
        t = " ".join(nice(h[r]) for h in head if h[r])
        low = t.lower()
        extra = []
        for a in after:
            v = nice(a[r])
            if v and v.lower() not in low and v not in extra:
                extra.append(v)
        titles.append((t + (", " + ", ".join(extra) if extra else ""))[:240])
        descs.append("; ".join(f"{k}: {v}" for k, v in attrs[r].items() if k != "size")[:1500])
    it = it.copy()
    it["title"] = titles
    it["description"] = descs
    it["attrs"] = [json.dumps(a) for a in attrs]
    return it


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(s).lower()).strip("_") or "general"


# ------------------------------------------------------------------ the tables
def load(write: bool = True, log=print, incoming: Path | None = None, data_dir: Path | None = None,
         cfg: dict | None = None) -> dict:
    """`incoming`, `data_dir`, `cfg`: another folder, output or mapping than the configured ones (tests)."""
    cfg = cfg or config()
    inc = Path(incoming) if incoming else incoming_dir()
    ddir = Path(data_dir) if data_dir else settings.data_dir
    report: dict = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "folder": str(inc), "files": {},
                    "warnings": [], "counts": {}}
    warn = report["warnings"].append

    # ---- items
    ic = cfg["items"]
    f = find(ic["file"], inc)
    if f is None:
        raise FileNotFoundError(f"no item file matching {ic['file']} in {inc}")
    report["files"]["items"] = f.name
    comp, skip = ic.get("compose") or {}, ic.get("skip") or {}
    parts, nraw, nskip, t0 = [], 0, 0, time.time()
    for raw in read_any(f, ic.get("sheet"), chunksize=100_000):
        nraw += len(raw)
        for col, values in skip.items():           # rows that aren't products (skip: {column: [values]})
            c = pick(raw, col)
            if c is not None:
                keep = ~raw[c].isin(values)
                nskip += int((~keep).sum())
                raw = raw[keep]
        part = columns(raw, ic["columns"], "items", report)
        if "brand" in part:
            part["brand"] = part["brand"].where(~part["brand"].str.lower().isin(BLANK_BRANDS), "")
        if "title" not in part:
            if not comp:
                raise ValueError("items: no description column, and no `compose` section in config/bulk_sources.yaml "
                                 f"to build one. The file has: {', '.join(map(str, raw.columns[:30]))}.")
            part = compose(raw, part, comp, set(report["columns"]["items"].values()))
            report["composed"] = True
        parts.append(part)
        if nraw >= 200_000:
            log(f"  {nraw:,} rows read ({time.time() - t0:.0f}s)")
    it = pd.concat(parts, ignore_index=True)
    del parts
    log(f"items: {f.name}, {nraw:,} rows" + (" (descriptions built from the attribute columns)" if report.get("composed") else ""))
    if nskip:
        warn(f"{nskip:,} rows skipped as not products ({', '.join(f'{k}: {v}' for k, v in skip.items())})")
    n0 = len(it)
    it = it[it["sku"].astype(str).str.strip() != ""]
    if len(it) < n0:
        warn(f"{n0 - len(it):,} item rows have no item number and were skipped")
    dup = it["sku"].duplicated()
    if dup.any():
        warn(f"{int(dup.sum()):,} duplicate item numbers (kept the first), e.g. {', '.join(it.loc[dup, 'sku'].head(5))}")
        it = it[~dup]
    if "description" in it:
        it["title"] = it["title"].where(it["title"] != "", it["description"].str.slice(0, 160))
    blank = it["title"] == ""
    if blank.any():
        warn(f"{int(blank.sum()):,} items have no description and were skipped (nothing to match them by)")
        it = it[~blank]
    it = it.reset_index(drop=True)
    p = pd.DataFrame({"sku": it["sku"], "title": it["title"]})
    for c, default in (("brand", ""), ("mfr_part", ""), ("upc", ""), ("description", "")):
        p[c] = it[c].fillna("") if c in it else default
    p["cat1"] = it["cat1"].where(it["cat1"] != "", "General") if "cat1" in it else "General"
    p["cat2"] = it["cat2"].where(it["cat2"] != "", p["cat1"]) if "cat2" in it else p["cat1"]
    p["cat3"] = it["cat3"].where(it["cat3"] != "", p["cat2"]) if "cat3" in it else p["cat2"]
    p["family"] = it["family"].where(it["family"] != "", p["cat3"].map(slug)) if "family" in it else p["cat3"].map(slug)
    p["family"] = p["family"].map(slug)
    p["uom"] = (it["uom"].str.upper().str.strip().replace("", "EA") if "uom" in it else "EA")
    st = it["status"].str.strip().str.upper().str[:1] if "status" in it else pd.Series("A", index=it.index)
    p["status"] = st.where(st.isin(["A", "D", "S"]), "A")
    if "attrs" in it:
        ok = it["attrs"].str.strip().str.startswith("{")
        p["attrs"] = it["attrs"].where(ok, None)
        p.loc[~ok, "attrs"] = [derive_attrs(t) for t in p.loc[~ok, "title"]]
    else:
        t0 = time.time()
        p["attrs"] = [derive_attrs(t) for t in p["title"]]
        log(f"attributes read from descriptions ({time.time() - t0:.0f}s)")
    if not (p["brand"] != "").any():
        warn("no manufacturer column: lines naming a brand can't prefer it; add one to the item file if you have it")
    report["counts"]["items"] = len(p)

    # ---- prices
    lp = it["list_price"] if "list_price" in it else pd.Series(np.nan, index=it.index)
    levels: dict[str, pd.Series] = {}
    pc = cfg.get("prices") or {}
    pf = find(pc.get("file", "prices.*"), inc) if pc else None
    if pf is not None:
        report["files"]["prices"] = pf.name
        praw = read_any(pf, pc.get("sheet"))
        log(f"prices: {pf.name}, {len(praw):,} rows")
        pr = columns(praw, pc["columns"], "prices", report)
        for name, lc in (pc.get("levels") or {}).items():
            col = pick(praw, lc.get("src"))
            if col is not None:
                pr[f"level:{name}"] = to_float(praw[col])
        pr = pr[pr["sku"] != ""].drop_duplicates("sku", keep="last").set_index("sku")
        unknown = ~pr.index.isin(p["sku"])
        if unknown.any():
            warn(f"{int(unknown.sum()):,} price rows are for item numbers not on the item list (ignored)")
        lp = p["sku"].map(pr["list_price"]).where(lambda s: s.notna(), lp.to_numpy())
        for c in pr.columns:
            if c.startswith("level:"):
                levels[c[6:]] = p["sku"].map(pr[c])
    nop = lp.isna() | (lp <= 0)
    if nop.any():
        warn(f"{int(nop.sum()):,} items have no list price (shown as 'price at branch')")
    p["list_price"] = lp.fillna(0.0).astype(float).to_numpy()
    report["counts"]["priced"] = int((~nop).sum())
    report["price_levels"] = list(levels)

    # ---- branches
    bc = cfg.get("branches") or {}
    bf = find(bc.get("file", "branches.*"), inc) if bc else None
    branches = None
    if bf is not None:
        report["files"]["branches"] = bf.name
        br = columns(read_any(bf), bc["columns"], "branches", report)
        br = br[br["branch_id"] != ""].drop_duplicates("branch_id")
        for c in ("city", "state"):
            if c not in br:
                br[c] = ""
        br["region"] = ""
        br["counter"] = True
        br["showroom"] = False
        branches = br
        log(f"branches: {bf.name}, {len(br)} locations")

    # ---- stock
    sc = cfg.get("stock") or {}
    sf = find(sc.get("file", "stock.*"), inc) if sc else None
    stock = pd.DataFrame({"sku": pd.Series(dtype=str), "branch_id": pd.Series(dtype=str), "qty": pd.Series(dtype=float)})
    if sf is not None:
        report["files"]["stock"] = sf.name
        sraw = read_any(sf, sc.get("sheet"))
        log(f"stock: {sf.name}, {len(sraw):,} rows")
        if sc.get("layout") == "wide":
            sku_col = pick(sraw, sc["columns"]["sku"]["src"])
            pre = sc.get("branch_prefix") or ""
            bcols = [c for c in sraw.columns if c != sku_col and str(c).startswith(pre)]
            if sku_col is None or not bcols:
                raise ValueError(f"stock (wide): need an item column and branch columns starting with '{pre}'")
            long = sraw[[sku_col] + bcols].melt(id_vars=sku_col, var_name="branch_id", value_name="qty")
            stock = pd.DataFrame({"sku": long[sku_col].astype(str).str.strip(),
                                  "branch_id": long["branch_id"].astype(str).str.slice(len(pre)).str.strip(),
                                  "qty": to_float(long["qty"])})
            report.setdefault("columns", {})["stock"] = {"sku": sku_col, "branches": f"{len(bcols)} columns {pre}*"}
        else:
            stock = columns(sraw, {k: v for k, v in sc["columns"].items()}, "stock", report)[["sku", "branch_id", "qty"]]
        stock["branch_id"] = stock["branch_id"].str.replace(r"\.0$", "", regex=True)
        stock = stock[(stock["sku"] != "") & stock["qty"].notna() & (stock["qty"] > 0)]
        stock = stock.groupby(["sku", "branch_id"], as_index=False)["qty"].sum()
        unknown = ~stock["sku"].isin(p["sku"])
        if unknown.any():
            warn(f"{int(unknown.sum()):,} stock rows are for item numbers not on the item list (ignored)")
            stock = stock[~unknown]
        from webbsearch.retrieval.branches import master
        known = set(branches["branch_id"]) if branches is not None else {str(b["branch_id"]) for b in master()}
        nb = sorted(set(stock["branch_id"]) - known)
        if nb:
            warn(f"stock names {len(nb)} branch{'es' if len(nb) > 1 else ''} not on the branch list ({', '.join(nb[:8])}); "
                 "they're added without a location, so transfer distances from them are guesses")
        report["counts"]["stock_rows"] = len(stock)
        report["counts"]["items_in_stock"] = int(stock["sku"].nunique())
    else:
        warn("no stock file: every line shows 'ask the branch' for stock")

    # stocking tier: from the file if given, else from how many branches carry the item
    if "stock_tier" in it and (it["stock_tier"] != "").any():
        t = it["stock_tier"].str.lower().str.strip()
        p["stock_tier"] = t.where(t.isin(["stocked", "regional", "dc", "special"]), "dc").to_numpy()
    else:
        nb_total = max(stock["branch_id"].nunique(), 1)
        share = p["sku"].map(stock.groupby("sku")["branch_id"].nunique() / nb_total).fillna(0)
        p["stock_tier"] = np.where(share >= 0.4, "stocked", np.where(share > 0, "regional", "dc"))
    p.loc[p["status"] == "S", "stock_tier"] = "special"
    report["counts"]["tiers"] = p["stock_tier"].value_counts().to_dict()
    p = p[PRODUCT_COLS]

    for w in report["warnings"]:
        log(f"  ! {w}")
    if not write:
        return report

    # ---- write
    cdir = ddir / "canonical"
    cdir.mkdir(parents=True, exist_ok=True)
    p.to_parquet(cdir / "products.parquet", index=False)
    stock.to_parquet(cdir / "stock.parquet", index=False)
    pr_out = pd.DataFrame({"sku": p["sku"], "list_price": p["list_price"]})
    for name, s in levels.items():
        pr_out[name] = s.to_numpy()
    pr_out.to_parquet(cdir / "prices.parquet", index=False)
    if branches is not None:
        branches[["branch_id", "name", "city", "state", "region", "counter", "showroom"]
                 + [c for c in ("lat", "lon") if c in branches]].to_parquet(cdir / "branches.parquet", index=False)
        (cdir / "branches.own").write_text("the branch file replaces Webb's branch list\n")
    else:
        from webbsearch.retrieval.branches import master
        m = master()
        pd.DataFrame([{k: str(b.get(k) or "") for k in ("branch_id", "name", "city", "state", "region")} for b in m]
                     ).to_parquet(cdir / "branches.parquet", index=False)
        (cdir / "branches.own").unlink(missing_ok=True)
        # stock at an id the Webb list lacks: keep it as a location of its own
        extra = sorted(set(stock["branch_id"]) - {str(b["branch_id"]) for b in m})
        if extra:
            b = pd.read_parquet(cdir / "branches.parquet")
            b = pd.concat([b, pd.DataFrame({"branch_id": extra, "name": [f"Branch {x}" for x in extra],
                                            "city": "", "state": "", "region": ""})], ignore_index=True)
            b.to_parquet(cdir / "branches.parquet", index=False)
    for name, cols in EMPTY.items():
        schema = pa.schema(cols)
        pq.write_table(pa.Table.from_pylist([], schema=schema), cdir / f"{name}.parquet")
    today = date.today().isoformat()
    sim = ddir / "sim"
    sim.mkdir(parents=True, exist_ok=True)
    (sim / "meta.json").write_text(json.dumps({"today": today, "feature_asof": today, "n_products": len(p),
                                               "source": "flexbulk.loader"}, indent=1))
    (cdir / "load_report.json").write_text(json.dumps(report, indent=1, default=str))
    log(f"wrote {len(p):,} items, {len(stock):,} stock rows, {len(levels)} price level(s) to {cdir}")
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true", help="read and check the files, write nothing")
    a = ap.parse_args(argv)
    r = load(write=not a.check)
    print(json.dumps(r["counts"], indent=1))


if __name__ == "__main__":
    main()
