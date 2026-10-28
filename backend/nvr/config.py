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
    # Parked vehicles in the tracker: a vehicle track whose event the verifier rejected, or that the track_max_seconds
    # split closed while it went on, opens no new event while it stays where it was (T-304's NW PTZ, 2026-10-19: 644
    # rejected events in 6 h, one a minute from each parked car). It opens one again once it moves, or after the hold.
    parked_track_move: float = 0.05         # its box centre moved this share of the frame: it is driving off
    parked_track_iou: float = 0.3           # ...or its box overlaps the held position by less than this
    parked_track_hold_s: float = 1800.0     # ...or this long has passed (a person may since have come to it)
    # An open event no live track owns any more (its track was lost without closing it) is closed and verified this
    # long after its last sample; at start-up every open event left from before the restart is closed that way at once
    orphan_event_after_s: float = 120.0
    # Cameras without object metadata (ruleevents.py): an event opened by an ONVIF detection (e.g. Reolink PeopleDetect)
    # whose state=false never comes is closed this long after the last state=true
    rule_event_on_timeout: float = 120.0
    camera_clock_offset: float = 0.0        # seconds to add to camera UtcTime to match PC clock
    clip_pre_roll: float = 5.0             # cameras report a person a step or two late: keep the doorway on the clip
    clip_post_roll: float = 3.0
    recording_lag: float = 3.0              # wait for MediaMTX to flush fMP4 parts before fetching

    # YOLO
    yolo_model: str = "yolo11s.pt"
    yolo_device: str = "cuda:0"             # "cuda:0", "cpu", or "hailo" (Hailo-8 PCIe; yolo_model is then a .hef, see hailo.py)
    hailo_retry_s: float = 600              # on the CPU fallback (Hailo not found / failing): try the Hailo again this often
    gpu_mem_mb: int = 0                     # this server's share of a shared GPU, MB (central instances; gpumem.py); 0 = no cap
    # The recording host's shared inference service (inferd.py, inference.py): its socket(s), comma-separated
    # ("/run/axiom-infer/gpu1.sock" or "127.0.0.1:8770"); empty = this server runs its own models (every on-site server)
    infer_socket: str = ""
    infer_token: str = ""                   # this instance's token (the host agent writes it; the service's --tokens)
    infer_timeout_s: float = 60.0           # one call, queueing on the service included
    infer_retry_s: float = 20.0             # reconnecting before a call gives up (InferUnavailable: the event waits)
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
    # Cameras that send no boxes (ruleevents.py: ONVIF yes/no events such as Reolink VehicleDetect): any car-like
    # object anywhere used to confirm them (cam6: 152 "verified" vehicles in 48 h, mostly parked ones at YOLO 0.3-0.5).
    boxless_min_conf: float = 0.5           # YOLO must be at least this sure of an object (in one of its frames) to count
    boxless_min_area: float = 0.0015        # ...and one of its boxes cover this share of the frame (0.15%)...
    boxless_min_side_px: int = 24           # ...or be this many pixels on its shorter side at YOLO's input size (yolo_imgsz)
    # labels the size floor applies to: a person is narrow (a walker across cam6's 4096x1248 panorama is ~13 px wide at
    # 1280, 0.1% of the frame), and the replay of cam6's 48 h lost 25 real people to it; add "person" to apply it anyway
    boxless_size_labels: list[str] = ["vehicle"]
    boxless_require_motion: bool = True     # vehicles must move or be new (absent from the pre-roll frame); see verifier.py
    boxless_single_frame_conf: float = 0.70  # one frame is enough for an object YOLO is this sure of, if it is new or passing

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
    # Optional second local model (vlmroute): a smaller Qwen in its own managed `ollama serve` on another GPU.
    # Work goes to the primary (vlm_model above); it falls back to this one while the primary is down, starting
    # or failing, and high-volume background work (synopses, journeys) also spills over to it when the primary's
    # queue is longer than fallback_when_queue_over. Unset (empty model) = one model, as before.
    fallback_vlm_model: str = ""                     # e.g. qwen3.5:9b
    fallback_ollama_url: str = "http://127.0.0.1:11437"
    fallback_ollama_gpu: str = ""                    # CUDA index in PCI order, e.g. "0"; empty = Ollama picks
    fallback_num_ctx: int = 6144                     # the fallback's own context (each instance keeps its own loaded)
    fallback_ollama_parallel: int = 1                # its OLLAMA_NUM_PARALLEL (it shares a GPU with YOLO)
    fallback_when_queue_over: int = 4                # primary in-flight + waiting above this: synopses use the fallback
    chat_frames: int = 4                   # frames per chat question (~1,050 tokens each)
    # "Ask about this clip" reaching beyond the clip ("how many times did he show up today?", "where else did they go?"):
    # identities.same_subject compares every sighting in the window with the clip's appearance fingerprint (cosine).
    # At or above *_same_min a sighting counts as the same person / vehicle; from *_possible_min up to that it is only
    # listed as possible (the answer never counts it as certain); below, it is left out.
    # People (OSNet re-ID; measured on this site: same person 0.8-0.9, different people median ~0.65):
    #   same 0.85 = identities.NAME_SIM["person"], what it takes to put a NAME on a sighting. Not journeys' 0.80
    #   (REID_AUTO_SIM): that confirms a link across two cameras inside a walking window; here most sightings are on the
    #   SAME camera, whose shared view and light lift different people too (indoors they reach ~0.82).
    #   possible 0.75 = merge_reid_min, what merging two tracks of one visit asks; journeys' 0.70 (REID_MIN_SIM) is only a
    #   candidate Qwen still checks, too loose to show an operator.
    # Vehicles (0.7 CLIP + 0.3 color: same truck twice 0.885, two white trucks of one fleet 0.89, white vs black pickup
    # 0.81, white pickup vs white van 0.77): same 0.87 = identities.NAME_SIM["vehicle"]; possible 0.82, just above a
    # differently colored vehicle of the same shape. Look-alike fleet vehicles still reach "same" by appearance: a
    # matching plate (events.plate) is the certain match, and two different plates are never the same vehicle.
    # A confirmed cross-camera journey with the clip's person (journeys.py) counts as the same person whatever the score.
    same_person_min: float = 0.85
    possible_person_min: float = 0.75
    same_vehicle_min: float = 0.87
    possible_vehicle_min: float = 0.82
    same_subject_max: int = 50             # matches returned (the most similar first); the answer says when it was capped
    embed_model: str = "nomic-embed-text"
    synopsis_images: int = 4
    # License plates (plate.py): a vehicle's description also asks Qwen for its plate, from a full-resolution crop of
    # the vehicle in the recording, only when the plate is clearly legible. A vehicle box shorter than plate_min_px
    # (pixels at the recorded resolution) is never asked about: its plate is a few pixels. "Read plate" on an event
    # runs it on demand (one run per event every plate_rate_s).
    plate_read: bool = True
    plate_min_px: int = 120
    plate_rate_s: float = 30.0
    # Traffic cameras (traffic.py): reads they push with the camera's own plate reader. Reads and their crops are kept
    # traffic_keep_days; the full snapshot only for a list hit (the recording has the frame) unless traffic_keep_full.
    # A read at traffic_attach_min_conf or more puts its plate on the vehicle event of that moment; a list hit needs
    # traffic_list_min_conf. traffic_push_base: the address cameras push to ("http://192.168.1.5:8080"); empty = this
    # server's address on the camera's network and `port`. No push for traffic_silent_s from a camera set up: alert.
    traffic_keep_days: int = 30
    traffic_keep_full: bool = False
    traffic_attach_min_conf: float = 0.6
    traffic_list_min_conf: float = 0.5
    traffic_push_base: str = ""
    traffic_silent_s: float = 900.0
    # A read sent without a vehicle picture gets one from the recording, at the read's time plus this: the recorded frame
    # showing what the camera read is earlier than its timestamp (cam8 2026-10-28: plate crops matched at -0.4 s)
    traffic_rec_shift_s: float = -0.4
    # Back-to-back fragments of one visit on a camera merge into one event before Qwen describes it (merge.py)
    track_merge_gap: float = 10.0           # a fragment starting within this of the previous one may merge
    merge_max_seconds: float = 300.0        # never grow one event beyond this
    merge_max_dist: float = 0.25            # normalized center distance between the last and first boxes
    merge_reid_min: float = 0.75            # people: appearance similarity needed when the camera gave a new track id
    merge_long_gap: float = 180.0           # people: gaps up to this merge too if YOLO sees them standing there throughout
    merge_gap_step_s: float = 5.0           # one recorded frame checked every this many seconds across such a gap
    merge_gap_max_frames: int = 40          # at most this many frames checked per pair (the step widens to fit)
    # People who overlap on a camera (a crew working an aisle) are one activity: one event, described once (merge.py
    # groups). A group never spans more than this from its first start to its last end; 0 = no group merging.
    group_merge_max_s: float = 600.0
    synopsis_labels: list[str] = ["person"]  # Qwen runs only for these; YOLO verifies every label
    anomaly_synopsis_min: float = 0.75  # ...and for any other label once it is this unusual for its camera
    # No Qwen synopsis for events YOLO is less sure of than this (pipeline.synopsis_gate; 0 = off). Settings -> System
    # edits it (settings table `synopsis_min_conf`, which wins over this default). Site rules, PPE checks and
    # unusual events are still described.
    synopsis_min_conf: float = 0.60

    # A small site without a GPU: no local Ollama at all, every Qwen task goes to the remote model (the hub's
    # shared AI, or the three NVR_REMOTE_VLM_* below). Search is keyword-only there (no local embeddings).
    local_vlm_enabled: bool = True
    footage_index_enabled: bool = True      # CLIP footage search index; off on a 2-core box (vehicle fingerprints still work)
    footage_index_dir: Path | None = None   # where footage.db lives; default <recordings_dir>/../index (central instances: /data/index)

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

    # The Site's AI schedule (aischedule.py, pushed by the hub): outside the Site's SOC hours, descriptions set to
    # "catch up when idle" run one at a time once Qwen has nothing else to do here. On a server without a local Qwen
    # (a central instance) they also wait while the shared vLLM is busy, read from its Prometheus /metrics:
    catchup_vllm_metrics_url: str = ""      # "" = NVR_REMOTE_VLM_URL without /v1, plus /metrics; "off" = don't ask
    catchup_vllm_busy_running: int = 2      # busy with this many requests running there (or any waiting)
    catchup_poll_s: float = 5.0             # how often a waiting catch-up description looks again

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
    # Event snapshots (snapshots.py): the picture the event list, alerts and the viewer's poster show. Full resolution
    # (2592 px, ~1 MB) filled a central instance's data dataset (47.6 GB for 48,929 snapshots); anything that needs
    # full resolution reads the recording (frames.recording_frames / verifier.event_frames).
    snapshot_max_px: int = 1280             # longest side of a new snapshot.jpg
    snapshot_quality: int = 80              # its JPEG quality (~150-250 KB)
    rejected_media_days: float = 3          # NVR_REJECTED_MEDIA_DAYS: rejected events older than this lose their
                                            # snapshot, wide shot and crops (a 320 px thumbnail stays); never events
                                            # with feedback, a lock or a keep-rule reason. 0 = keep them

    # Talk-down speakers (talkdown.py, tts.py): text-to-speech is a Piper neural voice when the piper-tts package (or a
    # `piper` binary) and at least one voice are installed, else Windows SAPI, else espeak-ng / espeak; none = no "Say"
    # field. Voices are <id>.onnx + <id>.onnx.json in the voices folder (tools/install_piper_voices.py fills it).
    tts_voices_dir: Path | None = None      # default: data_dir/voices
    tts_piper_bin: str = ""                 # an external piper binary; default `piper` on PATH (used without the package)
    tts_piper_model: str = ""               # one more voice anywhere on disk (an .onnx with its .onnx.json beside it)
    # NVR_TALKDOWN_WAKE_MS: a faint tone this long before every message, so a speaker whose amplifier sleeps after a
    # quiet spell is awake when the words start (talkdown.wake_tone). 0 = off.
    talkdown_wake_ms: int = 500

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
    # NVR_HUB_STILL_INTERVAL: seconds between the latest pictures (640 px JPEG) of each enabled camera sent to the hub,
    # which keeps only the newest per camera for its lists and offline tiles; an event opening sends one too. 0 = off.
    # 5 min: about 3 GB a month for 6 cameras (~44 KB each), which a cellular site can carry; 60 s would be ~15 GB.
    hub_still_interval: int = 300
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
    def fallback_vlm_enabled(self) -> bool:
        """A second local model is configured (and there is a local model at all)."""
        return self.local_vlm_enabled and bool(self.fallback_vlm_model.strip())

    @property
    def torch_device(self) -> str:
        """Where the torch models (PPE YOLO .pt, CLIP, re-ID) run: the YOLO device, except on a Hailo site, where only
        the verifier's HEF runs on the Hailo and everything torch stays on the CPU."""
        return "cpu" if self.yolo_device == "hailo" else self.yolo_device


settings = Settings()
