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
