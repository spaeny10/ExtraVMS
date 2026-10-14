import { describe, expect, it } from "vitest";
import { playOutcome } from "./talkdown";
import {
  VOICE_CATALOG, clampSpeed, enginesText, optionText, predict, previewText, sameSetting, speedText, voiceLabel, voiceName, voiceOptions,
} from "./talkdownVoice";

const piper = { piper: "piper-tts", sapi: true, espeak: null };

describe("talk-down voice", () => {
  it("names catalog voices, other ids and automatic", () => {
    expect(voiceName("en_US-amy-medium")).toBe("Amy");
    expect(voiceName("my_voice")).toBe("my_voice");
    expect(voiceName(null)).toBe("Automatic");
    expect(voiceLabel("en_GB-alan-medium")).toBe("Alan · British English · male · measured");
    expect(voiceLabel(null)).toMatch(/^Automatic/);
    expect(new Set(VOICE_CATALOG.map((v) => v.id)).size).toBe(VOICE_CATALOG.length);
  });

  it("clamps and labels the speed", () => {
    expect(clampSpeed(0.2)).toBe(0.7);
    expect(clampSpeed(9)).toBe(1.3);
    expect(clampSpeed(NaN)).toBe(1);
    expect(clampSpeed(1.234)).toBe(1.23);
    expect(speedText(1)).toBe("1.0× · normal");
    expect(speedText(0.85)).toBe("0.85× · slower");
    expect(speedText(1.1)).toBe("1.1× · faster");
  });

  it("predicts who speaks on a server: the voice, another Piper voice, SAPI, espeak", () => {
    const srv = { voices: ["en_US-amy-medium", "en_GB-alan-medium"], engines: piper };
    expect(predict("en_US-amy-medium", srv)).toEqual({ kind: "voice", voice: "en_US-amy-medium", ok: true, text: "installed ✓" });
    expect(predict(null, srv)).toMatchObject({ kind: "voice", voice: "en_US-amy-medium", ok: true, text: "Amy ✓" });
    // not installed: same language and gender first (the server's own order)
    expect(predict("en_US-ryan-high", srv)).toMatchObject({ kind: "piper", voice: "en_GB-alan-medium", ok: false, text: "not installed → Alan" });
    expect(predict("en_US-kristin-medium", srv).voice).toBe("en_US-amy-medium");
    // no Piper voices / no Piper at all
    expect(predict("en_US-amy-medium", { voices: [], engines: piper })).toMatchObject({ kind: "sapi", text: "not installed → Windows voice" });
    expect(predict("en_US-amy-medium", { voices: ["en_US-amy-medium"], engines: { piper: null, sapi: true } }).text).toBe("Piper not installed → Windows voice");
    expect(predict("en_US-amy-medium", { voices: [], engines: { piper: null, sapi: false, espeak: "espeak-ng" } }).text).toBe("not installed → espeak-ng");
    expect(predict(null, { voices: [], engines: { piper: null, sapi: false, espeak: null } })).toMatchObject({ kind: "none", text: "no text-to-speech" });
    expect(predict(null, { voices: [], engines: {} }).text).toBe("not reported yet");
  });

  it("lists automatic, the catalog and other installed voices, marking where each is missing", () => {
    const servers = [
      { name: "TSS-DT", voices: ["en_US-amy-medium", "my_voice"], engines: piper },
      { name: "Qwenbot", voices: ["en_US-amy-medium", "en_GB-alan-medium"], engines: piper },
    ];
    const opts = voiceOptions(servers);
    expect(opts[0]).toMatchObject({ id: null, missing: [] });
    expect(opts.map((o) => o.id).slice(1, 1 + VOICE_CATALOG.length)).toEqual(VOICE_CATALOG.map((v) => v.id));
    expect(opts.at(-1)).toMatchObject({ id: "my_voice", missing: ["Qwenbot"], installed: 1 });
    const amy = opts.find((o) => o.id === "en_US-amy-medium")!;
    const alan = opts.find((o) => o.id === "en_GB-alan-medium")!;
    const ryan = opts.find((o) => o.id === "en_US-ryan-high")!;
    expect(optionText(amy, 2)).toBe("Amy · US English · female · warm");
    expect(optionText(alan, 2)).toBe("Alan · British English · male · measured — not on TSS-DT");
    expect(optionText(ryan, 2)).toBe("Ryan · US English · male · deep — not installed");
    expect(optionText(opts[0], 2)).toMatch(/^Automatic/);
  });

  it("says who spoke a preview and which engines a server has", () => {
    expect(previewText({ engine: "piper", voice: "en_US-amy-medium", fallback: false }, "en_US-amy-medium")).toBe("Amy");
    expect(previewText({ engine: "sapi", voice: null, fallback: true }, "en_US-ryan-high")).toBe("Windows voice — Ryan isn't installed there");
    expect(previewText({ engine: "piper", voice: "en_GB-alan-medium", fallback: true }, "en_US-ryan-high")).toBe("Alan — Ryan isn't installed there");
    expect(enginesText(piper)).toBe("Piper · Windows voice");
    expect(enginesText({ piper: null, sapi: false, espeak: "espeak-ng" })).toBe("espeak-ng");
    expect(enginesText(null)).toBe("none");
  });

  it("compares settings as the server stores them", () => {
    expect(sameSetting({ voice: "a", speed: 1.0, chime: false }, { voice: "a", speed: 1.0001, chime: false })).toBe(true);
    expect(sameSetting({ voice: null, speed: 1, chime: false }, { voice: "a", speed: 1, chime: false })).toBe(false);
    expect(sameSetting({ voice: "a", speed: 1, chime: true }, { voice: "a", speed: 1, chime: false })).toBe(false);
    expect(sameSetting(null, { voice: "a", speed: 1, chime: false })).toBe(false);
  });

  it("the play toast says when a fallback speaks", () => {
    const tts = { fallback: true, label: "Windows voice", requested: "en_US-ryan-high" };
    expect(playOutcome([{ speaker: "Gate", ok: true, tts }]).ok).toBe("Playing on Gate · spoken by Windows voice (Ryan isn't installed there)");
    expect(playOutcome([{ speaker: "Gate", ok: true, tts: { ...tts, fallback: false } }]).ok).toBe("Playing on Gate");
  });
});
