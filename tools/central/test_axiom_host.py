"""Tests for tools/central/axiom_host.py. No Docker, nft or xfs_quota needed: commands go to a fake.

    .venv\\Scripts\\python.exe tools\\central\\test_axiom_host.py
"""
from __future__ import annotations

import asyncio
import json
import re
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


class Parsing(unittest.TestCase):
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
