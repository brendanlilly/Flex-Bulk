"""The photo index: product photos of real F.W. Webb items, and the part types a photo can be classified as.

Two tables, built by scripts/build_photo_index.py into data/index/photos/:

  * items: one CLIP embedding per product photo (fwwebb.com "Main" image of each real Webb item the prototype knows:
    the cross-reference lake's Webb-stocked parts and the job-kit items), with the item's brand, part number, noun and
    class. A nearest-neighbour search over these is "image likeness".
  * labels: the catalog's product types (family × type, e.g. "copper press 90 elbow", "hot surface igniter") and the
    lake's Webb nouns, each embedded from text prompts. A photo's similarity to them is a zero-shot classifier that
    covers the whole catalog, including the synthetic items that have no photos.

Production: the index covers every SKU with a DAM image and lives next to the product index (a k-NN field in
OpenSearch); the label set is the PIM's product-type taxonomy.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import sqlite3
from pathlib import Path

import numpy as np

from webbsearch.config import settings

# how a family's product type reads in words ("90 Elbow" in cu_fitting_press is a copper press 90 elbow)
PREFIX = {"cu_fitting_press": "copper press", "cu_fitting_sweat": "copper sweat", "iron_fitting": "black iron",
          "pvc_pressure": "PVC pressure", "dwv_fitting": "PVC DWV", "pex_fitting": "PEX", "push_fitting": "push-to-connect",
          "buttweld": "carbon steel buttweld", "grooved": "grooved pipe", "ss_fitting": "stainless steel threaded",
          "nohub": "cast iron no-hub", "duct": "sheet metal duct", "hangers": "pipe", "baseboard": "baseboard",
          "waterworks": "water main"}
NO_TYPE = {"ball_valve": "ball valve", "cu_pipe": "copper pipe", "disposal": "garbage disposal", "filter": "furnace air filter",
           "furnace": "gas furnace", "pex_tubing": "coil of PEX tubing", "prv": "pressure reducing valve",
           "wh_indirect": "indirect water heater", "wh_tank": "tank water heater"}
# the cross-reference lake's part classes, for labels built from the catalog's words
CLS_RULES = [(r"hard start|capacitor", "capacitor"), (r"gas (control )?valve", "gas_valve"), (r"igniter", "igniter"),
             (r"flame (sensor|rod)", "flame_sensor"), (r"inducer", "draft_inducer"),
             (r"blower motor|fan motor|condenser motor|ecm|replacement motor|^motor$|blower$", "motor"),
             (r"contactor", "contactor"), (r"relay", "relay"), (r"control board|integrated control|damper actuator", "control_board"),
             (r"transformer", "transformer"), (r"pressure (switch|control)", "pressure_control"),
             (r"thermostat", "thermostat"), (r"thermocouple", "thermocouple"), (r"ignition (control|module)", "ignition_control"),
             (r"limit switch|rollout switch|door switch", "safety_switch"),
             (r"fan blade|exhaust fan|ventilation fan|blower wheel|^fan$", "fan_ventilation"),
             (r"compressor$", "compressor"), (r"v belt|sheave|pulley", "motor_accessory"), (r"defrost", "timer_defrost"),
             (r"sequencer", "sequencer"), (r"fuse", "fuse")]
PROMPTS = ["a photo of a {}.", "a product photo of a {}, a plumbing or HVAC part.", "a close-up photo of a used {}."]


def label_cls(text: str, family: str = "") -> str | None:
    if family == "cu_fitting_press":
        return "press_fitting"
    t = text.lower()
    for rx, cls in CLS_RULES:
        if re.search(rx, t):
            return cls
    return None


def build_labels(products, xref_db: Path | None) -> list[dict]:
    """Product types from the catalog (family × type) and the lake's Webb nouns, merged by their words."""
    import collections
    seen: dict[str, dict] = {}

    def add(text, family, cat3, n, cls=None, query=None):
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            return
        k = text.lower()
        e = seen.setdefault(k, {"text": text, "query": query or text, "families": [], "cat3": [], "cls": None, "n": 0})
        if family and family not in e["families"]:
            e["families"].append(family)
        if cat3 and cat3 not in e["cat3"]:
            e["cat3"].append(cat3)
        e["cls"] = e["cls"] or cls or label_cls(text, family)
        e["n"] += int(n)

    c = collections.Counter()
    for fam, cat3, a in zip(products["family"], products["cat3"], products["attrs"]):
        d = json.loads(a) if isinstance(a, str) else (a or {})
        c[(fam, cat3, d.get("type"))] += 1
    for (fam, cat3, typ), n in c.items():
        if typ:
            words = f"{PREFIX[fam]} {typ}" if fam in PREFIX and not typ.lower().startswith(PREFIX[fam].lower()) else typ
        else:
            words = NO_TYPE.get(fam) or cat3
        add(words, fam, cat3, n)
    if xref_db and Path(xref_db).exists():
        db = sqlite3.connect(str(xref_db))
        for noun, cls, n in db.execute("select noun, cls, count(*) from part where webb_item<>'' and noun<>'' group by noun, cls"):
            words = f"copper press {noun}" if cls == "press_fitting" and "press" not in noun.lower() else noun
            add(words, "", "", n, cls=cls)
    return sorted(seen.values(), key=lambda e: -e["n"])


def index_dir() -> Path:
    return settings.index_dir / "photos"


def images_dir() -> Path:
    return settings.data_dir / "images" / "webb" / "Main"


class PhotoIndex:
    """Loaded read-only at the first photo search."""

    def __init__(self, path: Path | None = None):
        p = Path(path) if path else index_dir()
        self.ok = (p / "items.json").exists() and (p / "item_emb.npy").exists() and (p / "labels.json").exists()
        self.items: list[dict] = []
        self.labels: list[dict] = []
        self.item_emb = np.zeros((0, 512), np.float32)
        self.label_emb = np.zeros((0, 512), np.float32)
        if not self.ok:
            return
        self.items = json.loads((p / "items.json").read_text())
        self.item_emb = np.load(p / "item_emb.npy").astype(np.float32)
        self.labels = json.loads((p / "labels.json").read_text())
        self.label_emb = np.load(p / "label_emb.npy").astype(np.float32)
        self.by_item = {it["webb_item"]: k for k, it in enumerate(self.items)}
        self.meta = json.loads((p / "meta.json").read_text()) if (p / "meta.json").exists() else {}

    def neighbors(self, vec: np.ndarray, k: int = 12) -> list[tuple[dict, float]]:
        if not len(self.items):
            return []
        s = self.item_emb @ vec
        top = np.argsort(-s)[:k]
        return [(self.items[i], float(s[i])) for i in top]

    def zero_shot(self, vec: np.ndarray, k: int = 8, scale: float = 100.0) -> list[tuple[dict, float]]:
        """Label probabilities (softmax over CLIP's scaled cosine similarities), best first."""
        if not len(self.labels):
            return []
        s = (self.label_emb @ vec) * scale
        p = np.exp(s - s.max())
        p /= p.sum()
        top = np.argsort(-p)[:k]
        return [(self.labels[i], float(p[i])) for i in top]


def build(clip, products, xref_db: Path, webb_items: dict, img_dir: Path | None = None, out: Path | None = None,
          log=print) -> dict:
    """Embed every product photo and every label; write the index. Returns counts."""
    from PIL import Image
    img_dir = Path(img_dir) if img_dir else images_dir()
    out = Path(out) if out else index_dir()
    out.mkdir(parents=True, exist_ok=True)
    # item facts: the lake's Webb-stocked parts first, then job-kit items
    facts: dict[str, dict] = {}
    if xref_db and Path(xref_db).exists():
        db = sqlite3.connect(str(xref_db))
        db.row_factory = sqlite3.Row
        for r in db.execute("select brand, part, noun, cls, webb_item, mpn from part where webb_item<>'' order by ncross desc"):
            it = r["webb_item"]
            if it in facts:
                continue
            noun = r["noun"] or (r["cls"] or "").replace("_", " ")
            facts[it] = {"webb_item": it, "brand": r["brand"], "mpn": r["mpn"] or r["part"], "noun": noun, "cls": r["cls"],
                         "title": " ".join(x for x in (r["brand"], r["mpn"] or r["part"], noun) if x), "source": "xref"}
    for it, w in (webb_items or {}).items():
        if it in facts:
            continue
        facts[it] = {"webb_item": it, "brand": w.get("manufacturer") or "", "mpn": w.get("mpn") or "",
                     "noun": w.get("desc") or "", "cls": label_cls(w.get("desc") or ""), "title": w.get("desc") or "",
                     "source": "job kit"}
    files = sorted(img_dir.glob("*.jpg"))
    items, ims = [], []
    for f in files:
        it = f.stem
        if it not in facts:
            continue
        try:
            raw = f.read_bytes()
            im = Image.open(io.BytesIO(raw))
            im.load()
        except Exception:
            continue
        items.append({**facts[it], "md5": hashlib.md5(raw).hexdigest()[:12]})
        ims.append(im)
    log(f"photos: {len(items)} of {len(facts)} known Webb items")
    emb = clip.images(ims) if ims else np.zeros((0, 512), np.float32)
    labels = build_labels(products, xref_db)
    texts = [p.format(lb["text"].lower() if not lb["text"].isupper() else lb["text"]) for lb in labels for p in PROMPTS]
    le = clip.texts(texts).reshape(len(labels), len(PROMPTS), -1).mean(axis=1)
    le /= np.linalg.norm(le, axis=1, keepdims=True)
    log(f"labels: {len(labels)}")
    json.dump(items, (out / "items.json").open("w"))
    np.save(out / "item_emb.npy", emb.astype(np.float16))
    json.dump(labels, (out / "labels.json").open("w"))
    np.save(out / "label_emb.npy", le.astype(np.float16))
    meta = {"photos": len(items), "known_items": len(facts), "labels": len(labels), "model": "clip-vit-base-patch32",
            "prompts": PROMPTS}
    (out / "meta.json").write_text(json.dumps(meta))
    return meta
