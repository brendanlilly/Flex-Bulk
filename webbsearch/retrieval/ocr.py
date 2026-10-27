"""Read handwritten and scanned order notes: photos (JPEG, PNG, HEIC...) and image-only PDFs.

Engines, best first (the first one available is used; WEBB_OCR=claude|vision|tesseract forces one):

  claude     Claude vision, when ANTHROPIC_API_KEY is set: reads handwriting and trade shorthand and returns
             the item lines. No line positions, so no per-line crops.
  vision     Apple Vision (VNRecognizeTextRequest, accurate level), on a Mac with pyobjc: handwriting and
             print, on the machine, no network. Line positions -> per-line crops for checking.
  tesseract  Tesseract, if installed: printed scans and typed notes. Poor on handwriting; the page says so.

Every line comes back with its text, a confidence (0-1) and, where the engine gives positions, a small
crop of the image so the person can compare what was written with what was read. Lines read this way
are never confirmed automatically (bom.py).
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import sys

IMAGE_EXT = (".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".webp", ".bmp", ".gif")
MAX_SIDE = 3000          # downscale huge phone photos before OCR
MAX_PAGES = 10           # scanned PDF pages read


def _pil():
    from PIL import Image, ImageOps
    try:
        import pillow_heif      # iPhone photos
        pillow_heif.register_heif_opener()
    except Exception:
        pass
    return Image, ImageOps


def open_image(raw: bytes):
    """Bytes -> upright RGB PIL image (EXIF rotation applied, HEIC supported when pillow-heif is present)."""
    Image, ImageOps = _pil()
    im = Image.open(io.BytesIO(raw))
    im = ImageOps.exif_transpose(im)
    if im.mode not in ("RGB", "L"):
        bg = Image.new("RGB", im.size, "white")
        bg.paste(im.convert("RGBA"), mask=im.convert("RGBA").split()[-1])
        im = bg
    im = im.convert("RGB")
    if max(im.size) > MAX_SIDE:
        s = MAX_SIDE / max(im.size)
        im = im.resize((int(im.size[0] * s), int(im.size[1] * s)))
    return im


def pdf_pages(raw: bytes, dpi: int = 200, max_pages: int = MAX_PAGES) -> list:
    """Render PDF pages to PIL images (pypdfium2) for scanned PDFs with no text layer."""
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(raw)
    out = []
    for i in range(min(len(doc), max_pages)):
        out.append(doc[i].render(scale=dpi / 72).to_pil().convert("RGB"))
    doc.close()
    return out


# ------------------------------------------------------------------ engines
def _has_vision() -> bool:
    if sys.platform != "darwin":
        return False
    try:
        import Vision  # noqa: F401
        import Quartz  # noqa: F401
        return True
    except Exception:
        return False


def _has_tesseract() -> bool:
    try:
        import pytesseract
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


def engines(claude=None) -> list[str]:
    out = []
    if claude is not None and getattr(claude, "enabled", False):
        out.append("claude")
    if _has_vision():
        out.append("vision")
    if _has_tesseract():
        out.append("tesseract")
    return out


def pick_engine(claude=None) -> str | None:
    have = engines(claude)
    force = os.environ.get("WEBB_OCR", "").strip().lower()
    if force and force in have:
        return force
    return have[0] if have else None


ENGINE_LABEL = {"claude": "FlexIQ", "vision": "Apple handwriting recognition (on this Mac)",
                "tesseract": "Tesseract OCR (printed text; handwriting reads poorly)"}

TRADE_WORDS = ["cxc",   # (for engines that take a custom vocabulary) "ftg", "fip", "mip", "pex", "pvc", "cpvc", "dwv", "sweat", "npt", "sharkbite", "propress", "mfd",
               "capacitor", "igniter", "thermocouple", "contactor", "ball valve", "gate valve", "elbow", "coupling",
               "reducer", "bushing", "nipple", "tee", "wye", "sanitee", "flange", "union", "boiler", "circulator",
               "expansion tank", "water heater", "condenser", "furnace", "Honeywell", "White-Rodgers", "Taco",
               "Navien", "NIBCO", "Uponor", "Viega", "Watts", "Zurn", "Moen", "Delta", "Kohler", "Rheem", "Bradford White"]


def _vision(im) -> list[dict]:
    """Apple Vision text recognition on a PIL image -> observations with top-left normalized boxes."""
    import objc
    import Quartz
    import Vision
    from Foundation import NSData
    buf = io.BytesIO()
    im.save(buf, "PNG")
    raw = buf.getvalue()
    out = []
    with objc.autorelease_pool():
        data = NSData.dataWithBytes_length_(raw, len(raw))
        src = Quartz.CGImageSourceCreateWithData(data, None)
        cg = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
        req = Vision.VNRecognizeTextRequest.alloc().init()
        req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        # language correction "fixes" part numbers and trade shorthand into words; keep what was written
        req.setUsesLanguageCorrection_(False)
        req.setRecognitionLanguages_(["en-US"])
        try:
            req.setAutomaticallyDetectsLanguage_(False)
        except Exception:
            pass
        handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg, None)
        ok, err = handler.performRequests_error_([req], None)
        if not ok:
            raise RuntimeError(f"Vision failed: {err}")
        for obs in req.results() or []:
            cands = obs.topCandidates_(1)
            if not cands:
                continue
            c = cands[0]
            bb = obs.boundingBox()
            x, y, w, h = bb.origin.x, bb.origin.y, bb.size.width, bb.size.height
            out.append({"text": str(c.string()), "conf": float(c.confidence()),
                        "bbox": [x, 1 - y - h, x + w, 1 - y]})
    return out


def _tesseract(im) -> list[dict]:
    import pytesseract
    d = pytesseract.image_to_data(im, output_type=pytesseract.Output.DICT, config="--psm 6")
    W, H = im.size
    lines: dict[tuple, dict] = {}
    for i, word in enumerate(d["text"]):
        if not word.strip():
            continue
        key = (d["block_num"][i], d["par_num"][i], d["line_num"][i])
        x0, y0 = d["left"][i] / W, d["top"][i] / H
        x1, y1 = (d["left"][i] + d["width"][i]) / W, (d["top"][i] + d["height"][i]) / H
        c = max(0.0, float(d["conf"][i])) / 100
        ln = lines.setdefault(key, {"words": [], "conf": [], "bbox": [x0, y0, x1, y1]})
        ln["words"].append(word)
        ln["conf"].append(c)
        b = ln["bbox"]
        ln["bbox"] = [min(b[0], x0), min(b[1], y0), max(b[2], x1), max(b[3], y1)]
    return [{"text": " ".join(v["words"]), "conf": sum(v["conf"]) / len(v["conf"]), "bbox": v["bbox"]} for v in lines.values()]


def _claude(im, claude) -> list[dict]:
    from webbsearch.config import settings
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    schema = {"type": "object", "properties": {"lines": {"type": "array", "items": {"type": "object", "properties": {
        "text": {"type": "string", "description": "the line as written, abbreviations kept (3/4 cxc 90, 45/5 cap)"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "top": {"type": "number", "description": "top of the line as a fraction of the image height (0-1)"},
        "bottom": {"type": "number", "description": "bottom of the line as a fraction of the image height (0-1)"}},
        "required": ["text", "confidence"]}}},
        "required": ["lines"]}
    resp = claude.messages(
        model=settings.model_smart, max_tokens=2000,
        system="You transcribe handwritten and scanned plumbing/HVAC order notes for a distributor. Return every line as "
               "written, one per entry, keeping quantities, sizes, part numbers and trade shorthand exactly. Don't correct "
               "or expand. Mark a line low confidence when you are unsure of a character.",
        tools=[{"name": "note_lines", "description": "The lines of the note", "input_schema": schema}],
        tool_choice={"type": "tool", "name": "note_lines"},
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": base64.b64encode(buf.getvalue()).decode()}},
            {"type": "text", "text": "Transcribe this note, one entry per line, with where each line sits on the page (top and bottom as fractions of the height)."}]}])
    block = next((b for b in resp.content if getattr(b, "type", "") == "tool_use"), None)
    lines = (block.input if block else json.loads(resp.content[0].text)).get("lines", [])
    conf = {"high": 0.95, "medium": 0.75, "low": 0.4}
    out = []
    for ln in lines:
        if not ln.get("text", "").strip():
            continue
        t, b = ln.get("top"), ln.get("bottom")
        ok = isinstance(t, (int, float)) and isinstance(b, (int, float)) and 0 <= t < b <= 1 and b - t < 0.2
        # a full-width strip where Claude says the line is: enough to check the reading against the handwriting
        out.append({"text": ln["text"], "conf": conf.get(ln.get("confidence"), 0.75), "bbox": [0.01, t, 0.99, b] if ok else None})
    return out


# ------------------------------------------------------------------ lines and crops
def group_lines(obs: list[dict]) -> list[dict]:
    """Observations on the same baseline (a quantity written apart from the item) become one line, left to right."""
    obs = sorted([o for o in obs if o.get("text", "").strip()], key=lambda o: ((o["bbox"] or [0, 0, 0, 0])[1]))
    lines: list[list[dict]] = []
    for o in obs:
        b = o["bbox"]
        if b is None:
            lines.append([o]); continue
        cy, h = (b[1] + b[3]) / 2, b[3] - b[1]
        for ln in lines:
            lb = ln[0]["bbox"]
            if lb is None:
                continue
            ly, lh = (lb[1] + lb[3]) / 2, lb[3] - lb[1]
            if abs(cy - ly) < 0.5 * max(h, lh) and not any(_overlap_x(b, x["bbox"]) for x in ln):
                ln.append(o)
                break
        else:
            lines.append([o])
    out = []
    for ln in lines:
        ln.sort(key=lambda o: (o["bbox"] or [0])[0])
        bb = [o["bbox"] for o in ln if o["bbox"]]
        box = [min(b[0] for b in bb), min(b[1] for b in bb), max(b[2] for b in bb), max(b[3] for b in bb)] if bb else None
        parts = [o["text"].strip() for o in ln]
        if len(parts) > 1 and re.fullmatch(r"\(?\d{1,4}\)?[.)]?", parts[0]):
            parts = [parts[0].strip("().") + " -"] + parts[1:]      # a count written apart: "10 - 3/4 cxc 90"
        out.append({"text": "  ".join(parts), "conf": min(o["conf"] for o in ln), "bbox": box})
    out.sort(key=lambda o: (o["bbox"] or [0, 0])[1])
    return out


def _overlap_x(a, b) -> bool:
    return bool(a and b) and min(a[2], b[2]) - max(a[0], b[0]) > 0.3 * min(a[2] - a[0], b[2] - b[0])


def crop(im, bbox, max_h: int = 52, pad: float = 0.012) -> str | None:
    """The written line as a small grayscale JPEG: a little margin sideways, barely any up and down, so the
    lines above and below don't crowd the one being checked."""
    if not bbox:
        return None
    W, H = im.size
    py = max(0.002, 0.12 * (bbox[3] - bbox[1]))
    x0, y0 = max(0, int((bbox[0] - pad) * W)), max(0, int((bbox[1] - py) * H))
    x1, y1 = min(W, int((bbox[2] + pad) * W)), min(H, int((bbox[3] + py) * H))
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None
    c = im.crop((x0, y0, x1, y1))
    s = min(1.0, max_h / c.size[1])
    if s < 1:
        c = c.resize((max(1, int(c.size[0] * s)), max(1, int(c.size[1] * s))))
    if c.size[0] > 640:
        c = c.crop((0, 0, 640, c.size[1]))
    buf = io.BytesIO()
    c.convert("L").save(buf, "JPEG", quality=70)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def thumb(im, w: int = 360) -> str:
    s = min(1.0, w / im.size[0])
    t = im.resize((max(1, int(im.size[0] * s)), max(1, int(im.size[1] * s)))) if s < 1 else im
    buf = io.BytesIO()
    t.convert("RGB").save(buf, "JPEG", quality=72)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def read_images(images: list, claude=None, engine: str | None = None) -> dict:
    """PIL images (a photo, or the pages of a scanned PDF) -> {engine, label, lines:[{text, conf, crop, page}], thumbs}."""
    engine = engine or pick_engine(claude)
    if not engine:
        return {"engine": None, "lines": [], "thumbs": [thumb(im) for im in images[:3]],
                "note": "No handwriting or OCR engine on this computer. On a Mac, install pyobjc-framework-Vision "
                        "(the installer does this); elsewhere switch on FlexIQ or install Tesseract."}
    lines = []
    for pno, im in enumerate(images):
        if engine == "vision":
            obs = _vision(im)
        elif engine == "tesseract":
            obs = _tesseract(im)
        else:
            try:
                obs = _claude(im, claude)
            except RuntimeError as ex:          # the public demo's daily Claude limit is used up
                return {"engine": engine, "lines": [], "thumbs": [thumb(i) for i in images[:3]],
                        "note": f"{ex}. Type the lines into Quick entry instead, or try again tomorrow."}
        for ln in (obs if engine == "claude" else group_lines(obs)):      # Claude returns lines in reading order
            t = re.sub(r"\s{3,}", "  ", ln["text"]).strip()
            if not t:
                continue
            lines.append({"text": t, "conf": round(float(ln["conf"]), 2), "crop": crop(im, ln["bbox"]), "page": pno + 1})
    return {"engine": engine, "label": ENGINE_LABEL.get(engine, engine), "lines": lines,
            "thumbs": [thumb(im) for im in images[:3]], "pages": len(images)}
