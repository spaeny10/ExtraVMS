#!/usr/bin/env bash
# Hailo-8 PCIe accelerator for an Axiom Vision site: driver, firmware, HailoRT, pyhailort and the YOLO HEF.
# Ubuntu 24.04, run as root. Idempotent: every step checks what is already there. Public sources only (GitHub and
# Hailo's public S3 buckets); no Hailo developer-zone login.
#
#   bash /opt/nvr/tools/hailo_setup.sh [--env] [--venv /opt/nvr/.venv] [--hef yolov11s]
#
# --env  also point the site at the Hailo: NVR_YOLO_DEVICE=hailo, NVR_YOLO_MODEL=<hef> in /opt/nvr/.env
#        (deploy_site.sh --hailo passes it). Restart the service afterwards: systemctl restart nvr
#
# What it installs (HailoRT 4.24.0 is the last line that supports Hailo-8; 5.x is Hailo-10/15 only):
#   hailo_pci kernel driver  github.com/hailo-ai/hailort-drivers (tag v4.24.0), DKMS, built for every kernel with headers
#   firmware                 /lib/firmware/hailo/hailo8_fw.bin (hailo-hailort S3, as the drivers' download_firmware.sh)
#   HailoRT + hailortcli     github.com/hailo-ai/hailort (tag v4.24.0), CMake, into /usr/local
#   pyhailort (hailo_platform) built from the same tree, installed into the NVR venv
#   HEF                      Hailo Model Zoo v2.19.0 (the zoo release for HailoRT 4.24), Hailo-8, COCO 80 classes,
#                            YOLOv8 NMS post-process in the HEF -> /opt/nvr/models/<name>.hef
set -euo pipefail

HRT_VERSION=4.24.0
MZ_VERSION=v2.19.0
NVR_DIR=/opt/nvr
VENV=$NVR_DIR/.venv
HEF=yolov11s
WRITE_ENV=0
SRC=/opt/hailo-src
while [ $# -gt 0 ]; do
  case "$1" in
    --env) WRITE_ENV=1; shift ;;
    --venv) VENV="$2"; shift 2 ;;
    --hef) HEF="${2%.hef}"; shift 2 ;;
    *) echo "unknown option $1"; exit 2 ;;
  esac
done
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }
declare -A HEF_SHA256=(
  [yolov11s]=5a9dbb513d2e31435753fe0fab0193cf9a3fb995124dc41f325a53b59ecacd15
  [yolov8s]=9e2453b38d18b9f5a212decb2c36d4b395a9129484e6d35dfaade62177f6f8c1
)

lspci -d 1e60: | grep -q . || { echo "no Hailo PCIe device (lspci -d 1e60:)"; exit 1; }

echo "== build packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq build-essential dkms cmake git python3-dev pkg-config wget curl "linux-headers-$(uname -r)" >/dev/null
mkdir -p "$SRC"

fetch() {  # fetch <repo> <tag> <dir>: shallow clone of one tag, reused if already there
  if [ ! -d "$3/.git" ] || [ "$(git -C "$3" describe --tags 2>/dev/null)" != "$2" ]; then
    rm -rf "$3"
    git -c advice.detachedHead=false clone -q --depth 1 -b "$2" "https://github.com/hailo-ai/$1.git" "$3"
  fi
}

echo "== hailo_pci driver $HRT_VERSION (DKMS)"
fetch hailort-drivers "v$HRT_VERSION" "$SRC/hailort-drivers"
if ! dkms status "hailo_pci/$HRT_VERSION" 2>/dev/null | grep -q "$(uname -r).*installed"; then
  dkms remove "hailo_pci/$HRT_VERSION" --all >/dev/null 2>&1 || true
  rm -rf "/usr/src/hailo_pci-$HRT_VERSION"
  # The standalone drivers repo has no common/include (the in-tree HailoRT layout does); nothing in it is needed.
  mkdir -p "$SRC/empty-include/include"
  make -C "$SRC/hailort-drivers/linux/pcie" install_dkms COMMON_INCLUDE_DIRECTORY="$SRC/empty-include/include"
fi
for k in /lib/modules/*; do  # also any newer kernel already installed (the next boot)
  kv=$(basename "$k")
  [ -e "$k/build" ] || continue
  dkms status "hailo_pci/$HRT_VERSION" -k "$kv" 2>/dev/null | grep -q installed && continue
  dkms install "hailo_pci/$HRT_VERSION" -k "$kv" --force >/dev/null && echo "  built for $kv"
done

echo "== firmware"
FW=/lib/firmware/hailo/hailo8_fw.bin
mkdir -p /lib/firmware/hailo
if [ ! -s "/lib/firmware/hailo/hailo8_fw.$HRT_VERSION.bin" ]; then
  wget -q -O "/lib/firmware/hailo/hailo8_fw.$HRT_VERSION.bin" "https://hailo-hailort.s3.eu-west-2.amazonaws.com/Hailo8/$HRT_VERSION/FW/hailo8_fw.$HRT_VERSION.bin"
fi
cmp -s "/lib/firmware/hailo/hailo8_fw.$HRT_VERSION.bin" "$FW" || cp -f "/lib/firmware/hailo/hailo8_fw.$HRT_VERSION.bin" "$FW"
install -m 644 "$SRC/hailort-drivers/linux/pcie/51-hailo-udev.rules" /etc/udev/rules.d/51-hailo-udev.rules   # /dev/hailo0 mode 0666
install -m 644 "$SRC/hailort-drivers/linux/pcie/hailo_pci.conf" /etc/modprobe.d/hailo_pci.conf
echo hailo_pci > /etc/modules-load.d/hailo_pci.conf
udevadm control --reload-rules
if ! lsmod | grep -q '^hailo_pci'; then
  modprobe hailo_pci
fi
for _ in 1 2 3 4 5; do [ -e /dev/hailo0 ] && break; sleep 1; done
[ -e /dev/hailo0 ] || { echo "/dev/hailo0 did not appear; dmesg | grep -i hailo"; exit 1; }

echo "== HailoRT $HRT_VERSION"
if ! hailortcli --version 2>/dev/null | grep -q "$HRT_VERSION"; then
  fetch hailort "v$HRT_VERSION" "$SRC/hailort"
  cmake -S "$SRC/hailort" -B "$SRC/hailort/build" -DCMAKE_BUILD_TYPE=Release >/dev/null
  cmake --build "$SRC/hailort/build" --config release --target install -j "$(nproc)" >/dev/null
  ldconfig
fi
hailortcli fw-control identify | grep -E "Firmware Version|Device Architecture|Product Name"

echo "== pyhailort into $VENV"
if [ -x "$VENV/bin/python" ]; then
  if ! "$VENV/bin/python" -c "import hailo_platform, importlib.metadata as m; assert m.version('hailort') == '$HRT_VERSION'" 2>/dev/null; then
    fetch hailort "v$HRT_VERSION" "$SRC/hailort"
    "$VENV/bin/pip" install -q setuptools wheel
    mkdir -p "$SRC/wheels"
    (cd "$SRC/hailort/hailort/libhailort/bindings/python/platform" &&
      CMAKE_BUILD_PARALLEL_LEVEL="$(nproc)" "$VENV/bin/pip" wheel -q --no-build-isolation --no-deps . -w "$SRC/wheels")
    "$VENV/bin/pip" install -q "$SRC/wheels/hailort-$HRT_VERSION"-*.whl
  fi
else
  echo "  no venv at $VENV yet (deploy_site.sh creates it); re-run this script afterwards"
fi

echo "== HEF $HEF (Model Zoo $MZ_VERSION, Hailo-8)"
mkdir -p "$NVR_DIR/models"
OUT="$NVR_DIR/models/$HEF.hef"
want="${HEF_SHA256[$HEF]:-}"
if [ ! -s "$OUT" ] || { [ -n "$want" ] && [ "$(sha256sum "$OUT" | cut -d' ' -f1)" != "$want" ]; }; then
  curl -fsSL -o "$OUT.part" "https://hailo-model-zoo.s3.eu-west-2.amazonaws.com/ModelZoo/Compiled/$MZ_VERSION/hailo8/$HEF.hef"
  if [ -n "$want" ] && [ "$(sha256sum "$OUT.part" | cut -d' ' -f1)" != "$want" ]; then
    echo "checksum mismatch for $HEF.hef"; rm -f "$OUT.part"; exit 1
  fi
  mv "$OUT.part" "$OUT"
fi
id -u nvr >/dev/null 2>&1 && chown nvr:nvr "$OUT"
(cd /tmp && HAILORT_LOGGER_PATH=NONE hailortcli run "$OUT" --frames-count 100 --measure-latency 2>&1 | grep -E "HW Latency|FPS" | tail -2) || true

if [ "$WRITE_ENV" = 1 ]; then
  echo "== $NVR_DIR/.env -> Hailo"
  ENV="$NVR_DIR/.env"
  touch "$ENV"
  setenv() { if grep -q "^$1=" "$ENV"; then sed -i "s|^$1=.*|$1=$2|" "$ENV"; else echo "$1=$2" >> "$ENV"; fi; }
  setenv NVR_YOLO_DEVICE hailo
  setenv NVR_YOLO_MODEL "$HEF.hef"
  setenv NVR_YOLO_IMGSZ 640   # the HEF's input; also the CPU fallback's size
  id -u nvr >/dev/null 2>&1 && chown nvr:nvr "$ENV"
  echo "  set NVR_YOLO_DEVICE=hailo NVR_YOLO_MODEL=$HEF.hef (systemctl restart nvr to apply)"
fi
echo "== Hailo ready: /dev/hailo0, HailoRT $HRT_VERSION, $OUT"
