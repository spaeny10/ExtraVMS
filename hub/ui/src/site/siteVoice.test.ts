import { describe, expect, it } from "vitest";
import type { SiteVoiceServer } from "../api";
import { previewServer, pushText, serverLine } from "./siteVoice";

const piper = { piper: "piper-tts", sapi: true, espeak: null };
const amy = { voice: "en_US-amy-medium", speed: 1, chime: false };
const srv = (id: string, over: Partial<SiteVoiceServer> = {}): SiteVoiceServer => ({
  server_id: id, server_name: id.toUpperCase(), online: true, supported: true, setting: amy, in_sync: true,
  voices: ["en_US-amy-medium"], engines: piper, effective: null, ...over,
});

describe("Site talk-down voice", () => {
  it("previews from an online server that has the voice, else any online one that takes voices", () => {
    const servers = [
      srv("old", { supported: false, voices: [] }),
      srv("tssdt", { voices: [] }),
      srv("off", { online: false, voices: ["en_GB-alan-medium"] }),
      srv("qwen", { voices: ["en_GB-alan-medium", "en_US-amy-medium"] }),
    ];
    expect(previewServer(servers, "en_GB-alan-medium")?.server_id).toBe("qwen");
    expect(previewServer(servers, "en_US-amy-medium")?.server_id).toBe("qwen");
    expect(previewServer(servers, "en_US-ryan-high")?.server_id).toBe("tssdt");   // nobody has it: a fallback answers
    expect(previewServer(servers, null)?.server_id).toBe("qwen");                  // automatic: a server with a Piper voice
    expect(previewServer([srv("x", { online: false }), srv("y", { supported: false })], "en_US-amy-medium")).toBeNull();
  });

  it("says per server who would speak and whether it took the saved setting", () => {
    expect(serverLine(srv("a"), "en_US-amy-medium", amy)).toEqual({ text: "installed ✓", ok: true });
    expect(serverLine(srv("a", { voices: [] }), "en_US-amy-medium", amy)).toEqual({ text: "not installed → Windows voice", ok: false });
    expect(serverLine(srv("a", { voices: ["en_GB-alan-medium"] }), "en_US-ryan-high", amy).text).toBe("not installed → Alan");
    const behind = { in_sync: false, setting: { voice: null, speed: 1, chime: false } };
    expect(serverLine(srv("a", behind), "en_US-amy-medium", amy).text).toBe("installed ✓ · applying…");
    expect(serverLine(srv("a", { ...behind, online: false }), "en_US-amy-medium", amy).text).toBe("installed ✓ · offline: takes it when it reconnects");
    expect(serverLine(srv("a", behind), "en_US-amy-medium", null).text).toBe("installed ✓");   // nothing saved yet
    expect(serverLine(srv("a", { supported: false }), "en_US-amy-medium", amy)).toEqual({ text: "its version has no talk-down voices: update the server", ok: null });
  });

  it("sums up a save's pushes", () => {
    const p = (name: string, ok: boolean, skipped = false) => ({ server_id: name, server_name: name, ok, skipped, error: ok ? null : "x", effective: null });
    expect(pushText([p("A", true), p("B", true)])).toBe("2 servers took it");
    expect(pushText([p("A", true), p("B", false), p("C", false, true)])).toBe("1 server took it; B didn't answer: it gets it on the next heartbeat");
    expect(pushText([])).toBe("no server online took it yet");
  });
});
