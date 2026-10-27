"""The search dictionary: the trade vocabulary the eCommerce team maintains (v1.7).

Each entry maps what people type to what the catalog calls it. Kinds:
  * synonym   "bathroom faucet" -> "lavatory faucet"        the phrase is searched as the target
  * spelling  "facuet" -> "faucet"                           same, for misspellings (the term must not be a catalog word)
  * brand     "B&G" -> "Bell & Gossett"                      a brand alias; the target must be a brand Webb carries
  * block     "cap", "PRV"                                   never rewritten on its own (ambiguous); longer phrases
                                                             such as "run cap" still can be
  * glossary  "SEER2" = seasonal energy efficiency ratio 2   a definition only; no effect on search
Statuses: live (applies to every search), review (proposed; no effect until someone approves it), off (reviewed and
switched off, kept with the reason).

Matching is on whole words, case-insensitive, with hyphens, slashes and spaces treated alike ("san-tee" = "san tee")
and a light plural rule ("hose bibs" matches "hose bib"). The longest phrase wins; replacements are not re-matched.
An entry can require context: `any` (at least one of these words in the search) and `none` (none of them).
A word that looks like a part number is never rewritten unless the entry names it exactly.

Entries live in data/merch/dictionary.json; every change is appended to dictionary_log.jsonl (who, when, before,
after), so any row's history can be shown and restored. Production: a table in the merchandising service's Postgres.
"""
from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import os
import re
import threading
import time
from pathlib import Path

KINDS = ("synonym", "spelling", "brand", "block", "glossary")
KIND_LABELS = {"synonym": "Synonym", "spelling": "Spelling", "brand": "Brand alias", "block": "Don't rewrite",
               "glossary": "Glossary"}
STATUSES = ("live", "review", "off")
APPLIES = ("synonym", "spelling", "brand")
DEPTS = ("Plumbing", "HVAC/R", "Heating", "PVF", "Water systems", "Tools", "General")
FIELDS = ("id", "kind", "term", "to", "any", "none", "dept", "status", "note", "definition", "qc", "flags", "source",
          "created_at", "created_by", "updated_at", "updated_by")
MAX_ENTRIES = 20_000
# Webb's region: two-letter state codes are also branch and address words ("Manchester NH")
STATES = {"ma", "nh", "vt", "me", "ri", "ct", "ny", "pa", "nj", "md", "de", "va"}
# words that are both a brand Webb carries and an ordinary trade word: a rule on them changes brand searches
BRAND_WORDS = {"watts": "watts (power)", "delta": "delta-T / delta-P", "carrier": "toilet and lavatory carriers",
               "state": "state names", "mars": "", "legend": "", "titan": "", "sloan": "", "elkay": "",
               "taco": "", "rheem": "", "moen": "", "zurn": "", "nibco": "", "apollo": "", "mueller": "Mueller copper tube"}

_APOS = re.compile(r"[’'`]")
_TOK = re.compile(r"\d+(?:[./]\d+)*[a-zµ\x00]*|[a-z0-9µδ&#+\x00]+")


def _tokens(s: str) -> list[tuple[str, int, int]]:
    """Lower-cased word tokens with their spans in `s` (apostrophes dropped, so plumber's = plumbers)."""
    s2 = _APOS.sub("\x00", s.lower())
    return [(m.group(0).replace("\x00", ""), m.start(), m.end()) for m in _TOK.finditer(s2)]


def _stem(t: str) -> str:
    if len(t) > 3 and t.endswith("s") and not t.endswith("ss") and not any(c.isdigit() for c in t):
        return t[:-3] + "y" if t.endswith("ies") else t[:-1]
    return t


def key(s: str) -> tuple[str, ...]:
    """The match form of a phrase: its stemmed word tokens."""
    return tuple(_stem(t) for t, _, _ in _tokens(s))


def _looks_like_part(t: str) -> bool:
    from webbsearch.retrieval.analyzer import looks_like_part_number
    return looks_like_part_number(t)


def entry_id(kind: str, term: str, to: str) -> str:
    return hashlib.md5(f"{kind}|{' '.join(key(term))}|{' '.join(key(to or ''))}".encode()).hexdigest()[:10]


# ---------------------------------------------------------------------------------------------------------- validation
def clean(e: dict) -> dict:
    """Normalize and bound one entry. Raises ValueError on a shape problem; content problems are lint flags."""
    kind = str(e.get("kind") or "synonym").strip().lower()
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    term = re.sub(r"\s+", " ", str(e.get("term") or "")).strip()[:80]
    to = re.sub(r"\s+", " ", str(e.get("to") or "")).strip()[:120]
    if not term or not key(term):
        raise ValueError("an entry needs the words people type")
    if kind in APPLIES and not to:
        raise ValueError(f"a {KIND_LABELS[kind].lower()} needs what to search instead")
    if kind in ("block",):
        to = ""
    status = str(e.get("status") or "review").strip().lower()
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")

    def words(v):
        v = v.split(",") if isinstance(v, str) else (v or [])
        return [re.sub(r"\s+", " ", str(x)).strip().lower()[:40] for x in v if str(x).strip()][:20]
    out = {"id": str(e.get("id") or entry_id(kind, term, to))[:24], "kind": kind, "term": term, "to": to,
           "any": words(e.get("any")), "none": words(e.get("none")),
           "dept": str(e.get("dept") or "").strip()[:40], "status": status,
           "note": str(e.get("note") or "").strip()[:500], "definition": str(e.get("definition") or "").strip()[:300],
           "qc": str(e.get("qc") or "").strip()[:500], "flags": [str(f)[:40] for f in (e.get("flags") or [])][:12],
           "source": str(e.get("source") or "").strip()[:80]}
    for k in ("created_at", "created_by", "updated_at", "updated_by"):
        out[k] = e.get(k)
    return out


class Lint:
    """Content checks for an entry, against the catalog and the rest of the dictionary. Errors keep an entry from
    going live; warnings are for the reviewer."""

    def __init__(self, df=None, brands=None, canon=None):
        self.df = df or (lambda tok: None)               # catalog documents containing a (normalized) word; None = unknown
        self.brands = {b.lower(): b for b in (brands or [])}
        self.canon = canon or {}

    def _cat_words(self, phrase: str) -> list[int | None]:
        from webbsearch.retrieval.analyzer import normalize
        return [self.df(t) for t in normalize(phrase) if len(t) > 1 and not t.isdigit()]

    def check(self, e: dict, others: list[dict] = ()) -> list[dict]:
        f = []

        def add(level, code, text):
            f.append({"level": level, "code": code, "text": text})
        k = key(e["term"])
        kind = e["kind"]
        has_ctx = bool(e["any"])
        if kind in APPLIES:
            if k == key(e["to"]):
                add("error", "same", "The target reads the same as the term.")
            if all(t.replace("/", "").replace(".", "").isdigit() for t in k) and not has_ctx:
                add("error", "bare_number", "A bare number (45, 90, 3/4) is a size, rating or part number too; add context words.")
            if len(k) == 1 and len(k[0]) <= 2 and not has_ctx:
                add("error", "short", f"“{e['term']}” is two letters; on its own it means several things. Add context words.")
            if len(k) == 1 and _looks_like_part(k[0]) and kind != "spelling" and not re.fullmatch(r"r-?\d{2,3}[a-z]?", k[0]):
                add("warn", "part_number", "The term looks like a part number; an item cross (Bulk order) may fit better.")
            for o in others:
                if o["id"] == e["id"] or o["status"] == "off":
                    continue
                if o["kind"] == "block" and key(o["term"]) == k and not has_ctx:
                    add("error", "blocked", f"“{o['term']}” is on the don't-rewrite list ({o.get('note') or 'ambiguous'}). Use a longer phrase or add context.")
                elif o["kind"] in APPLIES and key(o["term"]) == k and key(o["to"]) != key(e["to"]) \
                        and set(o["any"]) == set(e["any"]) and o["status"] == "live":
                    add("error", "conflict", f"Another live entry sends “{o['term']}” to “{o['to']}”.")
                elif o["kind"] in APPLIES and key(o["term"]) == k and key(o["to"]) == key(e["to"]) and o["kind"] == kind:
                    add("warn", "duplicate", "Duplicate of another entry.")
            tk = set(k) | {t for t, _, _ in _tokens(e["term"])}
            if len(k) <= 2 and tk & STATES and not has_ctx:
                add("warn", "state", "Also a state abbreviation in Webb's region (addresses, branch names).")
            for t in tk:
                if t in BRAND_WORDS and kind != "brand":
                    add("warn", "brand_word", f"“{t}” is also a brand" + (f" and means {BRAND_WORDS[t]}" if BRAND_WORDS[t] else "") + ".")
                if t in self.canon and self.canon[t] and len(k) == 1:
                    add("warn", "builtin", f"Built-in vocabulary already reads “{t}” as “{self.canon[t]}”.")
            if kind == "brand" and self.brands and e["to"].lower() not in self.brands:
                add("warn", "unknown_brand", f"“{e['to']}” isn't a brand in the catalog or the cross-reference.")
            extra = [w for w in set(key(e["to"])) - set(k) if len(w) > 2]
            if kind != "brand" and set(key(e["to"])) >= set(k) and extra:
                add("warn", "adds_words", f"The target repeats the term and adds “{' '.join(sorted(extra))}”; it narrows or dilutes the search.")
            tw, fw = self._cat_words(e["term"]), self._cat_words(e["to"])
            if fw and all(x == 0 for x in fw if x is not None) and any(x is not None for x in fw):
                add("warn", "target_missing", "None of the target's words are in the catalog: the rewrite would find nothing new.")
            elif tw and fw and all(x is not None for x in tw + fw) and min(tw) > 5 * max(1, min(fw)) and kind == "synonym":
                add("warn", "backwards", "The catalog uses the term's words more than the target's; the rule may be backwards.")
            if kind == "spelling" and tw and all(x for x in tw if x is not None) and any(x is not None for x in tw):
                add("error", "real_word", "The “misspelling” is a word the catalog uses.")
        if kind == "glossary" and not (e.get("definition") or e.get("to")):
            add("warn", "no_definition", "A glossary entry needs a definition.")
        return f


# ---------------------------------------------------------------------------------------------------------- matching
class Matcher:
    """Live entries compiled for query time."""

    def __init__(self, entries: list[dict]):
        self.by_first: dict[str, list[tuple[tuple, dict]]] = {}
        for e in entries:
            if e["kind"] not in APPLIES or e["status"] != "live":
                continue
            k = key(e["term"])
            if k:
                self.by_first.setdefault(k[0], []).append((k, e))
        for v in self.by_first.values():
            v.sort(key=lambda x: -len(x[0]))
        self.n = sum(len(v) for v in self.by_first.values())

    @staticmethod
    def _has(words: set, qkey: tuple, phrase: str) -> bool:
        pk = key(phrase)
        if len(pk) == 1:
            return pk[0] in words
        return any(qkey[i:i + len(pk)] == pk for i in range(len(qkey) - len(pk) + 1))

    def apply(self, q: str) -> tuple[str, list[dict]]:
        if not self.n or not q:
            return q, []
        toks = _tokens(q)
        qkey = tuple(_stem(t) for t, _, _ in toks)
        words = set(qkey)
        out, applied, i, last = [], [], 0, 0
        while i < len(toks):
            hit = None
            for k, e in self.by_first.get(qkey[i], ()):
                if qkey[i:i + len(k)] != k:
                    continue
                span = toks[i:i + len(k)]
                if any(_looks_like_part(t) and t != _tokens(e["term"])[j][0] for j, (t, _, _) in enumerate(span)):
                    continue
                if e["any"] and not any(self._has(words, qkey, w) for w in e["any"]):
                    continue
                if e["none"] and any(self._has(words, qkey, w) for w in e["none"]):
                    continue
                hit = (k, e)
                break
            if hit is None:
                i += 1
                continue
            k, e = hit
            s, t = toks[i][1], toks[i + len(k) - 1][2]
            out.append(q[last:s])
            out.append(e["to"])
            applied.append({"id": e["id"], "kind": e["kind"], "from": q[s:t], "to": e["to"]})
            last = t
            i += len(k)
        if not applied:
            return q, []
        out.append(q[last:])
        return re.sub(r"\s+", " ", "".join(out)).strip(), applied


# ---------------------------------------------------------------------------------------------------------- store
class Conflict(Exception):
    pass


class Dictionary:
    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.path = self.root / "dictionary.json"
        self.log_path = self.root / "dictionary_log.jsonl"
        self.lock = threading.Lock()
        self.version, self._entries = 0, {}
        self.lint = Lint()
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                d = json.loads(self.path.read_text())
                self.version = int(d.get("version", 0))
                self._entries = {e["id"]: e for e in d.get("entries", [])}
            except (json.JSONDecodeError, OSError, KeyError):
                pass
        self.matcher = Matcher(list(self._entries.values()))

    # ------------------------------------------------------------------ read
    def entries(self) -> list[dict]:
        return list(self._entries.values())

    def get(self, eid: str) -> dict | None:
        return self._entries.get(eid)

    def apply(self, q: str, entries: list[dict] | None = None) -> tuple[str, list[dict]]:
        m = self.matcher if entries is None else Matcher(entries)
        return m.apply(q)

    def with_draft(self, draft: dict | None, remove: str | None = None) -> list[dict]:
        """The live set with one entry added, changed (as if live) or removed: for "try it"."""
        es = [e for e in self._entries.values() if e["id"] not in {remove, (draft or {}).get("id")}]
        if draft:
            d = clean(draft)
            d["status"] = "live"
            es.append(d)
        return es

    def counts(self) -> dict:
        c: dict = {"total": len(self._entries), "status": {s: 0 for s in STATUSES}, "kind": {k: 0 for k in KINDS}}
        for e in self._entries.values():
            c["status"][e["status"]] += 1
            c["kind"][e["kind"]] += 1
        c["live_rules"] = self.matcher.n
        return c

    def history(self, eid: str | None = None, limit: int = 200) -> list[dict]:
        if not self.log_path.exists():
            return []
        out = []
        for ln in reversed(self.log_path.read_text().splitlines()):
            try:
                r = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if eid and r.get("id") != eid and eid not in (r.get("ids") or []):
                continue
            out.append(r)
            if len(out) >= limit:
                break
        return out

    # ------------------------------------------------------------------ write
    def _check_base(self, base_version):
        if base_version is not None and int(base_version) != self.version:
            raise Conflict(f"the dictionary changed since you loaded it (now version {self.version}); reload and reapply")

    def _flags(self, e: dict) -> dict:
        others = [o for o in self._entries.values() if o["id"] != e["id"]]
        e["flags"] = [x["code"] for x in self.lint.check(e, others)]
        errs = [x["text"] for x in self.lint.check(e, others) if x["level"] == "error"]
        if e["status"] == "live" and errs:
            raise ValueError("Can't go live: " + " ".join(errs))
        return e

    def save(self, entry: dict, who: str = "", note: str = "", base_version: int | None = None) -> dict:
        with self.lock:
            self._check_base(base_version)
            e = clean(entry)
            old = self._entries.get(e["id"]) if entry.get("id") else None
            if old is None and e["id"] in self._entries:
                old = self._entries[e["id"]]
            if old is None and len(self._entries) >= MAX_ENTRIES:
                raise ValueError(f"the dictionary holds at most {MAX_ENTRIES:,} entries")
            now = time.strftime("%Y-%m-%d %H:%M:%S")
            e["created_at"] = (old or {}).get("created_at") or now
            e["created_by"] = (old or {}).get("created_by") or (who or "unknown")[:60]
            e["updated_at"], e["updated_by"] = now, (who or "unknown")[:60]
            self._flags(e)
            if old and {k: v for k, v in old.items() if k not in ("updated_at", "updated_by")} == \
                    {k: v for k, v in e.items() if k not in ("updated_at", "updated_by")}:
                return old
            self._entries[e["id"]] = e
            self._commit([{"action": "edit" if old else "add", "id": e["id"], "before": old, "after": e,
                           "note": note[:200]}], who)
            return e

    def delete(self, eid: str, who: str = "", note: str = "", base_version: int | None = None) -> None:
        with self.lock:
            self._check_base(base_version)
            old = self._entries.pop(eid, None)
            if old is None:
                raise KeyError(eid)
            self._commit([{"action": "delete", "id": eid, "before": old, "after": None, "note": note[:200]}], who)

    def set_status(self, ids: list[str], status: str, who: str = "", note: str = "",
                   base_version: int | None = None) -> dict:
        """Approve, send back to review or switch off several entries at once. Entries with errors can't go live;
        they are reported and left as they were."""
        if status not in STATUSES:
            raise ValueError(f"status must be one of {', '.join(STATUSES)}")
        with self.lock:
            self._check_base(base_version)
            changed, refused = [], []
            for eid in ids[:5000]:
                e = self._entries.get(eid)
                if not e or e["status"] == status:
                    continue
                new = {**e, "status": status, "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "updated_by": (who or "unknown")[:60]}
                try:
                    self._flags(new)
                except ValueError as ex:
                    refused.append({"id": eid, "term": e["term"], "why": str(ex)})
                    continue
                changed.append({"action": "status", "id": eid, "before": e, "after": new, "note": note[:200]})
                self._entries[eid] = new
            if changed:
                self._commit(changed, who)
            return {"changed": len(changed), "refused": refused}

    def restore(self, eid: str, at: str, who: str = "") -> dict:
        """Put an entry back the way it was after the change logged at `at` (or before it was deleted)."""
        rec = next((r for r in self.history(eid, 5000) if r.get("at") == at), None)
        if rec is None:
            raise KeyError(f"no change to {eid} at {at}")
        snap = rec.get("after") or rec.get("before")
        if snap is None:
            raise ValueError("nothing to restore")
        snap = {**snap, "status": "review"} if rec.get("action") == "delete" else snap
        return self.save(snap, who, note=f"restored the version of {at}")

    def import_rows(self, rows: list[dict], who: str = "", source: str = "", replace_source: bool = False) -> dict:
        """Add or update many entries (a reviewed spreadsheet). Rows with an id update that entry; others are keyed
        by kind + term + target. `replace_source`: entries from the same source that aren't in the file are removed."""
        with self.lock:
            now = time.strftime("%Y-%m-%d %H:%M:%S")
            log, seen, bad = [], set(), []
            for n, r in enumerate(rows, 1):
                try:
                    e = clean({**r, "source": r.get("source") or source})
                except ValueError as ex:
                    bad.append({"row": n, "why": str(ex)})
                    continue
                old = self._entries.get(e["id"])
                e["created_at"] = (old or {}).get("created_at") or now
                e["created_by"] = (old or {}).get("created_by") or (who or "import")[:60]
                e["updated_at"], e["updated_by"] = now, (who or "import")[:60]
                seen.add(e["id"])
                self._entries[e["id"]] = e
                log.append({"action": "import", "id": e["id"], "before": old, "after": e, "note": source[:80]})
            removed = []
            if replace_source and source:
                for eid, e in list(self._entries.items()):
                    if e.get("source") == source and eid not in seen:
                        removed.append(self._entries.pop(eid))
                        log.append({"action": "delete", "id": eid, "before": e, "after": None, "note": f"not in {source}"})
            # lint after everything is in (conflicts and blocks depend on the whole set); errors send a row to review
            demoted = 0
            for eid in seen:
                e = self._entries[eid]
                others = [o for o in self._entries.values() if o["id"] != eid]
                checks = self.lint.check(e, others)
                e["flags"] = [x["code"] for x in checks]
                if e["status"] == "live" and any(x["level"] == "error" for x in checks):
                    e["status"] = "review"
                    demoted += 1
            if log:
                self._commit(log, who)
            return {"imported": len(seen), "removed": len(removed), "demoted_to_review": demoted, "bad_rows": bad}

    def relint(self) -> int:
        """Recompute every entry's flags (after the catalog or the lint rules change)."""
        with self.lock:
            n = 0
            es = list(self._entries.values())
            for e in es:
                fl = [x["code"] for x in self.lint.check(e, [o for o in es if o["id"] != e["id"]])]
                if fl != e.get("flags"):
                    e["flags"] = fl
                    n += 1
            if n:
                self._write()
            return n

    def _write(self):
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": self.version, "entries": sorted(self._entries.values(), key=lambda e: e["term"].lower())},
                                  indent=0, ensure_ascii=False))
        os.replace(tmp, self.path)

    def _commit(self, recs: list[dict], who: str):
        self.version += 1
        at = time.strftime("%Y-%m-%d %H:%M:%S")
        self._write()
        with open(self.log_path, "a") as f:
            for r in recs:
                f.write(json.dumps({"version": self.version, "at": at, "who": (who or "unknown")[:60], **r},
                                   ensure_ascii=False) + "\n")
        self.matcher = Matcher(list(self._entries.values()))

    # ------------------------------------------------------------------ files
    CSV_COLS = ("id", "kind", "term", "to", "any", "none", "dept", "status", "definition", "note", "qc", "source")

    def to_csv(self, entries: list[dict] | None = None) -> str:
        from webbsearch.csv_safe import csv_safe_row
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(self.CSV_COLS)
        for e in sorted(entries if entries is not None else self._entries.values(), key=lambda e: (e["kind"], e["term"].lower())):
            w.writerow(csv_safe_row([", ".join(e[c]) if c in ("any", "none") else e.get(c, "") for c in self.CSV_COLS]))
        return buf.getvalue()

    @staticmethod
    def rows_from_file(name: str, raw: bytes) -> list[dict]:
        """CSV or Excel with a header row (id, kind, term, to, any, none, dept, status, definition, note, ...).
        The workbook's own columns (Search Term, Synonym, Department, Handling, Notes) are read too."""
        alias = {"search term": "term", "synonym": "to", "department": "dept", "notes": "note", "handling": "handling",
                 "target": "to", "context": "any", "context (any)": "any", "not with": "none", "verdict": "verdict"}
        if name.lower().endswith((".xlsx", ".xlsm")):
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
            sheets = [[[("" if c is None else str(c)) for c in r] for r in ws.iter_rows(values_only=True)] for ws in wb.worksheets]
            ok = [d for d in sheets if any(any(c.strip().lower() in ("term", "search term") for c in r) for r in d[:20])]
            data = max(ok or sheets, key=len)           # the sheet with the most rows (the complete table)
        else:
            data = list(csv.reader(io.StringIO(raw.decode("utf-8-sig", errors="replace"))))
        hi = next((i for i, r in enumerate(data[:20]) if any(c.strip().lower() in ("term", "search term") for c in r)), None)
        if hi is None:
            raise ValueError("no header row with a “term” (or “Search Term”) column in the first 20 rows")
        head = [alias.get(c.strip().lower(), c.strip().lower()) for c in data[hi]]
        out = []
        for r in data[hi + 1:]:
            d = {h: (r[j] if j < len(r) else "").strip() for j, h in enumerate(head) if h}
            if not d.get("term"):
                continue
            if "kind" not in d and d.get("handling"):
                d["kind"] = {"do not expand": "block", "spelling correction": "spelling", "brand mapping": "brand",
                             "attribute mapping": "glossary"}.get(d["handling"].lower(), "synonym")
            out.append(d)
        return out


def builtin_vocabulary() -> list[dict]:
    """The analyzer's built-in rules (applied to the catalog and to searches alike; a change needs a rebuild)."""
    from webbsearch.retrieval.analyzer import CANON, CONCEPT_SYNONYMS
    out = [{"term": k, "to": v or "(ignored)", "where": "catalog and searches"} for k, v in CANON.items()
           if k not in ("'", "&", "-")]
    out += [{"term": v, "to": k, "where": "added to products that mention the target"} for k, v in CONCEPT_SYNONYMS.items()]
    return out


def copy_entries(es: list[dict]) -> list[dict]:
    return copy.deepcopy(es)
