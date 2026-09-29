#!/usr/bin/env bash
# Install or update a NewVMS site on Ubuntu 24.04 (run as root). Idempotent: safe to re-run after copying new code.
#
#   1. copy the repo to /opt/nvr (rsync/scp from the dev PC, or git clone), including models/ and frontend/dist
#   2. bash /opt/nvr/tools/deploy_site.sh [--recordings-disk /dev/sdX] [--cpu]
#
# --recordings-disk formats that whole disk as ext4 (ALL DATA ON IT IS LOST) and mounts it at /srv/nvr/recordings.
# --cpu writes a .env for a box without an NVIDIA GPU: YOLO on the CPU, no local Ollama (Qwen via the hub).
set -euo pipefail

NVR_DIR=/opt/nvr
NVR_USER=nvr
MEDIAMTX_VERSION=v1.21.1
DISK=""
CPU=0
while [ $# -gt 0 ]; do
  case "$1" in
    --recordings-disk) DISK="$2"; shift 2 ;;
    --cpu) CPU=1; shift ;;
    *) echo "unknown option $1"; exit 2 ;;
  esac
done
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }
[ -d "$NVR_DIR/backend/nvr" ] || { echo "copy the repo to $NVR_DIR first"; exit 1; }

echo "== packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3-venv python3-dev ffmpeg ethtool curl rsync >/dev/null

echo "== user and folders"
id -u "$NVR_USER" >/dev/null 2>&1 || useradd --system --home "$NVR_DIR" --shell /usr/sbin/nologin "$NVR_USER"
mkdir -p /srv/nvr/backups "$NVR_DIR/data" "$NVR_DIR/runtime"

if [ -n "$DISK" ]; then
  echo "== recordings disk $DISK"
  if ! blkid "$DISK" >/dev/null 2>&1 || ! blkid "$DISK" | grep -q 'TYPE="ext4"'; then
    wipefs -a "$DISK"
    mkfs.ext4 -q -L nvr-recordings "$DISK"
  fi
  mkdir -p /srv/nvr/recordings
  grep -q "LABEL=nvr-recordings" /etc/fstab || echo "LABEL=nvr-recordings /srv/nvr/recordings ext4 defaults,noatime,nofail 0 2" >> /etc/fstab
  mountpoint -q /srv/nvr/recordings || mount /srv/nvr/recordings
else
  mkdir -p /srv/nvr/recordings
fi

echo "== MediaMTX $MEDIAMTX_VERSION"
MTX_DIR="$NVR_DIR/bin/mediamtx"
if [ ! -x "$MTX_DIR/mediamtx" ] || ! "$MTX_DIR/mediamtx" --version 2>/dev/null | grep -q "$MEDIAMTX_VERSION"; then
  mkdir -p "$MTX_DIR"
  curl -fsSL "https://github.com/bluenviron/mediamtx/releases/download/$MEDIAMTX_VERSION/mediamtx_${MEDIAMTX_VERSION}_linux_amd64.tar.gz" | tar -xz -C "$MTX_DIR" mediamtx
  chmod +x "$MTX_DIR/mediamtx"
fi

echo "== python environment"
if [ ! -x "$NVR_DIR/.venv/bin/python" ]; then
  python3 -m venv "$NVR_DIR/.venv"
fi
PIP="$NVR_DIR/.venv/bin/pip"
"$PIP" install -q --upgrade pip wheel
if [ "$CPU" = 1 ]; then
  "$PIP" install -q torch torchvision --index-url https://download.pytorch.org/whl/cpu
else
  "$PIP" install -q torch torchvision --index-url https://download.pytorch.org/whl/cu126
fi
(cd "$NVR_DIR/backend" && "$PIP" install -q -r requirements.txt)

echo "== .env"
ENV="$NVR_DIR/backend/.env"
if [ ! -f "$ENV" ]; then
  cat > "$ENV" <<EOF
NVR_RECORDINGS_DIR=/srv/nvr/recordings
NVR_BACKUP_DIR=/srv/nvr/backups
NVR_MEDIAMTX_EXE=$MTX_DIR/mediamtx
NVR_HUB_URL=ws://192.168.105.105:8000/agent
EOF
  if [ "$CPU" = 1 ]; then
    cat >> "$ENV" <<EOF
# no NVIDIA GPU: YOLO on the CPU, no local Ollama (Qwen via the hub's shared AI)
NVR_LOCAL_VLM_ENABLED=0
NVR_OLLAMA_EXE=/nonexistent
NVR_YOLO_DEVICE=cpu
NVR_YOLO_MODEL=yolo11n.pt
NVR_YOLO_IMGSZ=640
NVR_VERIFY_FRAMES=4
NVR_FOOTAGE_INDEX_ENABLED=0
EOF
  fi
  echo "wrote $ENV (edit NVR_HUB_URL for a real hub)"
fi

echo "== permissions"
chown -R "$NVR_USER:$NVR_USER" "$NVR_DIR" /srv/nvr

echo "== systemd"
cat > /etc/systemd/system/nvr.service <<EOF
[Unit]
Description=NewVMS site (recording, detection, hub agent)
After=network-online.target
Wants=network-online.target

[Service]
User=$NVR_USER
WorkingDirectory=$NVR_DIR/backend
ExecStart=$NVR_DIR/.venv/bin/python -m nvr
Restart=always
RestartSec=5
LimitNOFILE=65536
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable nvr >/dev/null
systemctl restart nvr
sleep 8
systemctl --no-pager --lines=5 status nvr || true
echo
echo "== done. UI: http://$(hostname -I | awk '{print $1}'):8080   claim code: curl -s localhost:8080/api/hub"
