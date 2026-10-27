"""Trade words in 12 languages (v2.4): a search typed in Spanish, Portuguese, Polish, Russian, Chinese, Albanian,
Haitian Creole, Vietnamese, Arabic, French, Italian or Ukrainian is read into the catalog's English before anything
else reads it, and the page says so ("Searched as ... · Portuguese").

The languages are the ones F.W. Webb's trades speak: Census ACS 2020-2024 PUMS, construction and installation/repair
workers in Webb's nine states who speak English less than "very well", weighted by Webb's branches per state.

Each language is a file in terms/: one line per concept, `english<TAB>term|term|...`. All languages translate to the
same English concepts (terms/concepts.tsv), so coverage is measurable. A term:
    * "válvula* de bola"   words separated by spaces; a word ending in * is a stem (válvula, válvulas)
    * accents and case don't matter: terms and searches are folded (ł→l, đ→d, ё→е, أ→ا, ة→ه, full-width → ASCII)
    * Chinese terms are matched as written (no spaces); Arabic terms also match with ال, وال, بال, لل, ب, ل
Which language: the alphabet (Cyrillic, Chinese, Arabic), the accents only one language uses, and which terms match.
A multiword term counts 3, a word of 4+ letters that isn't English 2, a short or English word 0.5, the accents 1 and
the language's own small words 0.5 each (up to 1.5, only alongside a term). Read as that language when the best
score is at least 2. So an English search ("3/4 te", "van tool", "gang box") is never touched.

Sizes the way each trade says them: DN15/DN20 (any of these languages), Chinese 4分 = 1/2 in., 6分 = 3/4 in.,
1寸 = 1 in., 寸二 = 1-1/4 in., 寸半 = 1-1/2 in.; Vietnamese phi 21 = 1/2 in., phi 27 = 3/4 in. (PVC outside diameters
in mm); the half, three-quarter and quarter words are terms in each file."""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).resolve().parent / "terms"

LANGS: dict[str, dict] = {
    "es": {"name": "Spanish", "script": "latin", "signal": r"[ñ¿¡]",
           "small": {"de", "del", "para", "con", "sin", "los", "las", "el", "la", "una", "un", "y", "en", "por"}},
    "pt": {"name": "Portuguese", "script": "latin", "signal": r"[ãõ]|ç[ãaõo]",
           "small": {"de", "do", "da", "dos", "das", "para", "com", "sem", "o", "os", "as", "um", "uma", "e", "em",
                     "no", "na", "p/"}},
    "pl": {"name": "Polish", "script": "latin", "signal": r"[ąćęłńśźż]",
           "small": {"do", "dla", "z", "ze", "i", "na", "bez", "w", "oraz", "pod", "od"}},
    "ru": {"name": "Russian", "script": "cyrillic", "signal": r"[ыэъё]",
           "small": {"для", "с", "со", "и", "на", "под", "в", "к", "из", "по", "без", "от", "dlya", "pod"}},
    "zh": {"name": "Chinese", "script": "cjk", "signal": None, "small": {"的", "个", "個", "用", "和", "与", "與"}},
    "sq": {"name": "Albanian", "script": "latin", "signal": r"[ë]",
           "small": {"për", "per", "me", "i", "e", "të", "te", "dhe", "pa", "nga", "në", "ne"}},
    "ht": {"name": "Haitian Creole", "script": "latin", "signal": r"[èò]",
           "small": {"pou", "ak", "nan", "yon", "san", "sou", "la", "an"}},
    "vi": {"name": "Vietnamese", "script": "latin",
           "signal": r"[ăâđơưạảấầẩẫậắằẳẵặẹẻẽếềểễệỉịọỏốồổỗộớờởỡợụủứừửữựỳỵỷỹ]",
           "small": {"cho", "của", "cua", "và", "va", "loại", "loai", "cái", "cai"}},
    "ar": {"name": "Arabic", "script": "arabic", "signal": None,
           "small": {"و", "في", "من", "مع", "على", "لل", "ل"}},
    "fr": {"name": "French", "script": "latin", "signal": r"[èêëîïôûùœ]|\b[dl]['’]",
           "small": {"de", "du", "des", "la", "le", "les", "à", "a", "au", "aux", "pour", "avec", "sans", "en", "et",
                     "d'", "l'"}},
    "it": {"name": "Italian", "script": "latin", "signal": r"[àìòù]",
           "small": {"di", "del", "della", "dei", "da", "per", "con", "senza", "il", "lo", "gli", "a", "ad", "in",
                     "e"}},
    "uk": {"name": "Ukrainian", "script": "cyrillic", "signal": r"[іїєґ]",
           "small": {"для", "з", "із", "зі", "і", "й", "та", "на", "під", "в", "у", "без", "від"}},
}
ORDER = list(LANGS)                      # tie-break: the bigger trade population first
# targets a file may use that aren't concepts of their own (sizes, units, small words)
EXTRA_TARGETS = {"inch", "1/2 inch", "3/4 inch", "1/4 inch", "3/8 inch", "1 inch", "1-1/4 inch", "1-1/2 inch",
                 "2 inch", "ft", "gallon", "volt", "ton", "for", "copper pipe", "black iron pipe", "air vent",
                 "condensing unit", "shower valve", "drain valve", "sewage pump", "pvc", "3/8 inch", "reversing valve", "TXV valve"}

_CYR, _CJK, _ARAB = re.compile(r"[Ѐ-ӿ]"), re.compile(r"[㐀-䶿一-鿿]"), re.compile(r"[؀-ۿ]")
_MANUAL = str.maketrans({"ł": "l", "Ł": "l", "đ": "d", "Đ": "d", "ø": "o", "ß": "ss", "œ": "oe", "æ": "ae", "ı": "i",
                         "ة": "ه", "ى": "ي", "ـ": "", "’": "'", "‘": "'", "–": "-", "—": "-", "‐": "-",
                         **{chr(0x660 + i): str(i) for i in range(10)}, **{chr(0x6f0 + i): str(i) for i in range(10)}})
_ARTICLE = r"(?:و?(?:ال|بال|لل|ب|ل)?)"


_MANUAL_TONES = str.maketrans({"’": "'", "‘": "'", "–": "-", "—": "-", "‐": "-"})


def lfold(lang: str, s: str) -> str:
    """Fold for one language. Vietnamese keeps its tone marks and đ (cỡ = size, co = elbow; công = power, cống = drain);
    its untoned spellings are entries of their own. Every other language folds accents away."""
    if lang == "vi":
        return unicodedata.normalize("NFC", (s or "").translate(_MANUAL_TONES)).lower()
    return fold(s)


def fold(s: str) -> str:
    """Lowercase, no accents or tashkeel, Arabic letter forms joined, Arabic-Indic and full-width digits as ASCII."""
    s = unicodedata.normalize("NFKD", (s or "").translate(_MANUAL))
    return "".join(c for c in s if not unicodedata.combining(c)).translate(_MANUAL).lower()


# ------------------------------------------------------------------ the files
@lru_cache(maxsize=1)
def _concepts() -> tuple[dict, ...]:
    out = []
    for line in (DATA / "concepts.tsv").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            en, tier = (line.split("\t") + ["full"])[:2]
            out.append({"en": en.strip(), "tier": tier.strip()})
    return tuple(out)


def concepts() -> list[str]:
    return [c["en"] for c in _concepts()]


def core_concepts() -> list[str]:
    return [c["en"] for c in _concepts() if c["tier"] == "core"]


@lru_cache(maxsize=None)
def entries(lang: str) -> dict[str, list[str]]:
    """english -> its terms in `lang`, as written in the file."""
    out: dict[str, list[str]] = {}
    for n, line in enumerate((DATA / f"{lang}.tsv").read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.startswith("#"):
            continue
        if "\t" not in line:
            raise ValueError(f"{lang}.tsv line {n}: english<TAB>terms")
        en, terms = line.split("\t", 1)
        ts = [t.strip() for t in terms.split("|") if t.strip()]
        out.setdefault(en.strip(), []).extend(ts)
    return out


def coverage(lang: str) -> dict:
    have = {en for en, ts in entries(lang).items() if ts}
    full, core = concepts(), core_concepts()
    return {"full": round(sum(c in have for c in full) / len(full), 3),
            "core": round(sum(c in have for c in core) / len(core), 3), "terms": sum(len(v) for v in entries(lang).values())}


@lru_cache(maxsize=1)
def english_vocab() -> frozenset:
    """Words English searches use (the catalog's words and common trade words). A term that is one of these counts
    for little until the search shows its language some other way."""
    return frozenset(w.strip() for w in (DATA / "english_vocab.txt").read_text(encoding="utf-8").split() if w.strip())


def _term_rx(term: str, script: str, lang: str = "") -> tuple[str, int, int, bool]:
    """(regex, words, length, weak) for one term."""
    t = lfold(lang, term)
    if script == "cjk":
        return re.escape(t), 1 if len(t) < 3 else 2, len(t), len(t) < 2
    parts = t.split()
    rx = []
    for i, p in enumerate(parts):
        star = p.endswith("*")
        base = p.rstrip("*")
        piece = re.escape(base) + (r"\w*" if star else "")
        if script == "arabic":            # ال (the), و (and), ب (with), ل (for) attach to the word
            piece = (_ARTICLE if i == 0 else "(?:ال)?") + piece
        rx.append(piece)
    body = r"[\s\-]+".join(rx)
    def english(p: str) -> bool:
        b = p.rstrip("*")
        return (len(b) <= 3 or b in english_vocab()
                or (p.endswith("*") and any(w.startswith(b) and len(w) <= len(b) + 3 for w in _vocab_prefix(b[:3]))))
    # a word that's also English (or a short one), or a phrase made only of English words ("push fit")
    # in a phrase, only a real English word of 3+ letters counts as English ("van bi" is Vietnamese, "push fit" isn't)
    weak = (english(parts[0]) if len(parts) == 1 else
            all(len(p.rstrip("*")) >= 3 and english(p) for p in parts)) if parts else True
    return rf"(?<![\w]){body}(?![\w])", len(parts), len(t), weak


@lru_cache(maxsize=None)
def _vocab_prefix(p: str) -> tuple[str, ...]:
    return tuple(w for w in english_vocab() if w.startswith(p))


_KEY = 3                                 # terms are indexed by their first letters (as written, folded)
_ARABIC_PREFIXES = ("وال", "بال", "لل", "ال", "و", "ب", "ل")


@lru_cache(maxsize=None)
def _compiled(lang: str):
    """Terms indexed by their first letters: key -> [(compiled, english, words, weak)], longest terms first, so a
    search checks only the few terms that could start at each word."""
    script = LANGS[lang]["script"]
    items = []
    for en, ts in entries(lang).items():
        for t in ts:
            sub = script
            if script == "cyrillic" and not _CYR.search(t):
                sub = "latin"                                 # Russian typed in Latin letters
            if script == "cjk" and not _CJK.search(t):
                sub = "latin"
            rx, words, n, weak = _term_rx(t, sub, lang)
            body = rx[len("(?<![\\w])"):] if rx.startswith("(?<![\\w])") else rx
            ft = lfold(lang, t)
            key = ft.replace("*", "")[:_KEY] if sub != "cjk" else ft[:1]
            items.append((words, n, key, re.compile(body), en, weak))
    items.sort(key=lambda x: (-x[0], -x[1]))
    index: dict[str, list] = {}
    for words, n, key, rx, en, weak in items:
        index.setdefault(key, []).append((rx, en, words, weak))
    return index


_WORD_START = re.compile(r"(?<![\w])[\w/]")


def _matches(lang: str, t: str) -> list[tuple[int, int, str, int, bool]]:
    """Leftmost-longest, non-overlapping term matches in folded text `t`."""
    index = _compiled(lang)
    cjk = LANGS[lang]["script"] == "cjk"
    starts = range(len(t)) if cjk else [m.start() for m in _WORD_START.finditer(t)]
    out, pos = [], 0
    for i in starts:
        if i < pos:
            continue
        keys = {t[i:i + k] for k in (1, 2, 3)}
        if LANGS[lang]["script"] == "arabic":
            for pre in _ARABIC_PREFIXES:
                if t.startswith(pre, i):
                    j = i + len(pre)
                    keys |= {t[j:j + k] for k in (1, 2, 3)}
        best = None
        for key in keys:
            for rx, en, words, weak in index.get(key, ()):
                m = rx.match(t, i)
                if m and (best is None or (words, m.end()) > (best[3], best[1])):
                    best = (i, m.end(), en, words, weak)
                    break                                    # the bucket is longest-first
        if best:
            out.append(best)
            pos = best[1]
    return out


# ------------------------------------------------------------------ sizes
DN = {15: "1/2", 20: "3/4", 25: "1", 32: "1-1/4", 40: "1-1/2", 50: "2", 65: "2-1/2", 80: "3", 100: "4", 125: "5",
      150: "6"}
_DN = re.compile(r"(?<![\w])(?:dn|ду)\s*(\d{2,3})(?![\w])")
_ZH_NUM = re.compile(r"(?<![\d一二两兩三四五六七八九十])([一二两兩三四五六八])(?=\s*(?:分|寸|英寸))")
ZH_DIGIT = {"一": "1", "二": "2", "两": "2", "兩": "2", "三": "3", "四": "4", "五": "5", "六": "6", "八": "8"}
_ZH_CUN_HALF = re.compile(r"(?<![\d.])(\d)\s*寸\s*(半|二|2)(?![\d.])")       # 1寸半 = 1-1/2, 1寸2 = 1-1/4, 2寸半
_ZH_FEN = re.compile(r"(?<![\d.])(\d)\s*分(?![钟鐘])")                         # 4分 = 1/2 in., not 6分钟 (minutes)
_ZH_CUN = re.compile(r"(?<![\d.])(\d+(?:\.\d)?)\s*(?:英)?寸|寸二|寸半")
FEN = {"1": "1/8", "2": "1/4", "3": "3/8", "4": "1/2", "6": "3/4"}
CUN = {"1": "1", "1.2": "1-1/4", "1.5": "1-1/2", "2": "2", "2.5": "2-1/2", "3": "3", "4": "4", "5": "5", "6": "6",
       "寸二": "1-1/4", "寸半": "1-1/2"}
_VI_PHI = re.compile(r"(?<![\w])(?:phi|φ|ϕ)\s*(\d{2,3})(?![\w])")
PHI = {21: "1/2", 27: "3/4", 34: "1", 42: "1-1/4", 49: "1-1/2", 60: "2", 75: "2-1/2", 76: "2-1/2", 90: "3", 114: "4",
       168: "6", 220: "8"}                 # Bình Minh PVC outside diameters (76 is the steel size)


def _sizes(t: str, lang: str) -> str:
    t = _DN.sub(lambda m: f" {DN[int(m.group(1))]} inch " if int(m.group(1)) in DN else m.group(0), t)
    if lang == "zh":
        t = _ZH_NUM.sub(lambda m: ZH_DIGIT[m.group(1)], t)          # 四分 = 4分, 一寸半 = 1寸半

        def half(m):
            n, tail = m.group(1), m.group(2)
            if tail == "半":
                return f" {n}-1/2 inch "
            return " 1-1/4 inch " if n == "1" else m.group(0)        # 1寸2 / 1寸二 = 1.2寸 = 1-1/4
        t = _ZH_CUN_HALF.sub(half, t)
        t = _ZH_FEN.sub(lambda m: f" {FEN[m.group(1)]} inch " if m.group(1) in FEN else m.group(0), t)

        def cun(m):
            k = m.group(1) if m.group(1) else m.group(0)
            return f" {CUN[k]} inch " if k in CUN else m.group(0)
        t = _ZH_CUN.sub(cun, t)
    if lang == "vi":
        t = _VI_PHI.sub(lambda m: f" {PHI[int(m.group(1))]} inch " if int(m.group(1)) in PHI else m.group(0), t)
    return t


def _size_evidence(t: str) -> dict:
    ev = {}
    if _VI_PHI.search(t):
        ev["vi"] = 2.0
    return ev


# ------------------------------------------------------------------ reading a search
def _scripts(raw: str) -> set:
    s = set()
    if _CJK.search(raw):
        s.add("cjk")
    if _ARAB.search(raw):
        s.add("arabic")
    if _CYR.search(raw):
        s.add("cyrillic")
    return s


def _candidates(raw: str) -> list[str]:
    sc = _scripts(raw)
    if "cjk" in sc:
        return ["zh"]
    if "arabic" in sc:
        return ["ar"]
    if "cyrillic" in sc:
        return ["ru", "uk"]
    return [k for k, v in LANGS.items() if v["script"] == "latin"] + ["ru"]     # Russian typed in Latin letters


def score(lang: str, raw: str, t: str, ms=None) -> float:
    ms = _matches(lang, t) if ms is None else ms
    if not ms:
        return 0.0
    s = sum(0.5 if weak else 3.0 if words > 1 else 2.0 for _, _, _, words, weak in ms)
    sig = LANGS[lang]["signal"]
    signal = bool(sig and re.search(sig, raw.lower()))
    size = _size_evidence(fold(raw)).get(lang, 0.0)
    if not signal and not size and LANGS[lang]["script"] not in _scripts(raw) and all(weak for *_, weak in ms):
        return 0.0                         # only English-looking words matched: an English search
    if signal:
        s += 3.0 if LANGS[lang]["script"] == "cyrillic" else 1.0
    toks = re.findall(r"[\w']+/?", t)
    s += min(1.5, 0.5 * sum(1 for x in toks if x in {lfold(lang, w) for w in LANGS[lang]["small"]}))
    s += size
    if LANGS[lang]["script"] in ("cjk", "arabic"):
        s += 1.0
    return s


def detect(q: str) -> str | None:
    raw = q or ""
    best, top = None, 0.0
    for lang in _candidates(raw):
        t = lfold(lang, _sizes(lfold(lang, raw), lang))
        sc = score(lang, raw, t)
        if sc > top + 1e-9:
            best, top = lang, sc
    return best if top >= 2.0 else None


def translate(q: str) -> tuple[str, str | None]:
    """(the search in the catalog's English, the language code) or (q unchanged, None) for an English search."""
    lang = detect(q)
    if lang is None:
        return q, None
    t = lfold(lang, _sizes(lfold(lang, q), lang))
    ms, taken, out, last = _matches(lang, t), [], [], 0
    for a, b, en, _, _ in ms:                        # finditer is leftmost-longest and never overlaps
        out.append(t[last:a])
        out.append(f" {en} ")
        last = b
    out.append(t[last:])
    text = "".join(out)
    small = {lfold(lang, w) for w in LANGS[lang]["small"]}
    words = [w for w in re.split(r"\s+", text) if w and w not in small]
    # Chinese: what's left between the terms is spoken filler (的, 个) or untranslated words, kept apart
    return re.sub(r"\s+", " ", " ".join(words)).strip(), lang


def name(lang: str) -> str:
    return LANGS[lang]["name"]


def warm() -> None:
    """Build every language's index and the English word list once, at engine start (about 1 s)."""
    for lang in LANGS:
        _compiled(lang)
    translate("3/4 ball valve")
