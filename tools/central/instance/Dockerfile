# Axiom Vision central instance: the ordinary site server (backend/, frontend/dist, tunnelproto/, models/) as an image.
#
# Build from the repository root (BuildKit, the default since Docker 23, then uses Dockerfile.dockerignore beside
# this file instead of the root .dockerignore, which leaves models/ and frontend/dist out):
#
#   docker build -f tools/central/instance/Dockerfile -t axiom/instance:$(git rev-parse --short HEAD) .
#   docker tag axiom/instance:$(git rev-parse --short HEAD) axiom/instance:latest
#
# models/ must hold the weights a site uses (yolo11s.pt, yolo11n.pt, ppe_yolov8s.pt, osnet_x0_25_msmt17.pt and the
# open_clip/ cache): they are gitignored, so copy them onto the build machine first (the same set deploy_site.sh
# expects). The container never downloads models: its firewall allows only the site, the hub and the shared vLLM.
#
# No Ollama here: Qwen comes from the host's shared vLLM (tools/central/ai). Runtime settings come from the
# instance.env axiom_host.py writes; the defaults below only make a bare `docker run` safe.

ARG MEDIAMTX_VERSION=v1.21.1

# ---- 1. site UI bundle
FROM node:22-alpine AS ui
WORKDIR /src
COPY frontend/package*.json frontend/
RUN cd frontend && npm ci --no-audit --no-fund
COPY frontend frontend
RUN cd frontend && npm run build

# ---- 2. MediaMTX (same pinned release as tools/deploy_site.sh), checksum-verified
FROM ubuntu:24.04 AS mediamtx
ARG MEDIAMTX_VERSION
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends ca-certificates curl >/dev/null \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /mtx
RUN set -eu; f="mediamtx_${MEDIAMTX_VERSION}_linux_amd64.tar.gz"; \
    base="https://github.com/bluenviron/mediamtx/releases/download/${MEDIAMTX_VERSION}"; \
    curl -fsSLO "$base/$f"; curl -fsSL "$base/checksums.sha256" -o checksums.sha256; \
    grep -E " \*?$f\$" checksums.sha256 | grep -q . || { echo "no checksum for $f" >&2; exit 1; }; \
    grep -E " \*?$f\$" checksums.sha256 | sha256sum -c -; \
    tar -xzf "$f" mediamtx; chmod 0755 mediamtx

# ---- 3. the server
# Plain Ubuntu 24.04 (Python 3.12) rather than an nvidia/cuda base: the cu128 torch wheels bundle their CUDA and
# cuDNN libraries, and the NVIDIA Container Toolkit mounts the driver (libcuda, NVENC/NVDEC) at run time. A40 and
# A10 are Ampere (sm_86), which cu128 covers; so do Blackwell cards if one is added later.
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
# ffmpeg: clips and the low-bitrate playback transcodes (NVENC via the driver's libnvidia-encode, mounted by the
# toolkit when NVIDIA_DRIVER_CAPABILITIES includes "video"). libgl1/libglib2: opencv-python, which ultralytics pulls in.
RUN apt-get update -qq \
 && apt-get install -y -qq --no-install-recommends python3 python3-venv python3-dev build-essential \
      ffmpeg ca-certificates libgl1 libglib2.0-0t64 tzdata >/dev/null \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/nvr
RUN python3 -m venv /opt/nvr/.venv
ENV PATH=/opt/nvr/.venv/bin:$PATH
RUN pip install --no-cache-dir -q --upgrade pip wheel \
 && pip install --no-cache-dir -q torch torchvision --index-url https://download.pytorch.org/whl/cu128

# dependencies first (layer cache): requirements.txt installs ../tunnelproto in editable mode
COPY tunnelproto tunnelproto
COPY backend/requirements.txt backend/requirements.txt
RUN cd backend && pip install --no-cache-dir -q -r requirements.txt \
 && apt-get purge -y -qq build-essential python3-dev >/dev/null && apt-get autoremove -y -qq >/dev/null

COPY --from=mediamtx /mtx/mediamtx bin/mediamtx/mediamtx
COPY models models
COPY backend/nvr backend/nvr
COPY --from=ui /src/frontend/dist frontend/dist
# bytecode at build time: the root filesystem is read-only at run time
RUN python -m compileall -q backend/nvr tunnelproto .venv/lib/python3.12/site-packages >/dev/null || true \
 && chmod -R a+rX /opt/nvr \
 && useradd --system --uid 10001 --home-dir /tmp --shell /usr/sbin/nologin nvr

# Each instance runs as its own uid (axiom_host.py: --user 20000+slot), so nothing here may depend on a passwd
# entry or a writable home: HOME and every cache live on the /tmp tmpfs, USER/LOGNAME stop getpass() failing.
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp USER=nvr LOGNAME=nvr \
    XDG_CACHE_HOME=/tmp/cache TORCH_HOME=/tmp/torch TRITON_CACHE_DIR=/tmp/triton MPLCONFIGDIR=/tmp/matplotlib \
    YOLO_CONFIG_DIR=/tmp/ultralytics YOLO_OFFLINE=1 HF_HUB_OFFLINE=1 \
    CUDA_DEVICE_ORDER=PCI_BUS_ID NVIDIA_DRIVER_CAPABILITIES=compute,utility,video \
    NVR_DATA_DIR=/data NVR_RECORDINGS_DIR=/recordings NVR_RUNTIME_DIR=/data/runtime NVR_BACKUP_DIR=/data/backups \
    NVR_MEDIAMTX_EXE=/opt/nvr/bin/mediamtx/mediamtx NVR_OLLAMA_EXE=/nonexistent \
    NVR_LOCAL_VLM_ENABLED=0 NVR_DIRECT_ENABLED=0 NVR_HOST=127.0.0.1

RUN mkdir -p /data /recordings && chown nvr:nvr /data /recordings
VOLUME ["/data", "/recordings"]
USER nvr
WORKDIR /opt/nvr/backend
# Nothing is published: the instance only dials out (hub tunnel, cameras, vLLM). 8080 listens on loopback.
HEALTHCHECK --interval=60s --timeout=10s --start-period=180s --retries=3 \
  CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8080/api/hub', timeout=8)"]
CMD ["python", "-m", "nvr"]
