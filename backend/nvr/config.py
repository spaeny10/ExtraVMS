"""Runtime settings, loaded from environment / .env."""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", env_prefix="NVR_", extra="ignore")

    # Storage: recordings on the big HDD, everything else on the SSD.
    recordings_dir: Path = Path("D:/NVR/recordings")
    data_dir: Path = ROOT / "data"          # sqlite db, snapshots, event clips
    runtime_dir: Path = ROOT / "runtime"    # generated configs, logs

    # MediaMTX
    mediamtx_exe: Path = ROOT / "bin" / "mediamtx" / "mediamtx.exe"
    mediamtx_api: str = "http://127.0.0.1:9997"
    mediamtx_playback: str = "http://127.0.0.1:9996"
    mediamtx_rtsp: str = "rtsp://127.0.0.1:8554"
    mediamtx_webrtc_port: int = 8889            # WHEP signalling; localhost only, the NVR proxies it (/api/whep)
    webrtc_media_port: int = 8189               # WebRTC video (UDP, TCP fallback): forward this port for remote live view
    webrtc_public_hosts: list[str] = []         # extra public IPs/hostnames to offer; the one in the browser's URL is added automatically
    segment_duration: str = "10m"
    playback_format: str = "fmp4"           # what /api/playback serves: fmp4 (progressive) or mp4 (indexed first)
    backup_dir: Path = Path("D:/NVR/backups")   # nightly database copies (backup.py)

    # Event engine
    track_min_seconds: float = 1.0          # camera track must persist this long to become an event
    track_end_gap: float = 2.0              # seconds without the object before the track is closed
    track_max_seconds: float = 60.0         # long tracks are split so synopses stay timely
    camera_clock_offset: float = 0.0        # seconds to add to camera UtcTime to match PC clock
    clip_pre_roll: float = 5.0             # cameras report a person a step or two late: keep the doorway on the clip
    clip_post_roll: float = 3.0
    recording_lag: float = 3.0              # wait for MediaMTX to flush fMP4 parts before fetching

    # YOLO
    yolo_model: str = "yolo11s.pt"
    yolo_device: str = "cuda:0"
    yolo_imgsz: int = 1280
    yolo_conf: float = 0.25
    verify_frames: int = 6                  # frames sampled per event
    verify_min_hits: int = 2                # frames where YOLO must agree with the camera
    verify_iou: float = 0.2

    # PPE compliance (ppe.py): people who stay in a "ppe" zone are checked for the hard hat / hi-vis vest it requires
    ppe_model: str = "ppe_yolov8s.pt"       # under models/; Apache-2.0 YOLOv8s (huggingface killuminati1/construction-ppe-yolov8)
    ppe_conf: float = 0.4                   # a frame calls an item present / missing at this detector confidence
    ppe_imgsz: int = 960
    ppe_frames: int = 4                     # frames checked per person per zone
    ppe_min_dwell_s: float = 3.0            # zone default: seconds inside before the person is checked (a brisk walk across a yard zone is ~4 s)
    ppe_grace_s: float = 3.0                # zone default: frames from this long after walking in (time to put a hat on)
    ppe_vlm_confirm: bool = True            # Qwen looks at the person when the detector says missing or can't tell
    ppe_vlm_all: bool = False               # ...or at every checked person (Qwen decides; ~1 s each): catches caps the
                                            # detector takes for hard hats (eval: hat recall 0.89 -> 0.97)

    # Qwen via Ollama (a dedicated `ollama serve` pinned to GPU 1)
    ollama_exe: Path = Path.home() / "AppData/Local/Programs/Ollama/ollama.exe"
    ollama_url: str = "http://127.0.0.1:11435"
    ollama_gpu: str = "1"
    vlm_model: str = "qwen3.5:9b"            # same speed as qwen2.5vl:7b, ~40% of the image tokens, better grounding
    vlm_num_ctx: int = 6144
    # Requests Qwen serves at once. 1 on an 8 GB card. A 24 GB card serving several sites through the hub can
    # take 2: one request's image encoding overlaps another's token generation (~+40% throughput; each slot
    # holds its own vlm_num_ctx KV cache).
    ollama_parallel: int = 1
    chat_frames: int = 4                    # frames per chat question (~1,050 tokens each)
    embed_model: str = "nomic-embed-text"
    synopsis_images: int = 4
    # Back-to-back fragments of one visit on a camera merge into one event before Qwen describes it (merge.py)
    track_merge_gap: float = 10.0           # a fragment starting within this of the previous one may merge
    merge_max_seconds: float = 300.0        # never grow one event beyond this
    merge_max_dist: float = 0.25            # normalised centre distance between the last and first boxes
    merge_reid_min: float = 0.75            # people: appearance similarity needed when the camera gave a new track id
    merge_long_gap: float = 180.0           # people: gaps up to this merge too if YOLO sees them standing there throughout
    merge_gap_step_s: float = 5.0           # one recorded frame checked every this many seconds across such a gap
    merge_gap_max_frames: int = 40          # at most this many frames checked per pair (the step widens to fit)
    synopsis_labels: list[str] = ["person"]  # Qwen runs only for these; YOLO verifies every label
    anomaly_synopsis_min: float = 0.75  # ...and for any other label once it is this unusual for its camera

    # A small site without a GPU: no local Ollama at all, every Qwen task goes to the remote model (the hub's
    # shared AI, or the three NVR_REMOTE_VLM_* below). Search is keyword-only there (no local embeddings).
    local_vlm_enabled: bool = True
    footage_index_enabled: bool = True      # CLIP footage search index; off on a 2-core box (vehicle fingerprints still work)

    # Optional larger remote Qwen (OpenAI-compatible, e.g. a RunPod Serverless vLLM endpoint). Put these three in
    # .env only. Unset = everything runs on the local model. See nvr/vlmroute.py.
    remote_vlm_url: str = ""            # e.g. https://api.runpod.ai/v2/<endpoint_id>/openai/v1
    remote_vlm_key: str = ""
    remote_vlm_model: str = ""          # e.g. Qwen/Qwen2.5-VL-32B-Instruct-AWQ
    remote_tasks: list[str] = ["assistant", "briefing", "journey", "unusual_review", "footage_verify"]
    remote_interactive_timeout_s: float = 12    # time to first token before a user-facing answer falls back to local
    remote_background_timeout_s: float = 240    # background work waits out a cold start
    remote_daily_budget_usd: float = 5.0
    remote_rate_usd_per_s: float = 0.0  # your endpoint's per-second GPU price; 0 = don't enforce the budget
    remote_idle_s: float = 300          # the endpoint's idle timeout (billed after each request)

    # Retention: the policy (continuous days, what to keep, free-space floor) lives in the database and is
    # edited in the UI (System → Retention); see nvr/keep.py for defaults.
    retention_dry_run: bool = False     # NVR_RETENTION_DRY_RUN=1: log decisions, delete nothing

    # API
    host: str = "0.0.0.0"
    port: int = 8080

    # Fleet hub (hub_agent.py): this site dials out to the hub; nothing is opened inbound.
    hub_enabled: bool = True
    hub_url: str = "wss://hub.axiomvision.ai/agent"   # the settings table can override (Settings -> System)
    hub_insecure: bool = False                         # NVR_HUB_INSECURE=1: accept a self-signed hub cert (dev only)


settings = Settings()
