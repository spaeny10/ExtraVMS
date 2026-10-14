"""Install Piper voices for talk-down (backend nvr/tts.py) into this server's voices folder.

Usage (from backend/, with the site's .env in place):
  ..\\.venv\\Scripts\\python.exe ..\\tools\\install_piper_voices.py                  the default set (DEFAULT below)
  ..\\.venv\\Scripts\\python.exe ..\\tools\\install_piper_voices.py --voices en_US-amy-medium,en_GB-alan-medium
  ..\\.venv\\Scripts\\python.exe ..\\tools\\install_piper_voices.py --all           every catalog voice (~650 MB)
  ..\\.venv\\Scripts\\python.exe ..\\tools\\install_piper_voices.py --list          the catalog, nothing downloaded
  options: --dir <folder> (default NVR_TTS_VOICES_DIR, else <data_dir>/voices), --no-pip, --force

1. pip-installs piper-tts into this Python when it can't be imported (not with --no-pip). Restart the NVR service
   afterwards: the server imports it once.
2. Reads the repository's manifest (voices.json of rhasspy/piper-voices on Hugging Face) and, for each chosen catalog
   voice, downloads <id>.onnx and <id>.onnx.json. Each file is written under a temporary name, checked against the
   manifest's size and MD5 (voices.json lists md5_digest, not sha256; the SHA-256 is printed for the record) and only
   then renamed into place. A voice already there with the manifest's size is skipped (--force: fetched again).
3. Prints what is installed. Nothing secret is read or printed; the downloads need no account.
New voices are picked up without a restart (tts.installed_voices rescans the folder). Each voice's dataset has its own
license: read its MODEL_CARD (printed with --list) before a commercial deployment.
"""
import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from nvr import tts  # noqa: E402  (catalog and folder only: it doesn't open the server's database)

DEFAULT = ["en_US-lessac-medium", "en_US-amy-medium", "en_US-ryan-high", "en_GB-alan-medium"]
MANIFEST = tts.HF_BASE + "voices.json"
UA = {"User-Agent": "axiom-vision-install-piper-voices/1"}
TIMEOUT_S = 60


def fetch(url: str, dest: Path | None = None) -> tuple[bytes | None, str, str, int]:
    """GET url into dest (streamed) or memory: (data or None, md5, sha256, size)."""
    md5, sha = hashlib.md5(), hashlib.sha256()
    size = 0
    buf = bytearray()
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=TIMEOUT_S) as r:
        out = dest.open("wb") if dest else None
        try:
            while chunk := r.read(1 << 20):
                md5.update(chunk)
                sha.update(chunk)
                size += len(chunk)
                if out:
                    out.write(chunk)
                else:
                    buf += chunk
        finally:
            if out:
                out.close()
    return (None if dest else bytes(buf)), md5.hexdigest(), sha.hexdigest(), size


def ensure_piper(pip: bool) -> None:
    if importlib.util.find_spec("piper") is not None:
        print("piper-tts: installed")
        return
    if not pip:
        print("piper-tts: NOT installed (--no-pip): the server will fall back to Windows SAPI / espeak until it is")
        return
    print("piper-tts: installing with pip ...")
    r = subprocess.run([sys.executable, "-m", "pip", "install", "piper-tts"])
    if r.returncode != 0:
        sys.exit("pip install piper-tts failed (see above)")
    print("piper-tts: installed; restart the NVR service so the server loads it")


def install(vid: str, manifest: dict, folder: Path, force: bool) -> bool:
    entry = manifest.get(vid)
    if not isinstance(entry, dict):
        print(f"  {vid}: not in the repository's voices.json, skipped")
        return False
    onnx = tts.repo_path(vid)
    files = entry.get("files") or {}
    for path, name in ((onnx, f"{vid}.onnx"), (onnx + ".json", f"{vid}.onnx.json")):
        want = files.get(path) or {}
        dest = folder / name
        if not force and dest.is_file() and want.get("size_bytes") and dest.stat().st_size == want["size_bytes"]:
            print(f"  {name}: already there")
            continue
        tmp = dest.with_name(dest.name + ".part")
        try:
            _, md5, sha, size = fetch(tts.HF_BASE + path, tmp)
        except OSError as e:
            tmp.unlink(missing_ok=True)
            print(f"  {name}: download failed ({e})")
            return False
        if want.get("size_bytes") and size != want["size_bytes"]:
            tmp.unlink(missing_ok=True)
            print(f"  {name}: size {size} != {want['size_bytes']} in voices.json, discarded")
            return False
        if want.get("md5_digest") and md5 != want["md5_digest"]:
            tmp.unlink(missing_ok=True)
            print(f"  {name}: MD5 mismatch, discarded")
            return False
        tmp.replace(dest)
        print(f"  {name}: {size / 1e6:.1f} MB, {'MD5 verified' if want.get('md5_digest') else 'no MD5 in voices.json'}, sha256 {sha}")
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description="Install Piper talk-down voices from rhasspy/piper-voices")
    ap.add_argument("--voices", help="comma-separated catalog voice ids (default: %s)" % ",".join(DEFAULT))
    ap.add_argument("--all", action="store_true", help="every catalog voice")
    ap.add_argument("--list", action="store_true", help="print the catalog and exit")
    ap.add_argument("--dir", type=Path, help="voices folder (default: the server's NVR_TTS_VOICES_DIR or <data_dir>/voices)")
    ap.add_argument("--no-pip", action="store_true", help="don't pip-install piper-tts")
    ap.add_argument("--force", action="store_true", help="download again even when present")
    args = ap.parse_args()
    known = {c["id"]: c for c in tts.catalog()}
    if args.list:
        for c in known.values():
            print(f"{c['id']:28} {c['label']:36} {c['url']}")
            print(f"{'':28} {'model card:':36} {c['url'].rsplit('/', 1)[0]}/MODEL_CARD")
        return
    wanted = list(known) if args.all else [v.strip() for v in (args.voices.split(",") if args.voices else DEFAULT) if v.strip()]
    unknown = [v for v in wanted if v not in known]
    if unknown:
        sys.exit(f"not in the catalog: {', '.join(unknown)} (see --list)")
    folder = args.dir or tts.voices_dir()
    folder.mkdir(parents=True, exist_ok=True)
    ensure_piper(not args.no_pip)
    print(f"voices folder: {folder}")
    data, *_ = fetch(MANIFEST)
    manifest = json.loads(data)
    ok = []
    for v in wanted:
        print(f"{v} ({known[v]['label']})")
        if install(v, manifest, folder, args.force):
            ok.append(v)
    print(f"\n{len(ok)} of {len(wanted)} voices installed in {folder}")
    have = sorted(p.name[:-5] for p in folder.glob("*.onnx") if (folder / (p.name + ".json")).is_file())
    print("installed here now: " + (", ".join(have) or "none"))
    if len(ok) != len(wanted):
        sys.exit(1)


if __name__ == "__main__":
    main()
