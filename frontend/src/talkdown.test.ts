import { describe, expect, it } from "vitest";
import type { DescribeAnswer, SpeakerStatus, TalkdownLibrary, TalkdownMessage } from "./api";
import { ALL, PRESETS_MAX, TALK_IDLE, TALK_MAX_MS, type TalkAction, WARN_IDLE, asOf, clientsOf, clock, defaultChoice, describeNote, fillTemplate, isActive,
  lastResult, messageOn, micErrorText, micProblem, onlineCount, playOutcome, playableMessages, playingBar, presetLook, serversOf, speakerKey, speakerState,
  speakersByCamera, splitPresets, statusText, talkCloseText, talkLabel, talkReducer, targets, uploadProblem, warnChoices, warnReducer } from "./talkdown";

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

describe("speakers on several servers (the hub: a Site's camera groups)", () => {
  const hub = [
    { id: "spk1", name: "Back wall (Alpha)", server: "s_a" },
    { id: "spk1", name: "Lot (Bravo)", server: "s_b" },
    { id: "spk2", name: "Gate (Alpha)", server: "s_a" },
  ];
  it("a speaker is server/id on the hub (ids are unique per server only), its id on a server", () => {
    expect(hub.map(speakerKey)).toEqual(["s_a/spk1", "s_b/spk1", "s_a/spk2"]);
    expect(speakerKey({ id: "spk1" })).toBe("spk1");
    expect(defaultChoice([hub[1]])).toBe("s_b/spk1");
    expect(targets("s_b/spk1", hub).map((s) => s.name)).toEqual(["Lot (Bravo)"]);
    expect(targets("spk1", hub)).toEqual([]);
    expect(targets(ALL, hub)).toHaveLength(3);
    expect(serversOf(hub)).toEqual(["s_a", "s_b"]);
    expect(serversOf([{ id: "x", name: "x" }])).toEqual([""]);
  });
  it("each speaker's calls go to its own server, never the camera's", () => {
    const camera = "camera-server", a = "client-a", b = "client-b";
    const to = clientsOf([{ server: "s_a", site: a }, { server: "s_b", site: b }, { server: "s_a", site: a }], camera);
    expect([to("s_a"), to("s_b")]).toEqual([a, b]);
    expect(to("s_x")).toBe(camera);
    // the server's own UI: speakers name no server, everything goes through the panel's site
    expect(clientsOf([{}, {}], camera)("")).toBe(camera);
  });
  it("messages are per server: the first one's list, kept where every other target server has the same name", () => {
    const m = (id: number, name: string, available = true): TalkdownMessage => ({ id, name, kind: "tts", text: name, duration_s: null, created_at: 0, available });
    const lib = (messages: TalkdownMessage[]): TalkdownLibrary => ({ messages, tts: { available: true, engine: "x" }, max_s: 120, max_upload: 1, types: [".wav"] });
    const libs = {
      s_a: lib([m(1, "Leave now"), m(2, "Police called"), m(3, "Private", false), m(4, "Gate only")]),
      s_b: lib([m(7, "police called "), m(8, "Leave now"), m(9, "Private")]),
    };
    expect(playableMessages(libs, ["s_a"]).map((x) => x.id)).toEqual([1, 2, 4]);
    expect(playableMessages(libs, ["s_a", "s_b"]).map((x) => x.id)).toEqual([1, 2]);
    expect(playableMessages(libs, ["s_b", "s_a"]).map((x) => x.id)).toEqual([7, 8]);
    expect(playableMessages(libs, ["s_a", "s_c"])).toEqual([]);    // that server's library isn't loaded (yet)
    expect(playableMessages(libs, [])).toEqual([]);
    expect(messageOn(libs.s_b, { name: "Police Called" })).toBe(7);
    expect(messageOn(libs.s_a, { name: "Private" })).toBeNull();   // not available there
    expect(messageOn(undefined, { name: "Leave now" })).toBeNull();
  });
});

describe("Describe & warn", () => {
  const templates = [
    { id: "leave", name: "Leave now", text: "this is private property and you are being recorded. Leave now." },
    { id: "police", name: "Police called", text: "the police have been called. Leave the property immediately." },
    { id: "custom", name: "Custom", text: "" },
  ];
  const answer = (over: Partial<DescribeAnswer> = {}): DescribeAnswer => {
    const phrase = over.phrase !== undefined ? over.phrase : "You in the red shirt by the white truck";
    return {
      people: 1, options: [{ description: "in the red shirt", position: "by the white truck", phrase: "You in the red shirt by the white truck" }],
      phrase, group_phrase: null, confidence: "high", filtered: false, template: "leave", templates,
      text: fillTemplate(templates[0].text, phrase), frame_ts: 1_700_000_000, model: "qwen3.8:27b", error: null, ...over,
    };
  };
  const ready = (a: DescribeAnswer) => warnReducer(warnReducer(WARN_IDLE, { type: "look" }), { type: "answer", answer: a });

  it("fills a template after the address, or alone without one", () => {
    expect(fillTemplate(templates[0].text, "You in the red shirt by the white truck"))
      .toBe("You in the red shirt by the white truck: this is private property and you are being recorded. Leave now.");
    expect(fillTemplate(templates[1].text, null)).toBe("The police have been called. Leave the property immediately.");
    expect(fillTemplate("", "You by the gate")).toBe("You by the gate: ");   // custom: the operator types the rest
    expect(fillTemplate("", null)).toBe("");
  });

  it("looking, then the answer to review; nothing plays by itself", () => {
    expect(warnReducer(WARN_IDLE, { type: "look" })).toEqual({ stage: "looking" });
    const s = ready(answer());
    expect(s.stage).toBe("ready");
    if (s.stage !== "ready") return;
    expect(s.text).toBe("You in the red shirt by the white truck: this is private property and you are being recorded. Leave now.");
    expect(s.edited).toBe(false);
    expect(describeNote(s.answer)).toBe("1 person");
    expect(warnChoices(s.answer)).toEqual([]);   // one person: no chips
    expect(asOf(1_700_000_000)).toMatch(/^as of \d/);
    // an answer that arrives after Cancel is dropped
    expect(warnReducer(WARN_IDLE, { type: "answer", answer: answer() })).toEqual(WARN_IDLE);
    expect(warnReducer(s, { type: "close" })).toEqual(WARN_IDLE);
    expect(warnReducer({ stage: "looking" }, { type: "failed" })).toEqual(WARN_IDLE);
  });

  it("the operator edits the text; another template refills it", () => {
    let s = ready(answer());
    s = warnReducer(s, { type: "edit", text: "You in the red shirt: step away from the truck." });
    expect(s.stage === "ready" && s.text).toBe("You in the red shirt: step away from the truck.");
    expect(s.stage === "ready" && s.edited).toBe(true);
    s = warnReducer(s, { type: "template", id: "police" });
    expect(s.stage === "ready" && s.text).toBe("You in the red shirt by the white truck: the police have been called. Leave the property immediately.");
    expect(s.stage === "ready" && [s.template, s.edited]).toEqual(["police", false]);
    s = warnReducer(s, { type: "template", id: "custom" });
    expect(s.stage === "ready" && s.text).toBe("You in the red shirt by the white truck: ");
    // edits and template picks before an answer change nothing
    expect(warnReducer({ stage: "looking" }, { type: "edit", text: "x" })).toEqual({ stage: "looking" });
    expect(warnReducer(WARN_IDLE, { type: "template", id: "police" })).toEqual(WARN_IDLE);
  });

  it("nobody visible: says so and offers the general message", () => {
    const a = answer({ people: 0, options: [], phrase: null });
    const s = ready(a);
    expect(describeNote(a)).toBe("Nobody visible right now");
    expect(s.stage === "ready" && s.text).toBe("This is private property and you are being recorded. Leave now.");
    expect(warnChoices(a)).toEqual([]);
    expect(warnReducer(s, { type: "template", id: "police" })).toMatchObject({ text: "The police have been called. Leave the property immediately." });
    // Qwen unavailable: the error, and the general message too
    const down = answer({ people: null, options: [], phrase: null, frame_ts: null, model: null, error: "Qwen is unavailable right now." });
    expect(describeNote(down)).toBe("Qwen is unavailable right now.");
    expect(ready(down)).toMatchObject({ stage: "ready", text: "This is private property and you are being recorded. Leave now." });
  });

  it("several people: everyone or one of them, as chips", () => {
    const opts = [
      { description: "in the red shirt", position: "by the white truck", phrase: "You in the red shirt by the white truck" },
      { description: "with the backpack", position: "at the gate", phrase: "You with the backpack at the gate" },
      { description: "", position: "by the fence", phrase: "You by the fence" },
    ];
    const a = answer({ people: 3, options: opts, phrase: "You by the gate", group_phrase: "You by the gate", confidence: "low", text: fillTemplate(templates[0].text, "You by the gate") });
    expect(describeNote(a)).toBe("3 people (Qwen isn't sure)");
    const chips = warnChoices(a);
    expect(chips.map((c) => c.label)).toEqual(["Everyone", "in the red shirt by the white truck", "with the backpack at the gate", "by the fence"]);
    let s = ready(a);
    expect(s.stage === "ready" && s.text).toBe("You by the gate: this is private property and you are being recorded. Leave now.");
    s = warnReducer(s, { type: "pick", phrase: chips[2].phrase });
    expect(s).toMatchObject({ phrase: "You with the backpack at the gate", text: "You with the backpack at the gate: this is private property and you are being recorded. Leave now." });
    s = warnReducer(warnReducer(s, { type: "template", id: "police" }), { type: "pick", phrase: chips[0].phrase });
    expect(s).toMatchObject({ phrase: "You by the gate", text: "You by the gate: the police have been called. Leave the property immediately." });
    expect(warnChoices({ ...a, group_phrase: null })[0]).toEqual({ label: "Everyone", phrase: "You there" });
  });
});

describe("the panel: presets, the playing bar, each speaker", () => {
  it("presets are the first messages; the icon and accent come from the name", () => {
    const names = ["Leave now", "Police called", "Monitored site", "Closed", "Hello", "Smile", "Seventh", "Eighth"];
    const { presets, more } = splitPresets(names);
    expect(presets).toEqual(names.slice(0, PRESETS_MAX));
    expect(more).toEqual(["Seventh", "Eighth"]);
    expect(splitPresets(names.slice(0, 3))).toEqual({ presets: names.slice(0, 3), more: [] });
    expect(presetLook("Leave now")).toEqual({ icon: "alert", tone: "warn" });
    expect(presetLook("Police called")).toEqual({ icon: "siren", tone: "danger" });
    expect(presetLook("Monitored site")).toEqual({ icon: "camera", tone: "info" });
    expect(presetLook("You are being RECORDED")).toEqual({ icon: "camera", tone: "info" });
    expect(presetLook("Trespassing")).toEqual({ icon: "alert", tone: "warn" });
    expect(presetLook("Police: leave now")).toEqual({ icon: "siren", tone: "danger" });   // police wins over leave
    expect(presetLook("Good evening")).toEqual({ icon: "speaker", tone: "neutral" });
  });

  it("the bar: what plays, elapsed of total moving between polls, never past the end", () => {
    expect(playingBar([idle, undefined])).toBeNull();
    const bar = playingBar([idle, playing(2, 4)], 0.5)!;
    expect(bar).toEqual({ message: "Leave now", live: false, connecting: false, elapsed: 2.5, total: 4, fraction: 0.625, speakers: 1 });
    expect(playingBar([playing(3.8, 4)], 1)!.fraction).toBe(1);
    expect(`${clock(bar.elapsed)} / ${clock(bar.total!)}`).toBe("0:02 / 0:04");
    // the one playing wins over one still connecting; both count
    const connecting: SpeakerStatus = { ...playing(0), state: "connecting", elapsed_s: null, message: "Police called" };
    expect(playingBar([connecting, playing(1, 6)])).toMatchObject({ message: "Leave now", speakers: 2 });
    expect(playingBar([connecting], 3)).toMatchObject({ connecting: true, elapsed: 0, fraction: 0 });
    // a live talk has no length: elapsed only
    expect(playingBar([{ ...playing(7, null), message: "Live talk", live: true }], 0.4)).toMatchObject({ live: true, elapsed: 7.4, total: null, fraction: null });
    expect(clock(65.9)).toBe("1:05");
  });

  it("idle: the most recent result among the speakers", () => {
    const ended = (at: number, result: "played" | "failed", error: string | null = null): SpeakerStatus =>
      ({ ...idle, last: { message: "Leave now", by: "op", result, error, at, played_s: 3 } });
    expect(lastResult([idle, undefined])).toBeNull();
    expect(lastResult([ended(5, "played"), ended(9, "failed", "no answer")])).toEqual({ text: "Failed: no answer", failed: true });
    expect(lastResult([ended(9, "played"), ended(5, "failed", "x")])).toEqual({ text: "Played 'Leave now'", failed: false });
  });

  const check = (online: boolean, ok = online, error: string | null = null) =>
    ({ check: { ok, online, backchannel: ok, codecs: [], codec: ok ? "PCMU" : null, error }, checked_at: 100 });
  it("N/M online from the checks (the summary's flag before one is read)", () => {
    const sp = [{ id: "a", name: "A", online: true }, { id: "b", name: "B", online: true }, { id: "c", name: "C", online: null }];
    expect(onlineCount(sp, {})).toEqual({ online: 2, total: 3 });
    expect(onlineCount(sp, { a: check(true), b: check(false), c: check(true, false, "no backchannel") })).toEqual({ online: 2, total: 3 });
  });

  it("each speaker's state from real data: playing, ready, offline, failed with the reason", () => {
    expect(speakerState(playing(1), check(true))).toEqual({ tone: "playing", text: "Playing 'Leave now'" });
    expect(speakerState({ ...playing(1), live: true, message: "Live talk" }, undefined)).toEqual({ tone: "playing", text: "Live talk" });
    expect(speakerState({ ...playing(0), state: "connecting" }, check(false))).toEqual({ tone: "connecting", text: "Connecting…" });
    expect(speakerState(idle, check(true))).toEqual({ tone: "ready", text: "Ready" });
    expect(speakerState(idle, check(false))).toEqual({ tone: "offline", text: "Offline" });
    expect(speakerState(idle, check(true, false, "the backchannel offers no G.711 (PCMU / PCMA)")))
      .toEqual({ tone: "failed", text: "Failed: the backchannel offers no G.711 (PCMU / PCMA)" });
    const failed = (at: number): SpeakerStatus => ({ ...idle, last: { message: "x", by: "op", result: "failed", error: "can't reach the speaker", at, played_s: 0 } });
    expect(speakerState(failed(150), check(true))).toEqual({ tone: "failed", text: "Failed: can't reach the speaker" });
    expect(speakerState(failed(50), check(true))).toEqual({ tone: "ready", text: "Ready" });   // a good check since
    expect(speakerState(undefined, undefined, false)).toEqual({ tone: "offline", text: "Offline" });
    expect(speakerState(undefined, undefined, null)).toEqual({ tone: "unknown", text: "…" });
  });
});

describe("Hold to Talk", () => {
  const go = (...actions: TalkAction[]) => actions.reduce(talkReducer, TALK_IDLE);
  it("press, live, let go, ended; the label follows", () => {
    const opening = go({ type: "press", now: 1000 });
    expect(opening).toEqual({ stage: "opening", since: 1000 });
    expect(talkLabel(opening, 1200)).toBe("Connecting…");
    const live = talkReducer(opening, { type: "live", now: 1400 });
    expect(live).toEqual({ stage: "live", since: 1000, live: 1400 });
    expect(talkLabel(live, 8100)).toBe("Talking… 0:07");
    expect(talkReducer(live, { type: "press", now: 2000 })).toBe(live);   // a second press does nothing
    const closing = talkReducer(live, { type: "release" });
    expect(closing).toEqual({ stage: "closing", since: 1000, capped: false });
    expect(talkLabel(closing, 9000)).toBe("Finishing…");
    expect(talkReducer(closing, { type: "live", now: 9000 })).toBe(closing);
    expect(talkReducer(closing, { type: "ended" })).toEqual({ stage: "idle", note: null });
    expect(talkLabel(TALK_IDLE, 0)).toBe("Hold to talk");
  });
  it("letting go before it is live ends it too; a release while idle does nothing", () => {
    expect(go({ type: "press", now: 0 }, { type: "release" })).toEqual({ stage: "closing", since: 0, capped: false });
    expect(go({ type: "release" })).toBe(TALK_IDLE);
    expect(go({ type: "press", now: 0 }, { type: "release" }, { type: "ended", note: "Gate: unknown speaker" }))
      .toEqual({ stage: "idle", note: "Gate: unknown speaker" });
  });
  it("stops itself at 60 s from the press", () => {
    const live = go({ type: "press", now: 0 }, { type: "live", now: 300 });
    expect(talkReducer(live, { type: "tick", now: TALK_MAX_MS - 1 })).toBe(live);
    const capped = talkReducer(live, { type: "tick", now: TALK_MAX_MS });
    expect(capped).toEqual({ stage: "closing", since: 0, capped: true });
    expect(talkReducer(capped, { type: "ended" })).toEqual({ stage: "idle", note: "Stopped at the 60 s limit" });
    expect(talkReducer(go({ type: "press", now: 0 }), { type: "tick", now: 61_000 }).stage).toBe("closing");   // never got live
  });
  it("says why it can't talk instead of failing silently", () => {
    const ok = { secure: true, getUserMedia: true, audio: true, webSocket: true };
    expect(micProblem(ok)).toBeNull();
    expect(micProblem({ ...ok, secure: false })).toMatch(/secure page.*https/);
    expect(micProblem({ ...ok, getUserMedia: false })).toMatch(/can't send its microphone/);
    expect(go({ type: "failed", note: "No microphone found" })).toEqual({ stage: "idle", note: "No microphone found" });
    expect(micErrorText({ name: "NotAllowedError" })).toMatch(/denied/);
    expect(micErrorText({ name: "NotFoundError" })).toBe("No microphone found");
    expect(micErrorText({ name: "NotReadableError" })).toMatch(/in use/);
    expect(micErrorText(new Error("boom"))).toBe("The microphone could not start (boom)");
    expect(talkCloseText(1000)).toBeNull();
    expect(talkCloseText(4409)).toBe("The speaker is turned off in its settings");
    expect(talkCloseText(4501)).toMatch(/update it/);
    expect(talkCloseText(1006)).toBe("The connection to the speaker was lost");
  });
});
