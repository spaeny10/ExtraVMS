"""Image-text embeddings (OpenCLIP ViT-B-16, DataComp-XL weights, MIT) for searching all recorded footage.

Images and text map into the same 512-d space, so "red pickup truck" can be matched against frames that
never raised an event. Runs in fp16 on the YOLO GPU; call it from Pipeline.gpu so it takes turns with YOLO.
"""
from __future__ import annotations

import threading

import cv2
import numpy as np
import torch

from .config import ROOT, settings

MODEL, PRETRAINED = "ViT-B-16", "datacomp_xl_s13b_b90k"
CACHE_DIR = ROOT / "models" / "open_clip"
DIM = 512
SIZE = 224
# CLIP retrieves better with a caption than a bare phrase; the query embedding is the mean of these.
TEMPLATES = ("a photo of {}.", "a security camera photo of {}.", "{}")


class Clip:
    def __init__(self, device: str | None = None) -> None:
        import os
        if any(CACHE_DIR.glob("models--*/snapshots/*/*")):  # weights already downloaded: don't phone home
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
        import open_clip
        self.device = device or settings.yolo_device
        self.model, _, _ = open_clip.create_model_and_transforms(
            MODEL, pretrained=PRETRAINED, cache_dir=str(CACHE_DIR), device=self.device, precision="fp16")
        self.model.eval()
        self.tokenizer = open_clip.get_tokenizer(MODEL)
        cfg = getattr(self.model.visual, "preprocess_cfg", {}) or {}
        self.mean = np.array(cfg.get("mean", (0.48145466, 0.4578275, 0.40821073)), np.float32)
        self.std = np.array(cfg.get("std", (0.26862954, 0.26130258, 0.27577711)), np.float32)
        self.lock = threading.Lock()

    def _prep(self, bgr: np.ndarray) -> np.ndarray:
        # Squash to 224x224 rather than centre-crop: a crop would cut off the edges of the view.
        rgb = cv2.cvtColor(cv2.resize(bgr, (SIZE, SIZE), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2RGB)
        return ((rgb.astype(np.float32) / 255 - self.mean) / self.std).transpose(2, 0, 1)

    @torch.inference_mode()
    def embed_images(self, images: list[np.ndarray]) -> np.ndarray:
        """BGR images -> (n, 512) float32, L2-normalised."""
        if not images:
            return np.zeros((0, DIM), np.float32)
        x = torch.from_numpy(np.stack([self._prep(i) for i in images])).to(self.device).half()
        with self.lock:
            f = self.model.encode_image(x).float()
        return torch.nn.functional.normalize(f, dim=-1).cpu().numpy()

    @torch.inference_mode()
    def embed_text(self, text: str) -> np.ndarray:
        tokens = self.tokenizer([t.format(text.strip()) for t in TEMPLATES]).to(self.device)
        with self.lock:
            f = self.model.encode_text(tokens).float()
        f = torch.nn.functional.normalize(f, dim=-1).mean(0)
        return torch.nn.functional.normalize(f, dim=0).cpu().numpy()
