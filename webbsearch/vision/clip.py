"""CLIP ViT-B/32 image and text embeddings on ONNX Runtime (CPU; no torch).

Model files (data/models/clip-b32/): vision_model.onnx and text_model.onnx from Xenova/clip-vit-base-patch32 (the
OpenAI CLIP weights exported to ONNX, projected 512-d outputs), tokenizer.json. Embeddings are L2-normalized, so a dot
product is the cosine similarity. About 40 ms an image on 2 vCPU.
"""
from __future__ import annotations

import threading
from pathlib import Path

import numpy as np

from webbsearch.config import settings

MEAN = np.array([0.48145466, 0.4578275, 0.40821073], np.float32)
STD = np.array([0.26862954, 0.26130258, 0.27577711], np.float32)
EOT = 49407
DIM = 512


def model_dir() -> Path:
    return settings.data_dir / "models" / "clip-b32"


def available() -> bool:
    d = model_dir()
    return (d / "vision_model.onnx").exists() and (d / "text_model.onnx").exists() and (d / "tokenizer.json").exists()


def preprocess(im) -> np.ndarray:
    """PIL image -> (3, 224, 224) float32: shortest side to 224 (bicubic), center crop, CLIP normalization.
    Transparent product shots are flattened on white, as they are displayed."""
    from PIL import Image
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
        im = Image.alpha_composite(bg, im)
    im = im.convert("RGB")
    w, h = im.size
    s = 224 / min(w, h)
    im = im.resize((max(224, round(w * s)), max(224, round(h * s))), Image.BICUBIC)
    w, h = im.size
    left, top = (w - 224) // 2, (h - 224) // 2
    im = im.crop((left, top, left + 224, top + 224))
    a = np.asarray(im, np.float32) / 255.0
    a = (a - MEAN) / STD
    return a.transpose(2, 0, 1)


def _norm(x: np.ndarray) -> np.ndarray:
    return x / np.maximum(np.linalg.norm(x, axis=1, keepdims=True), 1e-9)


class Clip:
    def __init__(self, path: Path | None = None, threads: int = 2):
        import onnxruntime as ort
        from tokenizers import Tokenizer
        d = Path(path) if path else model_dir()
        self._ort, self._dir = ort, d
        self._so = ort.SessionOptions()
        self._so.intra_op_num_threads = threads
        self._so.log_severity_level = 3
        self.vision = ort.InferenceSession(str(d / "vision_model.onnx"), self._so, providers=["CPUExecutionProvider"])
        self._text = None                  # the text encoder (250 MB) loads only when labels are embedded (index builds)
        self.tok = Tokenizer.from_file(str(d / "tokenizer.json"))
        self.tok.no_padding()
        self.tok.no_truncation()
        self.lock = threading.Lock()

    @property
    def text(self):
        if self._text is None:
            self._text = self._ort.InferenceSession(str(self._dir / "text_model.onnx"), self._so, providers=["CPUExecutionProvider"])
        return self._text

    def images(self, ims: list, batch: int = 16) -> np.ndarray:
        out = []
        for k in range(0, len(ims), batch):
            x = np.stack([preprocess(im) for im in ims[k:k + batch]]).astype(np.float32)
            with self.lock:
                out.append(self.vision.run(["image_embeds"], {"pixel_values": x})[0])
        return _norm(np.concatenate(out)) if out else np.zeros((0, DIM), np.float32)

    def texts(self, texts: list[str], batch: int = 64) -> np.ndarray:
        out = []
        for k in range(0, len(texts), batch):
            ids = [e.ids[:77] for e in self.tok.encode_batch(texts[k:k + batch])]
            ids = [x if x[-1] == EOT else x[:76] + [EOT] for x in ids]
            n = max(len(x) for x in ids)
            arr = np.full((len(ids), n), EOT, np.int64)          # padded with end-of-text: pooling takes the first one
            for i, x in enumerate(ids):
                arr[i, :len(x)] = x
            with self.lock:
                out.append(self.text.run(["text_embeds"], {"input_ids": arr})[0])
        return _norm(np.concatenate(out)) if out else np.zeros((0, DIM), np.float32)


_CLIP: dict = {}
_CLIP_LOCK = threading.Lock()


def get() -> Clip | None:
    """The process's model, loaded once on first use (the warm-up thread and a search may ask together); None when the
    model files aren't installed."""
    with _CLIP_LOCK:
        if "m" not in _CLIP:
            try:
                _CLIP["m"] = Clip() if available() else None
            except Exception as e:     # e.g. onnxruntime can't load its runtime DLLs: labels and Claude still read photos
                print(f"search by photo: picture match off ({type(e).__name__}: {e})", flush=True)
                _CLIP["m"] = None
        return _CLIP["m"]
