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
    mediamtx_webrtc_port: int = 8889            # WHEP signaling; localhost only, the NVR proxies it (/api/whep)
    webrtc_media_port: int = 8189               # WebRTC video (UDP, TCP fallback): forward this port for remote live view
    webrtc_public_hosts: list[str] = []         # extra public IPs/hostnames to offer; the one in the browser's URL is added automatically
    # MediaMTX RTSP readers need the generated internal user/password (settings table `mediamtx_reader`);
    # NVR_RTSP_AUTH=0 restores the old anonymous reads from localhost and private networks.
    rtsp_auth: bool = True
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
    yolo_device: str = "cuda:0"             # "cuda:0", "cpu", or "hailo" (Hailo-8 PCIe; yolo_model is then a .hef, see hailo.py)
    hailo_retry_s: float = 600              # on the CPU fallback (Hailo not found / failing): try the Hailo again this often
    yolo_imgsz: int = 1280
    yolo_conf: float = 0.25
    verify_frames: int = 6                  # frames sampled per event
    verify_min_hits: int = 2                # frames where YOLO must agree with the camera
    verify_iou: float = 0.2
    # Parked vehicles (parked.py): the camera's analytics fire on shimmer/shadows near a parked machine and the
    # big, confident YOLO box around it "confirms" every one. Such events are rejected (reason in detections.rejected).
    parked_suppress: bool = True            # reject vehicle events whose YOLO box sat still while the camera saw motion
    parked_max_move: float = 0.02           # YOLO box center (and size) may wander this much of the frame and still be parked
    parked_cam_box_ratio: float = 0.25      # camera box smaller than this share of the YOLO box: the motion is not the vehicle
    parked_memory_min_events: int = 3       # static sightings (over >= 10 min) before a spot is remembered as a parking place

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
    merge_max_dist: float = 0.25            # normalized center distance between the last and first boxes
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
    remote_vlm_model: str = ""
    remote_vlm_no_think: str = "reasoning_effort"   # reasoning_effort | chat_template (vLLM) | off: see vlmroute.no_think          # e.g. Qwen/Qwen2.5-VL-32B-Instruct-AWQ
    remote_tasks: list[str] = ["assistant", "briefing", "journey", "unusual_review", "footage_verify"]
    remote_interactive_timeout_s: float = 12    # time to first token before a user-facing answer falls back to local
    remote_background_timeout_s: float = 240    # background work waits out a cold start
    remote_daily_budget_usd: float = 5.0
    remote_rate_usd_per_s: float = 0.0  # your endpoint's per-second GPU price; 0 = don't enforce the budget
    remote_idle_s: float = 300          # the endpoint's idle timeout (billed after each request)

    # Retention: the policy (continuous days, what to keep, free-space floor) lives in the database and is
    # edited in the UI (System → Retention); see nvr/keep.py for defaults.
    retention_dry_run: bool = False     # NVR_RETENTION_DRY_RUN=1: log decisions, delete nothing
    # Event media (clip.mp4, wide/crop jpgs under data_dir/events) can fill the disk on its own: a noisy PTZ
    # camera once wrote 193 GB of clips in 3 days next to 91 MB of recordings. See retention.enforce_disk_floor.
    event_media_min_free_gb: float = 0      # free-space floor on the data_dir volume; 0 = auto (the site floor
                                            # when it shares the recordings disk, else 10% of it, 10-200 GB)
    disk_floor_hysteresis_pct: float = 5.0  # event media pruning stops at floor + this % of the disk, not at the
                                            # floor itself, so it is not back again on the next clip
    disk_emergency_free_gb: float = 5.0     # before SQLite opens: free event media until this much is free
                                            # (at 0 bytes the database cannot open and the service restart-loops)
    event_rate_max_per_hour: int = 600      # per camera; above it events still open but keep no clip/crops
                                            # (snapshot only). 0 = no limit

    # API
    host: str = "0.0.0.0"
    port: int = 8080
    # Browsers may reach this server only under these Host names (DNS-rebinding guard, api.lan_guard): localhost,
    # this machine's hostname(s) and local IP addresses, webrtc_public_hosts, plus this comma-separated list
    # (e.g. "nvr.example.com,nvr-yard.lan" when the UI is opened under a DNS name or a port forward).
    allowed_hosts: str = ""
    # NVR_DEV_ORIGINS=1: also trust the hub UI's development origins (http://localhost:8000, http://localhost:5174)
    # for Direct-on-LAN CORS. Off in production.
    dev_origins: bool = False

    # Fleet hub (hub_agent.py): this site dials out to the hub; nothing is opened inbound.
    hub_enabled: bool = True
    hub_url: str = "wss://hub.axiomvision.ai/agent"   # the settings table can override (Settings -> System)
    hub_insecure: bool = False                         # NVR_HUB_INSECURE=1: accept a self-signed hub cert (dev only)
    hub_allow_insecure: bool = False                   # NVR_HUB_ALLOW_INSECURE=1: allow a plain ws:// hub other than localhost (dev only)
    # NVR_HUB_ENROLL_TOKEN: one-time enrollment token from the hub (central recording instances): an unenrolled
    # server presents it instead of a claim code and enrolls itself into the hub Site the token is bound to.
    # Ignored once enrolled; never logged.
    hub_enroll_token: str = ""
    # NVR_INSTANCE_NAME: a display name shown on Settings -> System, e.g. "Main Street · Central" for a central
    # recording instance. No other effect.
    instance_name: str = ""

    # Direct-on-LAN (direct.py): a browser that can reach this server on its LAN fetches live/playback/frames
    # straight from it, authorized by a short-lived token the hub mints, instead of through the hub tunnel.
    direct_enabled: bool = True
    https_port: int = 8443                  # the same app over HTTPS (self-signed cert in data_dir/tls) for those browsers
    # Low-bitrate playback (/api/playback?q=sd): concurrent 720p/700 kbps transcodes. 0 = auto: 4 with NVENC, 2 on CPU
    playback_transcode_max: int = 0

    @property
    def torch_device(self) -> str:
        """Where the torch models (PPE YOLO .pt, CLIP, re-ID) run: the YOLO device, except on a Hailo site, where only
        the verifier's HEF runs on the Hailo and everything torch stays on the CPU."""
        return "cpu" if self.yolo_device == "hailo" else self.yolo_device


settings = Settings()
