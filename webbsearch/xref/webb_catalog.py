"""
Webb's own catalogue identity: Control #, MPN, UPC.

The lake stores a Webb item number for stocked parts; the ALT1 export shows
that number IS Webb's Control #, on all 3,567 of them, exactly. So this file
does two jobs:

  1. Attach MPN and UPC to every part the lake already knows Webb stocks —
     which is what makes barcode scanning possible at all.
  2. Find parts the lake did NOT know were stocked, by matching brand + part
     number against the catalogue's MPN column.

Job 2 is the one that can go wrong, because manufacturers reuse part numbers
across brands freely: "50" belongs to a dozen companies. The Alt1 column
carries a brand prefix (SUP..., W/R..., RBS...), but the prefix vocabulary is
Webb's, not ours, and guessing at 1,810 of them would invent matches. So the
prefixes are LEARNED from the 3,567 pairs already proven identical, and a
prefix is only trusted when the lake agrees with itself about it — at least
three confirmations and 85% agreement. 37 prefixes clear that bar. Everything
else is left alone rather than guessed at.
"""
import collections, csv, os, re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV = f"{ROOT}/build/alt1_mpn_upc.csv"

# A prefix must be confirmed this many times overall before it is trusted at
# all, and a brand must be confirmed this many times under that prefix before
# the pair is admitted.
MIN_PREFIX_CONFIRMATIONS = 3
MIN_BRAND_CONFIRMATIONS = 2


def _norm(s):
    return "".join(ch for ch in (s or "").upper() if ch.isalnum())


def _digits(s):
    return "".join(ch for ch in (s or "") if ch.isdigit())


def _check_digit(body12):
    """UPC-A check digit. Webb's export stores 11 digits with the check digit
    dropped, so a scanner's 12 digits would never match a stored value."""
    tot = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(body12))
    return str((10 - tot % 10) % 10)


def upc_variants(raw):
    """Every form of one UPC a scanner or a spreadsheet might produce.

    Retail scanners emit UPC-A (12) or EAN-13 (13, a UPC-A with a leading 0).
    Webb's export is 11 digits — the check digit stripped — and Excel has eaten
    leading zeros besides. Matching on one spelling would fail most scans, so
    the index stores them all.
    """
    d = _digits(raw)
    if not d:
        return set()
    out = {d}
    if len(d) == 13 and d[0] == "0":
        d = d[1:]
        out.add(d)
    if len(d) == 12:
        out.add(d[:11])           # check digit dropped
        out.add(d.lstrip("0"))
    if len(d) == 11:
        out.add(d + _check_digit(d))
        out.add("0" + d)
    out.add(d.lstrip("0") or d)
    return {v for v in out if len(v) >= 8}


class Catalog:
    def __init__(self, path=CSV):
        self.rows = {}                       # cid -> {alt1, mpn, upc}
        self.by_brand_mpn = {}               # (brand, npart) -> cid
        self.prefix_brand = {}               # learned Alt1 prefix -> lake brand
        self.loaded = os.path.exists(path)
        if not self.loaded:
            print(f"  webb_catalog: {path} absent — no UPC/MPN enrichment")
            return
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                if r["cid"]:
                    self.rows[r["cid"]] = r

    def learn(self, known_pairs):
        """known_pairs: (webb_item, lake brand) for parts already proven Webb's."""
        if not self.loaded:
            return
        tally = collections.defaultdict(collections.Counter)
        for cid, brand in known_pairs:
            r = self.rows.get(cid)
            if not r or not r["mpn"] or not brand:
                continue
            a1, m = r["alt1"].upper(), r["mpn"].upper()
            if a1.endswith(m):
                p = a1[: len(a1) - len(m)]
                if p:
                    tally[p][brand] += 1
        # A prefix maps to a SET of brands, not to one.
        #
        # The plurality rule this replaces looked safer and was in fact wrong.
        # "SUP" resolves to Supco 275 times — but also to Mars 16, RBM 18 and
        # White-Rodgers 14, which dropped Supco to 79% and disqualified the
        # prefix entirely. Those are not competing manufacturers: the UPC index
        # proves all 1,304 shared barcodes resolve to exactly ONE Webb item, so
        # Supco / Mars / Essex / "Sealed Unit Parts Co." are alias spellings of
        # the same stocked part. Demanding one winner throws away the prefix
        # over a disagreement that does not exist.
        #
        # Requiring two independent confirmations per brand keeps the aliases
        # and still drops the true one-offs — a single stray pairing never
        # admits a brand on its own.
        for p, c in tally.items():
            if sum(c.values()) < MIN_PREFIX_CONFIRMATIONS:
                continue
            keep = {b for b, n in c.items() if n >= MIN_BRAND_CONFIRMATIONS}
            if keep:
                self.prefix_brand[p] = keep
        for cid, r in self.rows.items():
            m, a1 = r["mpn"], r["alt1"].upper()
            if not m:
                continue
            mu = m.upper()
            p = a1[: len(a1) - len(mu)] if a1.endswith(mu) else None
            for b in self.prefix_brand.get(p, ()):
                self.by_brand_mpn.setdefault((b, _norm(m)), cid)
        pairs = sum(len(v) for v in self.prefix_brand.values())
        print(f"  webb_catalog: {len(self.rows):,} catalogue rows, "
              f"{len(self.prefix_brand)} prefixes / {pairs} brand pairings "
              f"learned from ground truth")

    def enrich(self, brand, part, webb_item):
        """Return (cid, mpn, upc) for a part, or (webb_item, '', '')."""
        cid = webb_item or self.by_brand_mpn.get((brand, _norm(part)), "")
        r = self.rows.get(cid)
        if not r:
            return cid, "", ""
        return cid, r["mpn"], r["upc"]
