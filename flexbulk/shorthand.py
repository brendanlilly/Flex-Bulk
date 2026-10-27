"""ERP shorthand -> the words the item list uses ("MXG VLV ADJ 3W THM" -> mixing valve adjustable 3 way thermostatic).

Vendor and ERP descriptions are cut to fit a short field. Two sources of expansions, applied to what is searched
(the line staff see is not changed):
  1. a starting list of common ones (STARTER)
  2. learned: lines whose part # found their item pair an abbreviated description with the item's own words; an
     abbreviation is learned when its letters appear in order in a word of the item ("VLV" in "valve", "BNR" in
     "burner") and that word is the one it lines up with most often. Learned ones wait for review on the dashboard
     (Shorthand page) unless they were seen 3 or more times.
Stored in data/bulk/shorthand.json: {"on": {abbr: word}, "review": {abbr: {word, n, examples}}, "off": [abbr]}.
"""
from __future__ import annotations

import json
import os
import re
import threading
from collections import Counter, defaultdict
from pathlib import Path

from webbsearch.config import settings

STARTER = {
    "vlv": "valve", "vlvs": "valve", "valv": "valve", "bnr": "burner", "brnr": "burner", "tnk": "tank", "mtr": "motor",
    "blwr": "blower", "bwr": "blower", "pmp": "pump", "circ": "circulator", "insul": "insulation", 
    "tstat": "thermostat", "t-stat": "thermostat", "thm": "thermostatic",
    "mxg": "mixing", "mix": "mixing", "adj": "adjustable", "repl": "replacement", "rplc": "replacement",
    "malle": "malleable", "mall": "malleable", "galv": "galvanized", "cop": "copper", "cu": "copper", "brs": "brass",
    "brz": "bronze", "ss": "stainless steel", "sst": "stainless steel", "ci": "cast iron", "di": "ductile iron",
    "exp": "expansion", "expan": "expansion", "hyd": "hydronic", "htr": "heater", "wtr": "water", "wh": "water heater",
    "cond": "condensate", "condens": "condensate", "neut": "neutralizer", "neutr": "neutralizer", "sep": "separator",
    "elim": "eliminator", "mnfld": "manifold", "manif": "manifold", "brnch": "branch", 
    "redcr": "reducer", "rdcr": "reducer", "cplg": "coupling", "cpl": "coupling", "adpt": "adapter", "adptr": "adapter",
    "fem": "female", "fmle": "female", "thd": "threaded", "thrd": "threaded",     "prs": "press", "flg": "flange", "flgd": "flanged", "nip": "nipple", "unn": "union", "bshg": "bushing",
    "bush": "bushing", "sch": "schedule", "lf": "lead free", "nl": "no lead", 
    "ftg": "fitting", "ftgs": "fitting", "fitt": "fitting",
    "assy": "assembly", "asm": "assembly", "kt": "kit", "pkg": "package", "pk": "pack", "bx": "box",
    "elec": "electric", "elect": "electric", "ng": "natural gas", "lp": "propane", 
    "1p": "single phase", "3p": "three phase", "flex": "flexible", "rbr": "rubber",
    "jckt": "jacket", "jkt": "jacket", "slfsealg": "self sealing", "sil": "silver", "blk": "black", "wht": "white",
    "recept": "receptacle", "rcpt": "receptacle", "swt": "sweat", "disc": "disconnect",
    "cntrl": "control", "ctrl": "control", "ctl": "control", "xfmr": "transformer", 
    "snsr": "sensor", "dmpr": "damper", "dampr": "damper", "zn": "zone", "fltr": "filter",
    "filt": "filter", "humid": "humidifier", "dehum": "dehumidifier", "refrig": "refrigerant", 
    "cmpr": "compressor", "evap": "evaporator", "ucsd": "uncased",
    "csd": "cased", "ahu": "air handler", "hdler": "handler", "drn": "drain", 
    "lg": "large", "xl": "extra large", "hd": "heavy duty",
    "ell": "elbow", "ells": "elbow", "nom": "nominal",
}

PATH = Path(os.environ.get("FLEXBULK_SHORTHAND") or settings.data_dir / "bulk" / "shorthand.json")
WORD = re.compile(r"[A-Za-z][A-Za-z.-]*")


def _subseq(a: str, w: str) -> bool:
    """The abbreviation's letters appear in order in the word, starting with its first letter."""
    if not a or not w or a[0] != w[0]:
        return False
    it = iter(w)
    return all(c in it for c in a)


class Shorthand:
    def __init__(self, path: Path | None = None):
        self.path = Path(path or PATH)
        self.lock = threading.Lock()
        self.data = self._read()
        self._rx = None

    def _read(self) -> dict:
        try:
            d = json.loads(self.path.read_text())
            return {"on": d.get("on", {}), "review": d.get("review", {}), "off": d.get("off", [])}
        except (OSError, ValueError):
            return {"on": {}, "review": {}, "off": []}

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, sort_keys=True))
        os.replace(tmp, self.path)
        self._rx = None

    def table(self) -> dict[str, str]:
        t = {k: v for k, v in STARTER.items()}
        t.update(self.data["on"])
        for k in self.data["off"]:
            t.pop(k, None)
        return t

    def expand(self, text: str) -> str:
        """Whole-word replacements, case kept out of it; words already in the item list's language are untouched
        unless they are in the table."""
        t = self.table()
        if not text:
            return text
        def rep(m):
            w = m.group(0)
            x = t.get(w.lower().rstrip("."))
            return x if x else w
        out = re.sub(r"(?<![\w-])[A-Za-z][A-Za-z.]*(?![\w-])", rep, text)
        out = re.sub(r"\b(\d)(1/[248])\b", r"\1-\2", out)          # 11/4 -> 1-1/4, 21/2 -> 2-1/2 (ERP fields drop the dash)
        out = re.sub(r"(?<=\d)\s*inch(es)?\b", '"', out, flags=re.I)
        out = re.sub(r"(?<=\d)\s*(?:foot|feet)\b", "'", out, flags=re.I)
        return out

    # ------------------------------------------------------------------ learning
    def learn(self, pairs: list[tuple[str, str]], known_words=None) -> dict:
        """pairs: (the line's description, the item's title and attribute words) for lines whose part # found
        the item. Returns what was learned: {"on": {...}, "review": {...}}."""
        votes: dict[str, Counter] = defaultdict(Counter)
        ex: dict[str, list] = defaultdict(list)
        t = self.table()
        for desc, item in pairs:
            dw = [w.lower().strip(".-") for w in WORD.findall(desc or "")]
            iw = {w.lower().strip(".-") for w in WORD.findall(item or "") if len(w) >= 3}
            for a in set(dw):
                if len(a) < 2 or len(a) > 7 or a in iw or a in t:
                    continue
                if known_words is not None and known_words(a):
                    continue                     # a word the item list uses: not an abbreviation
                cands = [w for w in iw if len(w) > len(a) and _subseq(a, w)]
                for w in cands:
                    votes[a][w] += 1
                if cands and len(ex[a]) < 3:
                    ex[a].append(desc[:60])
        new_on, new_rev = {}, {}
        with self.lock:
            for a, c in votes.items():
                (w, n), *rest = c.most_common()
                if rest and rest[0][1] == n:
                    continue                     # two words tie: not sure what it means
                if a in self.data["off"]:
                    continue
                if n >= 3:
                    self.data["on"][a] = w
                    self.data["review"].pop(a, None)
                    new_on[a] = w
                elif a not in self.data["on"]:
                    r = self.data["review"].get(a) or {"word": w, "n": 0, "examples": []}
                    r.update(word=w, n=max(r["n"], n), examples=ex[a])
                    self.data["review"][a] = r
                    new_rev[a] = w
            self._write()
        return {"on": new_on, "review": new_rev}

    def accept(self, abbr: str, word: str) -> None:
        with self.lock:
            a = abbr.lower().strip()
            self.data["on"][a] = word.strip().lower()
            self.data["review"].pop(a, None)
            if a in self.data["off"]:
                self.data["off"].remove(a)
            self._write()

    def switch_off(self, abbr: str) -> None:
        with self.lock:
            a = abbr.lower().strip()
            self.data["review"].pop(a, None)
            self.data["on"].pop(a, None)
            if a not in self.data["off"]:
                self.data["off"].append(a)
            self._write()

    def listing(self) -> dict:
        return {"starter": STARTER, "on": self.data["on"], "off": self.data["off"],
                "review": sorted(({"abbr": k, **v} for k, v in self.data["review"].items()), key=lambda r: -r["n"])}


_sh: Shorthand | None = None


def get() -> Shorthand:
    global _sh
    if _sh is None:
        _sh = Shorthand()
    return _sh
