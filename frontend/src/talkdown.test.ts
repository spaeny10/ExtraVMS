import { describe, expect, it } from "vitest";
import type { SpeakerStatus } from "./api";
import { ALL, defaultChoice, isActive, playOutcome, speakersByCamera, statusText, targets, uploadProblem } from "./talkdown";

const idle: SpeakerStatus = { state: "idle", message: null, by: null, requested_at: null, started_at: null, elapsed_s: null, duration_s: null, last: null };
const playing = (elapsed: number, duration: number | null = 6): SpeakerStatus =>
  ({ ...idle, state: "playing", message: "Leave now", by: "op@example", started_at: 100, elapsed_s: elapsed, duration_s: duration });

describe("which speakers a camera has", () => {
  const list = [
    { id: "spk1", name: "Back wall", camera_ids: ["cam1", "cam2"], enabled: true, online: true },
    { id: "spk2", name: "Gate", camera_ids: ["cam1"], enabled: true },
    { id: "spk3", name: "Off", camera_ids: ["cam1"], enabled: false },
    { id: "spk4", name: "Unbound", camera_ids: [] },
  ];
  it("maps every bound camera to its speakers, enabled ones only", () => {
    const m = speakersByCamera(list);
    expect(m.cam1.map((s) => s.id)).toEqual(["spk1", "spk2"]);
    expect(m.cam2).toEqual([{ id: "spk1", name: "Back wall", online: true }]);
    expect(m.cam3).toBeUndefined();
  });
  it("one speaker is the choice; several start on All speakers", () => {
    const m = speakersByCamera(list);
    expect(defaultChoice(m.cam2)).toBe("spk1");
    expect(defaultChoice(m.cam1)).toBe(ALL);
    expect(targets(ALL, m.cam1).map((s) => s.id)).toEqual(["spk1", "spk2"]);
    expect(targets("spk2", m.cam1).map((s) => s.id)).toEqual(["spk2"]);
    expect(targets("spk9", m.cam1)).toEqual([]);
  });
});

describe("the live status line", () => {
  it("counts the seconds while playing, also between polls", () => {
    expect(statusText(playing(4))).toBe("Playing 'Leave now' · 4 / 6 s");
    expect(statusText(playing(4.2), 1.5)).toBe("Playing 'Leave now' · 5 / 6 s");
    expect(statusText(playing(9), 3)).toBe("Playing 'Leave now' · 6 / 6 s");   // never past the end
    expect(statusText(playing(4, null))).toBe("Playing 'Leave now' · 4 s");
    expect(statusText({ ...playing(0), state: "connecting", elapsed_s: null })).toBe("Connecting to play 'Leave now'…");
    expect(isActive(playing(1))).toBe(true);
    expect(isActive({ ...playing(0), state: "connecting" })).toBe(true);
    expect(isActive(idle)).toBe(false);
    expect(isActive(undefined)).toBe(false);
  });
  it("then says how the last one ended", () => {
    expect(statusText(idle)).toBe("Ready");
    expect(statusText(undefined)).toBe("…");
    const last = (result: "played" | "stopped" | "replaced" | "failed", error: string | null = null): SpeakerStatus =>
      ({ ...idle, last: { message: "Leave now", by: "op", result, error, at: 1, played_s: 3 } });
    expect(statusText(last("played"))).toBe("Played 'Leave now'");
    expect(statusText(last("stopped"))).toBe("Stopped 'Leave now'");
    expect(statusText(last("replaced"))).toBe("Replaced 'Leave now'");
    expect(statusText(last("failed", "the speaker refused the username or password"))).toBe("Failed: the speaker refused the username or password");
  });
});

describe("playing on one or all speakers", () => {
  it("one toast for what started, one per speaker that refused", () => {
    expect(playOutcome([{ speaker: "Back wall", ok: true }])).toEqual({ ok: "Playing on Back wall", failed: [] });
    expect(playOutcome([{ speaker: "Back wall", ok: true, replaced: "Police called" }, { speaker: "Gate", ok: true, replaced: "Police called" }]))
      .toEqual({ ok: "Playing on 2 speakers (replaced 'Police called')", failed: [] });
    expect(playOutcome([{ speaker: "Back wall", ok: true }, { speaker: "Gate", ok: false, error: "409 turned off" }]))
      .toEqual({ ok: "Playing on Back wall", failed: ["Gate: 409 turned off"] });
    expect(playOutcome([{ speaker: "Gate", ok: false }])).toEqual({ ok: null, failed: ["Gate: failed"] });
  });
  it("checks a one-off file before sending it", () => {
    expect(uploadProblem("leave.MP3", 1000)).toBeNull();
    expect(uploadProblem("leave.ogg", 2 * 1024 * 1024)).toBeNull();
    expect(uploadProblem("leave.wav", 2 * 1024 * 1024 + 1)).toBe("The file is larger than 2 MB");
    expect(uploadProblem("leave.exe", 10)).toBe("Choose an mp3, wav, m4a, ogg file");
    expect(uploadProblem("noext", 10)).toBe("Choose an mp3, wav, m4a, ogg file");
    expect(uploadProblem("empty.wav", 0)).toBe("The file is empty");
  });
});
