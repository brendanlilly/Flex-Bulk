"""Build the Extended Catalog: the -EC items, their columns and their own search index (design §4, §7, §12).

Production: Snowflake publishes the -EC items that pass the core-field check; an index job (OpenSearch, its own index
and shards) loads them. Here: a deterministic synthetic generator built from this copy's catalog, writing

  <out>/cols/*.npy        one row per -EC item, memory-mapped at serving time (nothing loaded into RAM)
  <out>/strings-<k>.bin   sku, part and title per row, with offsets in cols/str_off.npy
  <out>/index-<k>/        a Tantivy index per shard (published items only), searched in parallel
  <out>/manifest.json     counts, shards, families, brands, build time

Run:  python -m webbsearch.extended.build --n 10000000 --out data/extended/builds/<id>
The core catalog's part numbers are never reused: a coded item always wins over an -EC copy of itself."""
from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import re
import shutil
import sys
import time
from pathlib import Path

import numpy as np

from webbsearch.extended import gaps
from webbsearch.extended.suppliers import Suppliers, mix, unif
from webbsearch.retrieval.analyzer import analyze, compact_id, expand_concepts

SCHEMA_VERSION = 1
FLAG_HAZMAT, FLAG_IMAGE, FLAG_COMPLY_NEED, FLAG_COMPLY_OK, FLAG_PUBLISHED, FLAG_GROUP, FLAG_GAP = 1, 2, 4, 8, 16, 32, 64
COLS = {"tmpl": np.int32, "brand": np.int16, "fam": np.int16, "price_c": np.int32, "conf": np.uint8,
        "fqty": np.int32, "fage": np.uint16, "lead": np.int16, "wt": np.uint32, "dl": np.uint16, "dw": np.uint16,
        "dh": np.uint16, "fclass": np.uint16, "origin": np.int8, "flags": np.uint8}

POTABLE_GAS = {"ball_valve", "backflow", "cu_fitting_press", "cu_fitting_sweat", "cu_pipe", "faucet", "faucet_parts",
               "gas", "pex_fitting", "pex_tubing", "prv", "push_fitting", "stop_valve", "mixing_valve", "waterworks",
               "relief_valve", "wh_tank", "wh_tankless", "gate_check_valve", "expansion_tank"}
HAZMAT = {"refrigerant": 1.0, "sundries": 0.3}
WEIGHT = {"ac_condenser": (120, 280), "backflow": (3, 120), "ball_valve": (0.5, 40), "baseboard": (5, 40),
          "boiler": (180, 600), "boiler_parts": (0.3, 12), "buttweld": (1, 120), "capacitor": (0.2, 1.2),
          "circulator": (6, 40), "contactor": (0.4, 2), "cu_fitting_press": (0.1, 4), "cu_fitting_sweat": (0.05, 6),
          "cu_pipe": (8, 120), "disposal": (10, 25), "drainage": (1, 40), "duct": (2, 30), "dwv_fitting": (0.2, 15),
          "expansion_tank": (6, 60), "faucet": (3, 15), "faucet_parts": (0.1, 3), "filter": (5, 20),
          "flange": (2, 90), "furnace": (110, 190), "gas": (0.5, 6), "gate_check_valve": (1, 150),
          "grille": (1, 8), "grooved": (1, 60), "hangers": (0.1, 3), "hvac_parts": (0.3, 25),
          "iron_fitting": (0.2, 30), "minisplit": (40, 160), "mixing_valve": (1, 15), "motor": (5, 35),
          "nohub": (1, 40), "pex_fitting": (0.05, 1.5), "pex_tubing": (5, 60), "prv": (2, 40), "pumps": (15, 80),
          "push_fitting": (0.1, 3), "pvc_pressure": (0.05, 8), "radiant": (2, 25), "refrigerant": (25, 60),
          "relief_valve": (0.5, 4), "sink": (15, 90), "ss_fitting": (0.3, 30), "steam": (2, 80),
          "stop_valve": (0.2, 1), "sundries": (0.2, 5), "thermostat": (0.3, 2), "toilet": (60, 110),
          "tools": (1, 40), "waterworks": (2, 80), "well": (20, 200), "wh_indirect": (120, 260),
          "wh_parts": (0.3, 10), "wh_tank": (100, 320), "wh_tankless": (40, 80)}
LONG = {"cu_pipe", "pex_tubing", "duct", "baseboard"}

RE_FOR = re.compile(r"(\bfor )([A-Z0-9][A-Za-z0-9-]{2,})")
RE_SIZE = re.compile(r"(?<![\d/-])(\d+-\d+/\d+|\d+/\d+|\d+(?:\.\d+)?) in\.")
RE_NUM = re.compile(r"(?<![\w.,/-])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)(?![\w/-])")
SIZES = ["1/4", "3/8", "1/2", "3/4", "1", "1-1/4", "1-1/2", "2", "2-1/2", "3", "4", "6", "8"]
FINISHES = ["Polished Chrome", "Brushed Nickel", "Matte Black", "Oil Rubbed Bronze", "Brushed Gold",
            "Polished Nickel", "Spot Resist Stainless", "Stainless"]
SUFFIXES = ["Contractor Pack", "Bulk Pack", "Commercial", "Heavy Duty", "Service Kit"]
PERM_A, PERM_B, PERM_M = 48_271_037, 9_431_117, 100_000_000      # a bijection on 0..1e8-1 (A is coprime to 10)
UPPER = "ABCDEFGHJKLMNPRSTUVWXYZ"


def _reshape(s: str, h: int) -> str:
    """Same shape (letters, digits, punctuation), new characters: a sibling model number."""
    out = []
    for ch in s:
        if ch.isdigit():
            out.append(str(h % 10)); h //= 10
        elif ch.isalpha():
            out.append(UPPER[h % len(UPPER)]); h //= len(UPPER)
        else:
            out.append(ch)
        if h < 1000:
            h = h * 2654435761 + 97
    return "".join(out)


def size_value(v: str) -> float:
    """1-1/2 -> 1.5, 3/4 -> 0.75, 2 -> 2.0."""
    if "-" in v:
        w, f = v.split("-", 1)
        return float(w) + size_value(f)
    if "/" in v:
        a, b = v.split("/")
        return float(a) / float(b)
    return float(v)


def variant(title: str, fam: str, h: int) -> tuple[str, float]:
    """A sibling product of a catalog item and how its size compares (for its price and weight): another model it
    fits, another finish, another size, another rating, or a pack."""
    m = RE_FOR.search(title)
    if m:
        return title[:m.start(2)] + _reshape(m.group(2), h) + title[m.end(2):], 1.0
    if fam in ("faucet", "sink", "toilet", "faucet_parts"):
        for f in FINISHES:
            if f in title:
                return title.replace(f, FINISHES[(FINISHES.index(f) + 1 + h % (len(FINISHES) - 1)) % len(FINISHES)], 1), \
                    1.0 + 0.08 * (h % 3)
    m = RE_SIZE.search(title)
    if m and m.group(1) in SIZES:
        cur = SIZES.index(m.group(1))
        new = SIZES[(cur + 1 + h % (len(SIZES) - 1)) % len(SIZES)]
        ratio = size_value(new) / size_value(m.group(1))
        return title[:m.start(1)] + new + title[m.end(1):], float(np.clip(ratio ** 1.3, 0.08, 12.0))
    m = RE_NUM.search(title)
    if m:
        raw = m.group(1)
        v = float(raw.replace(",", ""))
        f = (0.5, 0.75, 1.25, 1.5, 2.0)[h % 5]
        nv = v * f
        txt = f"{nv:,.0f}" if "," in raw else (f"{nv:g}" if "." in raw else str(int(round(nv))))
        if txt != raw:
            return title[:m.start(1)] + txt + title[m.end(1):], float(f ** 0.7)
    return f"{title}, {SUFFIXES[h % len(SUFFIXES)]}", (4.0 if "Pack" in SUFFIXES[h % len(SUFFIXES)] else 1.1)


def variant_title(title: str, fam: str, h: int) -> str:
    return variant(title, fam, h)[0]


def variant_part(part: str, row: int) -> str:
    """The template's part number with its longest digit run replaced by a unique 8-digit number."""
    n = (row * PERM_A + PERM_B) % PERM_M
    runs = list(re.finditer(r"\d+", part))
    if not runs:
        return f"{part}-{n:08d}"
    m = max(runs, key=lambda r: len(r.group()))
    return part[:m.start()] + f"{n:08d}" + part[m.end():]


def part_pieces(part: str) -> str:
    """A part number's pieces, so "grundfos 15-58" finds UPS15-58FC: dash-separated chunks and letter/digit runs."""
    low = part.lower()
    chunks = [c for c in re.split(r"[^a-z0-9/]+", low) if c]
    runs = re.findall(r"[a-z]+|\d+", low)
    return " ".join(dict.fromkeys(chunks + runs))


def freight_class(lb: np.ndarray, cuft: np.ndarray) -> np.ndarray:
    """NMFC density classes (lb per cubic foot) for LTL."""
    d = np.where(cuft > 0, lb / np.maximum(cuft, 1e-6), 0)
    edges = [50, 35, 30, 22.5, 15, 13.5, 12, 10.5, 9, 8, 7, 6, 5, 4, 3, 2, 1]
    classes = [50, 55, 60, 65, 70, 77, 85, 92, 100, 110, 125, 150, 175, 200, 250, 300, 400]
    out = np.full(len(d), 500, np.uint16)
    for e_, c in zip(reversed(edges), reversed(classes)):
        out = np.where(d >= e_, c, out)
    return np.where(cuft > 0, out, 0).astype(np.uint16)


def load_templates(canonical_dir: Path) -> dict:
    import pyarrow.parquet as pq
    t = pq.read_table(canonical_dir / "products.parquet",
                      columns=["brand", "mfr_part", "title", "cat1", "cat2", "cat3", "family", "list_price"])
    d = {c: t.column(c).to_pylist() for c in t.column_names}
    d["list_price"] = np.asarray(d["list_price"], np.float64)
    return d


def _columns(n: int, out: Path, create: bool) -> dict:
    (out / "cols").mkdir(parents=True, exist_ok=True)
    mode = "w+" if create else "r+"
    cols = {}
    for c, dt in COLS.items():
        p = out / "cols" / f"{c}.npy"
        cols[c] = np.lib.format.open_memmap(p, mode=mode, dtype=dt, shape=(n,)) if create else np.load(p, mmap_mode="r+")
    p = out / "cols" / "str_off.npy"
    cols["str_off"] = np.lib.format.open_memmap(p, mode=mode, dtype=np.uint64, shape=(n + 1,)) if create \
        else np.load(p, mmap_mode="r+")
    return cols


def _fill_numeric(T: dict, sup: Suppliers, fams: list[str], rows: np.ndarray, tmpl: np.ndarray,
                  bid: np.ndarray, factor: np.ndarray | None = None) -> dict:
    """Vectorized per-row numbers for rows `rows` built from catalog templates `tmpl` (brand ids `bid`)."""
    fam_ix = {f: i for i, f in enumerate(fams)}
    fam = np.array([fam_ix[T["family"][t]] for t in tmpl.tolist()], np.int16)
    famname = [fams[f] for f in fam.tolist()]
    u = lambda k: unif(rows, 100 + k)  # noqa: E731
    factor = np.ones(len(rows)) if factor is None else factor
    base = T["list_price"][tmpl] * factor
    price = base * (0.85 + 0.3 * u(1))
    conf = np.clip(sup.a_conf[bid] + (u(2) - 0.5) * 0.3, 0.3, 0.995)
    has_group = u(3) > 0.015
    price_c = np.where(has_group, np.round(price * 100), -1).astype(np.int32)
    feeds = sup.feeds(rows, bid, salt=3)
    lo = np.array([WEIGHT.get(f, (0.5, 15))[0] for f in famname])
    hi = np.array([WEIGHT.get(f, (0.5, 15))[1] for f in famname])
    wt = np.clip((lo + (hi - lo) * u(4) ** 1.7) * np.sqrt(factor), 0.05, 2000)
    have_ship = u(5) < sup.a_ship[bid]
    dens = 4 + 22 * u(6)                       # lb per cubic foot
    cuin = wt / dens * 1728.0
    long = np.array([f in LONG for f in famname])
    a, hf = 0.8 + 0.4 * u(7), 0.6 + 0.4 * u(8)
    side = np.cbrt(cuin / (a * hf))
    length = np.clip(np.cbrt(cuin) * 5, 60, 240)
    cross = np.sqrt(cuin / length)
    dl = np.where(long, length, side)
    dw = np.where(long, cross, side * a)
    dh = np.where(long, cross, side * hf)
    fclass = freight_class(wt, dl * dw * dh / 1728.0)
    hz = np.array([HAZMAT.get(f, 0.0) for f in famname]) > u(9)
    need = np.array([f in POTABLE_GAS for f in famname])
    comply_ok = ~need | (u(10) < 0.95)
    image = u(11) < 0.97
    published = has_group & image & comply_ok
    flags = (hz * FLAG_HAZMAT | image * FLAG_IMAGE | need * FLAG_COMPLY_NEED | comply_ok * FLAG_COMPLY_OK
             | published * FLAG_PUBLISHED | has_group * FLAG_GROUP).astype(np.uint8)
    keep = lambda x, _: np.where(have_ship, x, 0)  # noqa: E731
    return {"tmpl": tmpl.astype(np.int32), "brand": bid.astype(np.int16), "fam": fam, "price_c": price_c,
            "conf": np.round(conf * 100).astype(np.uint8), "fqty": feeds["factory_qty"].astype(np.int32),
            "fage": np.minimum(feeds["feed_age_h"], 65000).astype(np.uint16),
            "lead": feeds["lead_days"].astype(np.int16), "wt": keep(np.maximum(1, np.round(wt * 10)), 0).astype(np.uint32),
            "dl": keep(np.maximum(1, np.round(dl * 10)), 0).astype(np.uint16),
            "dw": keep(np.maximum(1, np.round(dw * 10)), 0).astype(np.uint16),
            "dh": keep(np.maximum(1, np.round(dh * 10)), 0).astype(np.uint16), "fclass": keep(fclass, 0).astype(np.uint16),
            "origin": feeds["origin"].astype(np.int8), "flags": flags}


def _schema():
    import tantivy
    sb = tantivy.SchemaBuilder()
    sb.add_integer_field("row", stored=True, fast=True, indexed=True)
    for f in ("title", "brand", "cat", "syn", "ids", "pn"):
        sb.add_text_field(f, stored=False, tokenizer_name="whitespace")
    sb.add_text_field("sku", stored=False, tokenizer_name="raw")
    sb.add_text_field("fam", stored=False, tokenizer_name="raw")
    return sb.build()


def _shard(args) -> dict:
    """Build rows [a, b) into shard k: columns, strings and the shard's index."""
    k, a, b, out, T, sup, fams, coded, heap, gap_rows = args
    import tantivy
    out = Path(out)
    cols = _columns(0, out, create=False)
    t0 = time.time()
    ix = tantivy.Index(_schema(), path=str(_mkdir(out / f"index-{k}")))
    w = ix.writer(heap_size=heap, num_threads=1)
    blob = open(out / f"strings-{k}.bin", "wb")
    pos = 0
    syn_cache: dict = {}
    cat_cache: dict = {}
    brand_cache: dict = {}
    n_core = len(T["title"])
    published = skipped = 0
    CH = 50_000
    for c0 in range(a, b, CH):
        c1 = min(b, c0 + CH)
        rows = np.arange(c0, c1, dtype=np.int64)
        tmpl = (mix(rows, 1) % np.uint64(n_core)).astype(np.int64)
        gmask = rows < gap_rows
        bid = sup.ids([T["brand"][t] for t in tmpl.tolist()])
        for j in np.flatnonzero(gmask).tolist():          # the hand-written gap items
            g = gaps.ITEMS[int(rows[j])]
            tmpl[j] = g["_tmpl"]
            bid[j] = sup.id[g["brand"]]
        hs = mix(rows, 2)
        made = [None if r < gap_rows else variant(T["title"][t], T["family"][t], h)
                for r, t, h in zip(rows.tolist(), tmpl.tolist(), hs.tolist())]
        factor = np.array([1.0 if v is None else v[1] for v in made])
        num = _fill_numeric(T, sup, fams, rows, tmpl, bid, factor)
        num["flags"] = np.where(gmask, num["flags"] | FLAG_GAP | FLAG_PUBLISHED | FLAG_IMAGE | FLAG_GROUP
                                | FLAG_COMPLY_OK, num["flags"]).astype(np.uint8)
        for j in np.flatnonzero(gmask).tolist():
            g = gaps.ITEMS[int(rows[j])]
            num["price_c"][j] = int(round(g["price"] * 100))
            num["conf"][j] = 97
        for c, v in num.items():
            cols[c][c0:c1] = v
        offs = np.empty(c1 - c0, np.uint64)
        recs = []
        for j, (r, t) in enumerate(zip(rows.tolist(), tmpl.tolist())):
            if r < gap_rows:
                g = gaps.ITEMS[r]
                brand, part, title = g["brand"], g["part"], g["title"]
            else:
                brand = T["brand"][t]
                part = variant_part(str(T["mfr_part"][t]), r)
                title = made[j][0]
                if compact_id(part) in coded:
                    part = part + "E"
            code = sup.list[sup.id[brand]].code
            sku = f"{code}-{part}-EC"
            rec = f"{sku}\x1f{part}\x1f{title}".encode()
            offs[j] = pos
            pos += len(rec)
            recs.append(rec)
            if not num["flags"][j] & FLAG_PUBLISHED:
                skipped += 1
                continue
            published += 1
            ck = (T["cat1"][t], T["cat2"][t], T["cat3"][t])
            cat = cat_cache.get(ck)
            if cat is None:
                cat = cat_cache[ck] = analyze(" ".join(ck))
            syn = syn_cache.get(t) if r >= gap_rows else None
            if syn is None:
                syn = analyze(expand_concepts((title + " " + ck[2]).lower()))
                if r >= gap_rows and len(syn_cache) < 200_000:
                    syn_cache[t] = syn
            bt = brand_cache.get(brand)
            if bt is None:
                bt = brand_cache[brand] = analyze(brand)
            w.add_document(tantivy.Document(
                row=r, title=analyze(title), brand=bt, cat=cat, syn=syn,
                ids=f"{compact_id(part).lower()} {compact_id(sku).lower()}", pn=part_pieces(part), sku=sku.upper(),
                fam=T["family"][t]))
        blob.write(b"".join(recs))
        cols["str_off"][c0:c1] = offs
        if k == 0 and (c1 - a) % 500_000 < CH:
            print(f"  shard 0: {c1 - a:,}/{b - a:,} rows, {time.time() - t0:.0f}s", flush=True)
    blob.close()
    w.commit()
    w.wait_merging_threads()
    for c in cols.values():
        c.flush()
    return {"shard": k, "rows": [a, b], "bytes": pos, "published": published, "unpublished": skipped,
            "seconds": round(time.time() - t0, 1)}


def _mkdir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


def build(n: int, out: Path, canonical_dir: Path, shards: int = 2, heap: int = 200_000_000, log=print) -> dict:
    t0 = time.time()
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    T = load_templates(canonical_dir)
    coded = {compact_id(str(p)) for p in T["mfr_part"]}
    brands = sorted(set(T["brand"]) | {g["brand"] for g in gaps.ITEMS})
    sup = Suppliers(brands)
    fams = sorted(set(T["family"]))
    fam_first = {}
    for i, f in enumerate(T["family"]):
        fam_first.setdefault(f, i)
    for g in gaps.ITEMS:
        g["_tmpl"] = fam_first[g["family"]]
    n = max(n, len(gaps.ITEMS))
    _columns(n, out, create=True)
    bounds = np.linspace(0, n, shards + 1).astype(np.int64).tolist()
    jobs = [(k, bounds[k], bounds[k + 1], str(out), T, sup, fams, coded, heap, len(gaps.ITEMS))
            for k in range(shards)]
    log(f"Extended Catalog build: {n:,} items in {shards} shards -> {out}")
    if shards == 1:
        res = [_shard(jobs[0])]
    else:
        ctx = mp.get_context("fork")
        with ctx.Pool(shards) as pool:
            res = pool.map(_shard, jobs)
    # string offsets are per shard file; record each shard's row range
    cols = _columns(0, out, create=False)
    cols["str_off"][n] = 0
    cols["str_off"].flush()
    man = {"schema": SCHEMA_VERSION, "n": n, "shards": [{"k": r["shard"], "rows": r["rows"], "bytes": r["bytes"]}
                                                     for r in sorted(res, key=lambda r: r["shard"])],
           "published": sum(r["published"] for r in res), "unpublished": sum(r["unpublished"] for r in res),
           "gap_items": len(gaps.ITEMS), "families": fams, "brands": brands,
           "built": time.strftime("%Y-%m-%dT%H:%M:%S"), "seconds": round(time.time() - t0, 1)}
    (out / "manifest.json").write_text(json.dumps(man, indent=1))
    log(f"Extended Catalog built: {man['published']:,} published, {man['unpublished']:,} held back "
        f"(missing core fields), {man['seconds']:.0f}s")
    return man


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=2_000_000)
    ap.add_argument("--out", required=True)
    ap.add_argument("--shards", type=int, default=max(1, min(4, (os.cpu_count() or 2))))
    ap.add_argument("--heap", type=int, default=200_000_000)
    ap.add_argument("--canonical", default=None)
    a = ap.parse_args(argv)
    from webbsearch.config import settings
    out = Path(a.out)
    tmp = out.with_name(out.name + ".tmp")
    man = build(a.n, tmp, Path(a.canonical) if a.canonical else settings.canonical_dir, a.shards, a.heap)
    if out.exists():
        shutil.rmtree(out)
    tmp.rename(out)
    # point serving at the new build (an atomic rename of a one-line file)
    cur = out.parent.parent / "CURRENT" if out.parent.name == "builds" else out.parent / "CURRENT"
    t = cur.with_suffix(".tmp")
    t.write_text(out.name)
    t.replace(cur)
    # keep the new build and the one before it (a server may still have it open); older builds go
    for p in out.parent.glob("*.tmp"):                  # builds stopped part way
        shutil.rmtree(p, ignore_errors=True)
    builds = sorted((p for p in out.parent.iterdir() if p.is_dir() and not p.name.endswith(".tmp") and p != out),
                    key=lambda p: p.stat().st_mtime, reverse=True)
    for old in builds[1:]:
        shutil.rmtree(old, ignore_errors=True)
    print(json.dumps({k: man[k] for k in ("n", "published", "unpublished", "seconds")}))


if __name__ == "__main__":
    sys.exit(main())
