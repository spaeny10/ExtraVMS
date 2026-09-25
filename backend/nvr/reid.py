"""Person re-identification embeddings (OSNet-x0.25, trained on MSMT17).

Architecture after torchreid's OSNet by Kaiyang Zhou (MIT License,
https://github.com/KaiyangZhou/deep-person-reid; Zhou et al., "Omni-Scale Feature Learning for
Person Re-Identification", ICCV 2019). Only the weights file is downloaded; it is loaded with
`weights_only=True` so it cannot execute code.

`embed(crops)` returns an L2-normalised 512-d vector per person crop (BGR numpy images). Cosine
similarity between two events' mean vectors estimates whether they show the same person.
"""
from __future__ import annotations

import logging
import threading
import urllib.request
from pathlib import Path

import cv2
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from .config import ROOT, settings

log = logging.getLogger("nvr.reid")

WEIGHTS_URL = "https://drive.google.com/uc?export=download&id=1sSwXSUlj4_tHZequ_iZ8w_Jh0VaRQMqF"
WEIGHTS_PATH = ROOT / "models" / "osnet_x0_25_msmt17.pt"
INPUT_HW = (256, 128)
MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)
EMBED_DIM = 512


# ---------------------------------------------------------------- OSNet building blocks

class ConvLayer(nn.Module):
    def __init__(self, cin, cout, k, stride=1, padding=0, groups=1, IN=False):
        super().__init__()
        self.conv = nn.Conv2d(cin, cout, k, stride=stride, padding=padding, bias=False, groups=groups)
        self.bn = nn.InstanceNorm2d(cout, affine=True) if IN else nn.BatchNorm2d(cout)
        self.relu = nn.ReLU(inplace=True)

    def forward(self, x):
        return self.relu(self.bn(self.conv(x)))


class Conv1x1(nn.Module):
    def __init__(self, cin, cout, stride=1, groups=1):
        super().__init__()
        self.conv = nn.Conv2d(cin, cout, 1, stride=stride, padding=0, bias=False, groups=groups)
        self.bn = nn.BatchNorm2d(cout)
        self.relu = nn.ReLU(inplace=True)

    def forward(self, x):
        return self.relu(self.bn(self.conv(x)))


class Conv1x1Linear(nn.Module):
    def __init__(self, cin, cout, stride=1):
        super().__init__()
        self.conv = nn.Conv2d(cin, cout, 1, stride=stride, padding=0, bias=False)
        self.bn = nn.BatchNorm2d(cout)

    def forward(self, x):
        return self.bn(self.conv(x))


class LightConv3x3(nn.Module):
    """1x1 pointwise then 3x3 depthwise."""

    def __init__(self, cin, cout):
        super().__init__()
        self.conv1 = nn.Conv2d(cin, cout, 1, stride=1, padding=0, bias=False)
        self.conv2 = nn.Conv2d(cout, cout, 3, stride=1, padding=1, bias=False, groups=cout)
        self.bn = nn.BatchNorm2d(cout)
        self.relu = nn.ReLU(inplace=True)

    def forward(self, x):
        return self.relu(self.bn(self.conv2(self.conv1(x))))


class ChannelGate(nn.Module):
    def __init__(self, cin, reduction=16):
        super().__init__()
        self.global_avgpool = nn.AdaptiveAvgPool2d(1)
        self.fc1 = nn.Conv2d(cin, cin // reduction, 1, bias=True, padding=0)
        self.relu = nn.ReLU(inplace=True)
        self.fc2 = nn.Conv2d(cin // reduction, cin, 1, bias=True, padding=0)
        self.gate_activation = nn.Sigmoid()

    def forward(self, x):
        g = self.gate_activation(self.fc2(self.relu(self.fc1(self.global_avgpool(x)))))
        return x * g


class OSBlock(nn.Module):
    """Omni-scale block: four streams of 1-4 stacked light convs, fused by a shared channel gate."""

    def __init__(self, cin, cout, IN=False, bottleneck_reduction=4):
        super().__init__()
        mid = cout // bottleneck_reduction
        self.conv1 = Conv1x1(cin, mid)
        self.conv2a = LightConv3x3(mid, mid)
        self.conv2b = nn.Sequential(LightConv3x3(mid, mid), LightConv3x3(mid, mid))
        self.conv2c = nn.Sequential(LightConv3x3(mid, mid), LightConv3x3(mid, mid), LightConv3x3(mid, mid))
        self.conv2d = nn.Sequential(LightConv3x3(mid, mid), LightConv3x3(mid, mid), LightConv3x3(mid, mid),
                                    LightConv3x3(mid, mid))
        self.gate = ChannelGate(mid)
        self.conv3 = Conv1x1Linear(mid, cout)
        self.downsample = Conv1x1Linear(cin, cout) if cin != cout else None
        self.IN = nn.InstanceNorm2d(cout, affine=True) if IN else None

    def forward(self, x):
        x1 = self.conv1(x)
        x2 = self.gate(self.conv2a(x1)) + self.gate(self.conv2b(x1)) + self.gate(self.conv2c(x1)) + self.gate(self.conv2d(x1))
        out = self.conv3(x2) + (self.downsample(x) if self.downsample is not None else x)
        if self.IN is not None:
            out = self.IN(out)
        return F.relu(out)


class OSNet(nn.Module):
    def __init__(self, num_classes=1000, layers=(2, 2, 2), channels=(16, 64, 96, 128), feature_dim=EMBED_DIM):
        super().__init__()
        self.conv1 = ConvLayer(3, channels[0], 7, stride=2, padding=3)
        self.maxpool = nn.MaxPool2d(3, stride=2, padding=1)
        self.conv2 = self._make_layer(layers[0], channels[0], channels[1], reduce_spatial_size=True)
        self.conv3 = self._make_layer(layers[1], channels[1], channels[2], reduce_spatial_size=True)
        self.conv4 = self._make_layer(layers[2], channels[2], channels[3], reduce_spatial_size=False)
        self.conv5 = Conv1x1(channels[3], channels[3])
        self.global_avgpool = nn.AdaptiveAvgPool2d(1)
        self.fc = nn.Sequential(nn.Linear(channels[3], feature_dim), nn.BatchNorm1d(feature_dim), nn.ReLU(inplace=True))
        self.classifier = nn.Linear(feature_dim, num_classes)

    @staticmethod
    def _make_layer(n, cin, cout, reduce_spatial_size):
        layers = [OSBlock(cin, cout)] + [OSBlock(cout, cout) for _ in range(1, n)]
        if reduce_spatial_size:
            layers.append(nn.Sequential(Conv1x1(cout, cout), nn.AvgPool2d(2, stride=2)))
        return nn.Sequential(*layers)

    def forward(self, x):
        x = self.maxpool(self.conv1(x))
        x = self.conv5(self.conv4(self.conv3(self.conv2(x))))
        v = self.global_avgpool(x).flatten(1)
        return self.fc(v)  # features (the classifier is only used in training)


# ---------------------------------------------------------------- runtime

class ReID:
    def __init__(self, device: str | None = None):
        self.device = torch.device(device or settings.yolo_device if torch.cuda.is_available() else "cpu")
        if not WEIGHTS_PATH.exists():
            WEIGHTS_PATH.parent.mkdir(parents=True, exist_ok=True)
            log.info("downloading re-ID weights (~3 MB)")
            tmp = WEIGHTS_PATH.with_suffix(".part")
            urllib.request.urlretrieve(WEIGHTS_URL, tmp)
            tmp.replace(WEIGHTS_PATH)
        sd = torch.load(WEIGHTS_PATH, map_location="cpu", weights_only=True)
        sd = sd.get("state_dict", sd)
        sd = {k.removeprefix("module."): v for k, v in sd.items()}
        num_classes = sd["classifier.weight"].shape[0] if "classifier.weight" in sd else 1000
        self.model = OSNet(num_classes=num_classes)
        missing, unexpected = self.model.load_state_dict(sd, strict=False)
        if missing or unexpected:
            raise RuntimeError(f"re-ID weights don't match the model: missing={missing[:5]} unexpected={unexpected[:5]}")
        self.model.eval().to(self.device)
        self.lock = threading.Lock()
        log.info("re-ID OSNet-x0.25 loaded on %s", self.device)

    @torch.inference_mode()
    def embed(self, crops: list[np.ndarray]) -> np.ndarray | None:
        """Mean L2-normalised embedding of BGR person crops, or None if there are none."""
        crops = [c for c in crops if c is not None and c.size and min(c.shape[:2]) >= 16]
        if not crops:
            return None
        batch = []
        for c in crops:
            rgb = cv2.cvtColor(cv2.resize(c, (INPUT_HW[1], INPUT_HW[0]), interpolation=cv2.INTER_LINEAR), cv2.COLOR_BGR2RGB)
            batch.append(((rgb.astype(np.float32) / 255.0 - MEAN) / STD).transpose(2, 0, 1))
        x = torch.from_numpy(np.stack(batch)).to(self.device)
        with self.lock:
            f = F.normalize(self.model(x), dim=1)
        mean = F.normalize(f.mean(0, keepdim=True), dim=1)[0]
        return mean.cpu().numpy().astype(np.float32)


def person_crop(img: np.ndarray, box, pad: float = 0.05) -> np.ndarray:
    """Tight person crop from a normalized (l, t, r, b) box, with a little padding."""
    h, w = img.shape[:2]
    l, t, r, b = box
    pw, ph = (r - l) * pad, (b - t) * pad
    x1, y1 = int(max(0, l - pw) * w), int(max(0, t - ph) * h)
    x2, y2 = int(min(1, r + pw) * w), int(min(1, b + ph) * h)
    return img[y1:y2, x1:x2]
