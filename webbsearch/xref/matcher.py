"""
Forgiving part lookup — the reference implementation the app mirrors.

Measured against a naive normalised lookup, the field failure modes were:
brand + part 0%, partial 7%, O/0 confusion 31%, a single transposition 3%,
one dropped character 1%. A counter rep types all five of those.

The approach is a scored cascade, not a single query: run the cheap exact tests
first, fall through to progressively looser ones, and score every candidate so
the ranking is honest about how it was found. Nothing is discarded silently —
a loose match still surfaces, it just says how loose it is.
"""
import re, sqlite3
from webbsearch.xref.webb_catalog import upc_variants

norm = lambda s: re.sub(r"[^A-Z0-9]", "", (s or "").upper())
CONFUSE = str.maketrans({"O": "0", "I": "1", "L": "1", "S": "5",
                         "B": "8", "Z": "2", "G": "6", "Q": "0", "U": "V"})
skel = lambda s: norm(s).translate(CONFUSE)

# Brand tokens that also begin a real part number. Stripping these blindly
# would delete the thing the user was searching for.
UNSAFE_BRAND_TOKENS = {"ACC", "CAP", "ICM"}

HOW = {
    # A barcode is an identity, not a guess, so it outranks anything typed.
    "upc":        (105, "barcode matched"),
    "exact":      (100, "exact match"),
    "brand_part": (98,  "brand and part both matched"),
    "part_only":  (92,  "part number matched"),
    "skeleton":   (88,  "matched allowing for O/0 and I/1"),
    # This bonus reads like a bug -- a penalty that grows as you type -- and a
    # rewrite that replaced it with a positive, coverage-scaled bonus measured
    # WORSE: 88.8% against 89.2% overall, and 34.4% against 35.3% on the
    # half-typed shape it was meant to fix. Coverage rewards short part numbers
    # rather than likely ones, and at n=4..8 the "growing" penalty is a near
    # constant -0.8 to -1.6. Leave it: scripts/recall_check.py is the arbiter.
    "prefix":     (78,  "starts with what you typed"),
    "contains":   (66,  "contains what you typed"),
    "skel_loose": (60,  "loose match allowing for confusable characters"),
    "swap":       (72,  "two characters typed the other way round"),
    "edit1":      (55,  "one character different"),
    "edit2":      (44,  "two characters different"),
}


# The distance past which "closest thing we hold" stops being useful. Measured
# rather than guessed: real typos land at or under 1.5 ("36C0O-400" scores 1,
# "packard tr6" scores -0.5) and pure noise starts at 3 ("asdfgh" reaches for
# Wanbao ASD35). Past this, a confident nearest is worse than an honest nothing.
NEAR_MAX_COST = 2.5


def _edit_within(a, b, k):
    """Damerau-Levenshtein distance capped at k, or None if further apart.

    Damerau, not plain Levenshtein: swapping two adjacent characters is the
    single most common typing mistake, and ordinary Levenshtein charges it two
    edits, which pushed nearly every transposed part number out of range.
    Measured across 150 parts, this one change took transposition recall from
    5% to the high nineties.
    """
    if abs(len(a) - len(b)) > k:
        return None
    prev2, prev = None, list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j in range(1, len(b) + 1):
            cost = ca != b[j - 1]
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if (prev2 is not None and i > 1 and j > 1
                    and ca == b[j - 2] and a[i - 2] == b[j - 1]):
                v = min(v, prev2[j - 2] + 1)          # adjacent transposition
            cur.append(v)
        if min(cur) > k:
            return None
        prev2, prev = prev, cur
    return prev[-1] if prev[-1] <= k else None


class Matcher:
    def __init__(self, db):
        self.db = db
        self.db.row_factory = sqlite3.Row
        self.brands, self.initials = {}, {}
        for r in db.execute("select nname,initials,name,n from brand"):
            self.brands[r["nname"]] = r["name"]
            if r["initials"]:
                # ambiguous initials help nobody; keep only the unambiguous ones
                self.initials.setdefault(r["initials"], []).append((r["nname"], r["n"]))
        self.initials = {k: max(v, key=lambda x: x[1])[0]
                         for k, v in self.initials.items()
                         if len(v) == 1 or max(v, key=lambda x: x[1])[1] >= 5 * sorted(
                             (x[1] for x in v), reverse=True)[1]}

    # -- split a typed string into a brand guess and a part guess -----------
    def split_brand(self, q):
        """Return (brand_nname or None, remaining part text).

        Tries the longest leading run of words that names a brand, so
        "white rodgers 50a55-843" and "wr 50a55-843" both work, and a bare part
        number is left completely alone.
        """
        words = [w for w in re.split(r"[\s]+", (q or "").strip()) if w]
        if len(words) < 2:
            return None, q
        for take in range(min(3, len(words) - 1), 0, -1):
            cand = norm("".join(words[:take]))
            if not cand or cand in UNSAFE_BRAND_TOKENS:
                continue
            rest_n = norm(" ".join(words[take:]))
            if len(rest_n) < 3:
                continue          # "tr 67" is a part number, not a brand plus "67"
            if cand in self.brands:
                return cand, " ".join(words[take:])
            if cand in self.initials:                       # "WR" -> White-Rodgers
                return self.initials[cand], " ".join(words[take:])
            hits = [b for b in self.brands if b.startswith(cand)] if len(cand) >= 3 else []
            if len(hits) == 1:
                return hits[0], " ".join(words[take:])
        return None, q

    def by_code(self, raw, limit=10):
        """A scanned or typed barcode.

        A different kind of question from `search`: it has a right answer or
        none, and there is nothing to rank or to forgive."""
        seen, out = set(), []
        for v in upc_variants(raw):
            for r in self.db.execute(
                    "select p.* from upc u join part p on p.id = u.part "
                    "where u.code = ? limit 10", (v,)).fetchall():
                if r["id"] in seen:
                    continue
                seen.add(r["id"])
                base, why = HOW["upc"]
                out.append({"id": r["id"], "brand": r["brand"], "part": r["part"],
                            "cls": r["cls"], "ncross": r["ncross"],
                            "score": base, "how": "upc", "why": why})
            if len(out) >= limit:
                break
        return out[:limit]

    def search(self, q, limit=25, mode="full"):
        """`mode="type"` runs only the tiers that can answer between keystrokes.

        The cascade used to run in full on every keystroke, because a half-typed
        part number is almost always below the gate the fuzzy tiers sit behind.
        So the app fired roughly fifty LIKE queries per character to answer a
        question the user had not finished asking, and the only fix available
        was a longer debounce -- which is why typeahead felt both laggy and
        vague. Splitting the modes lets the cheap tiers answer immediately and
        the expensive ones wait until typing stops. Ranking is identical; only
        which tiers have run yet differs.
        """
        raw = (q or "").strip()
        # A pure digit string of barcode length is a scan, however it arrived.
        if re.fullmatch(r"\d{8,14}", raw):
            scanned = self.by_code(raw, limit)
            if scanned:
                return scanned
        if len(norm(raw)) < 2:
            return []
        bkey, rest = self.split_brand(raw)
        n, sk = norm(rest), skel(rest)
        seen, out = {}, []

        def add(row, how, bonus=0):
            if row["id"] in seen:
                return
            base, why = HOW[how]
            # a part with more crosses and a better score is the likelier target
            pop = min(6, (row["ncross"] or 0)) * 0.4 + max(0, (row["bestwmcr"] or 0)) * 0.02
            seen[row["id"]] = True
            out.append({"id": row["id"], "brand": row["brand"], "part": row["part"],
                        "cls": row["cls"], "ncross": row["ncross"],
                        "score": round(base + bonus + pop, 2), "how": how, "why": why})

        sql = lambda w, args, lim=40, order="ncross desc": self.db.execute(
            f"select * from part where {w} order by {order} limit {lim}", args).fetchall()

        if bkey:
            for r in sql("key = ?", (f"{bkey}|{n}",), 5): add(r, "brand_part")
        for r in sql("npart = ?", (n,), 12): add(r, "exact" if not bkey else "part_only")
        for r in sql("skel = ?", (sk,), 12): add(r, "skeleton")
        # Prefix from two characters, not three. "50" is a seek on ix_part_npart
        # and the counter has typed enough to be narrowing; below three, the app
        # used to show nothing at all while they typed.
        for r in sql("npart like ?", (n + "%",), 25):
            add(r, "prefix", bonus=-min(10, len(n) * 0.2))
        # The brand on its own. split_brand needs two words, so a bare
        # "honeywell" never reached it and the search returned nothing at all,
        # with every Honeywell part sitting on the phone.
        sole = n if n in self.brands else self.initials.get(n)
        if sole:
            for r in sql("key like ?", (f"{sole}|%",), 30): add(r, "prefix", bonus=4)
        if len(n) >= 3:
            for r in sql("npart like ?", ("%" + n + "%",), 25):
                # How deep in the hit starts. A match at position 1 is nearly a
                # prefix; one at position 7 is a coincidence, and the two used
                # to score identically. Measured recall-neutral -- it reorders
                # the loose tail rather than changing what is found.
                at = (r["npart"] or "").find(n)
                add(r, "contains", bonus=-min(8, max(0, at) * 1.5))
            # A full scan of skel, and the slowest tier in the cascade. It runs
            # when the user stops typing, not between keystrokes.
            if mode == "full":
                for r in sql("skel like ?", ("%" + sk + "%",), 20): add(r, "skel_loose")

        # Fuzzy is the expensive pass, so it only runs when the cheap ones came
        # up short, and only over parts of a similar length that share a first
        # OR second character with the query -- never a scan of all 48,443.
        # Blocking this way keeps a worst-case lookup inside a few milliseconds.
        # "Did a tier that means WE FOUND IT fire?", not "is the top score above
        # 88?". The score gate silently coupled the fuzzy cascade to the prefix
        # tier's base: raising prefix by four points switched fuzzy matching off
        # entirely and cost 2.2 points of dropped-character recall, with nothing
        # in the diff mentioning fuzzy. Mode gates it too -- the cascade is ~50
        # LIKE queries and a half-typed part number is not a typo to correct.
        found = mode != "full" or any(
            o["how"] in ("exact", "brand_part", "part_only", "skeleton") for o in out)

        # Transposition, resolved by index rather than by scanning: a swapped
        # pair leaves the sorted-character signature untouched.
        if len(n) >= 4 and not found:
            for r in self.db.execute(
                    "select * from part where sig = ? limit 30",
                    ("".join(sorted(sk)),)).fetchall():
                if r["id"] not in seen and _edit_within(sk, r["skel"] or "", 2) is not None:
                    add(r, "swap")
            # "Did a tier that means WE FOUND IT fire?", not "is the top score above
        # 88?". The score gate silently coupled the fuzzy cascade to the prefix
        # tier's base: raising prefix by four points switched fuzzy matching off
        # entirely and cost 2.2 points of dropped-character recall, with nothing
        # in the diff mentioning fuzzy. Mode gates it too -- the cascade is ~50
        # LIKE queries and a half-typed part number is not a typo to correct.
        found = mode != "full" or any(
            o["how"] in ("exact", "brand_part", "part_only", "skeleton") for o in out)

        # One wrong, missing, or extra character, resolved by index instead of
        # by scanning. Each variant is a LIKE pattern with a literal prefix, so
        # SQLite can use the npart index; a scan of a length-band was both
        # slower and biased -- ordering it by popularity quietly excluded the
        # long tail, which is most of the lake.
        if 4 <= len(n) <= 24 and not found:
            pats = []
            for i in range(len(n) + 1):
                pats.append((n[:i] + "_" + n[i:], len(n) + 1))      # they dropped one
            for i in range(len(n)):
                pats.append((n[:i] + "_" + n[i + 1:], len(n)))      # they mistyped one
                pats.append((n[:i] + n[i + 1:], len(n) - 1))        # they added one
            for pat, ln in pats:
                if pat.startswith("_") and len(pat) > 12:
                    continue        # no literal prefix to seek on; not worth it
                for r in self.db.execute(
                        "select * from part where length(npart) = ? and npart like ? "
                        "escape '\\' limit 8", (ln, pat)).fetchall():
                    add(r, "edit1")

        # Last resort: the words weren't a brand we know, so treat the longest
        # alphanumeric run as the part number and ignore the rest. Catches
        # "furnace board 50A55-843" and misspelled brands alike.
        if not out and " " in raw:
            # Pick the token most likely to BE the part number, not merely the
            # longest: a part number almost always carries digits and a brand
            # word almost never does. Ranking by length alone made "us mtrs 5462"
            # fall back to "mtrs" and find nothing.
            tok = max(re.split(r"\s+", raw),
                      key=lambda w: (any(c.isdigit() for c in w), len(norm(w))))
            if norm(tok) != n:
                return self.search(tok, limit, mode)
        out.sort(key=lambda x: -x["score"])
        return out[:limit]

    def nearest(self, q, limit=3):
        """The closest parts we hold, whatever the distance.

        `search` is a cascade with early exits: once a good tier hits, the
        looser tiers never run, which is right for finding the answer and wrong
        for the moment there is no answer. Then the app told a counter
        "nothing" and gave them a button that helps head office -- with 48,443
        parts on the phone and no attempt to show the nearest of them.

        This makes no claim to be right. It is deliberately separate so it can
        never leak into the ranked results.
        """
        bkey, rest = self.split_brand(q or "")
        n = norm(rest)
        if len(n) < 3:
            return []
        sk = skel(rest)
        seen = {}

        def take(rows):
            for r in rows:
                seen.setdefault(r["id"], r)

        # Shared opening characters, shortening the prefix until something comes
        # back. Indexed on skel, so even a two-character prefix is a seek.
        cut = min(5, len(sk))
        while cut >= 2 and len(seen) < 60:
            take(self.db.execute("select * from part where skel like ? limit 60",
                                 (sk[:cut] + "%",)).fetchall())
            cut -= 1
        # Same characters in any order -- the transposition case, exact on an index.
        take(self.db.execute("select * from part where sig = ? limit 20",
                             ("".join(sorted(sk)),)).fetchall())

        scored = []
        for r in seen.values():
            d = _edit_within(n, r["npart"], 6)
            # Anything past the cap still ranks, just below everything
            # measurable, by how much of the opening it shares.
            shared = 0
            rskel = r["skel"] or ""
            while shared < len(sk) and shared < len(rskel) and sk[shared] == rskel[shared]:
                shared += 1
            brand_bonus = -1.5 if bkey and norm(r["brand"]).startswith(bkey) else 0
            cost = (8 - shared * 0.4 if d is None else d) + brand_bonus
            scored.append((cost, r))

        # Ordinal, not locale collation: locale ignores punctuation and varies
        # by device language, so "3ARR3*2M*" and "3ARR3-A2M3" tie-broke one way
        # in the app and another here. A ranking must not depend on the locale.
        scored.sort(key=lambda x: (x[0], -(x[1]["ncross"] or 0), x[1]["part"]))

        return [{"id": r["id"], "brand": r["brand"], "part": r["part"],
                 "cls": r["cls"], "ncross": r["ncross"], "score": 0,
                 "how": "nearest", "why": "closest thing we hold"}
                for cost, r in scored if cost <= NEAR_MAX_COST][:limit]
