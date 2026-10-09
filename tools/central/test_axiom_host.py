"""Tests for tools/central/axiom_host.py. No Docker, nft or xfs_quota needed: commands go to a fake.

    .venv\\Scripts\\python.exe tools\\central\\test_axiom_host.py
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import axiom_host as ah  # noqa: E402

NVIDIA_SMI = "0, NVIDIA A40, 46068, 41234, 87\n1, NVIDIA A10, 23028, 3120, 12\n"
HUB = "203.0.113.10"
TOKEN = "enr_Zx81kQ3vT0pL9mN2bC4dE6fG"
VLLM_KEY = "test-vllm-key-not-a-secret-0123456789"


class FakeExec(ah.Exec):
    """Real file changes (in a temp dir), canned answers for every external command."""

    def __init__(self, answers: dict[str, str] | None = None) -> None:
        super().__init__(dry_run=False)
        self.calls: list[list[str]] = []
        self.answers = answers or {}

    def _run(self, argv, input, timeout):
        self.calls.append(list(argv))
        key = " ".join(argv[:3])
        if argv[0] == "nvidia-smi":
            return ah.Result(0, NVIDIA_SMI, "")
        if argv[0] == "findmnt":
            return ah.Result(0, "/srv/axiom xfs rw,noatime,attr2,inode64,prjquota\n", "")
        for k, v in self.answers.items():
            if key.startswith(k):
                return ah.Result(0, v, "")
        return ah.Result(0, "", "")

    def cmds(self, prefix: str) -> list[list[str]]:
        return [c for c in self.calls if " ".join(c).startswith(prefix)]


class ZfsState:
    """A pretend pool: datasets {name: {used, quota, available, busy}} and mounts {path: dataset}."""

    def __init__(self, root: Path, instances_on_zfs: bool = True) -> None:
        self.root = root
        self.datasets: dict[str, dict] = {
            "axiom": {"used": 2_000_000_000_000, "available": 48_000_000_000_000},
            "axiom/recordings": {"used": 1_900_000_000_000, "available": 48_000_000_000_000},
            "axiom/instances": {"used": 10_000_000_000, "available": 48_000_000_000_000},
        }
        self.mounts: dict[str, str] = {str(root): "axiom", str(root / "recordings"): "axiom/recordings"}
        if instances_on_zfs:
            self.mounts[str(root / "instances")] = "axiom/instances"
        for p in self.mounts:
            Path(p).mkdir(parents=True, exist_ok=True)


class ZfsExec(FakeExec):
    """FakeExec on a ZFS host: findmnt and zfs answer from a ZfsState and zfs create/destroy/set change it.
    dry_run=True runs nothing but the read-only queries (like the real Exec)."""

    def __init__(self, state: ZfsState, dry_run: bool = False) -> None:
        super().__init__()
        self.dry_run = dry_run
        self.state = state

    def _run(self, argv, input, timeout):
        self.calls.append(list(argv))
        st = self.state
        if argv[0] == "findmnt":
            if "--mountpoint" in argv:
                ds = st.mounts.get(argv[-1])
                return ah.Result(0, f"zfs {ds}\n", "") if ds else ah.Result(1, "", "")
            return ah.Result(0, f"{st.root} zfs rw,xattr,posixacl\n", "")
        if argv[0] != "zfs":
            self.calls.pop()   # FakeExec records it
            return super()._run(argv, input, timeout)
        sub, name = argv[1], argv[-1]
        if sub == "list":
            return ah.Result(0 if name in st.datasets else 1, name + "\n" if name in st.datasets else "", "")
        if sub == "get":
            props, names = argv[5].split(","), argv[6:]
            out = [f"{n}\t{p}\t{st.datasets[n].get(p, 0)}" for n in names if n in st.datasets for p in props]
            missing = [n for n in names if n not in st.datasets]
            return ah.Result(1 if missing else 0, "\n".join(out) + "\n", "".join(f"{n}: dataset does not exist\n" for n in missing))
        if sub == "create":
            parent, _, leaf = name.rpartition("/")
            if name in st.datasets or parent not in st.datasets:
                return ah.Result(1, "", f"cannot create '{name}'\n")
            pmount = next(p for p, d in st.mounts.items() if d == parent)
            mount = Path(pmount) / leaf
            mount.mkdir()
            st.mounts[str(mount)] = name
            st.datasets[name] = {"used": 0, "available": 48_000_000_000_000,
                                 "quota": int(argv[argv.index("-o") + 1].split("=")[1])}
            return ah.Result(0, "", "")
        if sub == "set":
            if name not in st.datasets:
                return ah.Result(1, "", f"cannot open '{name}'\n")
            st.datasets[name]["quota"] = int(argv[2].split("=")[1])
            return ah.Result(0, "", "")
        if sub == "destroy":
            if name not in st.datasets or st.datasets[name].get("busy"):
                return ah.Result(1, "", f"cannot destroy '{name}': filesystem has dependent snapshots\n")
            del st.datasets[name]
            for p, d in list(st.mounts.items()):
                if d == name:
                    del st.mounts[p]
                    shutil.rmtree(p)
            return ah.Result(0, "", "")
        return ah.Result(2, "", "unexpected zfs call")


def make_host(tmp: Path, exe: ah.Exec) -> ah.Host:
    tmp.mkdir(parents=True, exist_ok=True)
    ai_env = tmp / "ai.env"
    ai_env.write_text(f"VLLM_API_KEY={VLLM_KEY}\nAXIOM_VLM_MODEL=Qwen/Qwen2.5-VL-32B-Instruct-AWQ\n")
    return ah.Host({"root": str(tmp / "srv"), "hub_ips": [HUB], "ai_env": str(ai_env)}, exe)


VPN_ARGS = {"id": "acme-gate", "location": "loc_acme1", "name": "Acme Gate · Central", "mode": "vpn",
            "subnet": "10.20.7.0/24", "quota_gb": 4000, "gpu": 1, "enroll_token": TOKEN, "quota_mode": "xfs"}
FWD_ARGS = {"id": "beta-yard", "location": "loc_beta2", "name": "Beta Yard · Central", "mode": "forward",
            "public_ip": "198.51.100.7", "quota_gb": 2000, "gpu": "none", "quota_mode": "xfs"}


def chain(text: str, name: str) -> str:
    m = re.search(rf"\tchain {name} {{\n(.*?)\n\t}}", text, re.S)
    assert m, f"chain {name} missing"
    return m.group(1)


class DryRunCreate(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_vpn_plan(self):
        host = make_host(self.tmp, ah.Exec(dry_run=True))
        out = host.create_instance(dict(VPN_ARGS))
        dr = out["dry_run"]
        run = dr["docker_run"]
        for part in ("--name axiom-acme-gate", "--network axiom-acme-gate", "--ip 10.200.0.2", "--user 20000:20000",
                     "--cap-drop ALL", "--security-opt no-new-privileges:true", "--read-only", "--memory 8g",
                     "--memory-swap 8g", "--cpus 4", "--restart unless-stopped", "--log-opt max-size=50m",
                     "--gpus device=1", "--dns 1.1.1.1", "--init", "dst=/recordings", "dst=/data"):
            self.assertIn(part, run)
        self.assertNotRegex(run, r"(^| )(-p|--publish|--privileged|--network host)( |$)")
        self.assertTrue(run.endswith("axiom/instance:latest"))
        self.assertIn("--subnet 10.200.0.0/28 --gateway 10.200.0.1", dr["network"])
        self.assertIn("com.docker.network.bridge.name=axb0", dr["network"])
        env = dr["env"]
        for line in ("NVR_INSTANCE_NAME=Acme Gate · Central", "NVR_HUB_URL=wss://hub.axiomvision.ai/agent",
                     "NVR_DIRECT_ENABLED=0", "NVR_LOCAL_VLM_ENABLED=0", "NVR_REMOTE_VLM_URL=http://vllm:8000/v1",
                     "NVR_REMOTE_VLM_MODEL=Qwen/Qwen2.5-VL-32B-Instruct-AWQ", "NVR_YOLO_DEVICE=cuda:0",
                     "NVR_RECORDINGS_DIR=/recordings", "NVR_DATA_DIR=/data", "NVR_HOST=127.0.0.1"):
            self.assertIn(line + "\n", env)
        self.assertIn("NVR_HUB_ENROLL_TOKEN=<enroll token:", env)
        # secrets never appear in a dry run's output
        blob = json.dumps(out, ensure_ascii=False)
        self.assertNotIn(TOKEN, blob)
        self.assertNotIn(VLLM_KEY, blob)
        # quota: both directories in one XFS project, hard limit in KiB of 4000 decimal GB
        steps = [s["cmd"] for s in dr["steps"] if "cmd" in s]
        xfs = [c[3] for c in steps if c[0] == "xfs_quota"]
        self.assertEqual(len(xfs), 3)
        self.assertTrue(xfs[0].startswith("project -s -p ") and xfs[0].endswith(" 70000"))
        self.assertIn("recordings", xfs[0])
        self.assertIn("data", xfs[1])
        self.assertEqual(xfs[2], "limit -p bsoft=0 bhard=3906250000k 70000")
        # order: firewall loaded before the container starts
        kinds = [c[:2] for c in steps]
        self.assertLess(kinds.index(["nft", "-f"]), kinds.index(["docker", "run"]))
        self.assertLess(kinds.index(["docker", "network"]), kinds.index(["nft", "-f"]))
        fw = dr["firewall"]
        c = chain(fw, "inst_acme_gate")
        self.assertIn("ip daddr { 10.20.7.0/24 } accept", c)
        self.assertIn("ip daddr 10.200.0.3 tcp dport 8000 accept", c)
        self.assertIn(f"set hub4 {{ type ipv4_addr; elements = {{ {HUB} }} }}", fw)
        self.assertTrue(c.strip().splitlines()[-1].strip().startswith("counter drop"))
        # dry run changes nothing
        self.assertFalse((self.tmp / "srv").exists())
        self.assertEqual(out["instance"]["state"], "planned")

    def test_forward_plan_cpu(self):
        host = make_host(self.tmp, ah.Exec(dry_run=True))
        dr = host.create_instance(dict(FWD_ARGS))["dry_run"]
        self.assertNotIn("--gpus", dr["docker_run"])
        self.assertIn("NVR_YOLO_DEVICE=cpu\n", dr["env"])
        self.assertNotIn("NVR_HUB_ENROLL_TOKEN", dr["env"])
        c = chain(dr["firewall"], "inst_beta_yard")
        self.assertIn("ip daddr { 198.51.100.7 } meta l4proto tcp accept", c)
        self.assertNotIn("10.20.", c)

    def test_forward_ports_restricted(self):
        host = make_host(self.tmp, ah.Exec(dry_run=True))
        host.cfg["forward_tcp_ports"] = ["5541-5545", "8081-8085"]
        c = chain(host.create_instance(dict(FWD_ARGS))["dry_run"]["firewall"], "inst_beta_yard")
        self.assertIn("ip daddr { 198.51.100.7 } tcp dport { 5541-5545, 8081-8085 } accept", c)

    def test_dispatch_dry_run_flag(self):
        host = make_host(self.tmp, FakeExec())
        out = host.dispatch("create_instance", {**VPN_ARGS, "dry_run": True})
        self.assertIn("dry_run", out)
        self.assertEqual(host.load()["instances"], {})


class Validation(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.host = make_host(self.tmp, FakeExec())

    def bad(self, **kw):
        with self.assertRaises(ah.OpError):
            self.host.create_instance({**VPN_ARGS, **kw})

    def test_rejects(self):
        self.bad(id="Acme")
        self.bad(id="-x")
        self.bad(mode="nat")
        self.bad(subnet="")
        self.bad(subnet="8.8.8.0/24")                 # not private
        self.bad(subnet="10.200.3.0/24")              # inside the instance pool
        self.bad(subnet="10.20.7.1/24")               # host bits set
        self.bad(quota_gb=0)
        self.bad(gpu=7)                               # not in nvidia-smi
        self.bad(enroll_token="bad token\n")
        self.bad(name="two\nlines")
        self.bad(location="")
        with self.assertRaises(ah.OpError):
            self.host.create_instance({**FWD_ARGS, "public_ip": ""})

    def test_conflicts(self):
        self.host.create_instance(dict(VPN_ARGS))
        self.bad()                                    # same id
        self.bad(id="other", subnet="10.20.7.128/25")  # overlaps acme-gate's site subnet
        self.host.create_instance(dict(FWD_ARGS))
        with self.assertRaises(ah.OpError):
            self.host.create_instance({**FWD_ARGS, "id": "gamma"})   # same public IP

    def test_leftover_data_blocks_reuse(self):
        self.host.create_instance(dict(VPN_ARGS))
        (self.host.rec_dir("acme-gate") / "seg.mp4").write_bytes(b"x")
        self.host.delete_instance({"id": "acme-gate", "keep_data": True})
        self.bad()


class Isolation(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.exe = FakeExec()
        self.host = make_host(self.tmp, self.exe)
        self.a = self.host.create_instance(dict(VPN_ARGS))["instance"]
        self.b = self.host.create_instance(dict(FWD_ARGS))["instance"]
        self.reg = self.host.load()
        self.fw = self.host.firewall_text(self.reg)

    def test_registry_and_files(self):
        a, b = self.reg["instances"]["acme-gate"], self.reg["instances"]["beta-yard"]
        self.assertEqual((a["net_subnet"], a["ip"], a["vllm_ip"], a["uid"]), ("10.200.0.0/28", "10.200.0.2", "10.200.0.3", 20000))
        self.assertEqual((b["net_subnet"], b["ip"], b["vllm_ip"], b["uid"]), ("10.200.0.16/28", "10.200.0.18", "10.200.0.19", 20001))
        env = self.host.env_path("acme-gate").read_text(encoding="utf-8")
        self.assertIn(f"NVR_HUB_ENROLL_TOKEN={TOKEN}\n", env)
        self.assertIn(f"NVR_REMOTE_VLM_KEY={VLLM_KEY}\n", env)
        self.assertNotIn(TOKEN, self.host.registry_path.read_text())   # secrets live only in instance.env
        self.assertNotIn(VLLM_KEY, self.host.registry_path.read_text())
        self.assertEqual(len(self.exe.cmds("nft -f")), 2)                 # applied on each create

    def test_instances_cannot_reach_each_other(self):
        fw = self.fw
        ca, cb = chain(fw, "inst_acme_gate"), chain(fw, "inst_beta_yard")
        self.assertIn("10.20.7.0/24", ca)
        self.assertNotIn("198.51.100.7", ca)
        self.assertIn("198.51.100.7", cb)
        self.assertNotIn("10.20.7.0/24", cb)
        for c, own_vllm, other in ((ca, "10.200.0.3", "10.200.0.1"), (cb, "10.200.0.19", "10.200.0.3")):
            self.assertIn(f"ip daddr {own_vllm} tcp dport 8000 accept", c)
            self.assertNotIn(f"ip daddr {other} ", c)      # not the other instance's vLLM address or a gateway
            self.assertNotIn("10.200.0.0/16", c)            # no rule opens the pool
            self.assertTrue(c.strip().splitlines()[-1].strip().startswith("counter drop"))
        fwd = chain(fw, "forward").splitlines()
        jumps = [i for i, l in enumerate(fwd) if "jump inst_" in l]
        self.assertEqual([fwd[i].strip() for i in jumps],
                         ["ip saddr 10.200.0.2 jump inst_acme_gate", "ip saddr 10.200.0.18 jump inst_beta_yard"])
        pool_drop = next(i for i, l in enumerate(fwd) if "ip saddr 10.200.0.0/16 counter drop" in l)
        into_drop = next(i for i, l in enumerate(fwd) if "ip daddr 10.200.0.0/16 counter drop" in l)
        self.assertGreater(pool_drop, max(jumps))
        self.assertGreater(into_drop, pool_drop)
        self.assertIn("ip saddr 10.200.0.0/16 counter drop", chain(fw, "input"))
        self.assertIn("priority filter - 10", chain(fw, "forward"))
        # atomic replacement of exactly our table
        self.assertTrue(fw.splitlines()[3:6] == ["table inet axiom", "delete table inet axiom", "table inet axiom {"])

    def test_delete_removes_rules_and_data(self):
        self.host.delete_instance({"id": "acme-gate", "purge": True})
        fw = (self.tmp / "srv" / "firewall.nft").read_text()
        self.assertNotIn("inst_acme_gate", fw)
        self.assertIn("inst_beta_yard", fw)
        self.assertFalse(self.host.rec_dir("acme-gate").exists())
        self.assertTrue(self.exe.cmds("docker rm -f axiom-acme-gate"))
        self.assertTrue(self.exe.cmds("docker network rm axiom-acme-gate"))
        self.assertTrue([c for c in self.exe.cmds("xfs_quota") if "bhard=0 70000" in c[3]])
        self.assertFalse(self.exe.cmds("zfs"))                            # XFS hosts never call zfs
        # the next instance does not reuse slot 0 (or its uid / project id)
        c = self.host.create_instance({**VPN_ARGS, "id": "delta", "subnet": "10.20.9.0/24"})["instance"]
        self.assertEqual(c["network"], "10.200.0.32/28")

    def test_delete_keeps_footage_unless_purged(self):
        (self.host.rec_dir("beta-yard") / "seg.mp4").write_bytes(b"x")
        out = self.host.dispatch("delete_instance", {"id": "beta-yard", "purge": False})
        self.assertIn("data kept", out["detail"])
        self.assertTrue((self.host.rec_dir("beta-yard") / "seg.mp4").exists())
        self.host.dispatch("delete_instance", {"id": "acme-gate"})          # neither flag: keep
        self.assertTrue(self.host.rec_dir("acme-gate").exists())
        self.assertNotIn(TOKEN, self.host.env_path("acme-gate").read_text(encoding="utf-8"))

    def test_hub_style_ids(self):
        c = self.host.create_instance({**VPN_ARGS, "id": "ci_1a2b3c4d5e6f", "subnet": "10.20.11.0/24"})["instance"]
        self.assertEqual(c["id"], "ci_1a2b3c4d5e6f")
        fw = (self.tmp / "srv" / "firewall.nft").read_text()
        self.assertIn("jump inst_ci_1a2b3c4d5e6f", fw)
        self.assertIn("--hostname ci-1a2b3c4d5e6f", " ".join(self.exe.cmds("docker run")[-1]))
        with self.assertRaises(ah.OpError):   # same nft chain name as acme-gate
            self.host.create_instance({**VPN_ARGS, "id": "acme_gate", "subnet": "10.20.12.0/24"})

    def test_set_quota(self):
        out = self.host.set_quota({"id": "beta-yard", "quota_gb": 3000})
        self.assertEqual(out["instance"]["quota_gb"], 3000)
        self.assertIn("limit -p bsoft=0 bhard=2929687500k 70001", self.exe.cmds("xfs_quota")[-1][3])
        reg = self.host.load()   # pretend a non-XFS host: usage comes from the du cache
        reg["instances"]["beta-yard"]["quota_mode"] = "none"
        self.host.save(reg)
        self.host.du_cache["beta-yard"] = 2500.0
        with self.assertRaises(ah.OpError):
            self.host.set_quota({"id": "beta-yard", "quota_gb": 1000})
        self.host.set_quota({"id": "beta-yard", "quota_gb": 1000, "force": True})
        self.assertFalse(self.exe.cmds("zfs"))

    def test_quota_mode_none(self):
        out = self.host.create_instance({**VPN_ARGS, "id": "nq", "subnet": "10.20.13.0/24", "quota_mode": "none"})
        self.assertIn("NOT enforced", out["detail"])
        n = len(self.exe.cmds("xfs_quota"))
        self.assertIn("not enforced", self.host.set_quota({"id": "nq", "quota_gb": 50})["detail"])
        self.host.delete_instance({"id": "nq", "purge": True})
        self.assertEqual(len(self.exe.cmds("xfs_quota")), n)
        self.assertFalse(self.exe.cmds("zfs"))
        self.assertFalse(self.host.rec_dir("nq").exists())

    def test_finish_enroll_scrubs_token(self):
        self.exe.answers["docker exec axiom-acme-gate"] = json.dumps({"enrolled": True, "site_id": "s1", "cameras": 0})
        self.assertTrue(self.host.finish_enroll(self.host.load()["instances"]["acme-gate"]))
        env = self.host.env_path("acme-gate").read_text(encoding="utf-8")
        self.assertNotIn("NVR_HUB_ENROLL_TOKEN", env)
        self.assertIn("NVR_REMOTE_VLM_KEY=", env)
        self.assertFalse(self.host.load()["instances"]["acme-gate"]["enroll_pending"])
        self.assertFalse(self.host.finish_enroll(self.host.load()["instances"]["acme-gate"]))   # idempotent

    def test_reconcile_reattaches_vllm(self):
        self.exe.answers["docker inspect -f"] = json.dumps({"axiom-ai": {}, "axiom-acme-gate": {}})
        self.exe.answers["docker ps -a"] = "acme-gate\trunning\nbeta-yard\texited\n"
        before = len(self.exe.cmds("docker run"))
        out = self.host.reconcile()
        self.assertEqual(out["vllm_attached"], ["beta-yard"])
        self.assertEqual(out["containers_started"], [])                 # exited ones are left alone
        self.assertEqual(len(self.exe.cmds("docker run")), before)

    def test_list_view(self):
        self.exe.answers["docker ps -a"] = "acme-gate\trunning\n"
        views = {v["id"]: v for v in self.host.list_instances()["instances"]}
        self.assertEqual(views["acme-gate"]["state"], "running")
        self.assertEqual(views["beta-yard"]["state"], "missing")
        for k in ("id", "location_id", "name", "state", "cameras", "quota_gb", "used_gb", "gpu", "mode"):
            self.assertIn(k, views["acme-gate"])
        self.assertEqual(views["acme-gate"]["gpu"], 1)
        self.assertIsNone(views["beta-yard"]["gpu"])


ZVPN = {**VPN_ARGS, "quota_mode": "auto"}
ZFWD = {**FWD_ARGS, "quota_mode": "auto"}
REC_DS, INST_DS = "axiom/recordings/acme-gate", "axiom/instances/acme-gate"


class ZfsMode(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.state = ZfsState(self.tmp / "srv")
        self.exe = ZfsExec(self.state)
        self.host = make_host(self.tmp, self.exe)

    def zfs_calls(self, sub: str) -> list[list[str]]:
        return [c for c in self.exe.calls if c[:2] == ["zfs", sub]]

    def test_auto_detects_zfs(self):
        self.assertEqual(self.host.quota_mode_auto(), "zfs")
        self.assertEqual(self.host.zfs_parents(), {"recordings": "axiom/recordings", "instance": "axiom/instances"})
        # a ZFS root alone is not enough: the recordings directory itself must be a dataset's mountpoint
        del self.state.mounts[str(self.tmp / "srv" / "recordings")]
        self.assertNotEqual(self.host.quota_mode_auto(), "zfs")
        # and the XFS host of the other tests is still detected as XFS
        self.assertEqual(make_host(Path(tempfile.mkdtemp()), FakeExec()).quota_mode_auto(), "xfs")

    def test_create_makes_child_datasets_with_byte_quotas(self):
        out = self.host.create_instance(dict(ZVPN))
        self.assertEqual(self.zfs_calls("create"), [
            ["zfs", "create", "-o", "quota=50000000000", INST_DS],      # 50 GB instance dir (host.json default)
            ["zfs", "create", "-o", "quota=4000000000000", REC_DS]])   # 4000 decimal GB, in bytes (not 4000G = GiB)
        self.assertFalse(self.exe.cmds("xfs_quota"))
        rec = self.host.load()["instances"]["acme-gate"]
        self.assertEqual(rec["quota_mode"], "zfs")
        self.assertEqual(rec["datasets"], {"recordings": REC_DS, "instance": INST_DS})
        self.assertEqual(out["instance"]["quota_mode"], "zfs")
        self.assertNotIn("NOT enforced", out["detail"])
        # the dataset mountpoints are the instance's directories
        self.assertEqual(self.state.mounts[str(self.host.rec_dir("acme-gate"))], REC_DS)
        self.assertEqual(self.state.mounts[str(self.host.inst_dir("acme-gate"))], INST_DS)
        self.assertTrue((self.host.inst_dir("acme-gate") / "data").is_dir())
        self.assertTrue(self.host.env_path("acme-gate").exists())
        # the zfs creates come before anything is written into those directories
        order = [" ".join(c[:2]) for c in self.exe.calls]
        self.assertLess(max(i for i, c in enumerate(order) if c == "zfs create"), order.index("nft -f"))
        # instance dir quota from host.json
        self.host.cfg["instance_dir_quota_gb"] = 20
        self.host.create_instance(dict(ZFWD))
        self.assertIn(["zfs", "create", "-o", "quota=20000000000", "axiom/instances/beta-yard"], self.zfs_calls("create"))
        self.assertIn(["zfs", "create", "-o", "quota=2000000000000", "axiom/recordings/beta-yard"], self.zfs_calls("create"))

    def test_used_gb_and_capacity(self):
        self.host.create_instance(dict(ZVPN))
        self.state.datasets[REC_DS]["used"] = 812_400_000_000
        self.state.datasets[INST_DS]["used"] = 3_000_000_000
        view = self.host.list_instances()["instances"][0]
        self.assertEqual(view["used_gb"], 815.4)                       # recordings + instance dir
        self.assertFalse(self.exe.cmds("du"))
        self.host.refresh_du()
        self.assertFalse(self.exe.cmds("du"))                           # exact usage needs no du pass
        disk = self.host.capacity()["disks"][0]
        self.assertEqual(disk["fs"], "zfs")
        self.assertEqual(disk["quota_mode"], "zfs")
        self.assertEqual(disk["dataset"], "axiom/recordings")
        self.assertEqual(disk["total_gb"], 50000.0)                     # pool root used + available
        self.assertEqual(disk["free_gb"], 48000.0)                      # what axiom/recordings can still get
        self.assertTrue(self.exe.cmds("zfs get -Hp -o name,property,value used,available axiom axiom/recordings"))

    def test_create_dry_run_runs_no_zfs(self):
        dry = ZfsExec(self.state, dry_run=True)
        out = make_host(self.tmp, dry).create_instance(dict(ZVPN))
        steps = [s["cmd"] for s in out["dry_run"]["steps"] if "cmd" in s]
        self.assertIn(["zfs", "create", "-o", "quota=4000000000000", REC_DS], steps)
        self.assertIn(["zfs", "create", "-o", "quota=50000000000", INST_DS], steps)
        self.assertFalse([c for c in dry.calls if c[:2] in (["zfs", "create"], ["zfs", "destroy"], ["zfs", "set"])])
        self.assertNotIn(REC_DS, self.state.datasets)
        self.assertEqual(self.host.load()["instances"], {})

    def test_create_refusals(self):
        self.state.datasets[REC_DS] = {"used": 5, "available": 1}       # kept from an earlier instance
        with self.assertRaises(ah.OpError):
            self.host.create_instance(dict(ZVPN))
        del self.state.datasets[REC_DS]
        del self.state.mounts[str(self.tmp / "srv" / "recordings")]
        with self.assertRaises(ah.OpError):                             # explicit zfs, but no dataset there
            self.host.create_instance({**ZVPN, "quota_mode": "zfs"})
        self.assertFalse(self.zfs_calls("create"))

    def test_instances_dir_not_on_zfs(self):
        del self.state.mounts[str(self.tmp / "srv" / "instances")]
        out = self.host.create_instance(dict(ZVPN))
        self.assertEqual(self.zfs_calls("create"), [["zfs", "create", "-o", "quota=4000000000000", REC_DS]])
        self.assertIn("data/ has no quota", out["detail"])
        self.assertEqual(self.host.load()["instances"]["acme-gate"]["datasets"]["instance"], None)
        self.host.delete_instance({"id": "acme-gate", "purge": True})
        self.assertEqual(self.zfs_calls("destroy"), [["zfs", "destroy", REC_DS]])
        self.assertFalse(self.host.inst_dir("acme-gate").exists())     # plain directory: removed as before

    def test_rollback_destroys_new_datasets(self):
        real = self.exe._run

        def fail_docker_run(argv, input, timeout):
            if argv[:2] == ["docker", "run"]:
                self.exe.calls.append(list(argv))
                return ah.Result(125, "", "docker: image not found")
            return real(argv, input, timeout)
        self.exe._run = fail_docker_run
        with self.assertRaises(ah.CmdError):
            self.host.create_instance(dict(ZVPN))
        self.assertEqual(self.zfs_calls("destroy"), [["zfs", "destroy", REC_DS], ["zfs", "destroy", INST_DS]])
        self.assertNotIn(REC_DS, self.state.datasets)
        self.assertEqual(self.host.load()["instances"], {})

    def test_set_quota(self):
        self.host.create_instance(dict(ZVPN))
        self.state.datasets[REC_DS]["used"] = 2_900_000_000_000
        self.state.datasets[INST_DS]["used"] = 600_000_000_000         # not under quota_gb: not in the guard
        with self.assertRaises(ah.OpError) as cm:
            self.host.set_quota({"id": "acme-gate", "quota_gb": 2000})
        self.assertIn("already uses 2900 GB", str(cm.exception))
        self.assertFalse(self.zfs_calls("set"))
        out = self.host.set_quota({"id": "acme-gate", "quota_gb": 3000})
        self.assertEqual(self.zfs_calls("set"), [["zfs", "set", "quota=3000000000000", REC_DS]])
        self.assertNotIn("not enforced", out["detail"])
        self.assertEqual(self.state.datasets[REC_DS]["quota"], 3_000_000_000_000)
        self.host.set_quota({"id": "acme-gate", "quota_gb": 1000, "force": True})
        self.assertEqual(self.zfs_calls("set")[-1], ["zfs", "set", "quota=1000000000000", REC_DS])
        self.assertEqual(self.host.load()["instances"]["acme-gate"]["quota_gb"], 1000)
        self.assertFalse(self.exe.cmds("xfs_quota"))

    def test_delete_purge_destroys_only_the_two_children(self):
        self.host.create_instance(dict(ZVPN))
        self.host.create_instance(dict(ZFWD))
        out = self.host.delete_instance({"id": "acme-gate", "purge": True})
        self.assertIn("data removed", out["detail"])
        self.assertEqual(self.zfs_calls("destroy"), [["zfs", "destroy", REC_DS], ["zfs", "destroy", INST_DS]])
        self.assertFalse([c for c in self.exe.calls if c[0] == "zfs" and {"-r", "-R", "-f"} & set(c)])
        for kept in ("axiom", "axiom/recordings", "axiom/instances", "axiom/recordings/beta-yard"):
            self.assertIn(kept, self.state.datasets)
        self.assertNotIn(REC_DS, self.state.datasets)
        self.assertNotIn("acme-gate", self.host.load()["instances"])
        # the id can be used again
        self.host.create_instance(dict(ZVPN))

    def test_delete_keep_leaves_datasets(self):
        self.host.create_instance(dict(ZVPN))
        out = self.host.dispatch("delete_instance", {"id": "acme-gate"})   # neither flag: keep
        self.assertIn("data kept", out["detail"])
        self.assertFalse(self.zfs_calls("destroy"))
        self.assertFalse(self.zfs_calls("set"))
        self.assertEqual(self.state.datasets[REC_DS]["quota"], 4_000_000_000_000)
        self.assertNotIn(TOKEN, self.host.env_path("acme-gate").read_text(encoding="utf-8"))
        with self.assertRaises(ah.OpError):                             # kept data blocks re-creating the id
            self.host.create_instance(dict(ZVPN))

    def test_delete_refuses_names_that_are_not_the_instance_child(self):
        self.host.create_instance(dict(ZVPN))
        bad = ["axiom/recordings", "axiom", "axiom/recordings/beta-yard", "axiom/recordings/acme-gate/x",
               "axiom/recordings/acme-gate@snap", "other/recordings/acme-gate", "axiom/instances/acme-gate",
               "axiom/recordings/acme-gate "]
        for name in bad:
            reg = self.host.load()
            reg["instances"]["acme-gate"]["datasets"]["recordings"] = name
            self.host.save(reg)
            with self.assertRaises(ah.OpError, msg=name):
                self.host.delete_instance({"id": "acme-gate", "purge": True})
            self.assertIn("acme-gate", self.host.load()["instances"], name)   # refused before anything changed
            self.assertFalse(self.exe.cmds("docker rm"), name)
        self.assertFalse(self.zfs_calls("destroy"))
        # the parent dataset is no longer mounted where it was: refuse rather than guess
        reg = self.host.load()
        reg["instances"]["acme-gate"]["datasets"]["recordings"] = REC_DS
        self.host.save(reg)
        del self.state.mounts[str(self.tmp / "srv" / "recordings")]
        with self.assertRaises(ah.OpError):
            self.host.delete_instance({"id": "acme-gate", "purge": True})
        self.assertFalse(self.zfs_calls("destroy"))

    def test_zfs_child_names(self):
        self.assertEqual(ah.zfs_child("axiom/recordings", "ci_1a2b3c4d5e6f"), "axiom/recordings/ci_1a2b3c4d5e6f")
        for parent, iid in (("axiom/recordings", "../x"), ("axiom/recordings", "a/b"), ("axiom/recordings", ""),
                            ("axiom/recordings", "Acme"), ("", "acme"), (None, "acme"), ("axiom@s", "acme"),
                            ("axiom/", "acme"), ("/axiom", "acme")):
            with self.assertRaises(ah.OpError, msg=(parent, iid)):
                ah.zfs_child(parent, iid)
        self.assertEqual(ah.check_zfs_child(REC_DS, "axiom/recordings", "acme-gate"), REC_DS)
        with self.assertRaises(ah.OpError):
            ah.check_zfs_child("axiom/recordings", "axiom/recordings", "acme-gate")

    def test_delete_dry_run_prints_destroys_and_runs_nothing(self):
        self.host.create_instance(dict(ZVPN))
        dry = ZfsExec(self.state, dry_run=True)
        dhost = make_host(self.tmp, dry)
        out = dhost.delete_instance({"id": "acme-gate", "keep_data": False}) | ah._plan(dhost)
        steps = [s["cmd"] for s in out["dry_run"]["steps"] if "cmd" in s]
        self.assertIn(["zfs", "destroy", REC_DS], steps)
        self.assertIn(["zfs", "destroy", INST_DS], steps)
        self.assertFalse([s for s in out["dry_run"]["steps"] if "rmtree" in s])   # datasets, not rm -rf
        self.assertFalse([c for c in dry.calls if c[0] in ("zfs", "docker", "nft") and c[:2] not in
                          (["zfs", "get"], ["zfs", "list"], ["docker", "ps"])])
        self.assertIn(REC_DS, self.state.datasets)
        self.assertIn("acme-gate", self.host.load()["instances"])

    def test_destroy_failure_is_reported_not_forced(self):
        self.host.create_instance(dict(ZVPN))
        self.state.datasets[REC_DS]["busy"] = True
        out = self.host.delete_instance({"id": "acme-gate", "purge": True})
        self.assertIn(f"could not destroy ZFS dataset {REC_DS}", out["detail"])
        self.assertNotIn("data removed", out["detail"])
        self.assertIn(REC_DS, self.state.datasets)
        self.assertNotIn(INST_DS, self.state.datasets)
        self.assertTrue(self.host.rec_dir("acme-gate").exists())        # its files were not rm -rf'd either

    def test_reconcile_reapplies_zfs_quotas(self):
        self.host.create_instance(dict(ZVPN))
        self.state.datasets[REC_DS]["quota"] = 0                        # someone cleared it by hand
        self.host.reconcile(resolve=False)
        self.assertIn(["zfs", "set", "quota=4000000000000", REC_DS], self.zfs_calls("set"))
        self.assertIn(["zfs", "set", "quota=50000000000", INST_DS], self.zfs_calls("set"))
        self.assertEqual(self.state.datasets[REC_DS]["quota"], 4_000_000_000_000)


class FakeDNS:
    """A resolver: name -> addresses; a missing name (or one set to None) does not resolve."""

    def __init__(self, table: dict | None = None) -> None:
        self.table = dict(table or {})
        self.asked: list[str] = []

    def __call__(self, name: str) -> list[str]:
        self.asked.append(name)
        got = self.table.get(name)
        if got is None:
            raise OSError(f"{name}: Name or service not known")
        return list(got)


FORBID = [(ah.ipaddress.ip_network("10.200.0.0/16"), "the instance pool"),
          (ah.ipaddress.ip_network("10.201.0.0/24"), "the AI network"),
          (ah.ipaddress.ip_network(f"{HUB}/32"), "the hub")]


class CameraNetwork(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.exe = FakeExec()
        self.dns = FakeDNS({"cam1.example.net": ["203.0.113.21"], "cam2.dyn.example.org": ["198.51.100.40", "198.51.100.41"]})
        self.host = make_host(self.tmp, self.exe)
        self.host.resolver = self.dns
        self.host.create_instance(dict(VPN_ARGS))
        self.host.create_instance(dict(FWD_ARGS))

    def fw(self) -> str:
        return (self.tmp / "srv" / "firewall.nft").read_text()

    def test_validation_accepts(self):
        out = ah.check_camera_network(["192.168.105.0/24", "10.20.7.0/24", "100.64.12.0/24", "172.16.0.0/16", "192.168.105.0/24"],
                                      "203.0.113.7, 198.51.100.7", ["Cam1.Example.NET.", "cam1.example.net", "a-b.c-d.example"], FORBID)
        self.assertEqual(out, {"subnets": ["192.168.105.0/24", "10.20.7.0/24", "100.64.12.0/24", "172.16.0.0/16"],
                               "public_ips": ["203.0.113.7", "198.51.100.7"],
                               "hosts": ["cam1.example.net", "a-b.c-d.example"]})
        self.assertEqual(ah.check_camera_network(None, "", [], FORBID), {"subnets": [], "public_ips": [], "hosts": []})
        self.assertEqual(len(ah.check_camera_network([f"10.30.{i}.0/24" for i in range(32)], [], [], FORBID)["subnets"]), 32)

    def test_validation_refuses(self):
        bad_subnets = ["0.0.0.0/0", "10.0.0.0/8", "192.168.0.0/15", "8.8.8.0/24", "203.0.113.0/24", "10.200.3.0/24",
                       "10.200.0.0/16", "10.201.0.0/24", "10.201.0.128/25", "127.0.0.0/16", "169.254.1.0/24", "224.0.0.0/24",
                       "10.20.7.1/24", "fd00::/64", "nonsense", "10.20.7.0/33"]
        for s in bad_subnets:
            with self.assertRaises(ah.OpError, msg=s):
                ah.check_camera_network([s], [], [], FORBID)
        with self.assertRaises(ah.OpError):   # a subnet holding the hub's address
            ah.check_camera_network(["10.9.0.0/16"], [], [], FORBID + [(ah.ipaddress.ip_network("10.9.1.1/32"), "the hub")])
        for p in ("127.0.0.1", "0.0.0.0", "169.254.10.1", "224.1.1.1", "255.255.255.255", "240.0.0.1", "10.200.0.5",
                  "10.201.0.10", HUB, "2001:db8::1", "cam.example.net", "1.2.3"):
            with self.assertRaises(ah.OpError, msg=p):
                ah.check_camera_network([], [p], [], FORBID)
        for h in ("localhost", "cam", "-cam.example.net", "cam-.example.net", "cam..example.net", "cam_1.example.net",
                  "1.2.3.4", "999.1.1.1", "x." * 130 + "net", "a" * 64 + ".example.net", "cam.localhost", "cam example.net",
                  "cam.example.net/24", ""):
            if not h:
                continue
            with self.assertRaises(ah.OpError, msg=h):
                ah.check_camera_network([], [], [h], FORBID)
        with self.assertRaises(ah.OpError) as cm:   # 33 entries in all
            ah.check_camera_network([f"10.30.{i}.0/24" for i in range(20)], [f"203.0.113.{i}" for i in range(30, 42)],
                                    ["cam.example.net"], FORBID)
        self.assertIn("at most 32", str(cm.exception))

    def test_set_mixes_lan_vpn_and_remote_cameras(self):
        before = len(self.exe.cmds("nft -f"))
        out = self.host.dispatch("set_camera_network", {"id": "acme-gate", "subnets": ["192.168.105.0/24", "10.20.7.0/24"],
                                                         "public_ips": ["203.0.113.7"], "hosts": ["cam1.example.net", "cam2.dyn.example.org"]})
        self.assertIn("acme-gate cameras: 192.168.105.0/24, 10.20.7.0/24, 203.0.113.7, cam1.example.net, cam2.dyn.example.org", out["detail"])
        self.assertEqual(len(self.exe.cmds("nft -f")), before + 1)   # loaded, the same way create/delete do
        c = chain(self.fw(), "inst_acme_gate")
        self.assertIn('ip daddr { 10.20.7.0/24, 192.168.105.0/24 } accept comment "camera networks', c)
        self.assertIn("ip daddr { 198.51.100.40, 198.51.100.41, 203.0.113.7, 203.0.113.21 } meta l4proto tcp accept", c)
        self.assertIn("# cam1.example.net: 203.0.113.21", c)
        self.assertTrue(c.strip().splitlines()[-1].strip().startswith("counter drop"))
        cb = chain(self.fw(), "inst_beta_yard")                       # the other instance is untouched
        self.assertNotIn("192.168.105.0/24", cb)
        self.assertNotIn("203.0.113.7", cb)
        self.assertIn("ip daddr { 198.51.100.7 } meta l4proto tcp accept", cb)
        rec = self.host.load()["instances"]["acme-gate"]
        self.assertEqual(rec["hosts"], ["cam1.example.net", "cam2.dyn.example.org"])
        self.assertEqual(rec["host_ips"], {"cam1.example.net": ["203.0.113.21"], "cam2.dyn.example.org": ["198.51.100.40", "198.51.100.41"]})
        self.assertEqual(rec["mode"], "vpn")                         # mode stays (Peplink sheet)
        view = out["instance"]
        self.assertEqual((view["subnets"], view["public_ips"], view["hosts"]),
                         (["192.168.105.0/24", "10.20.7.0/24"], ["203.0.113.7"], ["cam1.example.net", "cam2.dyn.example.org"]))
        self.assertEqual(view["host_ips"]["cam1.example.net"], ["203.0.113.21"])
        # idempotent: the same request again changes nothing
        text = self.fw()
        again = self.host.set_camera_network({"id": "acme-gate", "subnets": ["192.168.105.0/24", "10.20.7.0/24"],
                                              "public_ips": ["203.0.113.7"], "hosts": ["cam1.example.net", "cam2.dyn.example.org"]})
        self.assertIn("(unchanged)", again["detail"])
        self.assertEqual(self.fw(), text)
        # a list left out stays; [] empties one
        self.host.set_camera_network({"id": "acme-gate", "hosts": []})
        rec = self.host.load()["instances"]["acme-gate"]
        self.assertEqual((rec["subnets"], rec["public_ips"], rec["hosts"], rec["host_ips"]),
                         (["192.168.105.0/24", "10.20.7.0/24"], ["203.0.113.7"], [], {}))
        self.assertNotIn("203.0.113.21", chain(self.fw(), "inst_acme_gate"))
        # everything emptied: only hub, DNS and vLLM remain
        out = self.host.set_camera_network({"id": "acme-gate", "subnets": [], "public_ips": ""})
        self.assertIn("no camera addresses", out["detail"])
        c = chain(self.fw(), "inst_acme_gate")
        self.assertNotIn("camera networks", c)
        self.assertNotIn("remote cameras", c)

    def test_set_refusals_change_nothing(self):
        text, reg = self.fw(), self.host.registry_path.read_text()
        for args in ({"id": "acme-gate", "subnets": ["0.0.0.0/0"]},
                     {"id": "acme-gate", "subnets": ["10.200.0.0/24"]},
                     {"id": "acme-gate", "public_ips": ["198.51.100.7"]},          # beta-yard's public IP
                     {"id": "beta-yard", "subnets": ["10.20.7.0/25"]},            # inside acme-gate's camera subnet
                     {"id": "beta-yard", "public_ips": ["10.20.7.20"]},           # an address in acme-gate's subnet
                     {"id": "acme-gate", "subnets": ["198.51.100.0/24"]},          # not private
                     {"id": "acme-gate", "hosts": ["not a name"]},
                     {"id": "nope", "subnets": ["192.168.105.0/24"]}):
            with self.assertRaises(ah.OpError, msg=args):
                self.host.set_camera_network(args)
        self.host.set_camera_network({"id": "acme-gate", "hosts": ["cam1.example.net"]})
        with self.assertRaises(ah.OpError):                               # same DNS name on two instances
            self.host.set_camera_network({"id": "beta-yard", "hosts": ["CAM1.example.net"]})
        self.host.set_camera_network({"id": "acme-gate", "hosts": []})
        self.assertEqual(self.fw(), text)
        self.assertEqual(json.loads(self.host.registry_path.read_text()), json.loads(reg))
        # its own subnet again is no conflict
        self.host.set_camera_network({"id": "acme-gate", "subnets": ["10.20.7.0/24", "192.168.105.0/24"]})

    def test_nft_failure_keeps_old_rules_and_registry(self):
        text, reg = self.fw(), json.loads(self.host.registry_path.read_text())
        real = self.exe._run

        def fail_nft(argv, input, timeout):
            if argv[0] == "nft":
                self.exe.calls.append(list(argv))
                return ah.Result(1, "", "Error: syntax error")
            return real(argv, input, timeout)
        self.exe._run = fail_nft
        with self.assertRaises(ah.CmdError):
            self.host.set_camera_network({"id": "acme-gate", "subnets": ["192.168.105.0/24"]})
        self.assertEqual(self.fw(), text)
        self.assertEqual(json.loads(self.host.registry_path.read_text()), reg)

    def test_dry_run_runs_nothing(self):
        text, reg = self.fw(), self.host.registry_path.read_text()
        n = len(self.exe.calls)
        out = self.host.dispatch("set_camera_network", {"id": "acme-gate", "subnets": ["192.168.105.0/24"],
                                                         "hosts": ["cam1.example.net"], "dry_run": True})
        self.assertIn("192.168.105.0/24", chain(out["dry_run"]["firewall"], "inst_acme_gate"))
        self.assertIn("203.0.113.21", chain(out["dry_run"]["firewall"], "inst_acme_gate"))   # the injected resolver
        self.assertIn(["nft", "-f", str(self.tmp / "srv" / "firewall.nft")], [s.get("cmd") for s in out["dry_run"]["steps"]])
        self.assertEqual(out["instance"]["state"], "planned")
        self.assertEqual(len(self.exe.calls), n)                         # the real exec ran nothing
        self.assertEqual(self.fw(), text)
        self.assertEqual(self.host.registry_path.read_text(), reg)

    def test_host_names_resolved_and_refreshed(self):
        self.dns.table["evil.example.net"] = ["10.200.0.18", "127.0.0.1", HUB, "203.0.113.99"]
        self.host.set_camera_network({"id": "acme-gate", "hosts": ["cam1.example.net", "evil.example.net", "gone.example.net"]})
        rec = self.host.load()["instances"]["acme-gate"]
        # pool, loopback and hub addresses a name resolves to are never opened; a name that does not resolve has none
        self.assertEqual(rec["host_ips"], {"cam1.example.net": ["203.0.113.21"], "evil.example.net": ["203.0.113.99"],
                                           "gone.example.net": []})
        c = chain(self.fw(), "inst_acme_gate")
        self.assertIn("ip daddr { 203.0.113.21, 203.0.113.99 } meta l4proto tcp accept", c)
        self.assertIn("# gone.example.net: not resolved yet", c)
        self.assertNotIn("10.200.0.18", c)
        reg = self.host.load()
        self.assertEqual(self.host.dns_changed(reg), "")
        # the dynamic DNS address moves: the 10-minute check notices, reconcile re-resolves and reloads
        self.dns.table["cam1.example.net"] = ["203.0.113.22"]
        self.assertIn("camera host addresses changed", self.host.dns_changed(reg))
        self.host.reconcile()
        c = chain(self.fw(), "inst_acme_gate")
        self.assertIn("203.0.113.22", c)
        self.assertNotIn("203.0.113.21", c)
        # a lookup that fails keeps the last known addresses
        del self.dns.table["cam1.example.net"]
        self.host.reconcile()
        self.assertIn("203.0.113.22", chain(self.fw(), "inst_acme_gate"))
        self.assertEqual(self.host.dns_changed(self.host.load()), "")
        # resolve=False (boot, --offline) never asks DNS
        asked = len(self.dns.asked)
        self.host.reconcile(resolve=False)
        self.assertEqual(len(self.dns.asked), asked)

    def test_old_registry_migrates(self):
        reg = json.loads(self.host.registry_path.read_text())
        for rec in reg["instances"].values():
            rec.pop("hosts")
            rec.pop("host_ips")
        self.host.registry_path.write_text(json.dumps(reg))
        loaded = self.host.load()["instances"]
        self.assertEqual((loaded["acme-gate"]["hosts"], loaded["acme-gate"]["host_ips"]), ([], {}))
        fw = self.host.firewall_text(self.host.load())
        self.assertIn("ip daddr { 10.20.7.0/24 } accept", chain(fw, "inst_acme_gate"))
        self.assertIn("ip daddr { 198.51.100.7 } meta l4proto tcp accept", chain(fw, "inst_beta_yard"))
        self.assertEqual(self.host.list_instances()["instances"][0]["hosts"], [])
        self.host.set_camera_network({"id": "beta-yard", "hosts": ["cam1.example.net"]})
        self.assertEqual(self.host.load()["instances"]["beta-yard"]["public_ips"], ["198.51.100.7"])

    def test_create_with_lists(self):
        out = self.host.create_instance({**VPN_ARGS, "id": "mixed", "subnet": None, "subnets": ["192.168.105.0/24"],
                                         "public_ips": ["203.0.113.7"], "hosts": ["cam1.example.net"]})
        v = out["instance"]
        self.assertEqual((v["subnets"], v["public_ips"], v["hosts"]), (["192.168.105.0/24"], ["203.0.113.7"], ["cam1.example.net"]))
        c = chain(self.fw(), "inst_mixed")
        self.assertIn("ip daddr { 192.168.105.0/24 } accept", c)
        self.assertIn("ip daddr { 203.0.113.7, 203.0.113.21 } meta l4proto tcp accept", c)
        # the hub's forward mode with a DNS name as the Site's public address: a camera host name
        out = self.host.create_instance({**FWD_ARGS, "id": "dyn", "public_ip": "cam2.dyn.example.org"})
        self.assertEqual((out["instance"]["public_ips"], out["instance"]["hosts"]), ([], ["cam2.dyn.example.org"]))
        self.assertIn("198.51.100.40", chain(self.fw(), "inst_dyn"))
        with self.assertRaises(ah.OpError):                               # nothing at all
            self.host.create_instance({**VPN_ARGS, "id": "empty", "subnet": None})

    def test_cli(self):
        ns = ah.argparse.Namespace(id="acme-gate", subnet=["192.168.105.0/24,10.20.7.0/24"], public_ip=None, host=None,
                                   clear=["hosts"])
        self.assertEqual(ah.camera_args(ns), {"id": "acme-gate", "subnets": ["192.168.105.0/24", "10.20.7.0/24"],
                                              "public_ips": None, "hosts": []})
        ns.clear, ns.subnet, ns.host = ["all"], None, ["cam1.example.net"]
        self.assertEqual(ah.camera_args(ns), {"id": "acme-gate", "subnets": [], "public_ips": [], "hosts": ["cam1.example.net"]})
        cfg = self.tmp / "host.json"
        cfg.write_text(json.dumps({"root": str(self.tmp / "srv"), "hub_ips": [HUB], "ai_env": str(self.tmp / "ai.env")}))
        text, reg = self.fw(), self.host.registry_path.read_text()
        buf = __import__("io").StringIO()
        with __import__("contextlib").redirect_stdout(buf):
            rc = ah.main(["--config", str(cfg), "set-camera-network", "--id", "acme-gate", "--subnet", "192.168.105.0/24",
                          "--public-ip", "203.0.113.7", "--dry-run"])
        self.assertEqual(rc, 0)
        printed = buf.getvalue()
        self.assertIn("# dry run: nothing changed", printed)
        self.assertIn("ip daddr { 192.168.105.0/24 } accept", printed)
        self.assertIn("ip daddr { 203.0.113.7 } meta l4proto tcp accept", printed)
        self.assertEqual((self.fw(), self.host.registry_path.read_text()), (text, reg))


class Parsing(unittest.TestCase):
    def test_zfs_get(self):
        p = ah.parse_zfs_get("axiom/recordings/a\tused\t812400000000\naxiom/recordings/a\tquota\t0\n"
                             "axiom/recordings/a\tavailable\t-\nbroken line\n")
        self.assertEqual(p, {"axiom/recordings/a": {"used": 812400000000, "quota": 0, "available": None}})
        self.assertEqual(ah.gb_bytes(4000), 4_000_000_000_000)
        self.assertEqual(ah.gb_bytes(0.5), 500_000_000)

    def test_nvidia_smi(self):
        g = ah.parse_nvidia_smi(NVIDIA_SMI + "2, NVIDIA RTX PRO 4000, Blackwell, 24467, [N/A], [N/A]\njunk\n")
        self.assertEqual(g[0], {"index": 0, "name": "NVIDIA A40", "mem_total_mb": 46068, "mem_used_mb": 41234,
                                "util": 87})
        self.assertEqual(g[1]["name"], "NVIDIA A10")
        self.assertEqual(g[2]["name"], "NVIDIA RTX PRO 4000, Blackwell")
        self.assertIsNone(g[2]["util"])
        self.assertEqual(len(g), 3)

    def test_meminfo(self):
        m = ah.parse_meminfo("MemTotal:       791198728 kB\nMemFree:  1000 kB\nMemAvailable:   700000000 kB\n")
        self.assertEqual(m, {"total": 791.2, "free": 700.0})

    def test_capacity(self):
        tmp = Path(tempfile.mkdtemp())
        host = make_host(tmp, FakeExec())
        host.create_instance(dict(VPN_ARGS))
        cap = host.capacity()
        self.assertEqual([g["index"] for g in cap["gpus"]], [0, 1])
        self.assertEqual([g["instances"] for g in cap["gpus"]], [0, 1])
        self.assertEqual(cap["instances"], 1)
        self.assertEqual(cap["allocated"]["quota_gb"], 4000)
        self.assertEqual(cap["disks"][0]["fs"], "xfs")
        for k in ("cpus", "load", "ram_gb", "gpus", "disks"):
            self.assertIn(k, cap)

    def test_env_placeholders_ignored(self):
        tmp = Path(tempfile.mkdtemp())
        p = tmp / "ai.env"
        p.write_text("VLLM_API_KEY=<generate: openssl rand -hex 24>\nAXIOM_VLM_MODEL=Qwen/X\n# c\n")
        self.assertEqual(ah.read_env_file(p), {"AXIOM_VLM_MODEL": "Qwen/X"})


VLLM_METRICS = """# HELP vllm:num_requests_running Number of requests in model execution batches.
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{engine="0",model_name="Qwen/Qwen3.8-27B-FP8"} 2.0
# TYPE vllm:num_requests_waiting gauge
vllm:num_requests_waiting{engine="0",model_name="Qwen/Qwen3.8-27B-FP8"} 3.0
vllm:num_requests_waiting_by_reason{engine="0",model_name="Qwen/Qwen3.8-27B-FP8",reason="capacity"} 1.0
vllm:num_requests_waiting_by_reason{engine="0",model_name="Qwen/Qwen3.8-27B-FP8",reason="deferred"} 2.0
vllm:kv_cache_usage_perc{engine="0",model_name="Qwen/Qwen3.8-27B-FP8"} 0.3412
vllm:prompt_tokens_total{engine="0",model_name="Qwen/Qwen3.8-27B-FP8"} %(prompt)s
vllm:generation_tokens_total{engine="0",model_name="Qwen/Qwen3.8-27B-FP8"} %(gen)s
vllm:e2e_request_latency_seconds_bucket{engine="0",le="1.0",model_name="Qwen/Qwen3.8-27B-FP8"} %(e1)s
vllm:e2e_request_latency_seconds_bucket{engine="0",le="5.0",model_name="Qwen/Qwen3.8-27B-FP8"} %(e5)s
vllm:e2e_request_latency_seconds_bucket{engine="0",le="+Inf",model_name="Qwen/Qwen3.8-27B-FP8"} %(einf)s
vllm:e2e_request_latency_seconds_count{engine="0",model_name="Qwen/Qwen3.8-27B-FP8"} %(einf)s
vllm:request_queue_time_seconds_bucket{engine="0",le="0.5",model_name="Qwen/Qwen3.8-27B-FP8"} 10.0
vllm:request_queue_time_seconds_bucket{engine="0",le="+Inf",model_name="Qwen/Qwen3.8-27B-FP8"} 10.0
vllm:cache_config_info{block_size="16",note="a \\"quoted\\" value"} 1.0
vllm:spec_decode_draft_acceptance_rate NaN
python_gc_objects_collected_total{generation="0"} 1234.0
"""
OLD_VLLM_METRICS = """vllm:num_requests_running{model_name="Qwen/Qwen2.5-VL-32B-Instruct-AWQ"} 1
vllm:num_requests_waiting{model_name="Qwen/Qwen2.5-VL-32B-Instruct-AWQ"} 0
vllm:num_requests_swapped{model_name="Qwen/Qwen2.5-VL-32B-Instruct-AWQ"} 0
vllm:gpu_cache_usage_perc{model_name="Qwen/Qwen2.5-VL-32B-Instruct-AWQ"} 0.05
vllm:prompt_tokens_total{model_name="Qwen/Qwen2.5-VL-32B-Instruct-AWQ"} 100
"""


def metrics(prompt=1000.0, gen=500.0, e1=4.0, e5=8.0, einf=10.0) -> str:
    return VLLM_METRICS % {"prompt": prompt, "gen": gen, "e1": e1, "e5": e5, "einf": einf}


INSPECT_VLLM = json.dumps({"axiom-acme-gate": {"IPAddress": "10.200.0.3"}, "axiom-ai": {"IPAddress": "10.201.0.10"}})


def system_line(verify=29, synopsis=0, verified=100, pending=29, yolo=True) -> str:
    return "some warning on stderr-ish stdout\n" + json.dumps({
        "verify_q": verify, "synopsis_q": synopsis, "yolo_ready": yolo, "vlm_ready": True, "vlm_state": "ready",
        "yolo_frame_ms": 11.5, "events": {"verified": verified, "rejected": 20, "pending": pending, "open": 2}}) + "\n"


class WorkExec(FakeExec):
    """FakeExec whose `docker exec` answers come from a table per container, optionally after a delay (a hung
    instance) or failing."""

    def __init__(self) -> None:
        super().__init__({"docker ps -a": "acme-gate\trunning\nbeta-yard\trunning\n",
                          "docker inspect -f": INSPECT_VLLM})
        self.system: dict[str, str] = {}
        self.delay: dict[str, float] = {}
        self.fail: dict[str, str] = {}

    def _run(self, argv, input, timeout):
        if argv[:2] == ["docker", "exec"]:
            self.calls.append(list(argv))
            name = argv[2]
            if name in self.delay:   # a docker exec that hangs (even past its own timeout)
                __import__("time").sleep(self.delay[name])
            if name in self.fail:
                return ah.Result(1, "", self.fail[name])
            return ah.Result(0, self.system.get(name, ""), "")
        return super()._run(argv, input, timeout)


class Work(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.exe = WorkExec()
        self.host = make_host(self.tmp, self.exe)
        self.host.create_instance(dict(VPN_ARGS))   # gpu 1
        self.host.create_instance(dict(FWD_ARGS))   # CPU
        self.fetched: list[str] = []
        self.page = metrics()

        def get(url, timeout):
            self.fetched.append(url)
            if isinstance(self.page, Exception):
                raise self.page
            return self.page
        self.host.work.http_get = get
        self.exe.system = {"axiom-acme-gate": system_line(), "axiom-beta-yard": system_line(verify=3, verified=10)}

    # -- pure parts
    def test_parse_prometheus(self):
        p = ah.parse_prometheus(metrics())
        self.assertEqual(p["vllm:num_requests_running"], [({"engine": "0", "model_name": "Qwen/Qwen3.8-27B-FP8"}, 2.0)])
        self.assertEqual(p["vllm:cache_config_info"][0][0]["note"], 'a "quoted" value')
        self.assertNotIn("vllm:spec_decode_draft_acceptance_rate", p)   # NaN dropped
        self.assertEqual(p["vllm:e2e_request_latency_seconds_bucket"][2][0]["le"], "+Inf")
        self.assertEqual(ah.parse_prometheus("garbage line here\n# c\n\nx{a=\"1\"} notanumber\nok 1 1700000000000\n"),
                         {"ok": [({}, 1.0)]})   # a trailing timestamp is allowed

    def test_vllm_sample_current_and_older_names(self):
        s = ah.vllm_sample(ah.parse_prometheus(metrics()))
        self.assertEqual((s["running"], s["waiting"], s["waiting_capacity"], s["kv_cache_pct"], s["model"]),
                         (2, 3, 1, 34.1, "Qwen/Qwen3.8-27B-FP8"))
        self.assertEqual(s["counters"], {"prompt": 1000.0, "gen": 500.0})
        self.assertEqual(set(s["hists"]), {"queue", "e2e"})
        old = ah.vllm_sample(ah.parse_prometheus(OLD_VLLM_METRICS))   # gpu_cache_usage_perc, no by_reason, no histograms
        self.assertEqual((old["running"], old["waiting"], old["waiting_capacity"], old["kv_cache_pct"]), (1, 0, None, 5.0))
        self.assertEqual(old["counters"], {"prompt": 100.0, "gen": None})
        self.assertEqual(old["hists"], {})
        none = ah.vllm_sample({})
        self.assertEqual((none["running"], none["waiting"], none["kv_cache_pct"], none["model"]), (None, None, None, None))

    def test_quantiles_and_rates(self):
        inf = float("inf")
        self.assertAlmostEqual(ah.histogram_quantile(0.5, {1.0: 4, 5.0: 8, inf: 10}), 2.0)   # 5th of 10: 1 + 4*(1/4)
        self.assertEqual(ah.histogram_quantile(0.5, {1.0: 0, inf: 10}), 1.0)                 # all in +Inf: the top bound
        self.assertIsNone(ah.histogram_quantile(0.5, {1.0: 0, inf: 0}))
        self.assertIsNone(ah.histogram_quantile(0.5, {}))
        a = ah.vllm_sample(ah.parse_prometheus(metrics()))
        b = ah.vllm_sample(ah.parse_prometheus(metrics(prompt=7000, gen=5900, e1=5, e5=12, einf=14)))
        r = ah.vllm_rates(b, 130.0, a, 100.0)
        self.assertEqual((r["prompt_tps"], r["gen_tps"]), (200.0, 180.0))
        self.assertEqual(r["e2e_p50_s"], 2.33)         # 4 new requests: 1 under 1 s, 3 in 1-5 s: the 2nd at 1 + 4 * 1/3
        self.assertIsNone(r["queue_p50_s"])            # no new queued requests in between
        self.assertEqual(ah.vllm_rates(b, 130.0, None, None)["gen_tps"], None)   # first read: no rate yet
        restarted = ah.vllm_rates(a, 130.0, b, 100.0)                            # counters went backwards
        self.assertEqual((restarted["prompt_tps"], restarted["gen_tps"], restarted["e2e_p50_s"]), (None, None, None))

    def test_rate_per_min(self):
        d = __import__("collections").deque()
        self.assertIsNone(ah.rate_per_min(d, 0, 100, 300))
        self.assertEqual(ah.rate_per_min(d, 30, 101, 300), 2.0)
        self.assertEqual(ah.rate_per_min(d, 60, 101, 300), 1.0)
        for t in range(90, 600, 30):
            r = ah.rate_per_min(d, t, 101, 300)
        self.assertEqual(r, 0.0)                                   # the window slid past the one event
        self.assertIsNone(ah.rate_per_min(d, 630, 50, 300))       # retention deleted events: start over
        self.assertEqual(ah.rate_per_min(d, 690, 51, 300), 1.0)
        self.assertIsNone(ah.rate_per_min(d, 720, None, 300))
        self.assertEqual(ah.events_done({"verified": 5, "rejected": 2, "error": 1, "masked": 1, "pending": 9, "open": 3}), 9)
        self.assertIsNone(ah.events_done(None))

    def test_parse_instance_work(self):
        w = ah.parse_instance_work(system_line())
        self.assertEqual((w["verify_q"], w["synopsis_q"], w["yolo_ready"], w["done"]), (29, 0, True, 120))
        with self.assertRaises(ValueError):
            ah.parse_instance_work("")
        with self.assertRaises(ValueError):
            ah.parse_instance_work("Traceback ...\nConnectionRefusedError")
        self.assertIsNone(ah.parse_instance_work('{"verify_q": "x", "yolo_ready": 1}')["verify_q"])

    # -- the monitor
    def test_metrics_url(self):
        self.assertEqual(self.host.work.metrics_url(), "http://10.201.0.10:8000/metrics")   # the AI network, not .3
        h2 = make_host(self.tmp / "b", FakeExec({"docker inspect -f": "{}"}))
        self.assertIsNone(h2.work.metrics_url())
        h3 = make_host(self.tmp / "c", FakeExec())
        h3.cfg["vllm_metrics_url"] = "http://192.0.2.5:9000/metrics"
        self.assertEqual(h3.work.metrics_url(), "http://192.0.2.5:9000/metrics")

    def test_collect_and_capacity(self):
        work = self.host.work.collect()
        v = work["vllm"]
        self.assertTrue(v["ok"])
        self.assertEqual((v["running"], v["waiting"], v["waiting_capacity"], v["kv_cache_pct"], v["gpu"]), (2, 3, 1, 34.1, 0))
        self.assertIsNone(v["gen_tps"])                              # one read so far
        self.assertEqual(self.fetched, ["http://10.201.0.10:8000/metrics"])
        a, b = work["instances"]["acme-gate"], work["instances"]["beta-yard"]
        self.assertEqual((a["verify_q"], a["synopsis_q"], a["gpu"], a["ok"], a["yolo_ready"], a["vlm_ready"]), (29, 0, 1, True, True, True))
        self.assertEqual((b["verify_q"], b["gpu"]), (3, None))
        self.assertIsNone(a["verify_rate_per_min"])
        execs = self.exe.cmds("docker exec")
        self.assertEqual({c[2] for c in execs}, {"axiom-acme-gate", "axiom-beta-yard"})
        self.assertIn("/api/system", execs[0][-1])
        # a second read 30 s later: rates from the deltas
        w = self.host.work
        w._vllm_reads[-1] = (w._vllm_reads[-1][0] - 30, w._vllm_reads[-1][1])
        w._done["acme-gate"][-1] = (w._done["acme-gate"][-1][0] - 60, 110)   # 10 fewer done a minute ago
        self.page = metrics(prompt=7000, gen=5900, e1=5, e5=12, einf=14)
        work = w.collect()
        self.assertAlmostEqual(work["vllm"]["gen_tps"], 180.0, delta=1)
        self.assertAlmostEqual(work["vllm"]["prompt_tps"], 200.0, delta=1)
        self.assertAlmostEqual(work["instances"]["acme-gate"]["verify_rate_per_min"], 10.0, delta=0.2)
        cap = self.host.capacity()
        self.assertEqual(cap["work"]["instances"].keys(), {"acme-gate", "beta-yard"})
        self.assertEqual([g.get("verify_q") for g in cap["gpus"]], [0, 29])   # A40: no instances; A10: acme-gate's
        json.dumps(cap)   # goes out as JSON

    def test_capacity_without_collection(self):
        cap = make_host(self.tmp / "x", FakeExec()).capacity()
        self.assertNotIn("work", cap)
        self.assertNotIn("verify_q", cap["gpus"][0])

    def test_hung_and_failing_instances(self):
        self.host.cfg.update(work_budget_s=0.6, work_exec_timeout_s=0.6)
        self.host.work.collect()                                   # good values first
        self.exe.delay["axiom-beta-yard"] = 1.5                    # hangs well past the budget
        self.exe.fail["axiom-acme-gate"] = "Error response from daemon: container is restarting"
        self.exe.system["axiom-beta-yard"] = system_line(verify=99)
        t0 = __import__("time").monotonic()
        work = self.host.work.collect()
        self.assertLess(__import__("time").monotonic() - t0, 2.0)  # the pass never waits for the hung one
        a, b = work["instances"]["acme-gate"], work["instances"]["beta-yard"]
        self.assertFalse(a["ok"])
        self.assertIn("restarting", a["error"])
        self.assertEqual(a["verify_q"], 29)                        # last good value kept, with its time
        self.assertIsNotNone(a["at"])
        self.assertFalse(b["ok"])
        self.assertEqual(b["verify_q"], 3)
        self.assertEqual(b["error"], "no answer within 0.6 s")
        # the vLLM goes away: its last numbers stay, marked not ok; the hung exec is not started a second time
        self.page = OSError("connection refused")
        n = len(self.exe.cmds("docker exec axiom-beta-yard"))
        work = self.host.work.collect()
        self.assertEqual(len(self.exe.cmds("docker exec axiom-beta-yard")), n)
        self.assertEqual(work["instances"]["beta-yard"]["error"], "the last read has not ended yet")
        self.assertEqual(work["instances"]["beta-yard"]["verify_q"], 3)
        self.assertFalse(work["vllm"]["ok"])
        self.assertIn("connection refused", work["vllm"]["error"])
        self.assertEqual(work["vllm"]["running"], 2)
        # an instance whose container is not running is not exec'd
        self.exe.answers["docker ps -a"] = "acme-gate\texited\nbeta-yard\trunning\n"
        n = len(self.exe.cmds("docker exec axiom-acme-gate"))
        self.exe.delay.clear()
        work = self.host.work.collect()
        self.assertEqual(len(self.exe.cmds("docker exec axiom-acme-gate")), n)
        self.assertEqual(work["instances"]["acme-gate"]["error"], "container exited")

    def test_no_vllm_metrics(self):
        self.page = "# nothing here\nprocess_cpu_seconds_total 1\n"
        v = self.host.work.collect()["vllm"]
        self.assertFalse(v["ok"])
        self.assertIn("no vllm", v["error"])
        h2 = make_host(self.tmp / "nov", FakeExec({"docker ps -a": ""}))
        h2.exe.answers["docker inspect -f"] = ""
        v = h2.work.collect()["vllm"]
        self.assertEqual((v["ok"], v["error"]), (False, "no axiom-vllm container"))

    def test_heartbeat_carries_work(self):
        agent = ah.Agent(self.host, "wss://hub.example.test/host-agent", "t")
        self.assertNotIn("work", agent.heartbeat()["capacity"])
        self.host.work.collect()
        hb = agent.heartbeat()
        self.assertEqual(hb["capacity"]["work"]["instances"]["acme-gate"]["verify_q"], 29)
        self.assertLess(len(json.dumps(hb)), 64 * 1024)


class SetResources(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.exe = FakeExec()
        self.host = make_host(self.tmp, self.exe)
        self.host.host_cpus = lambda: 80
        self.host.create_instance(dict(VPN_ARGS))

    def rec(self):
        return self.host.load()["instances"]["acme-gate"]

    def test_validation(self):
        for args, msg in (({}, "cpus and / or mem_gb"), ({"cpus": 0.5}, "cpus must be 1-64"), ({"cpus": 65}, "1-64"),
                          ({"mem_gb": 1}, "mem_gb must be 2-512"), ({"mem_gb": 600}, "2-512"), ({"cpus": "lots"}, "a number"),
                          ({"cpus": float("nan")}, "1-64")):
            with self.assertRaises(ah.OpError) as e:
                self.host.set_resources({"id": "acme-gate", **args})
            self.assertIn(msg, str(e.exception))
        self.host.host_cpus = lambda: 6
        with self.assertRaises(ah.OpError) as e:
            self.host.set_resources({"id": "acme-gate", "cpus": 8})
        self.assertIn("more than this host has (6)", str(e.exception))
        with self.assertRaises(ah.OpError):
            self.host.set_resources({"id": "nope", "cpus": 2})
        self.assertEqual((self.rec()["cpus"], self.rec()["mem_gb"]), (4, 8))

    def test_change_applies_live(self):
        n = len(self.exe.calls)
        out = self.host.set_resources({"id": "acme-gate", "cpus": 8})
        self.assertIn("4 -> 8 CPUs", out["detail"])
        self.assertIn("applied live", out["detail"])
        self.assertEqual((self.rec()["cpus"], self.rec()["mem_gb"]), (8, 8))
        self.assertEqual(self.exe.calls[n:], [["docker", "update", "--cpus", "8", "--memory", "8g", "--memory-swap", "8g",
                                                "axiom-acme-gate"]])   # no rm, no run: recording never stops
        self.assertEqual(self.exe.cmds("docker rm"), [])
        n = len(self.exe.calls)
        out = self.host.set_resources({"id": "acme-gate", "cpus": 8, "mem_gb": 8})
        self.assertIn("unchanged", out["detail"])
        self.assertEqual(len(self.exe.calls), n)

    def test_change_recreates_when_live_update_fails(self):
        class NoUpdate(FakeExec):
            def _run(self, argv, input, timeout):
                if argv[:2] == ["docker", "update"]:
                    self.calls.append(list(argv))
                    return ah.Result(1, "", "Error response from daemon: Cannot update container: memory below usage")
                return super()._run(argv, input, timeout)
        self.host.exe = NoUpdate()
        n = len(self.host.exe.calls)
        out = self.host.set_resources({"id": "acme-gate", "cpus": 8})
        self.assertIn("4 -> 8 CPUs", out["detail"])
        self.assertEqual((out["instance"]["cpus"], out["instance"]["mem_gb"]), (8, 8))
        self.assertEqual((self.rec()["cpus"], self.rec()["mem_gb"]), (8, 8))
        self.exe = self.host.exe
        new = self.exe.calls[n:]
        self.assertEqual(new[0][:2], ["docker", "update"])
        new = new[1:]
        self.assertEqual(new[0], ["docker", "rm", "-f", "axiom-acme-gate"])
        run = new[1]
        self.assertEqual(run[:3], ["docker", "run", "-d"])
        self.assertEqual(run[run.index("--cpus") + 1], "8")
        self.assertEqual(run[run.index("--memory") + 1], "8g")
        self.host.set_resources({"id": "acme-gate", "mem_gb": 16})
        run = self.exe.cmds("docker run")[-1]
        self.assertEqual((run[run.index("--cpus") + 1], run[run.index("--memory") + 1], run[run.index("--memory-swap") + 1]),
                         ("8", "16g", "16g"))
        n = len(self.exe.calls)
        out = self.host.set_resources({"id": "acme-gate", "cpus": 8, "mem_gb": 16})
        self.assertIn("unchanged", out["detail"])
        self.assertEqual(len(self.exe.calls), n)   # nothing recreated

    def test_dry_run(self):
        reg = self.host.registry_path.read_text()
        n = len(self.exe.calls)
        out = self.host.dispatch("set_resources", {"id": "acme-gate", "cpus": 8, "mem_gb": 12, "dry_run": True})
        self.assertEqual(self.host.registry_path.read_text(), reg)
        self.assertEqual(len(self.exe.calls), n)   # nothing ran
        cmds = [s["cmd"] for s in out["dry_run"]["steps"] if "cmd" in s]
        self.assertEqual(cmds[0], ["docker", "update", "--cpus", "8", "--memory", "12g", "--memory-swap", "12g", "axiom-acme-gate"])
        # the CLI
        cfg = self.tmp / "host.json"
        cfg.write_text(json.dumps({"root": str(self.tmp / "srv"), "hub_ips": [HUB], "ai_env": str(self.tmp / "ai.env")}))
        buf = __import__("io").StringIO()
        with __import__("contextlib").redirect_stdout(buf):
            rc = ah.main(["--config", str(cfg), "set-resources", "--id", "acme-gate", "--cpus", "2", "--dry-run"])
        self.assertEqual(rc, 0)
        printed = json.loads(buf.getvalue())
        self.assertIn("4 -> 2 CPUs", printed["detail"])
        self.assertTrue(printed["dry_run"]["steps"])
        self.assertEqual(self.host.registry_path.read_text(), reg)

    def test_failed_start_restores_old_limits(self):
        class Refuse(FakeExec):
            def _run(self, argv, input, timeout):
                if argv[:2] == ["docker", "update"]:
                    self.calls.append(list(argv))
                    return ah.Result(1, "", "Error response from daemon: range of CPUs is from 0.01 to 12.00")
                if argv[:2] == ["docker", "run"] and "16" in argv[argv.index("--cpus") + 1]:
                    self.calls.append(list(argv))
                    return ah.Result(125, "", "docker: Error response from daemon: range of CPUs is from 0.01 to 12.00")
                return super()._run(argv, input, timeout)
        self.host.exe = exe = Refuse()
        with self.assertRaises(ah.CmdError):
            self.host.set_resources({"id": "acme-gate", "cpus": 16})
        self.assertEqual(self.rec()["cpus"], 4)
        run = exe.cmds("docker run")[-1]
        self.assertEqual(run[run.index("--cpus") + 1], "4")   # re-run with the old limits

    def test_restart_recreate_uses_same_path(self):
        self.host.set_resources({"id": "acme-gate", "cpus": 6})
        self.host.restart_instance({"id": "acme-gate", "recreate": True})
        run = self.exe.cmds("docker run")[-1]
        self.assertEqual(run[run.index("--cpus") + 1], "6")


class FakeWS:
    """Delivers the given frames, then waits until `expect` frames were sent before ending the connection."""

    def __init__(self, frames: list, expect: int) -> None:
        self.frames, self.expect, self.sent = frames, expect, []
        self.done = asyncio.Event()

    async def send(self, text: str) -> None:
        self.sent.append(json.loads(text))
        if len(self.sent) >= self.expect:
            self.done.set()

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for f in self.frames:
            yield f if isinstance(f, str) else json.dumps(f)
        await asyncio.wait_for(self.done.wait(), 10)


class AgentFrames(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.exe = FakeExec({"docker ps -a": "acme-gate\trunning\n"})
        self.host = make_host(self.tmp, self.exe)
        self.agent = ah.Agent(self.host, "wss://hub.example.test/host-agent", "host-token")

    def session(self, frames, expect):
        ws = FakeWS(frames, expect)
        asyncio.run(self.agent.serve(ws))
        return ws.sent

    def test_hello_and_commands(self):
        frames = [
            {"t": "cmd", "id": 1, "op": "create_instance", "args": dict(VPN_ARGS)},
            {"t": "cmd", "id": 2, "op": "list", "args": {}},
            {"t": "cmd", "id": 3, "op": "explode", "args": {}},
            {"t": "cmd", "id": "4", "op": "list"},
            {"t": "cmd", "id": 5, "op": "set_quota", "args": {"id": "nope", "quota_gb": 100}},
            {"t": "ping"},
            "not json",
            {"t": "welcome", "host_id": "h1"},
        ]
        sent = self.session(frames, expect=7)
        hello = sent[0]
        self.assertEqual(hello["t"], "hello")
        self.assertEqual(hello["proto"], 1)
        self.assertEqual(hello["version"], ah.VERSION)
        self.assertIn("hostname", hello)
        self.assertEqual([g["name"] for g in hello["capacity"]["gpus"]], ["NVIDIA A40", "NVIDIA A10"])
        results = {f["id"]: f for f in sent if f["t"] == "result"}
        self.assertTrue(results[1]["ok"])
        self.assertEqual(results[1]["instance"]["id"], "acme-gate")
        self.assertEqual(results[1]["instance"]["location_id"], "loc_acme1")
        self.assertTrue(results[2]["ok"])
        self.assertIn("instances", results[2])
        self.assertFalse(results[3]["ok"])
        self.assertIn("unknown op", results[3]["detail"])
        self.assertFalse(results["4"]["ok"])
        self.assertFalse(results[5]["ok"])
        self.assertIn("no instance", results[5]["detail"])
        self.assertIn({"t": "pong"}, sent)
        for r in results.values():
            self.assertEqual(set(r) - {"t", "id", "ok", "detail", "instance", "instances", "dry_run"}, set())
            self.assertNotIn(TOKEN, json.dumps(r))

    def test_heartbeat_frame(self):
        self.host.create_instance(dict(VPN_ARGS))
        hb = self.agent.heartbeat()
        self.assertEqual(hb["t"], "heartbeat")
        self.assertEqual(hb["capacity"]["instances"], 1)
        self.assertEqual(hb["instances"][0]["state"], "running")

    def test_results_queue_while_disconnected(self):
        async def go():
            await self.agent.send(await self.agent.execute({"t": "cmd", "id": 9, "op": "list", "args": {}}))
            await self.agent.send({"t": "heartbeat"})   # not queued
        asyncio.run(go())
        self.assertEqual([f["id"] for f in self.agent.outbox], [9])
        sent = self.session([], expect=2)
        self.assertEqual(sent[0]["t"], "hello")
        self.assertEqual((sent[1]["t"], sent[1]["id"]), ("result", 9))
        self.assertFalse(self.agent.outbox)


class Tls(unittest.TestCase):
    def test_ws_only_for_localhost(self):
        self.assertIsNone(ah.ssl_context("ws://localhost:8000/host-agent"))
        with self.assertRaises(SystemExit):
            ah.ssl_context("ws://hub.axiomvision.ai/host-agent")
        ctx = ah.ssl_context("wss://hub.axiomvision.ai/host-agent")
        self.assertTrue(ctx.check_hostname)


if __name__ == "__main__":
    unittest.main(verbosity=1)
